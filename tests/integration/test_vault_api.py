"""M7.3 服务端加密代存：API 集成测试（HTTPS 门控 / 主密钥门控 / CRUD / 会话错误分支）。

注意：live 模式下不 POST 建真实会话（会触发真实网络 job），只测
vault CRUD、门控错误分支，以及建会话时的两个安全错误分支
（saved_key_scope_missing / no_saved_key），二者都在生成任务启动前返回。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

MASTER = "test-master-key-for-vault-api-1234"


@pytest.fixture
def live_settings(work_dir, seed_dir, kb_dir):
    return Settings(
        app_mode="live",
        db_path=work_dir / "vault.db",
        seed_dir=seed_dir,
        kb_dir=kb_dir,
        rate_limit_per_minute=10,
        max_concurrency=2,
        request_timeout_seconds=5,
        max_input_chars=1000,
        trust_proxy_headers=True,  # 用 X-Forwarded-Proto 模拟 https
    )


@pytest.fixture
def live_client(live_settings, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    app = create_app(live_settings)
    with TestClient(app) as client:  # http：配合 X-Forwarded-Proto 头模拟 https
        yield client


def test_status_disabled_in_demo(client):
    res = client.get("/api/vault")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert body["reason"] == "demo_mode"
    assert body["https"] is False
    assert body["scopes"] == []


def test_status_enabled_without_master(live_settings, monkeypatch):
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    app = create_app(live_settings)
    with TestClient(app) as client:
        res = client.get("/api/vault")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert body["reason"] == "vault_not_configured"


def test_put_requires_master_key(live_settings, monkeypatch):
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    app = create_app(live_settings)
    with TestClient(app) as live_client:
        res = live_client.put(
            "/api/vault",
            json={"client_id": "c1", "scope": "deepseek", "api_key": "sk-fake"},
        )
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "vault_not_configured"


def test_put_forbidden_in_demo(client):
    res = client.put(
        "/api/vault",
        json={"client_id": "c1", "scope": "deepseek", "api_key": "sk-fake"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "vault_demo_mode"


def test_put_requires_https(live_settings, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    app = create_app(live_settings)
    with TestClient(app) as client:  # http 且无 X-Forwarded-Proto
        res = client.put(
            "/api/vault",
            json={"client_id": "c1", "scope": "deepseek", "api_key": "sk-fake"},
        )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "vault_https_required"


def test_put_allowed_via_forwarded_proto(live_client):
    """trust_proxy_headers=True 时，X-Forwarded-Proto: https 视为 HTTPS。"""
    res = live_client.put(
        "/api/vault",
        headers={"X-Forwarded-Proto": "https"},
        json={"client_id": "c1", "scope": "deepseek", "api_key": "sk-fake-123"},
    )
    assert res.status_code == 200
    assert res.json() == {"saved": True, "scope": "deepseek"}


def test_save_list_delete_roundtrip(live_client):
    https = {"X-Forwarded-Proto": "https"}
    # 初始为空
    res = live_client.get("/api/vault", params={"client_id": "c1"}, headers=https)
    assert res.json()["enabled"] is True
    assert res.json()["https"] is True
    assert res.json()["scopes"] == []

    # 保存
    res = live_client.put(
        "/api/vault",
        headers=https,
        json={"client_id": "c1", "scope": "deepseek", "api_key": "sk-deepseek-123"},
    )
    assert res.status_code == 200
    # 明文绝不出现在响应里
    assert "sk-deepseek-123" not in res.text

    # 重复保存会覆盖，且 scope 列表不重复
    res = live_client.put(
        "/api/vault",
        headers=https,
        json={"client_id": "c1", "scope": "deepseek", "api_key": "sk-deepseek-456"},
    )
    assert res.status_code == 200
    assert "sk-deepseek-456" not in res.text

    res = live_client.put(
        "/api/vault",
        headers=https,
        json={"client_id": "c1", "scope": "qwen", "api_key": "sk-qwen-789"},
    )
    assert res.status_code == 200
    assert live_client.get("/api/vault", params={"client_id": "c1"}, headers=https).json()[
        "scopes"
    ] == ["deepseek", "qwen"]

    # 不同 client 互相隔离
    assert live_client.get("/api/vault", params={"client_id": "c2"}, headers=https).json()[
        "scopes"
    ] == []

    # 删除
    res = live_client.request(
        "DELETE",
        "/api/vault",
        headers=https,
        json={"client_id": "c1", "scope": "deepseek"},
    )
    assert res.status_code == 200
    assert res.json()["deleted"] is True
    assert live_client.get("/api/vault", params={"client_id": "c1"}, headers=https).json()[
        "scopes"
    ] == ["qwen"]

    res = live_client.request(
        "DELETE",
        "/api/vault",
        headers=https,
        json={"client_id": "c1", "scope": "deepseek"},
    )
    assert res.json()["deleted"] is False  # 已删，再次删除返回 False


def test_put_validation_errors(live_client):
    # 缺 api_key / scope → 422
    res = live_client.put("/api/vault", json={"client_id": "c1", "scope": "deepseek"})
    assert res.status_code == 422
    res = live_client.put(
        "/api/vault",
        headers={"X-Forwarded-Proto": "https"},
        json={"client_id": "c1", "api_key": "sk-x"},
    )
    assert res.status_code == 422


def test_sessions_saved_key_scope_missing(live_client):
    """勾选代存但没选供应商 / 没填 Base URL → 400 saved_key_scope_missing。"""
    res = live_client.post(
        "/api/sessions",
        headers={"X-Forwarded-Proto": "https"},
        json={
            "client_id": "c1",
            "goal": "了解 RAG",
            "level": "beginner",
            "use_saved_key": True,
        },
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "saved_key_scope_missing"


def test_sessions_saved_key_not_saved_yet(live_client):
    """勾选代存、有供应商、但还没保存过 → 400 no_saved_key（生成任务不会启动）。"""
    res = live_client.post(
        "/api/sessions",
        headers={"X-Forwarded-Proto": "https"},
        json={
            "client_id": "c1",
            "goal": "了解 RAG",
            "level": "beginner",
            "provider_id": "deepseek",
            "use_saved_key": True,
        },
    )
    assert res.status_code == 400
    body = res.json()["error"]
    assert body["code"] == "no_saved_key"
    assert "保存后才会调用你自己的模型接口来回答" in body["message"]


def test_healthz_reports_vault_feature(client, live_settings, monkeypatch):
    # demo 模式：vault=False
    assert client.get("/api/healthz").json()["features"]["vault"] is False
    # live + 主密钥：vault=True
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    app = create_app(live_settings)
    with TestClient(app) as live_client:
        assert live_client.get("/api/healthz").json()["features"]["vault"] is True