"""可选的服务端加密代存（Key Custody）。

设计要点（M7.3）：
- 纯浏览器 BYOK 仍是默认：Key 只存在访客 localStorage、随请求头 X-LLM-Key 单次透传；
- 本模块提供**可选项**：访客明确勾选后，把 Key 用服务器主密钥（Fernet）加密成密文保存，
  之后生成学习路径时无需再重复填写 Key，服务端会用保存的密文解密后转发给访客自己的模型服务商；
- 主密钥只从环境变量 ``VAULT_MASTER_KEY`` 读取，绝不进入 Settings / 代码 / 数据库；
- 落库的只有 Fernet 密文（``cipher_blob``），任何时候都不以明文写库、不打印日志；
- 前置条件：真实模式（demo 模式 FakeLLM 不调用真实模型，代存没有意义）+ HTTPS；
- 唯一例外表 ``vault_entries`` 是刻意设计的：仅存放密文 blob，列名刻意避开任何
  key/secret/credential 字样的命名（见 tests/unit/test_no_key_storage.py 的显式开白）。
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select

from ..agents.errors import PipelineError
from ..models.tables import VaultEntry

#: 主密钥环境变量名（只读环境变量，不进 Settings/配置）
MASTER_KEY_ENV = "VAULT_MASTER_KEY"


class VaultError(PipelineError):
    """代存模块自身可预期的失败（未配置 / 非 HTTPS / Demo 模式等）。"""


def _master_key() -> str | None:
    """读取主密钥；未配置或空白返回 None（代存功能关闭）。"""
    raw = os.environ.get(MASTER_KEY_ENV, "").strip()
    return raw or None


def _fernet() -> Fernet | None:
    """由主密钥派生 Fernet 密钥；未配置则返回 None。"""
    master = _master_key()
    if master is None:
        return None
    digest = hashlib.sha256(master.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def vault_available(settings) -> bool:
    """代存是否可用：真实模式且主密钥已配置（HTTPS 在请求层单独校验）。"""
    return not settings.demo and _fernet() is not None


def encrypt_secret(plain: str) -> str:
    f = _fernet()
    if f is None:
        raise VaultError(
            "服务器未配置主密钥（VAULT_MASTER_KEY），代存功能未启用。",
            status_code=503,
            error_code="vault_not_configured",
        )
    return f.encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    f = _fernet()
    if f is None:
        raise VaultError(
            "服务器未配置主密钥（VAULT_MASTER_KEY），代存功能未启用。",
            status_code=503,
            error_code="vault_not_configured",
        )
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:  # 密文被篡改或主密钥更换
        raise VaultError(
            "无法解密代存的 Key（密文可能已损坏或主密钥已更换），请重新保存。",
            status_code=400,
            error_code="vault_decrypt_failed",
        ) from exc


def save_secret(session, client_id: str, scope: str, api_key: str) -> VaultEntry:
    """保存（同 client_id+scope 重复保存即更新密文）。"""
    token = encrypt_secret(api_key)
    entry = session.scalar(
        select(VaultEntry).where(
            VaultEntry.client_id == client_id, VaultEntry.scope == scope
        )
    )
    now = datetime.now(UTC)
    if entry is None:
        entry = VaultEntry(
            client_id=client_id,
            scope=scope,
            cipher_blob=token,
            created_at=now,
            updated_at=now,
        )
        session.add(entry)
    else:
        entry.cipher_blob = token
        entry.updated_at = now
    session.flush()
    return entry


def get_secret(session, client_id: str, scope: str) -> str | None:
    """取回解密后的明文 Key；未保存过返回 None。"""
    entry = session.scalar(
        select(VaultEntry).where(
            VaultEntry.client_id == client_id, VaultEntry.scope == scope
        )
    )
    if entry is None:
        return None
    return decrypt_secret(entry.cipher_blob)


def delete_secret(session, client_id: str, scope: str) -> bool:
    result = session.execute(
        delete(VaultEntry).where(
            VaultEntry.client_id == client_id, VaultEntry.scope == scope
        )
    )
    session.flush()
    return result.rowcount > 0


def list_scopes(session, client_id: str) -> list[str]:
    rows = session.scalars(
        select(VaultEntry.scope)
        .where(VaultEntry.client_id == client_id)
        .order_by(VaultEntry.scope)
    ).all()
    return list(rows)


def resolve_key(
    settings, session, client_id: str, scope: str | None, header_key: str | None
) -> tuple[str | None, dict | None]:
    """生成会话时的 Key 解析：请求头优先，其次才用代存。

    返回 ``(key, error)``，error 为可直接响应的错误信封（None 表示继续）。
    - 请求头 X-LLM-Key 存在 → 直接用它（行为与 M6 完全一致）；
    - 无请求头且 scope 为空 → 不代存，走原有 missing_key 逻辑（error=None）；
    - 无请求头、scope 有值 → 尝试代存：未保存过 → no_saved_key；解密失败 → 透传。
    """
    if header_key:
        return header_key, None
    if not scope:
        return None, None
    if settings.demo:
        # Demo 模式 FakeLLM 忽略一切 Key；代存本身在 demo 下也不可用
        return None, None
    try:
        saved = get_secret(session, client_id, scope)
    except VaultError as exc:
        return None, {"status_code": exc.status_code, "code": exc.error_code, "message": exc.message}
    if saved is None:
        return None, {
            "status_code": 400,
            "code": "no_saved_key",
            "message": (
                f"还没有为「{scope}」代存 Key。请先在「服务端加密代存」里保存你的 API Key，"
                "保存后才会调用你自己的模型接口来回答，之后可免填生成。"
            ),
        }
    return saved, None