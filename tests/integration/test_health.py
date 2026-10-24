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


def test_webp_mascot_served_with_image_mime(client: TestClient) -> None:
    """WebP 立绘必须带 image/webp，否则浏览器可能按 text/plain 拒绝渲染。"""
    response = client.get("/static/img/whale-xiaoyu-search-q4.webp")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/webp")


def test_index_has_local_search_mode_hint(client: TestClient) -> None:
    """首页要说明「未接入 API 时是本地检索」，并留出接入入口。"""
    html = client.get("/").text
    assert 'id="kb-mode"' in html
    assert "本地检索" in html
    assert 'href="/connect"' in html
    # 小立绘挪到搜索框右侧（.kb-mascot），不再是 hero 顶部的大图
    assert "kb-mascot" in html
    assert "whale-xiaoyu-search-q4.webp" in html


def test_kb_and_connect_pages_render(client: TestClient) -> None:
    kb = client.get("/kb")
    assert kb.status_code == 200
    assert "知识库" in kb.text
    assert "/static/kb-page.js" in kb.text

    connect = client.get("/connect")
    assert connect.status_code == 200
    assert "接入" in connect.text


def test_index_drops_duplicate_byok_card(client: TestClient) -> None:
    """接入 API 已独立成 /connect 页，首页不再重复放接入表单。"""
    html = client.get("/").text
    assert 'id="byok-card"' not in html
    # 首页不应再出现填 Key 的输入框，避免两处重复接入
    assert 'id="api-key"' not in html
    assert 'id="provider"' not in html


def test_index_xiaoyu_chat_is_gated(client: TestClient) -> None:
    """鲸小鱼对话默认锁定：没接入自己的模型时不允许使用。"""
    html = client.get("/").text
    assert 'id="xiaoyu-card"' in html
    assert 'data-state="locked"' in html
    assert 'id="xiaoyu-locked"' in html
    assert "未接入" in html
    # 对话框样式独立成文件，由模板的 head block 引入
    assert "/static/xiaoyu.css" in html
