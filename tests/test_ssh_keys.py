"""SSH 公钥解析与去重测试。"""

import base64
import struct

import pytest

from lab_ops_panel.domain.ssh_keys import parse_public_key, preview_public_keys


def _key(key_type: str = "ssh-ed25519", comment: str = "test@example") -> str:
    blob = struct.pack(">I", len(key_type)) + key_type.encode() + b"test-key-material"
    return f"{key_type} {base64.b64encode(blob).decode()} {comment}"


def test_public_key_has_openssh_sha256_fingerprint() -> None:
    """合法 Ed25519 公钥返回 SHA256 指纹和标准行。"""

    key = parse_public_key(_key())
    assert key.key_type == "ssh-ed25519"
    assert key.fingerprint.startswith("SHA256:")
    assert key.normalized() == _key()


def test_public_key_rejects_authorized_keys_options() -> None:
    """默认拒绝 command 等 authorized_keys 选项。"""

    with pytest.raises(ValueError, match="选项"):
        parse_public_key(f'command="id" {_key()}')


def test_public_key_rejects_private_key() -> None:
    """私钥材料不能进入预览或日志。"""

    with pytest.raises(ValueError, match="私钥"):
        parse_public_key("-----BEGIN OPENSSH PRIVATE KEY-----")


def test_public_key_rejects_mismatched_blob_type() -> None:
    """文本声明类型必须与二进制 blob 类型一致。"""

    with pytest.raises(ValueError, match="不一致"):
        parse_public_key(_key().replace("ssh-ed25519", "ssh-rsa", 1))


def test_public_key_preview_deduplicates_fingerprints() -> None:
    """同一批次的重复指纹只标记第一条为有效。"""

    value = _key()
    preview = preview_public_keys(f"{value}\n{value}\ninvalid")
    assert preview["counts"] == {"valid": 1, "duplicate": 1, "invalid": 1}
    assert preview["rows"][1]["duplicate"] is True
    assert "encoded" not in preview["rows"][0]
