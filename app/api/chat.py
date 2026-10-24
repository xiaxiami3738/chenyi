"""角色对话接口：和「鲸小鱼」聊天。

设计要点：
- Demo / 未接入模型：返回站内预设台词（deterministic），零 Token、可测试；
- 真实模式：必须携带 Key（请求头 X-LLM-Key，或已加密代存的 Key），
  用访客自己的模型额度，服务端只在单次请求内使用，不落库、不写日志。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..agents.errors import PipelineError
from ..llm.client import LLMClientError
from ..llm.schemas import ChatMessage
from ..services.persona import PERSONA_ID, build_system_prompt, scripted_reply
from ..services.pipeline import build_llm
from ..services.vault import resolve_key

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    """一次单轮对话请求（历史由前端自己保留，服务端不存）。"""

    client_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1000)
    provider_id: str | None = Field(default=None)
    base_url: str | None = Field(default=None)
    model: str | None = Field(default=None)
    use_saved_key: bool = Field(default=False)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


@router.post("/chat", response_model=None)
async def chat(body: ChatRequest, request: Request) -> dict | JSONResponse:
    """和鲸小鱼说一句话，拿回她的回答。"""
    settings = request.app.state.settings

    # 本地模式：用预设台词，保证第一次来的人也能聊上两句，且不花钱。
    if settings.demo:
        return {
            "reply": scripted_reply(body.message),
            "mode": "demo",
            "scripted": True,
            "persona": PERSONA_ID,
        }

    api_key = (request.headers.get("x-llm-key") or "").strip() or None

    # 允许使用服务端加密代存的 Key（需要先选择供应商或填自定义 Base URL 作为 scope）。
    if body.use_saved_key:
        scope = body.provider_id or (body.base_url or "").strip() or None
        if scope is None:
            return _error(
                400,
                "saved_key_scope_missing",
                "使用服务器代存 Key 时需要先选择供应商（或填写自定义 Base URL）。",
            )
        session = request.app.state.session_factory()
        try:
            resolved, vault_error = resolve_key(
                settings,
                session,
                client_id=body.client_id,
                scope=scope,
                header_key=None,
            )
        finally:
            session.close()
        if vault_error:
            return _error(
                vault_error["status_code"], vault_error["code"], vault_error["message"]
            )
        if resolved:
            api_key = resolved

    try:
        llm = build_llm(
            settings,
            provider_id=body.provider_id,
            base_url=body.base_url,
            model=body.model,
            api_key=api_key,
        )
    except PipelineError as exc:
        return _error(exc.status_code, exc.error_code, exc.message)

    messages = [
        ChatMessage.system(build_system_prompt()),
        ChatMessage.user(body.message),
    ]
    try:
        #: 聊天气泡不需要长回复，600 tokens 足够，也省访客的钱。
        reply = await llm.chat(messages, max_tokens=600)
    except LLMClientError as exc:
        code = getattr(exc, "error_code", None) or "upstream_error"
        return _error(502, code, exc.message)

    reply = (reply or "").strip()
    if not reply:
        return _error(502, "empty_reply", "模型这次没有返回内容，请再试一次。")
    return {
        "reply": reply[:1000],
        "mode": "live",
        "scripted": False,
        "persona": PERSONA_ID,
    }