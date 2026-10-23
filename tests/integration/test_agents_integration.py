"""agents 集成测试：重构后的 _run_steps 六角色编排（与 test_pipeline / test_sessions 对齐）。

重点验证 M2 新增能力：
- 各角色积木仍产出与 M1 完全相同的事件流 / 落库结果；
- 校验失败自动重试 1 次（失败一次后恢复，会话最终 ready）；
- 连续两次失败 → 该步 AgentRun failed(invalid_output)、会话 failed、error 事件。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.errors import PipelineError
from app.llm.fake import FakeLLM
from app.models.db import session_scope
from app.models.tables import AgentRun, LearnSession
from app.services.pipeline import run_generation


def _run(coro):
    return asyncio.run(coro)


def _create_session(client) -> int:
    for db in session_scope(client.app.state.session_factory):
        row = LearnSession(
            client_id="agents-integration",
            goal="从零学会 RAG 应用开发",
            level="beginner",
            weak_points="没写过 Python 项目",
            daily_minutes=30,
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


class FlakyOnceLLM:
    """整个流水线第一次调用返回坏 JSON、之后恢复正常 —— 验证重试恢复。"""

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


class AlwaysBadLLM:
    async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
        return "这不是 JSON"


def test_agents_pipeline_full_event_flow(client) -> None:
    """六角色积木编排：事件流与落库结果和 M1 完全一致。"""
    factory = client.app.state.session_factory
    session_id = _create_session(client)
    collector = Collector()
    _run(
        run_generation(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=FakeLLM(),
            factory=factory,
            publish=collector,
        )
    )
    for db in session_scope(factory):
        row = db.get(LearnSession, session_id)
        assert row.status == "ready"
        plans = sorted(row.plans, key=lambda p: p.day_index)
        assert [p.day_index for p in plans] == list(range(1, 8))
        steps = [run.step for run in sorted(row.runs, key=lambda r: r.id)]
        assert steps[0:3] == ["init", "diagnose", "plan"]
        assert steps[-1] == "finalize"
        assert all(run.status == "done" for run in row.runs)

    types = [t for t, _ in collector.events]
    assert types[0] == "progress"
    assert types.count("lesson") == 7
    assert types.count("term") == 14
    assert types.count("quiz") == 14
    assert collector.events[-1][0] == "done"
    assert collector.events[-1][1]["status"] == "ready"
    assert collector.events[-1][1]["plan_count"] == 7


def test_retry_recovers_and_session_ready(client) -> None:
    """诊断第一步输出坏 JSON → 自动重试成功，整个会话照常 ready。"""
    factory = client.app.state.session_factory
    session_id = _create_session(client)
    llm = FlakyOnceLLM()
    collector = Collector()
    _run(
        run_generation(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=llm,
            factory=factory,
            publish=collector,
        )
    )
    assert llm.calls == 1 + 1 + 1 + 7 + 7  # 首败 + 重试 + plan + 7 explain + 7 quiz
    for db in session_scope(factory):
        row = db.get(LearnSession, session_id)
        assert row.status == "ready"
        assert all(run.status == "done" for run in row.runs)
    assert collector.events[-1][0] == "done"


def test_retry_gives_up_and_session_failed(client) -> None:
    """连续两次坏输出：AgentRun 记 failed(invalid_output)、会话 failed、error 事件。"""
    factory = client.app.state.session_factory
    session_id = _create_session(client)
    collector = Collector()
    with pytest.raises(PipelineError) as exc_info:
        _run(
            run_generation(
                session_id=session_id,
                settings=client.app.state.settings,
                llm=AlwaysBadLLM(),
                factory=factory,
                publish=collector,
            )
        )
    assert exc_info.value.error_code == "invalid_output"
    assert collector.events[-1] == (
        "error",
        {"code": "invalid_output", "message": exc_info.value.message},
    )
    for db in session_scope(factory):
        row = db.get(LearnSession, session_id)
        assert row.status == "failed"
        failed = [r for r in row.runs if r.status == "failed"]
        assert len(failed) == 1
        assert failed[0].step == "diagnose"
        assert failed[0].error_code == "invalid_output"
        # 失败后不再继续生成（plan 及之后无新 plan 行）
        assert len(row.plans) == 0