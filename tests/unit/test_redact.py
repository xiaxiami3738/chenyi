"""密钥脱敏：三条防线都要有测试。"""

from __future__ import annotations

import logging

import pytest

from app.llm.redact import (
    RedactionFilter,
    install_log_redaction,
    mask_secrets,
    safe_headers,
)

SECRET = "sk-abcdef1234567890abcdef1234567890"


@pytest.mark.parametrize(
    "raw",
    [
        f"key={SECRET}",
        f"Authorization: Bearer {SECRET}",
        f"X-LLM-Key: {SECRET}",
        f"api_key={SECRET}",
    ],
)
def test_mask_secrets_hides_key(raw: str) -> None:
    masked = mask_secrets(raw)
    assert SECRET not in masked
    assert "***" in masked


def test_mask_secrets_keeps_normal_text() -> None:
    text = "今天学了 RAG 和向量数据库"
    assert mask_secrets(text) == text


def test_safe_headers_masks_sensitive_headers() -> None:
    headers = {
        "X-LLM-Key": SECRET,
        "Authorization": f"Bearer {SECRET}",
        "Content-Type": "application/json",
    }
    safe = safe_headers(headers)
    assert safe["X-LLM-Key"] == "***"
    assert safe["Authorization"] == "***"
    assert safe["Content-Type"] == "application/json"


def test_log_filter_scrubs_record() -> None:
    record = logging.LogRecord(
        name="chenyi",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling provider with %s",
        args=(SECRET,),
        exc_info=None,
    )
    assert RedactionFilter().filter(record) is True
    assert SECRET not in record.getMessage()


def test_install_log_redaction_is_idempotent() -> None:
    logger = logging.getLogger("chenyi.test.redact")
    install_log_redaction(logger)
    install_log_redaction(logger)
    filters = [f for f in logger.filters if isinstance(f, RedactionFilter)]
    assert len(filters) == 1