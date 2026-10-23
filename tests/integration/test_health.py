"""健康检查与首页可用性。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_reports_demo_mode(client: TestClient) -> None:
    response = client.get("/api/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["demo"] is True
    assert body["mode"] == "demo"
    assert body["features"]["byok"] is True
    assert body["version"]


def test_index_page_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "AI 开发入门向导" in response.text
    assert "让普通人快速接手 AI 开发" in response.text
    # 页脚必须有备案位占位注释，方便拿到备案号后直接填
    assert "beian.miit.gov.cn" in response.text


def test_index_has_kb_search_interface(client: TestClient) -> None:
    """首页以查询浏览为主：搜索框 + 浏览区先于生成卡片出现。"""
    response = client.get("/")
    html = response.text
    assert "kb-form" in html
    assert "kb-input" in html
    assert "browse-card" in html
    assert html.index("kb-form") < html.index("start-card")


def test_static_assets_served(client: TestClient) -> None:
    for path in ("/static/style.css", "/static/app.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.content