"""ChatClient：统一的 OpenAI 兼容对话客户端（httpx）。

职责：
- 调 ``{base_url}/chat/completions``，支持非流式与 SSE 流式（逐 token）；
- 消化 ``providers.py`` 里各家预设的差异（``max_tokens_field`` 等 compat 字段）；
- 超时用 ``Settings.request_timeout_seconds``；
- 安全铁律：Key 只存在于本次请求的内存上下文（``LLMConfig``），绝不打日志、
  绝不下发到业务层之外；任何可能进日志的文本都先过 ``mask_secrets``。
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from ..config import Settings
from .redact import mask_secrets
from .schemas import ChatMessage

logger = logging.getLogger("chenyi.llm.client")

DEFAULT_MAX_TOKENS_FIELD = "max_tokens"
DEFAULT_MAX_TOKENS = 2048


class LLMClientError(Exception):
    """上游模型调用失败，带面向用户的中文信息与错误码。

    Attributes:
        message: 可直接展示给用户的中文提示。
        status_code: 上游 HTTP 状态码（网络层失败时为 None）。
        error_code: 稳定的错误码，供 AgentRun.error_code / SSE error 事件使用。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str = "upstream_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


#: 自定义 baseURL 白名单：本地地址（Ollama）允许 http，其余只允许 https
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def validate_base_url(url: str) -> str:
    """校验自定义 baseURL：只允许 https；本机地址允许 http（Ollama 例外）。

    同时阻断私网 / 链路本地 IP（防 SSRF）。只是尽力而为的静态校验：
    域名形式的地址不做 DNS 解析（避免阻塞事件循环），真正的网络层兜底交给
    出网 ACL 与部署边界。
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValueError("Base URL 不是合法的地址") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL 必须形如 https://host/v1（本机地址可 http）")

    host = parsed.hostname or ""
    is_local = host.lower() in LOCAL_HOSTS or host.startswith("127.")
    if parsed.scheme == "http" and not is_local:
        raise ValueError("非本机地址只允许使用 https")

    if host and not is_local:  # 本机地址是白名单例外，其余地址要挡私网
        try:
            addr = ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            addr = None
        if addr is not None and (
            addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
        ):
            raise ValueError("不允许使用内网或本机回环地址（防 SSRF）")
    return url


@dataclass(frozen=True)
class LLMConfig:
    """一次调用的完整上游信息。

    ``api_key`` 只存在于这个对象里，随请求结束即可被回收。
    """

    base_url: str
    model: str
    api_key: str
    max_tokens_field: str = DEFAULT_MAX_TOKENS_FIELD
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_seconds: int = 120
    response_format: dict | None = field(
        default_factory=lambda: {"type": "json_object"}, repr=False
    )


def config_from_settings(
    settings: Settings,
    *,
    base_url: str,
    model: str,
    api_key: str,
    max_tokens_field: str = DEFAULT_MAX_TOKENS_FIELD,
) -> LLMConfig:
    """把 Settings 与本次请求的供应商信息组装成调用配置。"""
    return LLMConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens_field=max_tokens_field,
        timeout_seconds=settings.request_timeout_seconds,
    )


class ChatClient:
    """OpenAI 兼容 chat/completions 客户端。

    用法（流水线内，一次调用一个实例）::

        client = ChatClient(config)
        text = await client.chat(messages, json_mode=True)
        async for token in client.stream(messages):
            ...
    """

    def __init__(
        self,
        config: LLMConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        #: 测试注入 MockTransport 用；生产环境为 None（走默认网络栈）
        self._transport = transport

    # ── 内部拼装 ───────────────────────────────────────────────

    def _endpoint(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def _payload(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": [message.model_dump() for message in messages],
        }
        tokens = max_tokens if max_tokens is not None else self.config.max_tokens
        #: 各家最大 token 字段名可能不同（如某些网关用 max_output_tokens）
        payload[self.config.max_tokens_field] = tokens
        if json_mode and self.config.response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            transport=self._transport,
        )

    # ── 非流式 ─────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """整段返回模型回答文本。"""
        payload = self._payload(
            messages, json_mode=json_mode, max_tokens=max_tokens
        )
        logger.debug(
            "调用 %s（json_mode=%s）", self._endpoint(), json_mode
        )
        async with self._make_client() as client:
            try:
                response = await client.post(
                    self._endpoint(), headers=self._headers(), json=payload
                )
            except httpx.TimeoutException as exc:
                raise LLMClientError(
                    f"上游模型响应超时（超过 {self.config.timeout_seconds} 秒），请重试",
                    error_code="timeout",
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMClientError(
                    f"上游模型接口不可达：{mask_secrets(str(exc))}",
                    error_code="upstream_unreachable",
                ) from exc
            self._raise_for_status(response)
            return self._extract_content(response.json())

    # ── 流式（SSE，逐 token） ──────────────────────────────────

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """按 token 产出 SSE 数据块里的 content 片段。"""
        payload = self._payload(
            messages, json_mode=json_mode, max_tokens=max_tokens
        )
        async with self._make_client() as client:
            try:
                request = client.build_request(
                    "POST", self._endpoint(), headers=self._headers(), json=payload
                )
                response = await client.send(request, stream=True)
                self._raise_for_status(response)
                async for line in response.aiter_lines():
                    piece = self._parse_sse_line(line)
                    if piece:
                        yield piece
            except LLMClientError:
                raise
            except httpx.TimeoutException as exc:
                raise LLMClientError(
                    f"上游模型响应超时（超过 {self.config.timeout_seconds} 秒），请重试",
                    error_code="timeout",
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMClientError(
                    f"上游模型接口不可达：{mask_secrets(str(exc))}",
                    error_code="upstream_unreachable",
                ) from exc

    # ── 解析辅助 ───────────────────────────────────────────────

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        if status in (401, 403):
            raise LLMClientError(
                f"Key 无效或无权限（HTTP {status}），请检查后重试",
                status_code=status,
                error_code="invalid_key",
            )
        if status == 429:
            raise LLMClientError(
                "上游接口限流（HTTP 429），请稍后重试",
                status_code=status,
                error_code="upstream_rate_limited",
            )
        raise LLMClientError(
            f"上游模型返回异常（HTTP {status}），请稍后重试",
            status_code=status,
            error_code="upstream_error",
        )

    @staticmethod
    def _extract_content(data: dict) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMClientError(
                "上游接口返回了无法解析的响应结构，请稍后重试",
                error_code="bad_response",
            ) from None
        if isinstance(content, list):  # 某些接口返回分片数组
            content = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        return str(content or "")

    @staticmethod
    def _parse_sse_line(line: str) -> str | None:
        """解析一行 SSE 数据，返回其中的文本增量；无内容返回 None。"""
        stripped = line.strip()
        if not stripped.startswith("data:"):
            return None
        data = stripped[5:].strip()
        if not data or data == "[DONE]":
            return None
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("忽略无法解析的 SSE 数据：%s", mask_secrets(data))
            return None
        try:
            delta = chunk["choices"][0].get("delta", {})
        except (KeyError, IndexError, TypeError):
            return None
        content = delta.get("content")
        return content if isinstance(content, str) and content else None