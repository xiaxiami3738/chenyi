"""FakeLLM 单元测试：确定性、结构契约、流式分块。"""

from __future__ import annotations

import asyncio

from app.llm.fake import FakeLLM
from app.llm.schemas import ChatMessage, DiagnosisResult, ExplainResult, PlanResult, QuizResult


def _run(coro):
    return asyncio.run(coro)


def _messages(*contents: str) -> list[ChatMessage]:
    return [ChatMessage.user(content) for content in contents]


def test_plan_output_is_valid_plan_result() -> None:
    llm = FakeLLM()
    text = _run(llm.chat(_messages("任务标记：plan\n天数：5\n目标：学会 RAG\n薄弱点：无")))
    plan = PlanResult.model_validate_json(text)
    assert len(plan.days) == 5
    assert [day.day_index for day in plan.days] == [1, 2, 3, 4, 5]
    assert all(day.topic for day in plan.days)


def test_plan_days_clamped_3_to_7() -> None:
    llm = FakeLLM()
    small = PlanResult.model_validate_json(_run(llm.chat(_messages("任务标记：plan\n天数：1"))))
    big = PlanResult.model_validate_json(_run(llm.chat(_messages("任务标记：plan\n天数：99"))))
    assert len(small.days) == 3
    assert len(big.days) == 7


def test_diagnose_is_default_and_valid() -> None:
    llm = FakeLLM()
    out = DiagnosisResult.model_validate_json(
        _run(llm.chat(_messages("任务标记：diagnose\n目标：入门 AI 开发")))
    )
    assert out.summary
    assert out.render_message().startswith("诊断完成：")
    # 没有任务标记时兜底成诊断
    fallback = DiagnosisResult.model_validate_json(_run(llm.chat(_messages("今天学什么"))))
    assert fallback.summary


def test_explain_output_valid() -> None:
    llm = FakeLLM()
    out = ExplainResult.model_validate_json(
        _run(llm.chat(_messages("任务标记：explain\n主题：RAG 检索增强\n天次：1")))
    )
    assert out.lesson_title.startswith("RAG")
    assert out.lesson_body_md
    assert len(out.terms) >= 1


def test_quiz_output_valid_and_deterministic() -> None:
    llm = FakeLLM()
    msgs = _messages("任务标记：quiz\n主题：提示工程\n天次：2")
    first = QuizResult.model_validate_json(_run(llm.chat(msgs)))
    again = QuizResult.model_validate_json(_run(llm.chat(msgs)))
    assert first.model_dump() == again.model_dump()
    assert first.quizzes
    assert first.quizzes[0].kind == "choice"


def test_chat_is_deterministic() -> None:
    llm = FakeLLM()
    msgs = _messages("任务标记：plan\n天数：7\n目标：LLM 应用")
    assert _run(llm.chat(msgs)) == _run(llm.chat(msgs))


def test_stream_matches_chat() -> None:
    llm = FakeLLM()
    msgs = _messages("任务标记：plan\n天数：7\n目标：LLM 应用")

    async def collect() -> str:
        return "".join([chunk async for chunk in llm.stream(msgs)])

    assert _run(collect()) == _run(llm.chat(msgs))


def test_delay_seconds_is_configurable(monkeypatch) -> None:
    monkeypatch.setattr(FakeLLM, "delay_seconds", 0.01)
    out = DiagnosisResult.model_validate_json(_run(FakeLLM().chat(_messages("目标：x"))))
    assert out.summary