"""结构约束：数据库里不允许存在任何可以存放 Key 的字段。"""

from __future__ import annotations

import re

from app.models import Base

FORBIDDEN = re.compile(r"(api_?key|secret|password|credential|bearer)", re.IGNORECASE)


def test_no_table_can_store_credentials() -> None:
    offenders: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        for column in table.columns:
            if FORBIDDEN.search(column.name) or FORBIDDEN.search(table_name):
                offenders.append(f"{table_name}.{column.name}")
    assert offenders == [], f"发现可能存放密钥的字段：{offenders}"


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
    }
    assert expected.issubset(set(Base.metadata.tables))