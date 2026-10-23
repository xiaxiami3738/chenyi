"""M4 应用层限流 E2E：POST 按 IP 滑动窗口限流，超限 429，GET 不限流。

依赖：RateLimitMiddleware 由 Lead 在 app/main.py 的 create_app 中挂载
（本文件不改 main.py）。若尚未挂载，用例会 pytest.skip 并保留在仓库，
Lead 挂载后自动恢复为真实验证（前提：挂载参数取自 Settings，见下）。
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ratelimit import RateLimitMiddleware
from app.config import Settings
from app.main import create_app

#: 未挂载中间件时统一跳过的原因；挂载后此类目恢复为真实断言
SKIP_REASON = (
    "RateLimitMiddleware not mounted yet"
    "（Lead 需在 app/main.py 的 create_app 里用 add_middleware 挂载，"
    "limit/trust_proxy 取 settings 对应字段）"
)

GOAL = "从零学会开发一个 RAG 问答机器人"


def _post_session(client: TestClient, *, headers: dict[str, str] | None = None):
    """发送一次创建会话的写请求（与前端最小 body 一致）。"""
    payload = {
        "client_id": "rate-test",
        "goal": GOAL,
        "level": "beginner",
        "daily_minutes": 30,
    }
    return client.post("/api/sessions", json=payload, headers=headers or {})


def _app_with_middleware(settings: Settings, **overrides) -> FastAPI:
    """按替换后的 Settings 构建独立应用；中间件未挂载则跳过测试。

    max_concurrency 抬高：把「并发 busy 429」与「限流 429」两类响应
    隔离开，保证断言只针对滑动窗口限流。
    """
    new_settings = dataclasses.replace(
        settings, rate_limit_per_minute=3, max_concurrency=8, **overrides
    )
    app = create_app(new_settings)
    mounted = any(
        getattr(item, "cls", None) is RateLimitMiddleware
        for item in getattr(app, "user_middleware", ())
    )
    if not mounted:
        pytest.skip(SKIP_REASON)
    return app


def test_write_requests_rate_limited_after_limit(settings: Settings) -> None:
    """用例 1：同一 IP 连续写请求，前 3 次放行、第 4 次 429，GET 不受影响。"""
    app = _app_with_middleware(settings)
    with TestClient(app) as client:
        # (a) 前 3 次（配额内）全部正常创建，不是 429
        for _ in range(3):
            assert _post_session(client).status_code == 200

        # (b) 第 4 次（同一 IP "testclient"）被限流，错误信封 code=rate_limited
        fourth = _post_session(client)
        assert fourth.status_code == 429
        body = fourth.json()
        assert body["error"]["code"] == "rate_limited"
        assert body["error"]["message"]

        # (c) GET 不计入写限流：健康检查仍然 200
        assert client.get("/api/healthz").status_code == 200


def test_trust_proxy_limits_by_forwarded_for(settings: Settings) -> None:
    """用例 2：trust_proxy_headers=True 时按 X-Forwarded-For 首地址独立计数。"""
    app = _app_with_middleware(settings, trust_proxy_headers=True)
    with TestClient(app) as client:
        first_proxy = {"X-Forwarded-For": "1.2.3.4"}
        # 1.2.3.4 连续 3 次放行，第 4 次 429
        for _ in range(3):
            assert _post_session(client, headers=first_proxy).status_code == 200
        blocked = _post_session(client, headers=first_proxy)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"

        # 换一个 X-Forwarded-For 地址：不同 IP 独立计数，应放行
        other_proxy = {"X-Forwarded-For": "5.6.7.8"}
        assert _post_session(client, headers=other_proxy).status_code == 200