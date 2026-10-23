"""学习路径生成流水线（多 Agent 编排）。

调用序列（每一步对应一条 AgentRun 记录，失败即停并标记会话）：

    init → diagnose → plan → 逐天 (explain + quiz) → finalize

- ``init``：在 POST /api/sessions 里完成（建会话 + 首条 AgentRun），不进本模块；
- ``diagnose``：LLM 输出诊断摘要，正文只发 SSE progress 事件，不入库；
- ``plan``：LLM 按 600//daily_minutes 归一化天数输出 N 天路径，落库；
- ``explain``：逐天生成讲义（Lesson）+ 名词卡（Term），事件逐条推送；
- ``quiz``：逐天生成练习题（QuizItem），事件逐条推送；
- ``finalize``：按主题从策展表取链接挂 SessionLink，会话置为 ready。

安全铁律：
- Key 只存在于本次调用（build_llm 的内存配置里），不落库、不入日志；
- 模型输出里的 URL 一律剔除（strip_urls），链接只能来自 curated_links 表。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..config import Settings
from ..llm.client import ChatClient, LLMClientError, config_from_settings, validate_base_url
from ..llm.fake import FakeLLM
from ..llm.providers import PRESET_BY_ID
from ..llm.schemas import ChatMessage, DiagnosisResult, ExplainResult, PlanResult, QuizResult
from ..models.db import session_scope
from ..models.tables import (
    AgentRun,
    CuratedLink,
    LearnSession,
    Lesson,
    Plan,
    QuizItem,
    SessionLink,
    Term,
)
from .links import links_for_topic, strip_urls, topics_with_links

logger = logging.getLogger("chenyi.pipeline")


class PipelineError(Exception):
    """流水线自身可预期的失败（参数缺失 / 模型输出不合法等）。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        error_code: str = "internal",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


#: 任何 LLM 实现都满足的最小接口（FakeLLM 与 ChatClient 都是）
class ChatLLM(Protocol):
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str: ...


SESSION_STATUS_GENERATING = "generating"
SESSION_STATUS_READY = "ready"
SESSION_STATUS_FAILED = "failed"


def plan_days(daily_minutes: int) -> int:
    """天数确定性公式：时间越紧天数越多（上限 7，下限 3）。"""
    return max(3, min(7, 600 // daily_minutes))


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


# ── 提示词与解析 ─────────────────────────────────────────────

SYSTEM_PROMPT = (
    "你是一位耐心、严谨的 AI 开发入门导师。只输出合法 JSON，不要输出任何解释。"
    "回复里不允许出现任何 URL 链接。JSON 内容必须是中文。"
)


def build_messages(task: str, **fields: str | int) -> list[ChatMessage]:
    """构造一轮对话：系统约束 + 带任务标记与上下文参数的用户消息。"""
    head = f"任务标记：{task}"
    body = "\n".join(f"{label}：{value}" for label, value in fields.items())
    return [
        ChatMessage.system(SYSTEM_PROMPT),
        ChatMessage.user(head if not body else f"{head}\n{body}"),
    ]


_T = TypeVar("_T", bound=BaseModel)

#: 去掉模型常见的 ```json ``` 代码围栏
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_struct(text: str, model: type[_T]) -> _T:
    """把 LLM 输出解析成目标结构；解析失败抛 PipelineError(bad_format)。"""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            "模型输出的 JSON 无法解析，请重试或换个模型",
            error_code="bad_format",
        ) from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise PipelineError(
            f"模型输出不符合约定结构：{exc.errors()[:2]}",
            error_code="bad_format",
        ) from exc


def _error_code(exc: Exception) -> str:
    if isinstance(exc, PipelineError):
        return exc.error_code
    if isinstance(exc, LLMClientError):
        return exc.error_code
    return "internal"


# ── 编排主流程 ───────────────────────────────────────────────


async def run_generation(
    *,
    session_id: int,
    settings: Settings,
    llm: ChatLLM,
    factory: Callable[[], Session],
    publish: Callable[[str, dict], Awaitable[int]],
) -> None:
    """后台生成任务。所有 LLM 输出入库前先过 strip_urls。

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
            code = _error_code(exc)
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

    async def step(step_name: str, func: Callable[[], Awaitable[object]]) -> object:
        run = AgentRun(session_id=session_id, step=step_name, status="running")
        db.add(run)
        db.commit()
        start = time.perf_counter()
        try:
            result = await func()
        except Exception as exc:
            run.status = "failed"
            run.error_code = _error_code(exc)
            run.duration_ms = int((time.perf_counter() - start) * 1000)
            db.commit()
            raise
        run.status = "done"
        run.duration_ms = int((time.perf_counter() - start) * 1000)
        db.commit()
        return result

    # 1) 诊断：正文只走 SSE，不入库（tables 无诊断列，且保持表结构不动）
    diagnosis = await step(
        "diagnose",
        lambda: _call_llm(
            llm, build_messages("diagnose", 目标=goal, 水平=level, 薄弱点=weak_points), DiagnosisResult
        ),
    )
    await publish("progress", {"step": "diagnose", "message": diagnosis.render_message()})

    # 2) 规划：N 天路径落库
    days = plan_days(daily_minutes)
    plan_result = await step(
        "plan",
        lambda: _call_llm(
            llm, build_messages("plan", 天数=days, 目标=goal, 薄弱点=weak_points), PlanResult
        ),
    )
    plans: list[Plan] = []
    for day in plan_result.days:
        plan = Plan(
            session_id=session_id,
            day_index=day.day_index,
            topic=strip_urls(day.topic),
            objective=strip_urls(day.objective),
            deliverable=strip_urls(day.deliverable),
        )
        db.add(plan)
        db.flush()
        plans.append(plan)
    db.commit()
    await publish(
        "progress",
        {
            "step": "plan",
            "message": f"已为你规划 {len(plans)} 天的学习路径（每天约 {daily_minutes} 分钟）。",
        },
    )

    # 3) 逐天讲解 + 出题
    for plan in plans:
        explain = await step(
            f"explain.{plan.day_index}",
            lambda p=plan: _call_llm(
                llm, build_messages("explain", 主题=p.topic, 天次=p.day_index), ExplainResult
            ),
        )
        lesson = Lesson(
            plan_id=plan.id,
            title=strip_urls(explain.lesson_title),
            body_md=strip_urls(explain.lesson_body_md),
            code_snippets=[strip_urls(s) for s in explain.code_snippets],
            status="ready",
        )
        db.add(lesson)
        terms: list[Term] = []
        for card in explain.terms:
            term = Term(
                plan_id=plan.id,
                term=strip_urls(card.term),
                definition=strip_urls(card.definition),
                analogy=strip_urls(card.analogy),
                pitfalls=strip_urls(card.pitfalls),
                topic=strip_urls(card.topic) or plan.topic,
            )
            db.add(term)
            terms.append(term)
        db.commit()
        await publish(
            "lesson",
            {
                "day": plan.day_index,
                "topic": plan.topic,
                "title": lesson.title,
                "body_md": lesson.body_md,
                "code_snippets": lesson.code_snippets,
            },
        )
        for term in terms:
            await publish(
                "term",
                {
                    "day": plan.day_index,
                    "term": term.term,
                    "definition": term.definition,
                    "analogy": term.analogy,
                    "pitfalls": term.pitfalls,
                    "topic": term.topic,
                },
            )

        quiz = await step(
            f"quiz.{plan.day_index}",
            lambda p=plan: _call_llm(
                llm, build_messages("quiz", 主题=p.topic, 天次=p.day_index), QuizResult
            ),
        )
        for item in quiz.quizzes:
            item_row = QuizItem(
                plan_id=plan.id,
                kind=item.kind,
                question=strip_urls(item.question),
                options=[strip_urls(o) for o in item.options],
                answer=strip_urls(item.answer),
                rubric=strip_urls(item.rubric),
            )
            db.add(item_row)
            db.flush()
            await publish(
                "quiz",
                {
                    "day": plan.day_index,
                    "quiz_id": item_row.id,
                    "kind": item_row.kind,
                    "question": item_row.question,
                    "options": item_row.options,
                    "answer": item_row.answer,
                    "rubric": item_row.rubric,
                },
            )
        db.commit()

    # 4) 收官：策展链接（只来自 curated_links 表）+ 会话置 ready
    await step("finalize", lambda: _finalize(db, session_id, [p.topic for p in plans]))
    row = db.get(LearnSession, session_id)
    if row is not None:
        row.status = SESSION_STATUS_READY
    db.commit()
    summary = build_summary(db, session_id)
    await publish("done", {"status": "ready", **summary["stats"], "links": summary["links"]})


async def _call_llm(llm: ChatLLM, messages: list[ChatMessage], model: type[_T]) -> _T:
    """调用 LLM 并解析成约定结构（所有文本已由各步骤做 strip_urls）。"""
    text = await llm.chat(messages, json_mode=True)
    return parse_struct(text, model)


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