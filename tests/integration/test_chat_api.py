"""角色对话接口（/api/chat）的集成测试。

覆盖：本地模式预设台词、参数校验、真实模式缺 Key 的门控。
真实模型的网络调用不在测试里触发（会花钱且不稳定）。
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app


def _post_chat(client: TestClient, **body: object) -> object:
    payload = {"client_id": "chat-test-client", "message": "你好"}
    payload.update(body)
    return client.post("/api/chat", json=payload)


def test_demo_mode_returns_scripted_reply(client: TestClient) -> None:
    response = _post_chat(client)
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "demo"
    assert data["scripted"] is True
    assert data["persona"] == "jingxiaoyu"
    assert "主人" in data["reply"]


def test_demo_mode_protests_fat_fish_joke(client: TestClient) -> None:
    response = _post_chat(client, message="大肥鱼")
    assert response.status_code == 200
    assert "肥鱼" in response.json()["reply"]


def test_chat_rejects_blank_message(client: TestClient) -> None:
    response = _post_chat(client, message="")
    assert response.status_code == 422


def test_chat_rejects_overlong_message(client: TestClient) -> None:
    response = _post_chat(client, message="啊" * 1001)
    assert response.status_code == 422


def test_chat_requires_client_id(client: TestClient) -> None:
    response = client.post("/api/chat", json={"message": "你好"})
    assert response.status_code == 422


def test_live_mode_requires_key(settings) -> None:
    """真实模式下没有 Key：明确 400 missing_key，而不是静默失败。"""
    live_settings = replace(settings, app_mode="live")
    with TestClient(create_app(live_settings)) as live_client:
        response = _post_chat(live_client)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_key"


def test_healthz_reports_chat_feature(client: TestClient) -> None:
    features = client.get("/api/healthz").json()["features"]
    assert features["chat"] is True