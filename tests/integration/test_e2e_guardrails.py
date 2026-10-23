"""M4 全链路护栏 E2E：防幻觉 + Key 脱敏。

- 防幻觉：FakeLLM 的「模型输出」被注入恶意 URL（模拟模型编造链接），
  走完整 POST /api/sessions → 轮询 GET /api/sessions/{id}，
  断言下发的任何文本字段都不含 http / evil.example.com；
  链接只能来自策展白名单（seed 仅 langchain-ai/langchain 一条）。
- 脱敏：携带 X-LLM-Key 触发 400/422 错误，错误信封的任何文本都不得
  回显明文 Key（detail 由 mask_secrets 处理）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from fastapi.testclient import TestClient

from app.llm.fake import FakeLLM
from app.llm.redact import mask_secrets

GOAL = "从零学会开发一个 RAG 问答机器人"
EVIL_URL = "https://evil.example.com/x"
SECRET = "sk-TEST123456"

#: seed 里唯一合规的策展链接（conftest 的 seed_dir 只保留这一条）
WHITELISTED_URL = "https://github.com/langchain-ai/langchain"


def _payload(goal: str, **overrides: object) -> dict:
    body = {
        "client_id": "guardrail-test",
        "goal": goal,
        "level": "beginner",
        "daily_minutes": 30,
    }
    body.update(overrides)
    return body


def _post_session(client: TestClient, *, headers: dict[str, str] | None = None):
    return client.post("/api/sessions", json=_payload(GOAL), headers=headers or {})


def _wait_ready(client: TestClient, session_id: int, tries: int = 100) -> dict:
    """demo 模式后台立即生成：轮询摘要直到会话 ready。"""
    for _ in range(tries):
        resp = client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["session"]["status"] == "ready":
            return data
        time.sleep(0.05)
    raise AssertionError("会话未在预期时间内完成生成")  # pragma: no cover


def _iter_text_fields(data: object, skip_keys: set[str] | None = None) -> Iterator[str]:
    """深度遍历 dict/list，产出所有字符串叶节点（跳过 skip_keys 命中的键）。"""
    skip = skip_keys or frozenset()
    if isinstance(data, dict):
        for key, value in data.items():
            if key in skip:
                continue
            yield from _iter_text_fields(value, skip)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_text_fields(item, skip)
    elif isinstance(data, str):
        yield data


def _install_evil_url_fake(monkeypatch) -> None:
    """让 FakeLLM 的输出里混入恶意 URL，模拟模型幻觉编造链接。

    只污染 explain / quiz 两类会落库并最终出现在摘要里的字段；
    结构仍是合法 JSON，保证流水线正常解析、仅由 strip_urls 负责兜底。
    """

    async def poisoned_chat(
        self,
        messages,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        raw = await original_chat(
            self, messages, json_mode=json_mode, max_tokens=max_tokens
        )
        payload = json.loads(raw)
        if "lesson_body_md" in payload:  # explain：讲义正文 / 代码 / 名词卡
            payload["lesson_body_md"] = (
                f'{payload.get("lesson_body_md", "")}\n\n'
                f"深度阅读：[来源]({EVIL_URL}) 以及 {EVIL_URL}"
            )
            payload["code_snippets"] = [
                f"{snippet}\n# 参考：{EVIL_URL}" for snippet in payload.get("code_snippets", [])
            ]
            for card in payload.get("terms", []):
                card["definition"] = f'{card.get("definition", "")}（出处：{EVIL_URL}）'
        elif "quizzes" in payload:  # quiz：题干 / 评分要点
            for item in payload.get("quizzes", []):
                item["question"] = f'{item.get("question", "")}\n素材见 {EVIL_URL}'
                item["rubric"] = f'{item.get("rubric", "")} 可参考 {EVIL_URL}'
        return json.dumps(payload, ensure_ascii=False)

    original_chat = FakeLLM.chat
    monkeypatch.setattr(FakeLLM, "chat", poisoned_chat)


def test_model_output_urls_never_reach_api(monkeypatch, client: TestClient) -> None:
    """防幻觉：模型输出里的恶意 URL 被剔除，链接只能来自策展白名单。"""
    _install_evil_url_fake(monkeypatch)
    created = _post_session(client)
    assert created.status_code == 200
    summary = _wait_ready(client, created.json()["session_id"])

    # 1) 链接只能来自策展白名单：本测试的 seed 只有 langchain-ai/langchain 一条，
    #    主题「RAG 检索增强概览」子串命中「RAG 检索增强」，应能挂上。
    assert summary["links"], "命中策展主题时应挂上种子链接"
    for link in summary["links"]:
        assert link["url"].startswith("https://github.com/")
        assert EVIL_URL not in link["url"]
    assert any(link["url"] == WHITELISTED_URL for link in summary["links"])

    # 2) 除白名单链接的 url 字段外，摘要里任何文本字段都不得出现 http 或恶意域名
    for text in _iter_text_fields(summary, skip_keys={"url"}):
        assert "http" not in text, f"模型输出 URL 泄漏到摘要文本：{text!r}"
        assert "evil.example.com" not in text


def test_error_responses_never_leak_key(client: TestClient) -> None:
    """脱敏：带 X-LLM-Key 触发 400/422，错误信封的任何文本都不回显明文 Key。"""
    headers = {"X-LLM-Key": SECRET}

    # 400 goal_too_long：goal 超过 Settings.max_input_chars=1000
    too_long = client.post("/api/sessions", json=_payload("长" * 1001), headers=headers)
    assert too_long.status_code == 400
    assert too_long.json()["error"]["code"] == "goal_too_long"
    assert SECRET not in too_long.text

    # 422 校验失败：detail 经 mask_secrets 处理后才进错误信封
    invalid = client.post(
        "/api/sessions", json=_payload(GOAL, daily_minutes=5), headers=headers
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert SECRET not in invalid.text

    # 兜底：mask_secrets 对 sk- 形态直接打码（unit 层已有，此处 E2E 呼应）
    assert SECRET not in mask_secrets(f"X-LLM-Key: {SECRET}")


def test_success_response_never_echoes_key(client: TestClient) -> None:
    """脱敏：demo 成功链路（创建响应 + 完整摘要）同样不允许回显 Key。"""
    headers = {"X-LLM-Key": SECRET}
    created = _post_session(client, headers=headers)
    assert created.status_code == 200
    assert SECRET not in created.text

    summary = _wait_ready(client, created.json()["session_id"])
    assert SECRET not in json.dumps(summary, ensure_ascii=False)