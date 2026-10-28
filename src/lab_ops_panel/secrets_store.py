"""Windows DPAPI 凭据存储。

明文密码绝不会进入数据库、日志或 API 响应。DPAPI 使用当前 Windows 用户
的凭据加密，因此数据库文件被复制到其他账户下也无法解密。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

from lab_ops_panel.models import ServerSecret

CRYPTPROTECT_UI_FORBIDDEN = 0x1
CIPHER_VERSION = 1


class SecretBackendError(RuntimeError):
    """凭据后端不可用或密文损坏。"""


class SecretProtector(Protocol):
    """可替换的加密后端，测试使用内存假实现。"""

    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))


if sys.platform == "win32":  # pragma: no cover - 仅在 Windows 上执行
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    _kernel32.LocalFree.restype = wintypes.HLOCAL


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


class DpapiProtector:
    """Windows 用户级 DPAPI 加密（不使用可选熵，便于同账户迁移路径）。"""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecretBackendError("DPAPI 仅在 Windows 上可用")

    def protect(self, plaintext: bytes) -> bytes:
        if not plaintext:
            raise SecretBackendError("不能加密空内容")
        data_in, _buffer = _blob_from_bytes(plaintext)
        data_out = _DataBlob()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(data_in),
            "LabOpsPanel sudo",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(data_out),
        )
        if not ok:
            raise SecretBackendError(f"DPAPI 加密失败：{ctypes.WinError(ctypes.get_last_error())}")
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(data_out.pbData, wintypes.HLOCAL))

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext:
            raise SecretBackendError("密文为空")
        data_in, _buffer = _blob_from_bytes(ciphertext)
        data_out = _DataBlob()
        description = wintypes.LPWSTR()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(data_in),
            ctypes.byref(description),
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(data_out),
        )
        if not ok:
            raise SecretBackendError(
                f"DPAPI 解密失败（密文可能来自其他 Windows 账户）："
                f"{ctypes.WinError(ctypes.get_last_error())}"
            )
        try:
            if description:
                _kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(data_out.pbData, wintypes.HLOCAL))


def default_protector() -> SecretProtector:
    if sys.platform != "win32":
        raise SecretBackendError("当前平台不支持 DPAPI；LabOpsPanel 需要 Windows 凭据保护")
    if os.environ.get("LABOPS_SECRET_BACKEND", "").lower() == "disabled":
        raise SecretBackendError("凭据后端已被配置禁用")
    return DpapiProtector()


class CredentialStore:
    """sudo 密码的读写门面。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        protector: SecretProtector | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._protector = protector
        self._protector_error: str | None = None
        if protector is None:
            try:
                self._protector = default_protector()
            except SecretBackendError as exc:
                self._protector_error = str(exc)

    @property
    def available(self) -> bool:
        return self._protector is not None

    @property
    def unavailable_reason(self) -> str | None:
        return self._protector_error

    def _require_protector(self) -> SecretProtector:
        if self._protector is None:
            raise SecretBackendError(self._protector_error or "凭据后端不可用")
        return self._protector

    def set_password(self, server_id: int, password: str) -> datetime:
        if not password:
            raise ValueError("密码不能为空")
        protector = self._require_protector()
        ciphertext = protector.protect(password.encode("utf-8"))
        now = datetime.now(UTC)
        session = self._session_factory()
        try:
            secret = session.get(ServerSecret, server_id)
            if secret is None:
                secret = ServerSecret(
                    server_id=server_id,
                    ciphertext=ciphertext,
                    cipher_version=CIPHER_VERSION,
                    updated_at=now,
                )
                session.add(secret)
            else:
                secret.ciphertext = ciphertext
                secret.cipher_version = CIPHER_VERSION
                secret.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return now

    def clear_password(self, server_id: int) -> bool:
        session = self._session_factory()
        try:
            secret = session.get(ServerSecret, server_id)
            if secret is None:
                return False
            session.delete(secret)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def has_password(self, server_id: int) -> bool:
        session = self._session_factory()
        try:
            return session.get(ServerSecret, server_id) is not None
        finally:
            session.close()

    def secret_metadata(self, server_id: int) -> tuple[bool, datetime | None]:
        session = self._session_factory()
        try:
            secret = session.get(ServerSecret, server_id)
            if secret is None:
                return False, None
            return True, secret.updated_at
        finally:
            session.close()

    def get_password(self, server_id: int) -> str | None:
        session = self._session_factory()
        try:
            secret = session.get(ServerSecret, server_id)
            if secret is None:
                return None
            ciphertext = secret.ciphertext
            version = secret.cipher_version
        finally:
            session.close()
        if version != CIPHER_VERSION:
            raise SecretBackendError(f"不支持的密文版本：{version}")
        protector = self._require_protector()
        try:
            plaintext = protector.unprotect(ciphertext)
        except Exception as exc:
            raise SecretBackendError(f"无法解密保存的 sudo 密码：{exc}") from exc
        return plaintext.decode("utf-8")
