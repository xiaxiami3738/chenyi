"""HTTP 元信息接口：健康检查、供应商预设、策展链接。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request

from .. import __version__
from ..llm.providers import list_presets
from ..services import links_for_topic, topics_with_links
from ..services.vault import vault_available

router = APIRouter(prefix="/api", tags=["meta"])


def _open_session(request: Request):
    return request.app.state.session_factory()


@router.get("/healthz")
def healthz(request: Request) -> dict:
    """部署探针：Nginx / Docker HEALTHCHECK / CI 都用它。"""
    settings = request.app.state.settings
    return {
        "status": "ok",
        "mode": settings.app_mode,
        "demo": settings.demo,
        "version": __version__,
        "time": datetime.now(UTC).isoformat(),
        "features": {
            "byok": True,
            "curated_links": True,
            "anonymous_client_id": True,
            "kb": True,
            "chat": True,
            "vault": vault_available(settings),
        },
    }


@router.get("/providers")
def providers() -> dict:
    """返回可选的模型供应商预设（不含任何 Key）。"""
    return {
        "providers": list_presets(),
        "usage": (
            "访客自带 Key：默认 Key 只存在浏览器 localStorage，"
            "并随请求头 X-LLM-Key 单次透传，服务端不落库、不写日志。"
            "可选：在真实模式（HTTPS）下你也可以勾选「服务端加密代存」，"
            "Key 会用服务器主密钥加密保存，保存后才会调用你自己的模型接口来回答，之后可免填生成。"
        ),
    }


@router.get("/links")
def links(
    request: Request,
    topic: str | None = Query(default=None, description="主题过滤，留空返回全部"),
    limit: int = Query(default=12, ge=1, le=50),
) -> dict:
    """人工策展的 GitHub 链接。模型永远不会产出 URL，只能按主题取这里。"""
    session = _open_session(request)
    try:
        rows = links_for_topic(session, topic, limit)
        return {
            "topic": topic,
            "topics": topics_with_links(session),
            "links": [
                {
                    "id": row.id,
                    "topic": row.topic,
                    "title": row.title,
                    "url": row.url,
                    "why": row.why,
                    "tags": row.tags,
                    "last_checked_at": row.last_checked_at.isoformat()
                    if row.last_checked_at
                    else None,
                }
                for row in rows
            ],
        }
    finally:
        session.close()