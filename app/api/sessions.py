"""HTTP 会话接口：创建生成、SSE 实时进度、结果摘要。

约定：
- 创建会话即启动后台生成任务（demo 用 FakeLLM，live 用 ChatClient）；
- 进度通过 Server-Sent Events 推送：progress / lesson / term / quiz / done / error；
- X-LLM-Key 请求头单次透传，Key 只存在于本次请求上下文，不落库、不入日志；
- 并发上限用 Settings.max_concurrency 信号量，超限返回 HTTP 429。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .. import __version__
from ..llm.redact import mask_secrets
from ..models.tables import AgentRun, LearnSession, Plan
from ..services.pipeline import PipelineError, build_llm, build_summary, run_generation

logger = logging.getLogger("chenyi.sessions")

router = APIRouter(prefix="/api", tags=["sessions"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _open_session(request: Request):
    return request.app.state.session_factory()


class SessionCreateRequest(BaseModel):
    """创建学习会话的请求体（不含 Key —— Key 走 X-LLM-Key 请求头）。"""

    client_id: str = Field(min_length=1, max_length=64, description="匿名端标识")
    goal: str = Field(min_length=1, description="学习目标")
    level: Literal["zero", "beginner", "intermediate"] = "beginner"
    weak_points: str = Field(default="", max_length=2000)
    daily_minutes: int = Field(default=30, ge=10, le=480)
    provider_id: str | None = Field(default=None, description="供应商预设 id")
    base_url: str | None = Field(default=None, description="自定义 Base URL")
    model: str | None = Field(default=None, description="模型名（留空用预设默认）")


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """统一错误信封：{"error": {"code": ..., "message": ...}}。"""
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


@router.post("/sessions")
async def create_session(request: Request, body: SessionCreateRequest) -> JSONResponse:
    """创建会话并开始后台生成，返回 session_id。"""
    settings = request.app.state.settings
    if len(body.goal) > settings.max_input_chars:
        return _error(
            400,
            "goal_too_long",
            f"目标内容超过长度上限（{settings.max_input_chars} 字符）。",
        )

    key = request.headers.get("x-llm-key", "").strip() or None
    try:
        llm = build_llm(
            settings,
            provider_id=body.provider_id,
            base_url=body.base_url,
            model=body.model,
            api_key=key,
        )
    except PipelineError as exc:
        return _error(exc.status_code, exc.error_code, exc.message)

    # 并发限流：没有可用配额（locked）直接 429；否则取一个配额，
    # 由后台任务看完整体在 finally 里释放。竞态窗口极小，可接受。
    semaphore: asyncio.Semaphore = request.app.state.semaphore
    if semaphore.locked():
        return _error(429, "busy", "当前生成任务较多，请稍后再试。")
    await semaphore.acquire()
    try:
        session = _open_session(request)
        try:
            row = LearnSession(
                client_id=body.client_id,
                goal=body.goal,
                level=body.level,
                weak_points=body.weak_points,
                daily_minutes=body.daily_minutes,
                status="generating",
            )
            session.add(row)
            session.flush()
            session.add(
                AgentRun(session_id=row.id, step="init", status="done")
            )
            session.commit()
            session_id = row.id
        finally:
            session.close()

        bus = SessionEventBus(session_id=session_id)
        request.app.state.live[session_id] = bus
        asyncio.create_task(
            _generation_job(request, session_id, llm, bus)
        )
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "mode": settings.app_mode,
                "status": "generating",
                "version": __version__,
            },
        )
    except Exception:
        # 建会话失败要把信号量还回去，避免泄漏
        semaphore.release()
        raise


async def _generation_job(request: Request, session_id: int, llm, bus: SessionEventBus) -> None:
    """后台生成：跑流水线，结束无论成败都关流、还信号量、清事件总线。"""
    app = request.app
    try:
        await run_generation(
            session_id=session_id,
            settings=app.state.settings,
            llm=llm,
            factory=app.state.session_factory,
            publish=bus.publish,
        )
    except Exception as exc:  # noqa: BLE001 —— 流水线已发 error 事件，这里只兜底日志
        logger.error(
            "学习路径生成失败 session=%s err=%s",
            session_id,
            mask_secrets(str(exc)),
        )
    finally:
        await bus.close()
        # 总线在生成结束后即移除，后续连接走 DB 回放路径（done/error 单条）
        app.state.live.pop(session_id, None)
        app.state.semaphore.release()


# ── 事件总线（进程内，供 SSE 订阅） ─────────────────────────────


class SessionEventBus:
    """一次会话的进程内事件流：publish 写入，subscribe 增量消费。"""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self._events: list[tuple[int, str, dict]] = []
        self._seq = 0
        self._closed = False
        self._condition = asyncio.Condition()

    async def publish(self, event_type: str, payload: dict) -> int:
        """追加一条事件，返回其自增序号。"""
        async with self._condition:
            self._seq += 1
            self._events.append((self._seq, event_type, payload))
            self._condition.notify_all()
            return self._seq

    async def close(self) -> None:
        """标记流结束（通知所有等待者，随后被订阅方自然耗尽）。"""
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def subscribe(
        self, last_id: int = 0
    ) -> AsyncIterator[tuple[int, str, dict]]:
        """增量消费：yield (seq, event_type, payload)，流结束且耗尽后返回。"""
        while True:
            async with self._condition:
                pending = [event for event in self._events if event[0] > last_id]
            for seq, event_type, payload in pending:
                last_id = seq
                yield seq, event_type, payload
            async with self._condition:
                if self._closed and last_id >= self._seq:
                    return
                if last_id >= self._seq:
                    await self._condition.wait()


# ── SSE 端点 ──────────────────────────────────────────────────


@router.get("/sessions/{session_id}/events", response_model=None)
async def session_events(request: Request, session_id: int) -> StreamingResponse | JSONResponse:
    """SSE 实时进度；支持 Last-Event-ID 断点续传。事件中文内容。"""
    bus: SessionEventBus | None = request.app.state.live.get(session_id)
    if bus is None:
        return await _stream_from_db(request, session_id)

    last_id = _parse_last_event_id(request)

    async def event_stream():
        async for seq, event_type, payload in bus.subscribe(last_id):
            yield _sse_frame(seq, event_type, payload)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


def _parse_last_event_id(request: Request) -> int:
    try:
        return max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        return 0


def _sse_frame(seq: int, event_type: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {seq}\nevent: {event_type}\ndata: {data}\n\n"


async def _stream_from_db(request: Request, session_id: int) -> StreamingResponse | JSONResponse:
    """总线已移除（生成结束）时的回放路径：ready 发单条 done，failed 发 error。"""
    session = _open_session(request)
    try:
        row = session.get(LearnSession, session_id)
        if row is None:
            return _error(404, "not_found", "会话不存在。")
        if row.status == "ready":
            summary = build_summary(session, session_id)
            payload = {"status": "ready", **summary["stats"], "links": summary["links"]}
        elif row.status == "failed":
            failed = next(
                (run for run in row.runs if run.status == "failed"), None
            )
            payload = {
                "code": (failed.error_code if failed else "internal"),
                "message": "生成失败，请调整输入后重试。",
            }
        else:
            return _error(404, "not_found", "该会话没有可回放的事件。")

        async def event_stream():
            if row.status == "ready":
                yield _sse_frame(1, "done", payload)
            else:
                yield _sse_frame(1, "error", payload)

        return StreamingResponse(
            event_stream(), media_type="text/event-stream", headers=SSE_HEADERS
        )
    finally:
        session.close()


# ── 结果摘要 ──────────────────────────────────────────────────


@router.get("/sessions/{session_id}")
def get_session(request: Request, session_id: int) -> JSONResponse:
    """返回会话结果摘要（学习路径 / 名词卡 / 练习 / 链接 / 运行记录）。"""
    session = _open_session(request)
    try:
        row = session.get(LearnSession, session_id)
        if row is None:
            return _error(404, "not_found", "会话不存在。")
        summary = build_summary(session, session_id)
        summary["mode"] = request.app.state.settings.app_mode
        return JSONResponse(status_code=200, content=summary)
    finally:
        session.close()


# ── 历史列表与 Markdown 导出（M3） ────────────────────────


@router.get("/sessions")
def list_sessions(
    request: Request,
    client_id: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
) -> JSONResponse:
    """按匿名 client_id 列出历史会话（新→旧），带轻量统计。

    隔离原则：只返回该 client_id 自己的会话，绝不返回别人的。
    """
    if not client_id:
        return _error(400, "missing_client_id", "缺少 client_id 参数。")

    session = _open_session(request)
    try:
        rows = (
            session.scalars(
                select(LearnSession)
                .where(LearnSession.client_id == client_id)
                .options(
                    selectinload(LearnSession.plans).selectinload(Plan.lessons),
                    selectinload(LearnSession.plans).selectinload(Plan.terms),
                    selectinload(LearnSession.plans).selectinload(Plan.quiz_items),
                )
                .order_by(LearnSession.id.desc())
                .limit(limit)
            )
            .all()
        )
        # selectinload 已预加载关系，行对象脱管后仍可读属性
        result = []
        for row in rows:
            result.append(
                {
                    "id": row.id,
                    "goal": row.goal,
                    "level": row.level,
                    "status": row.status,
                    "daily_minutes": row.daily_minutes,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "stats": {
                        "plan_count": len(row.plans),
                        "lesson_count": sum(1 for p in row.plans if p.lessons),
                        "term_count": sum(len(p.terms) for p in row.plans),
                        "quiz_count": sum(len(p.quiz_items) for p in row.plans),
                    },
                }
            )
        return JSONResponse(status_code=200, content={"sessions": result})
    finally:
        session.close()


_LEVEL_LABELS = {
    "zero": "完全零基础",
    "beginner": "能写点代码，刚接触 AI",
    "intermediate": "用过 API / 做过小项目",
}
_KIND_LABELS = {"choice": "选择题", "blank": "填空题", "short": "简答题"}


def render_session_markdown(summary: dict) -> str:
    """把 build_summary 渲染成完整的中文 Markdown 文本（纯字符串模板）。"""
    session = summary["session"] or {}
    lines: list[str] = [
        f"# 学习路径 · 会话 #{session.get('id', '')}",
        "",
        f"- **学习目标**：{session.get('goal', '')}",
        f"- **当前水平**：{_LEVEL_LABELS.get(session.get('level', ''), session.get('level', ''))}",
        f"- **每日可投入**：{session.get('daily_minutes', '')} 分钟",
        f"- **生成时间**：{session.get('created_at', '') or '—'}",
        f"- **状态**：{session.get('status', '')}",
        "",
    ]

    plans = summary.get("plans", [])
    if not plans:
        lines.append("> 该会话还没有生成出学习计划。")
        lines.append("")
    for plan in plans:
        lines.append(f"## 第 {plan['day_index']} 天 · {plan['topic']}")
        lines.append("")
        lines.append(f"- **目标**：{plan.get('objective', '')}")
        lines.append(f"- **交付物**：{plan.get('deliverable', '')}")
        lines.append("")

        lesson = plan.get("lesson")
        if lesson:
            lines.append(f"### 讲义：{lesson.get('title', '')}")
            lines.append("")
            lines.append(lesson.get("body_md", "") or "（无正文）")
            lines.append("")
            for snippet in lesson.get("code_snippets", []):
                lines.append("```python")
                lines.append(snippet)
                lines.append("```")
                lines.append("")

        terms = plan.get("terms", [])
        if terms:
            lines.append("### 名词卡")
            lines.append("")
            for term in terms:
                lines.append(
                    f"- **{term.get('term', '')}**：{term.get('definition', '')}"
                )
                if term.get("analogy"):
                    lines.append(f"  - 类比：{term['analogy']}")
                if term.get("pitfalls"):
                    lines.append(f"  - 注意：{term['pitfalls']}")
            lines.append("")

        quiz_items = plan.get("quiz_items", [])
        if quiz_items:
            lines.append("### 练习")
            lines.append("")
            for item in quiz_items:
                kind = _KIND_LABELS.get(item.get("kind", ""), item.get("kind", ""))
                lines.append(f"**{kind}** {item.get('question', '')}")
                for opt in item.get("options", []):
                    lines.append(f"- {opt}")
                lines.append(f"**参考答案**：{item.get('answer', '')}")
                if item.get("rubric"):
                    lines.append(f"**评分要点**：{item['rubric']}")
                lines.append("")

    links = summary.get("links", [])
    lines.append("## 延伸阅读（人工策展）")
    lines.append("")
    if not links:
        lines.append("> 暂无匹配的策展链接。")
    for link in links:
        lines.append(
            f"- [{link.get('title', '')}]({link.get('url', '')}) —— "
            f"{link.get('why', '')}（主题：{link.get('topic', '')}）"
        )
    lines.append("")
    return "\n".join(lines)


@router.get("/sessions/{session_id}/export.md", response_model=None)
def export_session_markdown(request: Request, session_id: int) -> Response:
    """导出会话结果为 Markdown 附件（plan-{id}.md）。

    说明：此处不校验 client_id —— 会话 id 不可枚举（数据库自增 + 需要先建会话
    才能导出），且 demo 模式不含敏感数据；如后续需要更严格的隔离，
    可在导出时加一个 client_id 校验参数。
    """
    session = _open_session(request)
    try:
        row = session.get(LearnSession, session_id)
        if row is None:
            return _error(404, "not_found", "会话不存在。")
        summary = build_summary(session, session_id)
        markdown = render_session_markdown(summary)
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="plan-{session_id}.md"'
            },
        )
    finally:
        session.close()