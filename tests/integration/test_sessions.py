"""会话 API 集成测试：创建、摘要、SSE 事件流、限流、Key 守卫。"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.client import ChatClient, LLMConfig
from app.main import create_app
from app.models.db import session_scope
from app.models.tables import AgentRun, LearnSession
from app.services.pipeline import PipelineError

SECRET = "sk-abcdef1234567890abcdef1234567890"
GOAL = "从零学会开发一个 RAG 问答机器人"


def _post_session(client: TestClient, *, headers=None, **body) -> httpx.Response:
    payload = {"client_id": "api-test", "goal": GOAL, **body}
    return client.post("/api/sessions", json=payload, headers=headers or {})


def _wait_ready(client: TestClient, session_id: int, tries: int = 50) -> dict:
    for _ in range(tries):
        resp = client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["session"]["status"] == "ready":
            return data
    raise AssertionError("会话未在预期时间内完成生成")  # pragma: no cover


def _sse_events(response: httpx.Response) -> list[tuple[str, dict]]:
    """把 text/event-stream 响应解析成 (event, data) 列表。"""
    events: list[tuple[str, dict]] = []
    event_type: str | None = None
    data_parts: list[str] = []
    for raw_line in response.iter_lines():
        line = (raw_line or "").strip()
        if not line:
            if event_type is not None:
                events.append((event_type, json.loads("\n".join(data_parts))))
                event_type = None
                data_parts = []
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].strip())
        # "id:" 与注释行忽略
    return events


def test_create_session_returns_generating(client: TestClient) -> None:
    resp = _post_session(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "generating"
    assert data["mode"] == "demo"
    assert isinstance(data["session_id"], int)


def test_get_session_summary_schema(client: TestClient) -> None:
    created = _post_session(client).json()
    summary = _wait_ready(client, created["session_id"])
    assert set(summary) == {"session", "mode", "plans", "links", "runs", "stats"}
    assert summary["mode"] == "demo"
    assert summary["session"]["status"] == "ready"
    assert summary["stats"]["plan_count"] == 7
    assert len(summary["plans"]) == 7
    assert summary["runs"][0]["step"] == "init"


def test_sse_streams_live_events(monkeypatch, client: TestClient) -> None:
    """生成过程中连接 SSE：应收到多帧 progress/lesson/term/quiz/done。"""
    monkeypatch.setattr("app.llm.fake.FakeLLM.delay_seconds", 0.1)
    created = _post_session(client).json()
    session_id = created["session_id"]
    with client.stream("GET", f"/api/sessions/{session_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(response)
    types = [event_type for event_type, _ in events]
    assert "done" in types
    assert any(t in ("progress", "lesson", "term", "quiz") for t in types)
    done = next(payload for t, payload in events if t == "done")
    assert done["status"] == "ready"
    assert done["plan_count"] == 7


def test_sse_replays_single_done_after_generation(client: TestClient) -> None:
    created = _post_session(client).json()
    _wait_ready(client, created["session_id"])
    with client.stream(
        "GET", f"/api/sessions/{created['session_id']}/events"
    ) as response:
        events = _sse_events(response)
    assert len(events) == 1
    assert events[0][0] == "done"
    assert events[0][1]["status"] == "ready"
    assert "plan_count" in events[0][1]
    assert isinstance(events[0][1]["links"], list)


def test_sse_replays_error_for_failed_session(client: TestClient) -> None:
    for db in session_scope(client.app.state.session_factory):
        row = LearnSession(client_id="api-test", goal=GOAL, status="failed")
        db.add(row)
        db.flush()
        db.add(
            AgentRun(
                session_id=row.id,
                step="plan",
                status="failed",
                error_code="invalid_output",
            )
        )
        db.commit()
        session_id = row.id
    with client.stream("GET", f"/api/sessions/{session_id}/events") as response:
        events = _sse_events(response)
    assert len(events) == 1
    assert events[0][0] == "error"
    assert events[0][1]["code"] == "invalid_output"


def test_sessions_404(client: TestClient) -> None:
    resp = client.get("/api/sessions/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"

    with client.stream("GET", "/api/sessions/999999/events") as response:
        assert response.status_code == 404
        response.read()
        assert response.json()["error"]["code"] == "not_found"


def test_goal_too_long_returns_400(client: TestClient) -> None:
    resp = _post_session(client, goal="长" * 1001)
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "goal_too_long"


def test_validation_error_uses_envelope(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"goal": GOAL})  # 缺 client_id
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "invalid_request"


def test_concurrency_limit_returns_429(monkeypatch, work_dir, seed_dir) -> None:
    """应用层并发信号量：超限返回 429 busy。"""
    monkeypatch.setattr("app.llm.fake.FakeLLM.delay_seconds", 0.1)
    settings = Settings(
        app_mode="demo",
        db_path=work_dir / "busy.db",
        seed_dir=seed_dir,
        rate_limit_per_minute=10,
        max_concurrency=1,
        request_timeout_seconds=5,
        max_input_chars=1000,
    )
    with TestClient(create_app(settings)) as busy_client:
        first = _post_session(busy_client)
        assert first.status_code == 200
        second = _post_session(busy_client)
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "busy"


def test_live_mode_key_never_leaks(
    monkeypatch, caplog, work_dir, seed_dir
) -> None:
    """live 模式：Key 只随请求发送，不落库、不入日志、不进 API 响应。"""
    seen: dict = {}

    def responder(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        body = json.loads(request.content)
        text = "\n".join(m.get("content", "") for m in body["messages"])
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
                        "topic": "RAG 检索增强",
                        "objective": "掌握 RAG 全流程",
                        "deliverable": "完成一个检索问答 Demo",
                    }
                ]
            }
        elif task == "explain":
            payload = {
                "lesson_title": "RAG 检索增强：从概念到实践",
                "lesson_body_md": "本节课讲解 RAG 的索引与检索。",
                "code_snippets": ["print('retrieval')"],
                "terms": [
                    {
                        "term": "向量检索",
                        "definition": "用向量相似度召回相关内容。",
                        "analogy": "像查字典。",
                        "pitfalls": "注意阈值。",
                        "topic": "RAG 检索增强",
                    }
                ],
            }
        elif task == "quiz":
            payload = {
                "quizzes": [
                    {
                        "kind": "choice",
                        "question": "RAG 的中文全称是？",
                        "options": ["检索增强生成", "强化学习"],
                        "answer": "检索增强生成",
                        "rubric": "答对即可。",
                    }
                ]
            }
        else:
            payload = {"summary": "你适合从 RAG 入手。", "focus": ["向量检索"]}
        content = json.dumps(payload, ensure_ascii=False)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )

    def fake_build_llm(settings_, *, provider_id=None, base_url=None, model=None, api_key=None):
        if not api_key:
            raise PipelineError(
                "真实模式需要携带 X-LLM-Key，请填写 API Key 后重试",
                status_code=400,
                error_code="missing_key",
            )
        config = LLMConfig(
            base_url=base_url or "https://api.openai.com/v1",
            model=model or "gpt-4o-mini",
            api_key=api_key,
        )
        return ChatClient(config, transport=httpx.MockTransport(responder))

    import app.api.sessions as sessions_api

    monkeypatch.setattr(sessions_api, "build_llm", fake_build_llm)
    settings = Settings(
        app_mode="live",
        db_path=work_dir / "live.db",
        seed_dir=seed_dir,
        rate_limit_per_minute=10,
        max_concurrency=2,
        request_timeout_seconds=5,
        max_input_chars=1000,
    )
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(settings)) as live_client:
        resp = _post_session(live_client, headers={"x-llm-key": SECRET})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "live"
        session_id = resp.json()["session_id"]
        summary = _wait_ready(live_client, session_id)

        assert seen["authorization"] == f"Bearer {SECRET}"
        assert SECRET not in (work_dir / "live.db").read_bytes().decode("utf-8", "ignore")
        assert SECRET not in json.dumps(summary, ensure_ascii=False)
        assert SECRET not in caplog.text

        # 不带 Key 的 live 请求应当直接拒绝
        denied = _post_session(live_client, headers={})
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "missing_key"


# ── M3：匿名历史列表 + Markdown 导出 ─────────────────────


def _post_session_for(client: TestClient, client_id: str) -> int:
    resp = _post_session(client, client_id=client_id)
    assert resp.status_code == 200
    return resp.json()["session_id"]


def test_list_sessions_requires_client_id(client: TestClient) -> None:
    resp = client.get("/api/sessions")
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "missing_client_id"


def test_list_sessions_returns_own_sessions(client: TestClient) -> None:
    """按 client_id 返回该匿名用户的历史会话（新→旧）。"""
    first_id = _post_session_for(client, "api-test")
    second_id = _post_session_for(client, "api-test")
    _wait_ready(client, first_id)
    _wait_ready(client, second_id)

    resp = client.get("/api/sessions?client_id=api-test&limit=50")
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert len(sessions) >= 2
    # 新→旧排序：后创建的 second_id 排前面
    assert [s["id"] for s in sessions[:2]] == [second_id, first_id]
    item = sessions[0]
    assert set(item) == {
        "id", "goal", "level", "status", "daily_minutes", "created_at", "stats"
    }
    assert set(item["stats"]) == {
        "plan_count", "lesson_count", "term_count", "quiz_count"
    }
    assert item["status"] == "ready"
    assert item["goal"] == GOAL


def test_list_sessions_is_isolated_per_client(client: TestClient) -> None:
    """不同匿名 client_id 之间的会话互相隔离。"""
    _post_session_for(client, "api-test")
    other_id = _post_session_for(client, "other-user")
    _wait_ready(client, other_id)

    resp = client.get("/api/sessions?client_id=api-test&limit=50")
    sessions = resp.json()["sessions"]
    assert sessions
    assert all(s["id"] != other_id for s in sessions)


def test_list_sessions_rejects_bad_limit(client: TestClient) -> None:
    resp = client.get("/api/sessions?client_id=api-test&limit=500")
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "invalid_request"


def test_export_markdown_returns_plan_file(client: TestClient) -> None:
    """demo 全流程生成后导出 Markdown：内容完整且带策展链接。"""
    created = _post_session_for(client, "api-test")
    _wait_ready(client, created)

    resp = client.get(f"/api/sessions/{created}/export.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert f'plan-{created}.md' in resp.headers["content-disposition"]
    body = resp.text
    assert "学习路径" in body
    assert "第 1 天" in body
    assert "延伸阅读" in body
    assert "https://github.com" in body


def test_export_markdown_404_for_missing_session(client: TestClient) -> None:
    resp = client.get("/api/sessions/999999/export.md")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"