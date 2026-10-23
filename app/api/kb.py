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


@router.get("/overview")
def kb_overview(request: Request) -> dict:
    """首页浏览所需信息：章节目录 + 术语/链接主题。"""
    index = _index(request)
    if index is None:
        return {"chapters": [], "terms_topics": [], "links_topics": []}
    return index.overview()