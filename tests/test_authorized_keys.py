"""authorized_keys 原子写入和路径边界测试。"""

import base64
import os
import stat
import struct
from pathlib import Path

import pytest

from lab_ops_panel.domain.ssh_keys import parse_public_key
from lab_ops_panel.infrastructure.authorized_keys import import_authorized_keys


def _key() -> str:
    key_type = "ssh-ed25519"
    blob = struct.pack(">I", len(key_type)) + key_type.encode() + b"test-material"
    return f"{key_type} {base64.b64encode(blob).decode()} test"


def _acl(path: Path) -> str:
    return "user::rwx\ngroup::---\nother::---\n"


def test_authorized_keys_is_atomic_private_and_idempotent(tmp_path: Path) -> None:
    """导入结果权限正确，重复指纹不重复写入。"""

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    uid, gid = os.geteuid(), os.getegid()
    key = parse_public_key(_key())

    first = import_authorized_keys(home, uid, gid, [key], _acl, require_root=False)
    second = import_authorized_keys(home, uid, gid, [key], _acl, require_root=False)

    ssh_directory = home / ".ssh"
    authorized_keys = ssh_directory / "authorized_keys"
    assert first["added"] == 1
    assert second == {
        "added": 0,
        "duplicates": 1,
        "fingerprints": [],
        "path": str(authorized_keys),
    }
    assert authorized_keys.read_text(encoding="utf-8").splitlines() == [_key()]
    assert stat.S_IMODE(ssh_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(authorized_keys.stat().st_mode) == 0o600


def test_authorized_keys_rejects_ssh_directory_symlink(tmp_path: Path) -> None:
    """`.ssh` 符号链接不能把写入引向 home 之外。"""

    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir(mode=0o700)
    outside.mkdir()
    (home / ".ssh").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="非符号链接目录"):
        import_authorized_keys(
            home,
            os.geteuid(),
            os.getegid(),
            [parse_public_key(_key())],
            _acl,
            require_root=False,
        )
    assert list(outside.iterdir()) == []


def test_authorized_keys_rejects_extra_acl_write(tmp_path: Path) -> None:
    """命名用户额外写 ACL 必须阻断导入。"""

    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="额外授予"):
        import_authorized_keys(
            home,
            os.geteuid(),
            os.getegid(),
            [parse_public_key(_key())],
            lambda path: "user::rwx\nuser:9999:rwx\ngroup::---\nother::---\n",
            require_root=False,
        )
