"""DPAPI 凭据存储测试：只保存密文、错误密文可诊断、平台约束。"""

from __future__ import annotations

import sys

import pytest
from sqlalchemy import select

from lab_ops_panel.models import Server, ServerSecret
from lab_ops_panel.secrets_store import (
    CIPHER_VERSION,
    CredentialStore,
    DpapiProtector,
    SecretBackendError,
    default_protector,
)


@pytest.fixture
def store(container, protector):
    return CredentialStore(container.session_factory, protector)


def _server(container, alias: str = "lab-01") -> int:
    session = container.session_factory()
    try:
        server = Server(alias=alias, hostname="10.0.0.1", port=22, source="ssh_config")
        session.add(server)
        session.commit()
        return server.id
    finally:
        session.close()


def test_password_roundtrip_uses_ciphertext(container, store) -> None:
    server_id = _server(container)

    updated_at = store.set_password(server_id, "s3cret!")
    assert store.has_password(server_id) is True
    assert store.get_password(server_id) == "s3cret!"
    assert store.secret_metadata(server_id) == (True, updated_at)

    session = container.session_factory()
    try:
        row = session.get(ServerSecret, server_id)
        assert row is not None
        assert b"s3cret" not in row.ciphertext
        assert row.cipher_version == CIPHER_VERSION
    finally:
        session.close()


def test_clear_password_removes_secret(store, container) -> None:
    server_id = _server(container)
    store.set_password(server_id, "pw")

    assert store.clear_password(server_id) is True
    assert store.has_password(server_id) is False
    assert store.get_password(server_id) is None
    assert store.clear_password(server_id) is False


def test_corrupted_ciphertext_raises(container, store) -> None:
    server_id = _server(container)
    session = container.session_factory()
    try:
        session.add(ServerSecret(server_id=server_id, ciphertext=b"garbage", cipher_version=1))
        session.commit()
    finally:
        session.close()

    with pytest.raises(SecretBackendError):
        store.get_password(server_id)


def test_unsupported_cipher_version_raises(container, store) -> None:
    server_id = _server(container)
    store.set_password(server_id, "pw")
    session = container.session_factory()
    try:
        row = session.get(ServerSecret, server_id)
        row.cipher_version = 99
        session.commit()
    finally:
        session.close()

    with pytest.raises(SecretBackendError, match="密文版本"):
        store.get_password(server_id)


def test_empty_password_is_rejected(store, container) -> None:
    server_id = _server(container)

    with pytest.raises(ValueError, match="不能为空"):
        store.set_password(server_id, "")


def test_unavailable_backend_reports_reason(container, monkeypatch) -> None:
    def _raise() -> None:
        raise SecretBackendError("DPAPI 仅在 Windows 上可用")

    monkeypatch.setattr("lab_ops_panel.secrets_store.default_protector", _raise)
    unavailable = CredentialStore(container.session_factory)

    assert unavailable.available is False
    assert "DPAPI" in (unavailable.unavailable_reason or "")
    with pytest.raises(SecretBackendError):
        unavailable.set_password(_server(container), "pw")


def test_two_servers_keep_independent_secrets(container, store) -> None:
    first = _server(container, "lab-a")
    second = _server(container, "lab-b")
    store.set_password(first, "one")
    store.set_password(second, "two")

    assert store.get_password(first) == "one"
    assert store.get_password(second) == "two"


def test_secret_rows_are_scoped_to_server(container, store) -> None:
    server_id = _server(container)
    store.set_password(server_id, "pw")

    session = container.session_factory()
    try:
        rows = session.scalars(select(ServerSecret)).all()
        assert [row.server_id for row in rows] == [server_id]
    finally:
        session.close()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows 可用")
def test_dpapi_roundtrip_encrypts_bytes() -> None:
    protector = DpapiProtector()
    plaintext = "pä§§wörd-with-utf8-中文".encode()

    ciphertext = protector.protect(plaintext)
    assert ciphertext != plaintext
    assert plaintext not in ciphertext
    assert protector.unprotect(ciphertext) == plaintext


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows 可用")
def test_dpapi_rejects_tampered_ciphertext() -> None:
    protector = DpapiProtector()
    ciphertext = protector.protect(b"secret")

    with pytest.raises(SecretBackendError):
        protector.unprotect(ciphertext[:-1] + bytes([ciphertext[-1] ^ 0xFF]))


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows 可用")
def test_default_protector_is_dpapi_on_windows() -> None:
    assert isinstance(default_protector(), DpapiProtector)


@pytest.mark.skipif(sys.platform == "win32", reason="仅在非 Windows 验证平台拒绝")
def test_default_protector_rejects_non_windows() -> None:
    with pytest.raises(SecretBackendError):
        default_protector()


def test_secret_updated_at_is_utc(store, container) -> None:
    server_id = _server(container)
    updated_at = store.set_password(server_id, "pw")

    assert updated_at.tzinfo is not None
    assert updated_at.utcoffset() is not None
