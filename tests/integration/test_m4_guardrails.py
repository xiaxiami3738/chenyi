"""M4 应用层速率限制 + 脱敏/防幻觉 E2E 测试。

- 限流：RateLimitMiddleware 对 POST/PUT/DELETE 按 IP 滑动窗口（SlidingWindowRateLimiter），
  超限 HTTP 429 + 错误信封 rate_limited；GET 不限流；TRUST_PROXY_HEADERS=true 时取
  X-Forwarded-For 首个地址。
- 防幻觉：模型输出里的 URL 一律被 strip_urls 剔除，落到摘要里的链接只能来自人工策展
  白名单（seed 里唯一合规一条是 langchain-ai/langchain）；任何字段不得含 "http"。
- 脱敏：请求带 X-LLM-Key 时，错误信封的 message/detail 绝不回显 Key（mask_secrets）。
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client_for(**overrides: object) -> TestClient:
    """按覆盖项构造独立应用实例（不受 conftest client 共享）。
    注意：RateLimitMiddleware 的滑动窗口挂在 app 实例上，每个 TestClient
    一个独立实例才能测限流边界。Seed 用仓库默认策展数据即可（demo 生成
    不依赖 seed 内容多寡）。
    """
    settings = Settings(
        app_mode="demo",
        rate_limit_per_minute=10,
        max_concurrency=2,
        request_timeout_seconds=5,
        max_input_chars=1000,
    )
    return TestClient(create_app(dataclasses.replace(settings, **overrides)))


def _create_payload(**overrides: str) -> dict:
    payload = {
        "client_id": "m4-client",
        "goal": "学会 RAG 并用 LangChain 做一个问答机器人",
        "level": "beginner",
        "daily_minutes": 30,
    }
    payload.update(overrides)
    return payload


class TestRateLimit:
    def test_post_rate_limited_after_limit(self) -> None:
        with _client_for(rate_limit_per_minute=3) as client:
            for _ in range(3):
                resp = client.post("/api/sessions", json=_create_payload())
                assert resp.status_code == 200, resp.text
            resp = client.post("/api/sessions", json=_create_payload())
            assert resp.status_code == 429, resp.text
            body = resp.json()
            assert body["error"]["code"] == "rate_limited"
            assert "稍后再试" in body["error"]["message"]

    def test_get_not_rate_limited(self) -> None:
        with _client_for(rate_limit_per_minute=1) as client:
            # 只限写请求：GET 不受限流影响
            assert client.get("/api/healthz").status_code == 200
            assert client.get("/api/providers").status_code == 200
            # 但此时写请求应已超限（1 次/分钟限额，上面 GET 不计数）
            resp = client.post("/api/sessions", json=_create_payload())
            assert resp.status_code == 200, resp.text
            resp2 = client.post("/api/sessions", json=_create_payload())
            assert resp2.status_code == 429, resp2.text

    def test_trust_proxy_uses_forwarded_for(self) -> None:
        with _client_for(rate_limit_per_minute=2, trust_proxy_headers=True) as client:
            headers = {"X-Forwarded-For": "203.0.113.9"}
            for _ in range(2):
                assert (
                    client.post(
                        "/api/sessions", json=_create_payload(), headers=headers
                    ).status_code
                    == 200
                )
            # 第三个来自同一伪造 IP：429
            resp = client.post("/api/sessions", json=_create_payload(), headers=headers)
            assert resp.status_code == 429, resp.text
            # 不同 XFF IP 独立计数：放行
            resp2 = client.post(
                "/api/sessions",
                json=_create_payload(),
                headers={"X-Forwarded-For": "203.0.113.10"},
            )
            assert resp2.status_code == 200, resp2.text

    def test_proxy_off_ignores_forwarded_for(self) -> None:
        # trust_proxy_headers=False：限流键是 socket 对端（testclient），
        # X-Forwarded-For 不会绕过限流
        with _client_for(rate_limit_per_minute=2, trust_proxy_headers=False) as client:
            for _ in range(2):
                assert (
                    client.post(
                        "/api/sessions",
                        json=_create_payload(),
                        headers={"X-Forwarded-For": "203.0.113.9"},
                    ).status_code
                    == 200
                )
            resp = client.post(
                "/api/sessions",
                json=_create_payload(),
                headers={"X-Forwarded-For": "203.0.113.9"},
            )
            assert resp.status_code == 429, resp.text


class TestGuardrailsE2E:
    def test_live_generated_content_contains_no_urls(
        self, client: TestClient
    ) -> None:
        """端到端防幻觉：demo 全流程生成后，摘要里任何字段不得含 "http"。"""
        resp = client.post("/api/sessions", json=_create_payload())
        assert resp.status_code == 200, resp.text
        session_id = resp.json()["session_id"]

        # 等待生成完成（demo FakeLLM 立即完成；状态为 ready）
        summary = None
        for _ in range(40):
            detail = client.get(f"/api/sessions/{session_id}")
            assert detail.status_code == 200
            summary = detail.json()
            if summary["session"]["status"] == "ready":
                break
        else:
            pytest.fail("会话未在预期时间内完成生成")

        # 深度扫描所有字符串字段——但 links 例外：它来自人工策展白名单，
        # 本来就含 https://github.com 链接（模型生成内容才需要禁止 URL）
        def walk(value) -> None:
            if isinstance(value, str):
                assert "http" not in value, f"发现泄漏的 URL: {value!r}"
            elif isinstance(value, dict):
                for k, v in value.items():
                    if k == "links":
                        continue
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(summary)

    def test_links_only_from_curated_whitelist(self, client: TestClient) -> None:
        """摘要里的链接必须来自策展白名单（seed 唯一一条 langchain-ai/langchain）。"""
        resp = client.post("/api/sessions", json=_create_payload())
        session_id = resp.json()["session_id"]
        summary = None
        for _ in range(40):
            detail = client.get(f"/api/sessions/{session_id}")
            summary = detail.json()
            if summary["session"]["status"] == "ready":
                break
        else:
            pytest.fail("会话未在预期时间内完成生成")

        links = summary.get("links", [])
        assert links, "demo 生成后应至少带一条策展链接"
        for link in links:
            url = link["url"]
            assert url.startswith("https://github.com/"), url
            assert "langchain-ai/langchain" in url, url

    def test_llm_key_never_in_error_envelope(self, client: TestClient) -> None:
        """脱敏：带 X-LLM-Key 的请求出错时，错误信封不回显 Key。"""
        key = "sk-TEST123456"
        resp = client.post(
            "/api/sessions",
            json=_create_payload(goal="x" * 2000),  # 触发 goal_too_long
            headers={"X-LLM-Key": key},
        )
        assert resp.status_code == 400, resp.text
        text = resp.text
        assert key not in text, "错误响应泄漏了 LLM Key"