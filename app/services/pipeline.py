"""学习路径生成流水线（多 Agent 编排）。

调用序列（每一步对应一条 AgentRun 记录，失败即停并标记会话）：

    init → diagnose → plan → 逐天 (explain + quiz) → finalize

- ``init``：在 POST /api/sessions 里完成（建会话 + 首条 AgentRun），不进本模块；
- ``diagnose``（diagnostician）：LLM 输出诊断摘要，正文只发 SSE progress 事件，不入库；
- ``plan``（planner）：LLM 按 600//daily_minutes 归一化天数输出 N 天路径，落库；
- ``explain``（explainer）：逐天生成讲义（Lesson）+ 名词卡（Term），事件逐条推送；
- ``quiz``（quizmaster）：逐天生成练习题（QuizItem），事件逐条推送；
- ``finalize``：按主题从策展表取链接挂 SessionLink，会话置为 ready。

M2 起六角色拆到 app/agents 包成为独立积木，校验失败自动重试 1 次
（见 agents/validator.py），本模块只负责编排、LLM 工厂与会话收尾。

安全铁律：
- Key 只存在于本次调用（build_llm 的内存配置里），不落库、不入日志；
- 模型输出里的 URL 一律剔除（strip_urls），链接只能来自 curated_links 表。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..agents.base import AgentContext, ChatLLM, error_code_of, run_agent
from ..agents.diagnostician import Diagnostician
from ..agents.errors import PipelineError
from ..agents.explainer import Explainer
from ..agents.planner import Planner  # 再导出，对外路径不变
from ..agents.planner import plan_days as plan_days
from ..agents.quizmaster import Quizmaster
from ..config import Settings
from ..llm.client import ChatClient, config_from_settings, validate_base_url
from ..llm.fake import FakeLLM
from ..llm.providers import PRESET_BY_ID
from ..models.db import session_scope
from ..models.tables import CuratedLink, LearnSession, Plan, SessionLink
from .links import links_for_topic, topics_with_links

logger = logging.getLogger("chenyi.pipeline")

SESSION_STATUS_GENERATING = "generating"
SESSION_STATUS_READY = "ready"
SESSION_STATUS_FAILED = "failed"


# ── LLM 工厂 ─────────────────────────────────────────────────


def build_llm(
    settings: Settings,
    *,
    provider_id: str | None,
    base_url: str | None,
    model: str | None,
    api_key: str | None,
) -> ChatLLM:
    """按运行模式与本次请求的供应商信息构造 LLM。

    - demo 模式：恒用 FakeLLM（Key 即便带了也不使用、不落任何痕迹）；
    - live 模式：必带 X-LLM-Key，且要么选预设供应商、要么给自定义 Base URL。
    """
    if settings.demo:
        return FakeLLM()
    if not api_key:
        raise PipelineError(
            "真实模式需要携带 X-LLM-Key，请填写 API Key 后重试",
            status_code=400,
            error_code="missing_key",
        )
    preset = PRESET_BY_ID.get(provider_id) if provider_id else None
    if preset is not None:
        url = base_url or preset.base_url
        model_name = model or preset.default_model
        max_tokens_field = str(preset.compat.get("max_tokens_field", "max_tokens"))
    else:
        if not base_url:
            raise PipelineError(
                "请选择供应商预设，或填写自定义 Base URL",
                status_code=400,
                error_code="missing_provider",
            )
        try:
            url = validate_base_url(base_url)
        except ValueError as exc:
            raise PipelineError(
                str(exc), status_code=400, error_code="invalid_base_url"
            ) from exc
        model_name = model
        max_tokens_field = "max_tokens"
    config = config_from_settings(
        settings,
        base_url=url,
        model=model_name,
        api_key=api_key,
        max_tokens_field=max_tokens_field,
    )
    return ChatClient(config)


# ── 编排主流程 ───────────────────────────────────────────────


async def run_generation(
    *,
    session_id: int,
    settings: Settings,
    llm: ChatLLM,
    factory: Callable[[], Session],
    publish: Callable[[str, dict], Awaitable[int]],
) -> None:
    """后台生成任务。六角色由 agents 包执行，LLM 输出入库前先过 strip_urls。

    任一步骤失败：对应 AgentRun 记 failed + error_code，会话置 failed，
    发 SSE error 事件，然后异常继续上抛（由调用方收尾）。
    """
    # session_scope 是普通生成器：拿到会话后处理业务，正常退出 commit、
    # 异常 rollback，最后统一 close
    for db in session_scope(factory):
        try:
            await _run_steps(
                db=db,
                session_id=session_id,
                settings=settings,
                llm=llm,
                publish=publish,
            )
        except Exception as exc:  # noqa: BLE001 —— 无论什么错误都要收尾会话
            code = error_code_of(exc)
            db.execute(
                update(LearnSession)
                .where(LearnSession.id == session_id)
                .values(status=SESSION_STATUS_FAILED)
            )
            await publish(
                "error",
                {"code": code, "message": getattr(exc, "message", str(exc))},
            )
            db.commit()
            raise


async def _run_steps(
    *,
    db: Session,
    session_id: int,
    settings: Settings,
    llm: ChatLLM,
    publish: Callable[[str, dict], Awaitable[int]],
) -> None:
    goal = ""
    level = ""
    weak_points = ""
    daily_minutes = 30
    session_row = db.get(LearnSession, session_id)
    if session_row is not None:
        goal = session_row.goal
        level = session_row.level
        weak_points = session_row.weak_points
        daily_minutes = session_row.daily_minutes

    ctx = AgentContext(
        session_id=session_id,
        settings=settings,
        llm=llm,
        db=db,
        publish=publish,
    )

    # 1) 诊断：正文只走 SSE，不入库（tables 无诊断列，且保持表结构不动）
    await run_agent(
        Diagnostician(goal=goal, level=level, weak_points=weak_points),
        ctx,
    )

    # 2) 规划：N 天路径落库
    await run_agent(
        Planner(goal=goal, weak_points=weak_points, daily_minutes=daily_minutes),
        ctx,
    )
    plans = list(
        db.scalars(
            select(Plan)
            .where(Plan.session_id == session_id)
            .order_by(Plan.day_index)
        )
    )

    # 3) 逐天讲解 + 出题
    for plan in plans:
        await run_agent(Explainer(plan), ctx)
        await run_agent(Quizmaster(plan), ctx)

    # 4) 收官：策展链接（只来自 curated_links 表）+ 会话置 ready
    await run_agent(_FinalizeAgent([plan.topic for plan in plans]), ctx)
    row = db.get(LearnSession, session_id)
    if row is not None:
        row.status = SESSION_STATUS_READY
    db.commit()
    summary = build_summary(db, session_id)
    await publish("done", {"status": "ready", **summary["stats"], "links": summary["links"]})


class _FinalizeAgent:
    """收官步骤：把策展链接挂到会话（不调用 LLM，保留在 services 层）。"""

    name = "finalize"

    def __init__(self, topics: list[str]) -> None:
        self.topics = topics

    async def run(self, ctx: AgentContext) -> None:
        await _finalize(ctx.db, ctx.session_id, self.topics)
        return None


async def _finalize(db: Session, session_id: int, topics: list[str]) -> None:
    """按天主题挂策展链接（每主题最多 2 条、跨主题去重）。

    主题匹配策略：先精确匹配策展主题；miss 时做子串模糊匹配
    （任一方向包含即算命中）；仍 miss 的主题不重复补链接。
    """
    seen: set[int] = set()
    payload: list[dict] = []
    all_topics: list[str] = topics_with_links(db)
    for topic in topics:
        candidates: list[CuratedLink] = []
        if topic in all_topics:
            candidates = links_for_topic(db, topic, limit=2)
        else:
            # 子串模糊匹配：模型输出主题常带修饰词，如「大模型基础与工作原理」
            for ct in all_topics:
                if ct in topic or topic in ct:
                    match = links_for_topic(db, ct, limit=2)
                    if match:
                        candidates.extend(match)
                        break
        if not candidates:
            continue
        for link in candidates:
            if link.id in seen:
                continue
            seen.add(link.id)
            db.add(SessionLink(session_id=session_id, curated_link_id=link.id))
            payload.append(
                {
                    "title": link.title,
                    "url": link.url,
                    "why": link.why,
                    "topic": link.topic,
                }
            )
    db.commit()
    return None  # 保障返回值类型稳定（step 用不到）


# ── 会话摘要（GET /api/sessions/{id} 与 done 事件共用） ────────


def build_summary(db: Session, session_id: int) -> dict:
    """返回一次会话的结果摘要：会话信息 + 规划 + 链接 + 运行记录 + 统计。"""
    row = db.get(LearnSession, session_id)
    if row is None:
        return {"session": None}
    plans: list[dict] = []
    term_count = 0
    quiz_count = 0
    for plan in row.plans:
        lesson = plan.lessons[0] if plan.lessons else None
        terms = plan.terms
        quizzes = plan.quiz_items
        term_count += len(terms)
        quiz_count += len(quizzes)
        plans.append(
            {
                "id": plan.id,
                "day_index": plan.day_index,
                "topic": plan.topic,
                "objective": plan.objective,
                "deliverable": plan.deliverable,
                "lesson": (
                    {
                        "id": lesson.id,
                        "title": lesson.title,
                        "body_md": lesson.body_md,
                        "code_snippets": lesson.code_snippets,
                        "status": lesson.status,
                    }
                    if lesson
                    else None
                ),
                "terms": [
                    {
                        "id": term.id,
                        "term": term.term,
                        "definition": term.definition,
                        "analogy": term.analogy,
                        "pitfalls": term.pitfalls,
                        "topic": term.topic,
                    }
                    for term in terms
                ],
                "quiz_items": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "question": item.question,
                        "options": item.options,
                        "answer": item.answer,
                        "rubric": item.rubric,
                    }
                    for item in quizzes
                ],
            }
        )
    links: list[dict] = []
    for link in row.links:
        curated = db.get(CuratedLink, link.curated_link_id)
        if curated is not None:
            links.append(
                {
                    "id": link.id,
                    "title": curated.title,
                    "url": curated.url,
                    "why": curated.why,
                    "topic": curated.topic,
                }
            )
    return {
        "session": {
            "id": row.id,
            "client_id": row.client_id,
            "goal": row.goal,
            "level": row.level,
            "weak_points": row.weak_points,
            "daily_minutes": row.daily_minutes,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        },
        "plans": plans,
        "links": links,
        "runs": [
            {
                "step": run.step,
                "status": run.status,
                "attempt": run.attempt,
                "duration_ms": run.duration_ms,
                "error_code": run.error_code,
            }
            for run in row.runs
        ],
        "stats": {
            "plan_count": len(plans),
            "lesson_count": len(plans),
            "term_count": term_count,
            "quiz_count": quiz_count,
        },
    }