"""知识库检索：分词、索引、查询、出处。"""

from __future__ import annotations

from app.services.kb import KBDoc, KBIndex, build_kb_index, tokenize


def test_tokenize_mixed_chinese_and_latin() -> None:
    assert tokenize("什么是 Agent") == ["什么", "么是", "agent"]
    assert tokenize("RAG 检索") == ["rag", "检索"]
    assert tokenize("python-beautifulsoup") == ["python-beautifulsoup"]


def test_tokenize_drops_stopwords_and_short_latin() -> None:
    assert "是" not in tokenize("这是什么")
    assert "a" not in tokenize("just a test")


def _sample_docs() -> list[KBDoc]:
    return [
        KBDoc(
            kind="chapter",
            title="什么是 Agent",
            body="Agent 是能自主规划步骤、调用工具并逐步完成任务的智能体程序。",
            source="chapter1.md",
            topic="chapter1",
            section="什么是 Agent",
        ),
        KBDoc(
            kind="term",
            title="RAG",
            body="检索增强生成：先从知识库检索相关内容，再让模型基于检索结果作答。",
            source="terms.json",
            topic="RAG 检索增强",
            definition="检索增强生成：先从知识库检索相关内容，再让模型基于检索结果作答。",
            analogy="开卷考试：先翻书找到原文，再照着原文作答。",
            pitfalls="检索到的内容质量决定答案质量，别指望它纠正错误资料。",
        ),
        KBDoc(
            kind="link",
            title="langchain-ai/langchain",
            body="RAG 组件最全的框架",
            source="curated_links.json",
            topic="RAG 检索增强",
            url="https://github.com/langchain-ai/langchain",
        ),
    ]


def test_search_returns_ranked_hits() -> None:
    index = KBIndex(_sample_docs())
    hits = index.search("什么是 Agent", limit=5)
    assert hits
    top = hits[0]
    assert top.kind == "chapter"
    assert top.title == "什么是 Agent"
    assert top.source == "chapter1.md"
    assert "Agent" in top.snippet


def test_search_kind_aware() -> None:
    index = KBIndex(_sample_docs())
    hits = index.search("RAG", limit=5)
    kinds = {h.kind for h in hits}
    assert "term" in kinds
    assert "link" in kinds
    by_kind = {h.kind: h for h in hits}
    assert by_kind["link"].url == "https://github.com/langchain-ai/langchain"


def test_search_empty_query_returns_nothing() -> None:
    index = KBIndex(_sample_docs())
    assert index.search("") == []
    assert index.search("的") == []  # 全是停用词


def test_overview_groups_chapters_and_topics() -> None:
    index = KBIndex(_sample_docs())
    overview = index.overview()
    assert any(ch["file"] == "chapter1.md" for ch in overview["chapters"])
    assert "RAG 检索增强" in overview["links_topics"]


def test_build_kb_index_accepts_missing_dirs() -> None:
    from pathlib import Path

    index = build_kb_index(Path("不存在"), Path("也不存在"))
    assert index.docs == []
    assert index.search("任何") == []

def test_answer_returns_term_card_and_link() -> None:
    """首页本地问答：命中名词卡 + 一条最相关开源项目 + 章节。"""
    index = KBIndex(_sample_docs())
    body = index.answer("RAG 是什么")
    assert body["query"] == "RAG 是什么"
    assert body["terms"], "应命中名词卡"
    card = body["terms"][0]
    assert card["term"] == "RAG"
    assert card["topic"] == "RAG 检索增强"
    assert card["definition"]
    assert card["analogy"]
    assert card["pitfalls"]
    assert body["link"]["url"] == "https://github.com/langchain-ai/langchain"
    assert isinstance(body["chapters"], list)
    assert body["total"] >= 2


def test_answer_empty_query_is_empty() -> None:
    index = KBIndex(_sample_docs())
    body = index.answer("的")  # 全是停用词
    assert body["terms"] == []
    assert body["chapters"] == []
    assert body["link"] is None
    assert body["total"] == 0


def test_terms_filtered_by_topic() -> None:
    index = KBIndex(_sample_docs())
    cards = index.terms("RAG 检索增强")
    assert [card["term"] for card in cards] == ["RAG"]
    assert index.terms("不存在的主题") == []
    assert len(index.terms()) == 1  # 不传主题时返回全部名词卡
