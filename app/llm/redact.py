"""密钥脱敏。

对外的承诺：访客的 LLM Key 不写数据库、不写文件、不进日志。
这里提供两道保险：
1. :func:`mask_secrets` —— 把任意文本里的 Key 形态替换掉；
2. :func:`install_log_redaction` —— 给标准库 logging 挂一个过滤器，兜住漏网之鱼。
"""

from __future__ import annotations

import logging
import re

#: 常见的 Key 形态：sk-xxx、Bearer xxx、以及各类 32+ 位无分隔长串
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***"),
    (re.compile(r"(?i)\b(api[_-]?key|authorization|x-llm-key)\b\s*[:=]\s*[^\s,;\"']+"), r"\1=***"),
)

SENSITIVE_HEADERS = frozenset({"x-llm-key", "authorization", "cookie", "set-cookie"})


def mask_secrets(text: str) -> str:
    """把文本中的密钥形态统一替换为占位符。"""
    if not text:
        return text
    masked = text
    for pattern, replacement in _PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def safe_headers(headers) -> dict[str, str]:
    """返回可安全记录的请求头副本。"""
    out: dict[str, str] = {}
    for key, value in dict(headers).items():
        out[key] = "***" if key.lower() in SENSITIVE_HEADERS else mask_secrets(str(value))
    return out


class RedactionFilter(logging.Filter):
    """在日志落盘前做最后一次脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 防御性分支
            return True
        masked = mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """给指定 logger（默认 root）挂上脱敏过滤器，幂等。"""
    target = logger or logging.getLogger()
    if not any(isinstance(f, RedactionFilter) for f in target.filters):
        target.addFilter(RedactionFilter())