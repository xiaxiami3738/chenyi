"""供应商预设接口：只暴露公开信息，不能出现任何 Key。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.llm.providers import PRESETS, list_presets


def test_presets_are_public_information_only() -> None:
    assert PRESETS, "至少要有一个供应商预设"
    for preset in list_presets():
        assert preset["base_url"].startswith(("https://", "http://localhost"))
        assert preset["label"]
        assert preset["default_model"]
        joined = " ".join(str(value) for value in preset.values())
        assert "sk-" not in joined
        # 字段名里不允许出现任何凭证字段（docs 里的公开文档链接属于公开信息）
        assert not {"api_key", "apikey", "secret", "token", "password"} & {
            field.lower() for field in preset
        }


def test_providers_endpoint(client: TestClient) -> None:
    response = client.get("/api/providers")
    assert response.status_code == 200
    body = response.json()
    assert len(body["providers"]) == len(PRESETS)
    assert "localStorage" in body["usage"]
    assert "sk-" not in response.text