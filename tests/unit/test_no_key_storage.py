"""结构约束：数据库里不允许存在任何可以存放 **明文** Key 的字段。

M6 起的设计：Key 只存在于访客浏览器 localStorage，服务端不落库、不写日志。
M7.3 起用户选择了「服务端可选项加密代存」：**唯一例外**是 ``vault_entries`` 表，
它只存放用服务器主密钥（Fernet）加密后的密文 blob，且列名刻意避开
api_key/secret/password/credential/bearer 等字面命名，避免被误当明文凭证字段。
"""

from __future__ import annotations

import re

from app.models import Base

FORBIDDEN = re.compile(r"(api_?key|secret|password|credential|bearer)", re.IGNORECASE)

#: 唯一的加密代存例外表：列名不允许出现凭证字面，且只允许存密文 blob
VAULT_TABLE = "vault_entries"
VAULT_ALLOWED_COLUMNS = frozenset(
    {"id", "client_id", "scope", "cipher_blob", "created_at", "updated_at"}
)


def test_no_table_can_store_credentials() -> None:
    """除 vault_entries（密文例外）外，任何表的表名/列名都不得命中凭证字面。"""
    offenders: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name == VAULT_TABLE:
            continue  # 例外表单独校验
        for column in table.columns:
            if FORBIDDEN.search(column.name) or FORBIDDEN.search(table_name):
                offenders.append(f"{table_name}.{column.name}")
    assert offenders == [], f"发现可能存放密钥的字段：{offenders}"


def test_vault_table_columns_are_explicit_whitelist() -> None:
    """vault_entries 的列名必须恰好是显式白名单（避开凭证字面 + 只存 cipher_blob）。"""
    table = Base.metadata.tables.get(VAULT_TABLE)
    assert table is not None, "vault_entries 表必须存在"
    actual = {column.name for column in table.columns}
    assert actual == VAULT_ALLOWED_COLUMNS, f"vault_entries 列必须恰好是 {VAULT_ALLOWED_COLUMNS}"
    forbidden_in_vault = [
        name for name in actual if FORBIDDEN.search(name)
    ]
    assert forbidden_in_vault == [], f"vault_entries 列名不允许出现凭证字面：{forbidden_in_vault}"


def test_expected_tables_exist() -> None:
    expected = {
        "learn_sessions",
        "plans",
        "lessons",
        "terms",
        "quiz_items",
        "attempts",
        "reviews",
        "curated_links",
        "session_links",
        "agent_runs",
        VAULT_TABLE,
    }
    assert expected.issubset(set(Base.metadata.tables))