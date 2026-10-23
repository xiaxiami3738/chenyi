"""配置解析：默认值、模式校验、环境变量覆盖。"""

from __future__ import annotations

import pytest

from app.config import BASE_DIR, Settings, load_settings


def test_defaults_are_demo_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APP_MODE",
        "DB_PATH",
        "RATE_LIMIT_PER_MINUTE",
        "MAX_CONCURRENCY",
        "MAX_INPUT_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.app_mode == "demo"
    assert settings.demo is True
    assert settings.rate_limit_per_minute == 10
    assert settings.max_concurrency == 2
    assert settings.database_url.startswith("sqlite:///")


def test_live_mode_disables_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "LIVE")
    assert load_settings().demo is False


def test_invalid_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "production")
    with pytest.raises(ValueError):
        load_settings()


def test_invalid_int_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "很多")
    with pytest.raises(ValueError):
        load_settings()


def test_db_path_is_absolute_after_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", "data/custom.db")
    settings = load_settings()
    assert settings.db_path.is_absolute()
    assert settings.db_path.parent == BASE_DIR / "data"


def test_settings_has_no_credential_field() -> None:
    assert not any("key" in field for field in Settings.__dataclass_fields__)
    assert not isinstance(getattr(Settings, "seed_dir", None), str)