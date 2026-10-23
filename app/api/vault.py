"""服务端加密代存接口（M7.3，可选功能）。

安全门控（按用户确认的「HTTPS 为前置条件」）：
- PUT / DELETE 必须走 HTTPS（或受信代理头 X-Forwarded-Proto: https），否则 403；
- Demo 模式不调用真实模型，代存没有意义 → 403 demo_mode；
- 服务器未配置主密钥 VAULT_MASTER_KEY → 503 vault_not_configured；
- GET 只返回「是否可用 + 已保存的 scope 列表」，永不返回 Key 本身。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from ..services import vault
from ..services.vault import VaultError

router = APIRouter(prefix="/api/vault", tags=["vault"])


def _settings(request: Request):
    return request.app.state.settings


def _session_factory(request: Request):
    return request.app.state.session_factory


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def _request_is_https(request: Request) -> bool:
    settings = _settings(request)
    if request.url.scheme == "https":
        return True
    # 受信代理后（nginx 配置了 X-Forwarded-Proto）允许按代理头判断
    if settings.trust_proxy_headers:
        return request.headers.get("x-forwarded-proto", "").lower() == "https"
    return False


def _require_write_allowed(request: Request) -> None:
    """写操作的三道门：真实模式 + 已配置主密钥 + HTTPS。"""
    settings = _settings(request)
    if settings.demo:
        raise VaultError(
            "Demo 模式不调用真实模型，代存 Key 没有意义；站点开启真实模式（APP_MODE=live）后可启用。",
            status_code=403,
            error_code="vault_demo_mode",
        )
    if not vault.vault_available(settings):
        raise VaultError(
            "服务器未配置主密钥（环境变量 VAULT_MASTER_KEY），代存功能未启用。",
            status_code=503,
            error_code="vault_not_configured",
        )
    if not _request_is_https(request):
        raise VaultError(
            "服务端代存 Key 需要 HTTPS 连接（前置条件），请在安全环境或本地开发时通过 HTTPS 访问。",
            status_code=403,
            error_code="vault_https_required",
        )


@router.get("")
def vault_status(request: Request, client_id: str = "") -> dict:
    """查询代存状态：是否可用 + 该访客已保存的 scope 列表（不返回 Key）。"""
    settings = _settings(request)
    reason = None
    if settings.demo:
        reason = "demo_mode"
    elif not vault.vault_available(settings):
        reason = "vault_not_configured"
    enabled = reason is None
    scopes: list[str] = []
    if enabled and client_id:
        with _session_factory(request)() as session:
            scopes = vault.list_scopes(session, client_id)
    return {
        "enabled": enabled,
        "reason": reason,
        "https": _request_is_https(request),
        "scopes": scopes,
    }


@router.put("")
def vault_save(
    request: Request,
    client_id: str = Body(..., min_length=1, max_length=64),
    scope: str = Body(..., min_length=1, max_length=200),
    api_key: str = Body(..., min_length=1, max_length=512),
) -> dict:
    """保存：把当前 Key 用服务器主密钥加密后落库（重复保存即覆盖更新）。"""
    try:
        _require_write_allowed(request)
        with _session_factory(request)() as session:
            vault.save_secret(session, client_id, scope, api_key)
            session.commit()
    except VaultError as exc:
        return _error(exc.status_code, exc.error_code, exc.message)
    return {"saved": True, "scope": scope}


@router.delete("")
def vault_delete(
    request: Request,
    client_id: str = Body(..., min_length=1, max_length=64),
    scope: str = Body(..., min_length=1, max_length=200),
) -> dict:
    """删除已代存的 Key（按 client_id + scope 定位）。"""
    try:
        _require_write_allowed(request)
        with _session_factory(request)() as session:
            removed = vault.delete_secret(session, client_id, scope)
            session.commit()
    except VaultError as exc:
        return _error(exc.status_code, exc.error_code, exc.message)
    return {"deleted": removed, "scope": scope}