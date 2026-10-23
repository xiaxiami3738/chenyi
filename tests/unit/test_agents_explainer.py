"""explainer 单测：正常路径落 Lesson/Term + 重试恢复 + 重试耗尽失败。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext, run_agent
from app.agents.errors import OutputInvalidError
from app.agents.explainer import Explainer
from app.llm.fake import FakeLLM
from app.llm.schemas import ExplainResult
from app.models.db import session_scope
from app.models.tables import AgentRun, LearnSession, Lesson, Plan, Term


def _run(coro):
    return asyncio.run(coro)


async def _noop_publish(event_type: str, payload: dict) -> int:
    """publish 占位：什么都不做，返回伪事件序号。"""
    return 0


def _create_session(client) -> tuple[int, int]:
    """建会话 + 一天计划，返回 (session_id, plan_id)。"""
    for db in session_scope(client.app.state.session_factory):
        row = LearnSession(
            client_id="agents-explain", goal="学会 RAG", status="generating"
        )
        db.add(row)
        db.flush()
        db.add(AgentRun(session_id=row.id, step="init", status="done"))
        plan = Plan(session_id=row.id, day_index=1, topic="RAG 检索增强")
        db.add(plan)
        db.commit()
        return row.id, plan.id
    raise AssertionError("无法创建会话")  # pragma: no cover


class Collector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, payload: dict) -> int:
        self.events.append((event_type, payload))
        return len(self.events)


class FlakyLLM:
    """第一次返回坏 JSON，之后委托 FakeLLM。"""

    def __init__(self) -> None:
        self.calls = 0
        self.delegate = FakeLLM()

    async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
        self.calls += 1
        if self.calls == 1:
            return "坏 JSON"
        return await self.delegate.chat(
            messages, json_mode=json_mode, max_tokens=max_tokens
        )


def test_explainer_writes_lesson_and_terms(client) -> None:
    """正常路径：Lesson + Term 落库，lesson/term 事件按序推送。"""
    session_id, plan_id = _create_session(client)
    collector = Collector()
    db = client.app.state.session_factory()
    try:
        plan = db.get(Plan, plan_id)
        ctx = AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=FakeLLM(),
            db=db,
            publish=collector,
        )
        result = _run(run_agent(Explainer(plan), ctx))
        assert isinstance(result, ExplainResult)
        lesson = db.scalars(select(Lesson).where(Lesson.plan_id == plan_id)).one()
        assert lesson.status == "ready"
        assert lesson.title.startswith("RAG 检索增强")
        terms = list(db.scalars(select(Term).where(Term.plan_id == plan_id)))
        assert len(terms) == 2
        events = collector.events
        assert events[0][0] == "lesson"
        assert events[0][1]["day"] == 1
        assert events[0][1]["topic"] == "RAG 检索增强"
        assert [e[0] for e in events[1:]] == ["term", "term"]
        assert all(e[1]["day"] == 1 for e in events[1:])
    finally:
        db.close()


def test_retry_recovers_from_bad_json(client) -> None:
    session_id, plan_id = _create_session(client)
    db = client.app.state.session_factory()
    llm = FlakyLLM()
    try:
        ctx = AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=llm,
            db=db,
            publish=_noop_publish,
        )
        result = _run(run_agent(Explainer(db.get(Plan, plan_id)), ctx))
        assert isinstance(result, ExplainResult)
        assert llm.calls == 2
        lesson = db.scalars(select(Lesson).where(Lesson.plan_id == plan_id)).one()
        assert lesson.title  # 落库成功才有内容
    finally:
        db.close()


def test_retry_exhausted_fails_step(client) -> None:
    class BadLLM:
        async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
            return "{ 坏的 JSON"

    session_id, plan_id = _create_session(client)
    db = client.app.state.session_factory()
    try:
        ctx = AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=BadLLM(),
            db=db,
            publish=_noop_publish,
        )
        with pytest.raises(OutputInvalidError):
            _run(run_agent(Explainer(db.get(Plan, plan_id)), ctx))
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "explain.1")
        assert run.status == "failed"
        assert run.error_code == "invalid_output"
        lessons = list(db.scalars(select(Lesson).where(Lesson.plan_id == plan_id)))
        assert lessons == []  # 失败不留半截
    finally:
        db.close()