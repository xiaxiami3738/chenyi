"""ChatClient 单元测试：请求形状、错误映射、流式解析、Base URL 校验。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.llm.client import (
    ChatClient,
    LLMClientError,
    LLMConfig,
    config_from_settings,
    validate_base_url,
)
from app.llm.schemas import ChatMessage

MESSAGES = [ChatMessage.user("你好")]


def _run(coro):
    return asyncio.run(coro)


def _ok_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


def _client(transport: httpx.AsyncBaseTransport, **kwargs) -> ChatClient:
    config = LLMConfig(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key="sk-test-123",
        **kwargs,
    )
    return ChatClient(config, transport=transport)


def test_chat_request_shape_and_headers() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        seen["body"] = json.loads(request.content)
        return _ok_response("分析结果")

    client = _client(httpx.MockTransport(handler))
    text = _run(client.chat(MESSAGES, json_mode=True))
    assert text == "分析结果"
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-123"
    assert seen["body"]["model"] == "deepseek-chat"
    assert [m["role"] for m in seen["body"]["messages"]] == ["user"]
    assert seen["body"]["max_tokens"] == 2048
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_max_tokens_field_compat() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok_response("ok")

    client = _client(
        httpx.MockTransport(handler), max_tokens_field="max_output_tokens"
    )
    _run(client.chat(MESSAGES, json_mode=False))
    assert seen["body"]["max_output_tokens"] == 2048
    assert "max_tokens" not in seen["body"]
    assert "response_format" not in seen["body"]


def test_chat_joins_list_content() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [{"text": "A"}, {"text": "B"}],
                        }
                    }
                ]
            },
        )

    client = _client(httpx.MockTransport(handler))
    assert _run(client.chat(MESSAGES)) == "AB"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "invalid_key"),
        (403, "invalid_key"),
        (429, "upstream_rate_limited"),
        (500, "upstream_error"),
    ],
)
def test_error_status_mapping(status: int, expected_code: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "boom"}})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(LLMClientError) as exc_info:
        _run(client.chat(MESSAGES))
    assert exc_info.value.error_code == expected_code
    assert exc_info.value.status_code == status


def test_timeout_maps_to_timeout_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("连接超时")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(LLMClientError) as exc_info:
        _run(client.chat(MESSAGES))
    assert exc_info.value.error_code == "timeout"


def test_http_error_maps_to_unreachable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("无法连接")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(LLMClientError) as exc_info:
        _run(client.chat(MESSAGES))
    assert exc_info.value.error_code == "upstream_unreachable"


def test_stream_yields_delta_content_in_order() -> None:
    sse_body = (
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        'data: {"choices":[{"delta":{"content": 7}}]}\n\n'
        'data: {"choices":[{"delta":{}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )

    client = _client(httpx.MockTransport(handler))

    async def collect() -> list[str]:
        return [chunk async for chunk in client.stream(MESSAGES)]

    assert _run(collect()) == ["你", "好"]


def test_stream_ignores_malformed_sse_line() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="data: 不是JSON\n\n", headers={"content-type": "text/event-stream"}
        )

    client = _client(httpx.MockTransport(handler))

    async def collect() -> list[str]:
        return [chunk async for chunk in client.stream(MESSAGES)]

    assert _run(collect()) == []


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://api.openai.com/v1", True),
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:11434/v1", True),
        ("http://example.com/v1", False),
        ("ftp://example.com/v1", False),
        ("https://192.168.1.10/v1", False),
        ("https://10.0.0.1/v1", False),
        ("not-a-url", False),
    ],
)
def test_validate_base_url(url: str, ok: bool) -> None:
    if ok:
        assert validate_base_url(url) == url
    else:
        with pytest.raises(ValueError):
            validate_base_url(url)


def test_config_from_settings_reads_timeout(settings: Settings) -> None:
    config = config_from_settings(
        settings, base_url="https://x/v1", model="m", api_key="k"
    )
    assert config.timeout_seconds == settings.request_timeout_seconds
    assert config.api_key == "k"
    assert config.response_format == {"type": "json_object"}