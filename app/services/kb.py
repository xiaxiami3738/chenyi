"""本地知识库检索服务。

设计要点：
- 零 token：纯本地倒排索引 + 中文分词，不调用任何模型；
- 中文分词：CJK 二元组（bigram）+ 拉丁词切分，纯标准库；
- 内容来源：``data/kb/book/*.md``（开源书，Apache-2.0 署名见 data/kb/ATTRIBUTION.md）、
  ``seed/terms.json`` 名词卡、``seed/curated_links.json`` 策展链接；
- 检索结果带出处（章节/术语/链接），回答可信可核。
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

#: 不进索引的文件：习题答案不在知识问答里直接给答案
SKIP_FILES = frozenset({"reference-answers.md"})

#: 中文分词：连续 CJK 片段滑窗产生二元组；连续拉丁/数字串作为一个词
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")

#: 常见停用词（中文高频虚词 + 泛化问法），避免搜索结果被无意义词淹没
_STOPWORDS = frozenset(
    {
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "及",
        "或",
        "而",
        "也",
        "就",
        "都",
        "很",
        "这",
        "那",
        "个",
        "中",
        "上",
        "下",
        "里",
        "对",
        "为",
        "从",
        "到",
        "以",
        "有",
        "没",
        "不",
        "我",
        "你",
        "他",
        "她",
        "它",
        "们",
        "请问",
        "一下",
        "一个",
        "可以",
    }
)

_HAS_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _cjk_bigrams(seg: str) -> list[str]:
    """把一段连续 CJK 文本切成二元组（不足两个字符时退化为单字）。"""
    if len(seg) <= 1:
        return [seg]
    return [seg[i : i + 2] for i in range(len(seg) - 1)]


def tokenize(text: str) -> list[str]:
    """中文分词：CJK 二元组 + 拉丁词，统一小写，去掉停用词。

    "什么是 Agent" -> ["什么", "么是", "agent"]
    """
    out: list[str] = []
    cjk_buf: list[str] = []
    latin_buf: list[str] = []
    for ch in unicodedata.normalize("NFC", text):
        if _CJK_RE.match(ch):
            if latin_buf:
                out.extend(_WORD_RE.findall("".join(latin_buf).lower()))
                latin_buf = []
            cjk_buf.append(ch)
        else:
            if cjk_buf:
                out.extend(_cjk_bigrams("".join(cjk_buf)))
                cjk_buf = []
            latin_buf.append(ch)
    if cjk_buf:
        out.extend(_cjk_bigrams("".join(cjk_buf)))
    if latin_buf:
        out.extend(_WORD_RE.findall("".join(latin_buf).lower()))
    #: 过滤停用词与单字符拉丁词（单字母/单数字无检索价值）
    return [
        t
        for t in out
        if t not in _STOPWORDS and not (len(t) == 1 and t[0].isascii())
    ]


@dataclass
class KBDoc:
    """一条可检索的知识条目。"""

    kind: str  # "chapter" | "term" | "link"
    title: str
    body: str
    source: str  # 文件名（出处）
    topic: str = ""
    url: str = ""
    section: str = ""  # chapter 的小节标题
    # 名词卡的结构化字段（前端「词条解释」卡片直接使用，避免只给一段拼接文本）
    definition: str = ""
    analogy: str = ""
    pitfalls: str = ""


@dataclass
class KBHit:
    """一条检索结果。"""

    kind: str
    title: str
    source: str
    topic: str
    url: str
    snippet: str
    score: float

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "title": self.title,
            "source": self.source,
            "topic": self.topic,
            "url": self.url,
            "snippet": self.snippet,
            "score": round(self.score, 2),
        }


def _heading_level(line: str) -> int:
    """返回标题级别（## -> 2），不是标题返回 0。"""
    if not line.startswith("#"):
        return 0
    level, i = 0, 0
    while i < len(line) and line[i] == "#":
        level += 1
        i += 1
    return level if i < len(line) and line[i] in (" ", "\t") else 0


_MD_STRIP = re.compile(r"[`*_~#>\[\]()!|\\]")
_ATTR_STRIP = re.compile(r"\s*\{[^}]*\}\s*")


def _clean_text(raw: str) -> str:
    """去掉 Markdown 标记，压缩空白。"""
    text = _MD_STRIP.sub("", raw)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clean_title(raw: str) -> str:
    """标题清理：额外去掉 ``{.unnumbered}`` 这类属性标记。"""
    return _clean_text(_ATTR_STRIP.sub(" ", raw))


def parse_chapters(kb_dir: Path) -> list[KBDoc]:
    """扫描 data/kb/book/*.md，按 ## 小节切成检索条目。"""
    book_dir = kb_dir / "book"
    docs: list[KBDoc] = []
    if not book_dir.is_dir():
        return docs

    for path in sorted(book_dir.glob("*.md")):
        if path.name in SKIP_FILES:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        file_title = ""  # 文件级大标题（# 开头），用作 chapter 的 topic
        sections: list[tuple[str, list[str]]] = []
        current_title, current_lines = "", []
        for line in lines:
            level = _heading_level(line)
            if level == 1 and not file_title:
                file_title = _clean_title(line[level:].strip())
            if level >= 2:
                if current_title or current_lines:
                    sections.append((current_title, current_lines))
                current_title = _clean_title(line[level:].strip())
                current_lines = []
            elif level == 1:
                # 罕见：文件内多个 # 标题，忽略
                continue
            else:
                current_lines.append(line)
        if current_title or current_lines:
            sections.append((current_title, current_lines))

        topic = file_title or path.stem
        for title, body_lines in sections:
            body = _clean_text("\n".join(body_lines))
            if not body:
                continue
            docs.append(
                KBDoc(
                    kind="chapter",
                    title=title or topic,
                    body=body,
                    source=path.name,
                    topic=topic,
                    section=title or topic,
                )
            )
    return docs


def _load_terms(seed_dir: Path) -> list[KBDoc]:
    """读取 seed/terms.json 名词卡。"""
    path = seed_dir / "terms.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("terms", raw) if isinstance(raw, dict) else raw
    docs: list[KBDoc] = []
    for item in items:
        term = str(item.get("term", "")).strip()
        if not term:
            continue
        definition = str(item.get("definition", "")).strip()
        analogy = str(item.get("analogy", "")).strip()
        pitfalls = str(item.get("pitfalls", "")).strip()
        body = " ".join(part for part in (definition, analogy, pitfalls) if part)
        if not body:
            body = definition or term
        docs.append(
            KBDoc(
                kind="term",
                title=term,
                body=body,
                source="terms.json",
                topic=str(item.get("topic", "术语")),
                definition=definition,
                analogy=analogy,
                pitfalls=pitfalls,
            )
        )
    return docs


def _load_links(seed_dir: Path) -> list[KBDoc]:
    """读取 seed/curated_links.json 策展链接。"""
    path = seed_dir / "curated_links.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("links", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    docs: list[KBDoc] = []
    for item in items:
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        if not title or not url:
            continue
        why = str(item.get("why", "")).strip()
        body = " ".join(part for part in (why, " ".join(item.get("tags", []))) if part)
        docs.append(
            KBDoc(
                kind="link",
                title=title,
                body=body or title,
                source="curated_links.json",
                topic=str(item.get("topic", "延伸阅读")),
                url=url,
            )
        )
    return docs


class KBIndex:
    """内存倒排索引：构建一次，多次查询。"""

    def __init__(self, docs: list[KBDoc]):
        self.docs = docs
        #: term -> {doc_idx: 词频}
        self._postings: dict[str, dict[int, int]] = {}
        #: 标题词表：标题里的词额外加权
        self._title_terms: list[set[str]] = []
        self._build(docs)

    def _build(self, docs: list[KBDoc]) -> None:
        for idx, doc in enumerate(docs):
            title_terms = set(tokenize(doc.title))
            self._title_terms.append(title_terms)
            counted: set[str] = set()
            for term in tokenize(doc.title + " " + doc.body):
                if term in counted:
                    continue
                counted.add(term)
                postings = self._postings.setdefault(term, {})
                postings[idx] = postings.get(idx, 0) + 1

    # ── 查询 ───────────────────────────────────────────────

    def search(self, query: str, limit: int = 8) -> list[KBHit]:
        ranked, matched_terms = self._rank(query, limit)
        return [
            KBHit(
                kind=self.docs[idx].kind,
                title=self.docs[idx].title,
                source=self.docs[idx].source,
                topic=self.docs[idx].topic,
                url=self.docs[idx].url,
                snippet=self._snippet(self.docs[idx].body, matched_terms),
                score=score,
            )
            for idx, score in ranked
        ]

    def answer(self, query: str, limit: int = 8) -> dict:
        """本地零 Token 问答结果：把用户句子里命中的名词解释 + 一条最相关开源链接整理好。

        首页在「未接入 API」时使用：先给出知识库里的名词解释，再给一条最相关的
        GitHub 开源项目，最后附上书中相关章节，全部带出处，便于核对。
        """
        query_terms = set(tokenize(query))
        term_items = self._term_matches(query_terms, limit=3)
        # 章节按整体相关性排序（名词卡与链接单独挑，避免被章节的高分挤掉）
        ranked, matched_terms = self._rank(query, max(limit * 3, 12))
        chapters = [
            KBHit(
                kind=self.docs[idx].kind,
                title=self.docs[idx].title,
                source=self.docs[idx].source,
                topic=self.docs[idx].topic,
                url=self.docs[idx].url,
                snippet=self._snippet(self.docs[idx].body, matched_terms),
                score=score,
            ).to_dict()
            for idx, score in ranked
            if self.docs[idx].kind == "chapter"
        ][:limit]
        link_doc = self._best_link(query_terms)
        link = (
            {
                "kind": "link",
                "title": link_doc.title,
                "topic": link_doc.topic,
                "url": link_doc.url,
                "why": link_doc.body,
                "source": link_doc.source,
            }
            if link_doc
            else None
        )
        return {
            "query": query,
            "terms": term_items,
            "chapters": chapters,
            "link": link,
            "total": len(term_items) + len(chapters) + (1 if link else 0),
        }

    def _term_matches(self, query_terms: set[str], limit: int = 3) -> list[dict]:
        """从用户输入句子里找出知识库已有的名词，返回结构化词条卡片。

        只比对名词卡标题里的词：命中拉丁专有名词（如 agent、rag、token）权重更高，
        中文词按命中个数累计。这样「什么是 Agent」会先给出 Agent 词条，而不是被
        正文里到处出现的章节挤掉。
        """
        scored: list[tuple[int, float]] = []
        for idx, doc in enumerate(self.docs):
            if doc.kind != "term":
                continue
            overlap = query_terms & self._title_terms[idx]
            if not overlap:
                continue
            weight = sum(2.0 if token.isascii() else 1.0 for token in overlap)
            scored.append((idx, weight))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        cards: list[dict] = []
        for idx, weight in scored[:limit]:
            doc = self.docs[idx]
            cards.append(
                {
                    "term": doc.title,
                    "topic": doc.topic,
                    "definition": doc.definition,
                    "analogy": doc.analogy,
                    "pitfalls": doc.pitfalls,
                    "source": doc.source,
                    "score": round(weight, 2),
                }
            )
        return cards

    def _best_link(self, query_terms: set[str]):
        """挑一条与查询最相关的开源项目：标题命中 > 所属主题命中 > 说明命中。"""
        best_doc: KBDoc | None = None
        best_score = 0.0
        for idx, doc in enumerate(self.docs):
            if doc.kind != "link":
                continue
            score = (
                3.0 * len(query_terms & self._title_terms[idx])
                + 2.5 * len(query_terms & set(tokenize(doc.topic)))
                + 1.0 * len(query_terms & set(tokenize(doc.body)))
            )
            if score > best_score:
                best_doc, best_score = doc, score
        return best_doc

    def _rank(self, query: str, limit: int) -> tuple[list[tuple[int, float]], set[str]]:
        """BM25 近似的 TF-IDF 打分，返回 [(文档下标, 分数)] 与命中词集合。"""
        terms = tokenize(query)
        if not terms:
            return [], set()
        n_docs = max(len(self.docs), 1)
        scores: dict[int, float] = {}
        matched_terms: set[str] = set()
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            matched_terms.add(term)
            idf = math.log((n_docs + 1) / (len(postings) + 1)) + 1.0
            for doc_idx, tf in postings.items():
                tf_weight = 1.0 + math.log(1.0 + tf)
                if term in self._title_terms[doc_idx]:
                    tf_weight *= 2.5  # 标题命中加权
                scores[doc_idx] = scores.get(doc_idx, 0.0) + tf_weight * idf
        if not scores:
            return [], matched_terms
        return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit], matched_terms

    def overview(self) -> dict:
        """首页浏览所需信息：章节目录 + 术语/链接主题。"""
        chapters: dict[str, dict] = {}
        topics: dict[str, set[str]] = {"term": set(), "link": set()}
        for doc in self.docs:
            if doc.kind == "chapter":
                chapter = chapters.setdefault(doc.topic, {"file": doc.source, "title": doc.topic, "sections": []})
                if doc.section and doc.section not in chapter["sections"]:
                    chapter["sections"].append(doc.section)
            elif doc.kind in topics:
                topics[doc.kind].add(doc.topic)
        return {
            "chapters": [
                {"file": data["file"], "title": data["title"], "sections": data["sections"]}
                for data in sorted(chapters.values(), key=lambda d: d["file"])
            ],
            "terms_topics": sorted(topics["term"]),
            "links_topics": sorted(topics["link"]),
        }

    @staticmethod
    def _snippet(body: str, matched_terms: set[str], width: int = 90) -> str:
        """截取第一个命中词附近的文本作为摘要。"""
        lowered = body.lower()
        pos = -1
        for term in matched_terms:
            found = lowered.find(term.lower())
            if found != -1 and (pos == -1 or found < pos):
                pos = found
        if pos == -1:
            return body[:width] + ("…" if len(body) > width else "")
        start = max(0, pos - width // 2)
        end = min(len(body), start + width)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(body) else ""
        return prefix + body[start:end].strip() + suffix


def build_kb_index(kb_dir: Path, seed_dir: Path) -> KBIndex:
    """构建完整索引：章节 + 术语 + 策展链接。"""
    docs = parse_chapters(kb_dir)
    docs.extend(_load_terms(seed_dir))
    docs.extend(_load_links(seed_dir))
    return KBIndex(docs)