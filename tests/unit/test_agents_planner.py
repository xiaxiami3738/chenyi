"""planner 单测：正常路径落 Plan 表 + 校验失败重试 + plan_days 公式。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext, run_agent
from app.agents.errors import OutputInvalidError
from app.agents.planner import Planner, plan_days
from app.llm.fake import FakeLLM
from app.llm.schemas import PlanResult
from app.models.db import session_scope
from app.models.tables import AgentRun, LearnSession, Plan


def _run(coro):
    return asyncio.run(coro)


async def _noop_publish(event_type: str, payload: dict) -> int:
    """publish 占位：什么都不做，返回伪事件序号。"""
    return 0


def _create_session(client) -> int:
    for db in session_scope(client.app.state.session_factory):
        row = LearnSession(
            client_id="agents-planner", goal="学会 RAG", status="generating"
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


def test_plan_days_formula() -> None:
    assert plan_days(10) == 7
    assert plan_days(30) == 7
    assert plan_days(60) == 7
    assert plan_days(120) == 5
    assert plan_days(200) == 3
    assert plan_days(480) == 3


def test_planner_writes_plans_and_progress(client) -> None:
    """正常路径：PlanResult 落成 Plan 行，并发布 progress 事件。"""
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
        result = _run(run_agent(Planner(goal="学会 RAG", daily_minutes=30), ctx))
        assert isinstance(result, PlanResult)
        assert len(result.days) == 7
        plans = list(db.scalars(select(Plan).where(Plan.session_id == session_id)))
        assert len(plans) == 7
        assert [p.day_index for p in plans] == list(range(1, 8))
        assert all(p.topic for p in plans)
        assert collector.events == [
            (
                "progress",
                {
                    "step": "plan",
                    "message": "已为你规划 7 天的学习路径（每天约 30 分钟）。",
                },
            )
        ]
    finally:
        db.close()


def test_planner_model_days_come_from_llm(client) -> None:
    """天数由 plan_days 算出并进 prompt，LLM 输出中的 day_index 原样落库。"""

    class CustomLLM:
        async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
            text = "\n".join(m.content for m in messages)
            assert "任务标记：plan" in text
            assert "天数：5" in text
            return (
                '{"days": [{"day_index": 2, "topic": "主题二", '
                '"objective": "目标", "deliverable": "交付"}]}'
            )

    session_id = _create_session(client)
    db = client.app.state.session_factory()
    try:
        ctx = AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=CustomLLM(),
            db=db,
            publish=_noop_publish,
        )
        _run(run_agent(Planner(goal="g", daily_minutes=120), ctx))
        plans = list(db.scalars(select(Plan).where(Plan.session_id == session_id)))
        assert len(plans) == 1
        assert plans[0].day_index == 2
        assert plans[0].topic == "主题二"
    finally:
        db.close()


def test_retry_recovers_from_bad_json(client) -> None:
    session_id = _create_session(client)
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
        result = _run(run_agent(Planner(goal="g", daily_minutes=30), ctx))
        assert isinstance(result, PlanResult)
        assert llm.calls == 2
        plans = list(db.scalars(select(Plan).where(Plan.session_id == session_id)))
        assert len(plans) == 7
    finally:
        db.close()


def test_retry_exhausted_fails_step(client) -> None:
    class BadLLM:
        async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
            return "{ 坏的 JSON"

    session_id = _create_session(client)
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
            _run(run_agent(Planner(goal="g", daily_minutes=30), ctx))
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "plan")
        assert run.status == "failed"
        assert run.error_code == "invalid_output"
        # 失败时不允许留下半截计划
        plans = list(db.scalars(select(Plan).where(Plan.session_id == session_id)))
        assert plans == []
    finally:
        db.close()


def test_planner_strips_urls_from_topics(client) -> None:
    """模型输出里带 URL 的主题字段在落库前被剔除。"""

    class LeakyPlanLLM:
        async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
            return (
                '{"days": [{"day_index": 1, "topic": "主题 https://evil.io/x", '
                '"objective": "看 [文档](https://evil.io/y)", "deliverable": "交付"}]}'
            )

    session_id = _create_session(client)
    db = client.app.state.session_factory()
    try:
        ctx = AgentContext(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=LeakyPlanLLM(),
            db=db,
            publish=_noop_publish,
        )
        _run(run_agent(Planner(goal="g", daily_minutes=30), ctx))
        plans = list(db.scalars(select(Plan).where(Plan.session_id == session_id)))
        assert plans[0].topic == "主题 "
        assert plans[0].objective == "看 文档"
        assert "http" not in plans[0].topic and "http" not in plans[0].objective
    finally:
        db.close()