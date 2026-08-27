"""独立 sudoers 规则写入、校验和恢复测试。"""

import os
from pathlib import Path

import pytest

from lab_ops_panel.infrastructure.sudo_rules import (
    MANAGED_MARKER,
    apply_sudo_grant,
    revoke_sudo_grant,
)


def test_sudo_grant_is_independent_validated_and_idempotent(tmp_path: Path) -> None:
    """授权只写独立文件，重复调用不重复修改。"""

    sudoers = tmp_path / "sudoers"
    sudoers.write_text("root ALL=(ALL:ALL) ALL\n", encoding="utf-8")
    directory = tmp_path / "sudoers.d"
    directory.mkdir(mode=0o700)
    validated = []

    def validator(path: Path) -> None:
        validated.append(path)

    arguments = {
        "username": "alice",
        "uid": 1000,
        "sudoers_path": sudoers,
        "sudoers_directory": directory,
        "validator": validator,
        "owner_uid": os.geteuid(),
        "owner_gid": os.getegid(),
        "require_root": False,
    }
    first = apply_sudo_grant(**arguments)
    second = apply_sudo_grant(**arguments)

    rule = directory / "labopspanel-uid-1000"
    assert first["changed"] is True
    assert second["changed"] is False
    assert rule.read_text(encoding="utf-8").startswith(MANAGED_MARKER)
    assert sudoers.read_text(encoding="utf-8") == "root ALL=(ALL:ALL) ALL\n"
    assert sudoers in validated


def test_sudo_grant_restores_previous_rule_when_full_validation_fails(tmp_path: Path) -> None:
    """替换后全局 visudo 失败必须恢复原规则。"""

    sudoers = tmp_path / "sudoers"
    sudoers.write_text("root ALL=(ALL:ALL) ALL\n", encoding="utf-8")
    directory = tmp_path / "sudoers.d"
    directory.mkdir(mode=0o700)
    rule = directory / "labopspanel-uid-1000"
    previous = f"{MANAGED_MARKER}\n# UID: 1000\nalice ALL=(ALL:ALL) /usr/bin/id\n"
    rule.write_text(previous, encoding="utf-8")
    rule.chmod(0o440)
    calls = 0

    def validator(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("invalid")

    with pytest.raises(ValueError, match="invalid"):
        apply_sudo_grant(
            "alice",
            1000,
            sudoers,
            directory,
            validator,
            os.geteuid(),
            os.getegid(),
            False,
        )
    assert rule.read_text(encoding="utf-8") == previous


def test_sudo_revoke_refuses_unmanaged_rule(tmp_path: Path) -> None:
    """撤销接口不能删除手工 sudoers 规则。"""

    sudoers = tmp_path / "sudoers"
    sudoers.write_text("root ALL=(ALL:ALL) ALL\n", encoding="utf-8")
    directory = tmp_path / "sudoers.d"
    directory.mkdir(mode=0o700)
    rule = directory / "labopspanel-uid-1000"
    rule.write_text("alice ALL=(ALL:ALL) ALL\n", encoding="utf-8")
    rule.chmod(0o440)

    with pytest.raises(ValueError, match="拒绝撤销"):
        revoke_sudo_grant(
            "alice",
            1000,
            sudoers,
            directory,
            lambda path: None,
            os.geteuid(),
            os.getegid(),
            False,
        )
    assert rule.exists()
