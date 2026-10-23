"""strip_urls：模型输出里的任何 URL 一律剔除（防幻觉链接）。"""

from __future__ import annotations

from app.services.links import strip_urls


def test_bare_url_removed() -> None:
    out = strip_urls("参考 https://evil.example.com/a 的文档")
    assert "http" not in out
    assert "参考" in out and "的文档" in out


def test_markdown_link_keeps_text() -> None:
    assert strip_urls("见[官方文档](https://github.com/x/y)") == "见官方文档"


def test_multiple_urls_removed() -> None:
    src = "A https://a.io/x 且 [B](http://b.io/y)、C https://c.io/z"
    out = strip_urls(src)
    assert "http" not in out
    assert "A" in out and "B" in out and "C" in out


def test_fullwidth_punctuation_stops_url() -> None:
    out = strip_urls("链接 https://github.com/a/b）结尾")
    assert "http" not in out
    assert "结尾" in out


def test_no_url_unchanged() -> None:
    text = "今天学习 RAG 与向量数据库"
    assert strip_urls(text) == text