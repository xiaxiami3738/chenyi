"""agents.base 单测：run_agent 统一执行包装与 error_code_of 映射。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext, error_code_of, run_agent
from app.agents.errors import OutputInvalidError, PipelineError
from app.llm.fake import FakeLLM
from app.models.db import session_scope
from app.models.tables import AgentRun, LearnSession


def _run(coro):
    return asyncio.run(coro)


def _create_session(client) -> int:
    for db in session_scope(client.app.state.session_factory):
        row = LearnSession(
            client_id="agents-base", goal="学会 RAG", status="generating"
        )
        db.add(row)
        db.flush()
        db.add(AgentRun(session_id=row.id, step="init", status="done"))
        db.commit()
        return row.id
    raise AssertionError("无法创建会话")  # pragma: no cover


def _ctx(client, session_id):
    db = client.app.state.session_factory()
    return (
        AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=FakeLLM(),
            db=db,
            publish=lambda *_: asyncio_ok(0),
        ),
        db,
    )


class _DoneAgent:
    name = "demo_done"

    def __init__(self, value: str) -> None:
        self.value = value

    async def run(self, ctx: AgentContext) -> str:
        return self.value


class _FailAgent:
    name = "demo_fail"

    async def run(self, ctx: AgentContext) -> None:
        raise ValueError("模拟未知错误")


def test_run_agent_records_done(client) -> None:
    """成功路径：AgentRun 置 done 并记录耗时，返回值原样透传。"""
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id)
    try:
        result = _run(run_agent(_DoneAgent("ok"), ctx))
        assert result == "ok"
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "demo_done")
        assert run.status == "done"
        assert run.error_code is None
        assert run.duration_ms is not None and run.duration_ms >= 0
    finally:
        db.close()


def test_run_agent_failure_marks_failed(client) -> None:
    """失败路径：AgentRun 记 failed + error_code，并原样上抛异常。"""
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id)
    try:
        with pytest.raises(ValueError, match="模拟未知错误"):
            _run(run_agent(_FailAgent(), ctx))
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "demo_fail")
        assert run.status == "failed"
        assert run.error_code == "internal"  # 未知异常 → internal
        assert run.duration_ms is not None and run.duration_ms >= 0
    finally:
        db.close()


def test_run_agent_failure_uses_output_invalid_code(client) -> None:
    """校验失败的异常带 error_code → failed 记录里原样保留。"""

    class _BadOutputAgent:
        name = "demo_bad"

        async def run(self, ctx: AgentContext) -> None:
            raise OutputInvalidError("坏输出")

    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id)
    try:
        with pytest.raises(OutputInvalidError):
            _run(run_agent(_BadOutputAgent(), ctx))
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "demo_bad")
        assert run.status == "failed"
        assert run.error_code == "invalid_output"
    finally:
        db.close()


def test_error_code_of_mapping() -> None:
    assert error_code_of(OutputInvalidError("x")) == "invalid_output"
    assert error_code_of(PipelineError("m", error_code="boom")) == "boom"
    assert error_code_of(ValueError("x")) == "internal"

async def asyncio_ok(value: int = 0) -> int:
    """同步 publish 占位：返回可 await 的协程。"""
    return value
