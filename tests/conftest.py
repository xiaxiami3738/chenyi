"""pytest 公共夹具：临时 SQLite + 演示模式应用实例。"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = REPO_ROOT / ".pytest-tmp"


@pytest.fixture
def work_dir() -> Iterator[Path]:
    """每个用例一个干净目录。

    不用 pytest 的 tmp_path：在部分受限环境（如本机 DSH 沙箱）下，
    tmp_path / tempfile 会以 0o700 建目录，之后写入会被拒绝。这里显式用默认权限建目录。
    """
    TMP_ROOT.mkdir(exist_ok=True)
    directory = TMP_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def seed_dir(work_dir: Path) -> Path:
    """最小策展种子：一条合规、三条不合规（http / 非托管站 / 缺字段）。"""
    directory = work_dir / "seed"
    directory.mkdir()
    payload = {
        "version": 1,
        "links": [
            {
                "topic": "RAG 检索增强",
                "title": "langchain-ai/langchain",
                "url": "https://github.com/langchain-ai/langchain",
                "why": "RAG 组件最全的框架",
                "tags": ["rag"],
                "stars": 90000,
            },
            {
                "topic": "RAG 检索增强",
                "title": "明文链接",
                "url": "http://github.com/example/plain",
                "why": "应该被过滤：非 https",
            },
            {
                "topic": "RAG 检索增强",
                "title": "非托管站",
                "url": "https://evil.example.com/phishing",
                "why": "应该被过滤：不在允许域名内",
            },
            {"topic": "RAG 检索增强", "title": "缺 url", "why": "应该被过滤"},
        ],
    }
    (directory / "curated_links.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    terms_payload = {
        "version": 1,
        "terms": [
            {
                "term": "RAG",
                "topic": "RAG 检索增强",
                "definition": "检索增强生成：先从知识库检索相关内容，再让模型作答。",
                "analogy": "开卷考试：先翻资料再答题。",
                "pitfalls": "检索质量决定回答质量。",
            }
        ],
    }
    (directory / "terms.json").write_text(
        json.dumps(terms_payload, ensure_ascii=False), encoding="utf-8"
    )
    return directory


@pytest.fixture
def kb_dir(work_dir: Path) -> Path:
    """最小知识库种子：一小节开源书内容，供检索用例使用。"""
    directory = work_dir / "kb"
    book_dir = directory / "book"
    book_dir.mkdir(parents=True)
    (book_dir / "chapter1.md").write_text(
        "# AI Agent 入门\n"
        "\n"
        "## 什么是 Agent\n"
        "Agent 是能自主规划步骤、调用工具并逐步完成任务的智能体程序。\n"
        "像带工具箱的员工：拆任务、选工具、干活、汇报。\n"
        "\n"
        "## RAG 检索增强\n"
        "RAG 先从知识库检索相关内容，再让模型基于检索结果作答。\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def settings(work_dir: Path, seed_dir: Path, kb_dir: Path) -> Settings:
    return Settings(
        app_mode="demo",
        db_path=work_dir / "test.db",
        seed_dir=seed_dir,
        kb_dir=kb_dir,
        rate_limit_per_minute=10,
        max_concurrency=2,
        request_timeout_seconds=5,
        max_input_chars=1000,
    )


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client