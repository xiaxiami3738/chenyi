"""reviewer 单测：M2 样板 —— schema 校验 + 失败重试 1 次 + 预留复核接口。"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext, run_agent
from app.agents.errors import OutputInvalidError
from app.agents.reviewer import Reviewer
from app.llm.schemas import ReviewResult
from app.models.db import session_scope
from app.models.tables import AgentRun, LearnSession


def _run(coro):
    return asyncio.run(coro)


def _create_session(client) -> int:
    for db in session_scope(client.app.state.session_factory):
        row = LearnSession(
            client_id="agents-reviewer", goal="学会 RAG", status="generating"
        )
        db.add(row)
        db.flush()
        db.add(AgentRun(session_id=row.id, step="init", status="done"))
        db.commit()
        return row.id
    raise AssertionError("无法创建会话")  # pragma: no cover


class FixedLLM:
    def __init__(self, payload: dict, *, bad_calls: int = 0) -> None:
        self.payload = payload
        self.bad_calls = bad_calls
        self.calls = 0

    async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
        self.calls += 1
        if self.calls <= self.bad_calls:
            return "坏 JSON"
        return json.dumps(self.payload, ensure_ascii=False)


def _ctx(client, session_id, llm):
    db = client.app.state.session_factory()
    return (
        AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=llm,
            db=db,
            publish=lambda *_: asyncio_ok(0),
        ),
        db,
    )


def test_reviewer_parses_review_result(client) -> None:
    """正常路径：LLM 输出被校验成 ReviewResult。"""
    llm = FixedLLM({"verdict": "通过", "comment": "术语解释准确"})
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id, llm)
    try:
        result = _run(
            run_agent(Reviewer(content="讲义正文", subject="RAG 检索增强"), ctx)
        )
        assert isinstance(result, ReviewResult)
        assert result.verdict == "通过"
        assert result.comment == "术语解释准确"
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "reviewer")
        assert run.status == "done"
    finally:
        db.close()


def test_reviewer_retries_on_bad_json(client) -> None:
    """校验失败重试 1 次，恢复后成功。"""
    llm = FixedLLM({"verdict": "不通过", "comment": "超范围"}, bad_calls=1)
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id, llm)
    try:
        result = _run(run_agent(Reviewer(content="c", subject="s"), ctx))
        assert result.verdict == "不通过"
        assert llm.calls == 2
    finally:
        db.close()


def test_reviewer_retry_exhausted_fails_step(client) -> None:
    llm = FixedLLM({"verdict": "", "comment": ""}, bad_calls=99)
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id, llm)
    try:
        with pytest.raises(OutputInvalidError):
            _run(run_agent(Reviewer(content="c", subject="s"), ctx))
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "reviewer")
        assert run.status == "failed"
        assert run.error_code == "invalid_output"
    finally:
        db.close()


def test_reviewer_review_is_placeholder(client) -> None:
    """M2 预留接口：返回空实现（M5/M6 扩展真实复核）。"""
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id, FixedLLM({}))
    try:
        place = _run(Reviewer().review(ctx))
        assert set(place) == {"verdict", "comment"}
    finally:
        db.close()

async def asyncio_ok(value: int = 0) -> int:
    """同步 publish 占位：返回可 await 的协程。"""
    return value
