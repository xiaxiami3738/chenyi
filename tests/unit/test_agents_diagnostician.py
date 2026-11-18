"""diagnostician 单测：正常路径 + LLM 输出校验失败重试 1 次 + 重试耗尽失败。"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext, run_agent
from app.agents.diagnostician import Diagnostician
from app.agents.errors import OutputInvalidError
from app.agents.validator import CONTRACT_HEADING, RETRY_HINT, ask_llm_with_retry, contract_hint
from app.llm.fake import FakeLLM
from app.llm.schemas import DiagnosisResult, PlanResult
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


class BadTwiceThenOK:
    """前两次输出坏 JSON，第三次输出合法（验证最多重试 2 次）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
        self.calls += 1
        if self.calls <= 2:
            return '{"summary": 123}'  # 类型错误：summary 必须是字符串
        return '{"summary": "ok", "focus": []}'


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
    """校验失败自动重试：第二次输出合法则成功。"""
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
    """连续三次坏输出：抛 OutputInvalidError，AgentRun 记 failed(invalid_output)。"""
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
        assert llm.calls == 3
        runs = list(db.scalars(select(AgentRun)))
        run = next(r for r in runs if r.step == "diagnose")
        assert run.status == "failed"
        assert run.error_code == "invalid_output"
    finally:
        db.close()


def test_retry_twice_with_specific_error(client) -> None:
    """最多重试 2 次；重试消息附带上次校验的具体问题（模型可据此修正）。"""
    observed: list[list] = []

    class WrappingLLM(BadTwiceThenOK):
        async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
            observed.append(messages)
            return await super().chat(messages, json_mode=json_mode, max_tokens=max_tokens)

    llm = WrappingLLM()
    result = _run(ask_llm_with_retry(llm, [], DiagnosisResult))
    assert isinstance(result, DiagnosisResult)
    assert result.summary == "ok"
    assert llm.calls == 3
    assert len(observed) == 3
    # 第二次、第三次尝试都带「具体问题」（来自上次校验错误）
    for messages in observed[1:]:
        joined = "\n".join(m.content for m in messages)
        assert "具体问题：模型输出不符合约定结构" in joined
    # 尝试次数递增：1 次原始 + 2 次修正
    assert len(observed[1]) == len(observed[0]) + 2
    assert len(observed[2]) == len(observed[0]) + 2


def test_ask_llm_with_retry_passes_hint_on_retry() -> None:
    """每次调用都附带契约结构提示；重试额外追加修正提示（都不含任务标记）。"""

    observed: list[list] = []

    class RecordingLLM:
        async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
            observed.append(messages)
            if len(observed) == 1:
                return "坏 JSON"
            return '{"summary": "ok", "focus": ["重试成功"]}'

    _run(ask_llm_with_retry(RecordingLLM(), [], DiagnosisResult))
    assert len(observed) == 2
    # 首次 = 原消息 + 契约提示；重试 = 首次 + 修正提示 + 契约提示
    assert len(observed[1]) == len(observed[0]) + 2
    assert "任务标记" not in observed[1][-1].content
    # 契约提示在首末两次都出现，且包含必填字段名
    for messages in observed:
        joined = "\n".join(m.content for m in messages)
        assert CONTRACT_HEADING in joined
        assert '"summary"' in joined
        assert '"focus"' in joined
    # 重试修正提示也在
    assert RETRY_HINT in observed[1][-2].content


def test_contract_hint_shape() -> None:
    """契约提示：字段名/类型/必填齐全 + 样例 JSON，且不含干扰 FakeLLM 的标签短语。"""
    hint = contract_hint(DiagnosisResult)
    assert hint.startswith(CONTRACT_HEADING)
    assert '"summary"（string，必填）' in hint
    assert '"focus"（array[string]，可选）' in hint
    # 样例：JSON 可解析、key 为英文、无中文冒号标签
    assert "参考格式（照此结构输出即可）：" in hint
    sample = hint.split("参考格式（照此结构输出即可）：", 1)[1].strip()
    parsed = json.loads(sample)
    assert parsed["summary"] == "示例文本"
    assert parsed["focus"] == ["示例文本"]
    for tag in ("任务标记", "目标：", "主题：", "天数："):
        assert tag not in hint


def test_contract_hint_plan_result_sample() -> None:
    """嵌套结构（PlanResult）的样例生成：days 数组含完整 PlanDay 字段。"""
    hint = contract_hint(PlanResult)
    sample = json.loads(hint.split("参考格式（照此结构输出即可）：", 1)[1].strip())
    day = sample["days"][0]
    assert day == {
        "day_index": 1,
        "topic": "示例文本",
        "objective": "示例文本",
        "deliverable": "示例文本",
    }

async def asyncio_ok(value: int = 0) -> int:
    """同步 publish 占位：返回可 await 的协程。"""
    return value
