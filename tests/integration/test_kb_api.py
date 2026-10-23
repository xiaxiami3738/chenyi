"""知识库 HTTP 接口：/api/kb/search 与 /api/kb/overview。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def kb_client(settings: Settings, kb_dir, seed_dir, work_dir):
    """带真实 kb/seed 文件的应用实例。"""
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_kb_search_finds_chapter(kb_client: TestClient) -> None:
    resp = kb_client.get("/api/kb/search", params={"q": "什么是 Agent"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "什么是 Agent"
    assert body["total"] >= 1
    kinds = {hit["kind"] for hit in body["results"]}
    assert "chapter" in kinds
    top = body["results"][0]
    assert top["source"].endswith(".md")
    assert "Agent" in top["snippet"] or "Agent" in top["title"]


def test_kb_search_returns_terms_and_links(kb_client: TestClient) -> None:
    resp = kb_client.get("/api/kb/search", params={"q": "RAG", "limit": 10})
    assert resp.status_code == 200
    kinds = {hit["kind"] for hit in resp.json()["results"]}
    assert "term" in kinds
    assert "link" in kinds


def test_kb_search_empty_returns_empty(kb_client: TestClient) -> None:
    resp = kb_client.get("/api/kb/search", params={"q": "的"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_kb_search_validation(kb_client: TestClient) -> None:
    assert kb_client.get("/api/kb/search").status_code == 422  # 缺 q


def test_kb_overview(kb_client: TestClient) -> None:
    resp = kb_client.get("/api/kb/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["chapters"], list)
    assert isinstance(body["terms_topics"], list)
    assert isinstance(body["links_topics"], list)
    assert any(ch["file"] == "chapter1.md" for ch in body["chapters"])


def test_kb_routes_registered_in_healthz(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    assert resp.status_code == 200
    assert "kb" in resp.json()["features"]