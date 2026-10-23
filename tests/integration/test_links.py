"""策展链接：白名单过滤、接口返回、防幻觉约束。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.links import is_allowed_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/karpathy/nanoGPT", True),
        ("https://gitee.com/mirrors/llama.cpp", True),
        ("http://github.com/karpathy/nanoGPT", False),
        ("https://evil.example.com/a", False),
        ("https://raw.githubusercontent.com/a/b/main/x.py", False),
        ("javascript:alert(1)", False),
        ("", False),
    ],
)
def test_is_allowed_url(url: str, expected: bool) -> None:
    assert is_allowed_url(url) is expected


def test_seed_filters_invalid_entries(client: TestClient) -> None:
    body = client.get("/api/links").json()
    urls = [link["url"] for link in body["links"]]
    assert urls == ["https://github.com/langchain-ai/langchain"]
    assert body["topics"] == ["RAG 检索增强"]


def test_links_topic_filter(client: TestClient) -> None:
    assert client.get("/api/links", params={"topic": "RAG 检索增强"}).json()["links"]
    empty = client.get("/api/links", params={"topic": "不存在的主题"}).json()
    assert empty["links"] == []
    # 主题列表始终返回全部主题，便于前端渲染筛选条
    assert empty["topics"] == ["RAG 检索增强"]


def test_links_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/api/links", params={"limit": 0}).status_code == 422
    assert client.get("/api/links", params={"limit": 999}).status_code == 422


def test_link_payload_exposes_only_whitelisted_fields(client: TestClient) -> None:
    link = client.get("/api/links").json()["links"][0]
    assert set(link) == {"id", "topic", "title", "url", "why", "tags", "last_checked_at"}
    assert link["url"].startswith("https://github.com/")