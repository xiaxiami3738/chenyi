"""diagnostician 单测：正常路径 + LLM 输出校验失败重试 1 次 + 重试耗尽失败。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext, run_agent
from app.agents.diagnostician import Diagnostician
from app.agents.errors import OutputInvalidError
from app.agents.validator import ask_llm_with_retry
from app.llm.fake import FakeLLM
from app.llm.schemas import DiagnosisResult
from app.models.db import session_scope
from app.models.tables import AgentRun, LearnSession


def _run(coro):
    return asyncio.run(coro)


def _create_session(client, *, goal="从零学会 RAG 应用开发") -> int:
    for db in session_scope(client.app.state.session_factory):
        row = LearnSession(
            client_id="agents-diag",
            goal=goal,
            level="beginner",
            weak_points="没写过 Python",
            status="generating",
        )
        db.add(row)
        db.flush()
        db.add(AgentRun(session_id=row.id, step="init", status="done"))
        db.commit()
        return row.id
    raise AssertionError("无法创建会话")  # pragma: no cover


class Collector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, payload: dict) -> int:
        self.events.append((event_type, payload))
        return len(self.events)


class FlakyLLM:
    """第一次调用返回坏 JSON，之后委托给 FakeLLM（验证重试恢复）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.delegate = FakeLLM()

    async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
        self.calls += 1
        if self.calls == 1:
            return "这不是 JSON"
        return await self.delegate.chat(
            messages, json_mode=json_mode, max_tokens=max_tokens
        )


class BadLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
        self.calls += 1
        return "{ 坏的 JSON"


def test_diagnostician_publishes_progress(client) -> None:
    """正常路径：产出诊断意见并发布 progress 事件（正文不入库）。"""
    session_id = _create_session(client)
    collector = Collector()
    db = client.app.state.session_factory()
    try:
        ctx = AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=FakeLLM(),
            db=db,
            publish=collector,
        )
        result = _run(run_agent(Diagnostician(goal="学会 RAG"), ctx))
        assert isinstance(result, DiagnosisResult)
        assert result.summary
        assert result.focus
        assert collector.events == [
            ("progress", {"step": "diagnose", "message": result.render_message()})
        ]
    finally:
        db.close()


def test_retry_recovers_from_bad_json(client) -> None:
    """校验失败自动重试 1 次：第二次输出合法则成功。"""
    session_id = _create_session(client)
    db = client.app.state.session_factory()
    llm = FlakyLLM()
    try:
        ctx = AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=llm,
            db=db,
            publish=lambda *_: asyncio_ok(0),
        )
        result = _run(run_agent(Diagnostician(goal="学会 RAG"), ctx))
        assert isinstance(result, DiagnosisResult)
        assert llm.calls == 2
    finally:
        db.close()


def test_retry_exhausted_fails_step(client) -> None:
    """连续两次坏输出：抛 OutputInvalidError，AgentRun 记 failed(invalid_output)。"""
    session_id = _create_session(client)
    db = client.app.state.session_factory()
    llm = BadLLM()
    try:
        ctx = AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=llm,
            db=db,
            publish=lambda *_: asyncio_ok(0),
        )
        with pytest.raises(OutputInvalidError) as exc_info:
            _run(run_agent(Diagnostician(goal="学会 RAG"), ctx))
        assert exc_info.value.error_code == "invalid_output"
        assert llm.calls == 2
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "diagnose")
        assert run.status == "failed"
        assert run.error_code == "invalid_output"
    finally:
        db.close()


def test_ask_llm_with_retry_passes_hint_on_retry() -> None:
    """重试时消息列表会追加修正提示（不含任务标记，FakeLLM 仍可分发）。"""

    observed: list[list] = []

    class RecordingLLM:
        async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
            observed.append(messages)
            if len(observed) == 1:
                return "坏 JSON"
            return '{"summary": "ok", "focus": ["重试成功"]}'

    _run(ask_llm_with_retry(RecordingLLM(), [], DiagnosisResult))
    assert len(observed) == 2
    assert len(observed[1]) == len(observed[0]) + 1
    assert "任务标记" not in observed[1][-1].content

async def asyncio_ok(value: int = 0) -> int:
    """同步 publish 占位：返回可 await 的协程。"""
    return value
