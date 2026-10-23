"""M7.3 服务端加密代存：服务层单元测试（不依赖网络/模型）。"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Base
from app.models.db import make_engine, make_session_factory
from app.services.vault import (
    VaultError,
    decrypt_secret,
    delete_secret,
    encrypt_secret,
    get_secret,
    list_scopes,
    resolve_key,
    save_secret,
    vault_available,
)

MASTER = "test-master-key-123"


@pytest.fixture
def engine(work_dir):
    engine = make_engine(f"sqlite:///{work_dir / 'vault-unit.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    factory = make_session_factory(engine)
    with factory() as db_session:
        yield db_session


@pytest.fixture
def live_settings(work_dir):
    return Settings(
        app_mode="live",
        db_path=work_dir / "vault-live.db",
        seed_dir=work_dir / "seed",
        rate_limit_per_minute=10,
        max_concurrency=2,
        request_timeout_seconds=5,
        max_input_chars=1000,
    )


@pytest.fixture
def demo_settings(work_dir):
    return Settings(
        app_mode="demo",
        db_path=work_dir / "vault-demo.db",
        seed_dir=work_dir / "seed",
        rate_limit_per_minute=10,
        max_concurrency=2,
        request_timeout_seconds=5,
        max_input_chars=1000,
    )


def test_available_requires_live_and_master(live_settings, demo_settings, monkeypatch):
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    # 未配置主密钥 → 不可用（无论模式）
    assert vault_available(live_settings) is False
    assert vault_available(demo_settings) is False
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    # 真实模式 + 主密钥 → 可用；Demo 模式仍不可用
    assert vault_available(live_settings) is True
    assert vault_available(demo_settings) is False


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    plain = "sk-my-secret-key-abc123"
    token = encrypt_secret(plain)
    assert token != plain  # 密文绝不等同明文
    assert plain not in token
    assert decrypt_secret(token) == plain


def test_encrypt_without_master_raises(monkeypatch):
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    with pytest.raises(VaultError) as exc:
        encrypt_secret("sk-x")
    assert exc.value.status_code == 503
    assert exc.value.error_code == "vault_not_configured"


def test_decrypt_tampered_token(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    token = encrypt_secret("sk-original")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(VaultError) as exc:
        decrypt_secret(tampered)
    assert exc.value.status_code == 400
    assert exc.value.error_code == "vault_decrypt_failed"


def test_decrypt_wrong_master_key(monkeypatch):
    plain = "sk-rotate-me"
    monkeypatch.setenv("VAULT_MASTER_KEY", "old-master")
    token = encrypt_secret(plain)
    monkeypatch.setenv("VAULT_MASTER_KEY", "new-master")
    with pytest.raises(VaultError) as exc:
        decrypt_secret(token)
    assert exc.value.error_code == "vault_decrypt_failed"


def test_save_and_get_secret(session: Session, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    save_secret(session, "client-a", "deepseek", "sk-deepseek-1")
    session.commit()
    assert get_secret(session, "client-a", "deepseek") == "sk-deepseek-1"
    # 不同 scope / client 互不可见
    assert get_secret(session, "client-a", "qwen") is None
    assert get_secret(session, "client-b", "deepseek") is None


def test_save_upserts_by_client_and_scope(session: Session, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    save_secret(session, "client-a", "deepseek", "sk-v1")
    save_secret(session, "client-a", "deepseek", "sk-v2")
    save_secret(session, "client-a", "qwen", "sk-q")
    session.commit()
    assert list_scopes(session, "client-a") == ["deepseek", "qwen"]  # 按 scope 排序
    assert get_secret(session, "client-a", "deepseek") == "sk-v2"  # 后者覆盖


def test_delete_secret(session: Session, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    save_secret(session, "client-a", "deepseek", "sk-x")
    session.commit()
    assert delete_secret(session, "client-a", "deepseek") is True
    session.commit()
    assert list_scopes(session, "client-a") == []
    assert delete_secret(session, "client-a", "deepseek") is False  # 不存在


def test_resolve_header_takes_priority(session: Session, live_settings, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    save_secret(session, "client-a", "deepseek", "sk-saved")
    session.commit()
    key, error = resolve_key(
        live_settings, session, client_id="client-a", scope="deepseek", header_key="sk-header"
    )
    assert key == "sk-header"
    assert error is None


def test_resolve_no_scope_returns_none(session: Session, live_settings, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    key, error = resolve_key(live_settings, session, client_id="client-a", scope=None, header_key=None)
    assert key is None
    assert error is None


def test_resolve_demo_ignores_saved(session: Session, demo_settings, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    save_secret(session, "client-a", "deepseek", "sk-saved")
    session.commit()
    # Demo 模式：即便有代存也返回空，走 FakeLLM
    key, error = resolve_key(
        demo_settings, session, client_id="client-a", scope="deepseek", header_key=None
    )
    assert key is None
    assert error is None


def test_resolve_saved_key_used(session: Session, live_settings, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    save_secret(session, "client-a", "deepseek", "sk-saved")
    session.commit()
    key, error = resolve_key(
        live_settings, session, client_id="client-a", scope="deepseek", header_key=None
    )
    assert key == "sk-saved"
    assert error is None


def test_resolve_missing_saved_key_error(session: Session, live_settings, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", MASTER)
    key, error = resolve_key(
        live_settings, session, client_id="client-a", scope="deepseek", header_key=None
    )
    assert key is None
    assert error is not None
    assert error["status_code"] == 400
    assert error["code"] == "no_saved_key"
    assert "保存后才会调用你自己的模型接口来回答" in error["message"]


def test_resolve_corrupt_saved_key_error(session: Session, live_settings, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", "old-master")
    save_secret(session, "client-a", "deepseek", "sk-saved")
    session.commit()
    monkeypatch.setenv("VAULT_MASTER_KEY", "new-master")
    key, error = resolve_key(
        live_settings, session, client_id="client-a", scope="deepseek", header_key=None
    )
    assert key is None
    assert error is not None
    assert error["code"] == "vault_decrypt_failed"
    assert error["status_code"] == 400