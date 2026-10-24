"""本地知识库接口：零 token 检索 + 浏览概览。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/kb", tags=["kb"])


def _index(request: Request):
    """应用启动时构建好的 KBIndex（不存在时返回 None）。"""
    return getattr(request.app.state, "kb_index", None)


@router.get("/search")
def kb_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="查询词"),
    limit: int = Query(8, ge=1, le=20),
) -> dict:
    """本地全文检索。零 token，返回带出处的结果片段。"""
    index = _index(request)
    query = q.strip()
    if index is None or not query:
        return {"query": query, "total": 0, "results": []}
    results = index.search(query, limit=limit)
    return {
        "query": query,
        "total": len(results),
        "results": [hit.to_dict() for hit in results],
    }


@router.get("/terms")
def kb_terms(
    request: Request,
    topic: str = Query("", max_length=100, description="主题，留空表示全部"),
    limit: int = Query(50, ge=1, le=50),
) -> dict:
    """按主题列出名词卡（本地数据，零 token）。"""
    index = _index(request)
    if index is None:
        return {"topic": topic, "total": 0, "terms": []}
    terms = index.terms(topic.strip(), limit=limit)
    return {"topic": topic, "total": len(terms), "terms": terms}


@router.get("/answer")
def kb_answer(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="用户输入的一句话"),
    limit: int = Query(6, ge=1, le=20),
) -> dict:
    """本地零 Token 问答：名词解释 + 最相关开源项目 + 书中章节（全部带出处）。

    首页在「未接入自己的 API」时使用该接口：把用户句子里命中的名词卡片、
    一条最相关的 GitHub 链接、以及书中相关章节一起返回，回答不消耗任何 token。
    """
    index = _index(request)
    query = q.strip()
    empty = {"query": query, "terms": [], "chapters": [], "link": None, "total": 0}
    if index is None or not query:
        return empty
    return index.answer(query, limit=limit)


@router.get("/overview")
def kb_overview(request: Request) -> dict:
    """首页浏览所需信息：章节目录 + 术语/链接主题。"""
    index = _index(request)
    if index is None:
        return {"chapters": [], "terms_topics": [], "links_topics": []}
    return index.overview()