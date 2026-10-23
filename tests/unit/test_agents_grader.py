"""grader 单测：M2 样板 —— schema 校验 + 失败重试 1 次 + 预留评估接口。"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext, run_agent
from app.agents.errors import OutputInvalidError
from app.agents.grader import Grader
from app.llm.schemas import GradeResult
from app.models.db import session_scope
from app.models.tables import AgentRun, LearnSession


def _run(coro):
    return asyncio.run(coro)


def _create_session(client) -> int:
    for db in session_scope(client.app.state.session_factory):
        row = LearnSession(
            client_id="agents-grader", goal="学会 RAG", status="generating"
        )
        db.add(row)
        db.flush()
        db.add(AgentRun(session_id=row.id, step="init", status="done"))
        db.commit()
        return row.id
    raise AssertionError("无法创建会话")  # pragma: no cover


class FixedLLM:
    """固定返回给定 payload；可配置前 N 次返回坏 JSON 以测重试。"""

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


def test_grader_parses_grade_result(client) -> None:
    """正常路径：LLM 输出被校验成 GradeResult。"""
    llm = FixedLLM(
        {"score": "6/10", "comment": "思路清晰，但深度不足", "suggestion": "补充检索示例"}
    )
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id, llm)
    try:
        result = _run(run_agent(Grader(question="什么是 RAG？", answer="检索增强"), ctx))
        assert isinstance(result, GradeResult)
        assert result.score == "6/10"
        assert "深度不足" in result.comment
        assert result.suggestion == "补充检索示例"
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "grader")
        assert run.status == "done"
    finally:
        db.close()


def test_grader_retries_on_bad_json(client) -> None:
    """校验失败重试 1 次，恢复后成功。"""
    llm = FixedLLM(
        {"score": "9/10", "comment": "很好", "suggestion": "再接再厉"}, bad_calls=1
    )
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id, llm)
    try:
        result = _run(run_agent(Grader(question="q", answer="a"), ctx))
        assert result.score == "9/10"
        assert llm.calls == 2
    finally:
        db.close()


def test_grader_retry_exhausted_fails_step(client) -> None:
    """连续两次坏输出：AgentRun 记 failed(invalid_output)。"""
    llm = FixedLLM({"score": "1", "comment": "", "suggestion": ""}, bad_calls=99)
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id, llm)
    try:
        with pytest.raises(OutputInvalidError):
            _run(run_agent(Grader(question="q", answer="a"), ctx))
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "grader")
        assert run.status == "failed"
        assert run.error_code == "invalid_output"
    finally:
        db.close()


def test_grader_evaluate_is_placeholder(client) -> None:
    """M2 预留接口：返回空实现（M5/M6 扩展真实评估）。"""
    session_id = _create_session(client)
    ctx, db = _ctx(client, session_id, FixedLLM({}))
    try:
        place = _run(Grader().evaluate(ctx))
        assert set(place) == {"score", "comment", "suggestion"}
        assert place["score"] is None
    finally:
        db.close()

async def asyncio_ok(value: int = 0) -> int:
    """同步 publish 占位：返回可 await 的协程。"""
    return value
