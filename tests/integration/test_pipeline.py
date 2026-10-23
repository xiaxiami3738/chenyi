"""流水线集成测试：FakeLLM 端到端、URL 剔除、策展链接白名单、失败标记。"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.llm.fake import FakeLLM
from app.models.db import session_scope
from app.models.tables import AgentRun, CuratedLink, LearnSession
from app.services.links import strip_urls
from app.services.pipeline import PipelineError, plan_days, run_generation


def _run(coro):
    return asyncio.run(coro)


def _create_session(client: TestClient, *, goal: str = "从零学会 RAG 应用开发") -> int:
    factory = client.app.state.session_factory
    for db in session_scope(factory):
        row = LearnSession(
            client_id="pipeline-test",
            goal=goal,
            level="beginner",
            weak_points="没写过 Python 项目",
            daily_minutes=30,
            status="generating",
        )
        db.add(row)
        db.flush()
        # 与 POST /api/sessions 一致：生成器进场时补一条 init 记录
        db.add(AgentRun(session_id=row.id, step="init", status="done"))
        db.commit()
        return row.id
    raise AssertionError("无法创建会话")  # pragma: no cover


class Collector:
    """记录流水线发布的全部 SSE 事件。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, payload: dict) -> int:
        self.events.append((event_type, payload))
        return len(self.events)


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(10, 7), (30, 7), (60, 7), (120, 5), (200, 3), (480, 3)],
)
def test_plan_days_formula(minutes: int, expected: int) -> None:
    assert plan_days(minutes) == expected


def test_fake_llm_end_to_end(client: TestClient) -> None:
    """FakeLLM 走完整流水线：7 天计划、逐天讲义/术语/练习、会话 ready。"""
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
        for plan in plans:
            assert plan.topic
            assert len(plan.lessons) == 1
            assert len(plan.terms) == 2
            assert len(plan.quiz_items) == 2
        steps = [run.step for run in sorted(row.runs, key=lambda r: r.id)]
        assert steps[0] == "init"
        assert steps[1] == "diagnose"
        assert steps[2] == "plan"
        assert [s for s in steps[3:] if s.startswith("explain.")] == [
            f"explain.{i}" for i in range(1, 8)
        ]
        assert [s for s in steps[3:] if s.startswith("quiz.")] == [
            f"quiz.{i}" for i in range(1, 8)
        ]
        assert steps[-1] == "finalize"
        assert all(r.status == "done" for r in row.runs)

    types = [event_type for event_type, _ in collector.events]
    assert types.count("lesson") == 7
    assert types.count("term") == 14
    assert types.count("quiz") == 14
    assert types[-1] == "done"
    assert collector.events[-1][1]["status"] == "ready"
    assert collector.events[-1][1]["plan_count"] == 7


class LeakyLLM:
    """模拟会输出 URL 的"失控"模型：验证 strip_urls 兜底。"""

    FAKE_URL = "https://evil.example.com/phishing"

    def __init__(self, topic: str) -> None:
        self.topic = topic

    async def chat(
        self, messages, *, json_mode: bool = False, max_tokens: int | None = None
    ) -> str:
        text = "\n".join(m.content for m in messages)
        task = "diagnose"
        for token in ("diagnose", "plan", "explain", "quiz"):
            if f"任务标记：{token}" in text:
                task = token
                break
        if task == "plan":
            payload = {
                "days": [
                    {
                        "day_index": 1,
                        "topic": self.topic,
                        "objective": f"目标，详见 {self.FAKE_URL}",
                        "deliverable": f"交付物 [{self.FAKE_URL}](ad){self.FAKE_URL}",
                    }
                ]
            }
        elif task == "explain":
            payload = {
                "lesson_title": f"{self.topic} 讲义（参考 {self.FAKE_URL}）",
                "lesson_body_md": f"正文内容 {self.FAKE_URL} 以及更多",
                "code_snippets": [f"print(1)  # {self.FAKE_URL}"],
                "terms": [
                    {
                        "term": "示例术语",
                        "definition": f"定义 {self.FAKE_URL}",
                        "analogy": "类比",
                        "pitfalls": "注意",
                        "topic": self.topic,
                    }
                ],
            }
        elif task == "quiz":
            payload = {
                "quizzes": [
                    {
                        "kind": "choice",
                        "question": f"问题？{self.FAKE_URL}",
                        "options": [f"选项A {self.FAKE_URL}", "选项B"],
                        "answer": "选项A",
                        "rubric": "评分要点",
                    }
                ]
            }
        else:
            payload = {"summary": f"诊断摘要，参考 {self.FAKE_URL}", "focus": ["重点"]}
        return json.dumps(payload, ensure_ascii=False)


def test_llm_output_urls_are_stripped(client: TestClient) -> None:
    """模型输出里的 URL 一律剔除，链接只能来自策展表。"""
    factory = client.app.state.session_factory
    session_id = _create_session(client, goal="RAG 检索增强")
    _run(
        run_generation(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=LeakyLLM("RAG 检索增强"),
            factory=factory,
            publish=Collector(),
        )
    )
    for db in session_scope(factory):
        row = db.get(LearnSession, session_id)
        assert row.status == "ready"
        plan = row.plans[0]
        assert LeakyLLM.FAKE_URL not in plan.objective
        assert LeakyLLM.FAKE_URL not in plan.deliverable
        lesson = plan.lessons[0]
        assert LeakyLLM.FAKE_URL not in lesson.title
        assert LeakyLLM.FAKE_URL not in lesson.body_md
        assert all(LeakyLLM.FAKE_URL not in s for s in lesson.code_snippets)
        term = plan.terms[0]
        assert LeakyLLM.FAKE_URL not in term.definition
        quiz = plan.quiz_items[0]
        assert LeakyLLM.FAKE_URL not in quiz.question
        assert all(LeakyLLM.FAKE_URL not in o for o in quiz.options)

        # 链接只能来自策展表，且主题精确匹配
        links = [db.get(CuratedLink, link_row.curated_link_id) for link_row in row.links]
        assert len(links) == 1
        assert links[0].topic == "RAG 检索增强"
        assert links[0].url == "https://github.com/langchain-ai/langchain"


def test_strip_urls_cleans_all_fields(client: TestClient) -> None:
    """strip_urls 对单个字段的兜底行为（含 markdown 链接与中文标点）。"""
    text = "见 [官方文档](https://github.com/a/b) 与 https://evil.io/x 和 https://j.io/y）"
    cleaned = strip_urls(text)
    assert "http" not in cleaned
    assert "官方文档" in cleaned


def test_bad_llm_json_fails_session(client: TestClient) -> None:
    """LLM 持续输出坏 JSON：重试 1 次后 AgentRun 记 failed + error_code，会话 failed。"""

    class BadJSONLLM:
        calls = 0

        async def chat(self, messages, *, json_mode=False, max_tokens=None) -> str:
            BadJSONLLM.calls += 1
            return "这不是 JSON"

    factory = client.app.state.session_factory
    session_id = _create_session(client)
    collector = Collector()
    with pytest.raises(PipelineError) as exc_info:
        _run(
            run_generation(
                session_id=session_id,
                settings=client.app.state.settings,
                llm=BadJSONLLM(),
                factory=factory,
                publish=collector,
            )
        )
    assert exc_info.value.error_code == "invalid_output"
    assert BadJSONLLM.calls == 2  # 首调失败后重试 1 次

    assert collector.events[-1][0] == "error"
    assert collector.events[-1][1]["code"] == "invalid_output"

    for db in session_scope(factory):
        row = db.get(LearnSession, session_id)
        assert row.status == "failed"
        failed = [r for r in row.runs if r.status == "failed"]
        assert len(failed) == 1
        assert failed[0].error_code == "invalid_output"


def test_summary_schema_over_db(client: TestClient) -> None:
    """build_summary 返回的字段集合稳定（作为 API 摘要的底座）。"""
    from app.services.pipeline import build_summary

    factory = client.app.state.session_factory
    session_id = _create_session(client)
    _run(
        run_generation(
            session_id=session_id,
            settings=client.app.state.settings,
            llm=FakeLLM(),
            factory=factory,
            publish=Collector(),
        )
    )
    for db in session_scope(factory):
        summary = build_summary(db, session_id)
        assert summary["session"]["status"] == "ready"
        assert set(summary) == {"session", "plans", "links", "runs", "stats"}
        assert set(summary["stats"]) == {
            "plan_count",
            "lesson_count",
            "term_count",
            "quiz_count",
        }
        assert summary["stats"]["plan_count"] == 7
        first_plan = summary["plans"][0]
        assert set(first_plan) == {
            "id",
            "day_index",
            "topic",
            "objective",
            "deliverable",
            "lesson",
            "terms",
            "quiz_items",
        }
        assert first_plan["lesson"]["status"] == "ready"
        assert summary["links"] == []  # FakeLLM 主题与策展表不匹配，无链接