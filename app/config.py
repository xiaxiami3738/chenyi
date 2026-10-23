"""应用配置。

铁律：本项目**任何**配置里都不允许出现 LLM Key。
访客的 Key 只存在于其浏览器 localStorage，并以请求头形式在单次请求内透传。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEMO = "demo"
LIVE = "live"


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - 防御性分支
        raise ValueError(f"环境变量 {name} 必须是整数，当前值：{raw!r}") from exc


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """运行期设置。所有字段均可由环境变量覆盖。"""

    app_mode: str = DEMO
    db_path: Path = BASE_DIR / "data" / "app.db"
    rate_limit_per_minute: int = 10
    max_concurrency: int = 2
    request_timeout_seconds: int = 120
    max_input_chars: int = 8000
    seed_dir: Path = BASE_DIR / "data" / "seed"
    kb_dir: Path = BASE_DIR / "data" / "kb"
    trust_proxy_headers: bool = False

    @property
    def demo(self) -> bool:
        """Demo 模式下不需要任何 Key，用 FakeLLM 预置内容跑通闭环。"""
        return self.app_mode != LIVE

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


def load_settings() -> Settings:
    mode = (os.getenv("APP_MODE") or DEMO).strip().lower()
    if mode not in {DEMO, LIVE}:
        raise ValueError(f"APP_MODE 只能是 {DEMO} 或 {LIVE}，当前值：{mode!r}")

    db_path = Path(os.getenv("DB_PATH") or (BASE_DIR / "data" / "app.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return Settings(
        app_mode=mode,
        db_path=db_path,
        rate_limit_per_minute=_int_env("RATE_LIMIT_PER_MINUTE", 10),
        max_concurrency=_int_env("MAX_CONCURRENCY", 2),
        request_timeout_seconds=_int_env("REQUEST_TIMEOUT_SECONDS", 120),
        max_input_chars=_int_env("MAX_INPUT_CHARS", 8000),
        trust_proxy_headers=_bool_env("TRUST_PROXY_HEADERS", False),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()