"""PAM pwquality 策略安全修改与恢复测试。"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lab_ops_panel.infrastructure.pam import pam_policy_snapshot
from lab_ops_panel.infrastructure.pam_rules import (
    apply_policy_changes,
    render_policy_update,
    restore_policy_backup,
)


def _environment(tmp_path: Path) -> tuple[Path, Path, Callable[[], dict[str, Any]]]:
    common = tmp_path / "common-password"
    common.write_text(
        "password requisite pam_pwquality.so retry=3\n"
        "password [success=1 default=ignore] pam_unix.so obscure yescrypt\n",
        encoding="utf-8",
    )
    config = tmp_path / "security/pwquality.conf"
    config.parent.mkdir()
    config.parent.chmod(0o755)
    config.write_text("# 原配置\nminlen = 15\nusercheck = 1\n", encoding="utf-8")
    config.chmod(0o644)
    drop_ins = tmp_path / "security/pwquality.conf.d"
    drop_ins.mkdir()
    module = tmp_path / "usr/lib/security/pam_pwquality.so"
    module.parent.mkdir(parents=True)
    module.touch()

    def snapshot():  # type: ignore[no-untyped-def]
        return pam_policy_snapshot(common, config, drop_ins, str(module))

    return config, tmp_path / "backups", snapshot


def test_policy_update_preserves_comments_and_rejects_unknown_lines() -> None:
    """只改写已知唯一键，保留注释并拒绝不能理解的手工配置。"""

    rendered = render_policy_update(
        "# comment\nminlen = 15  # old\n", {"minlen": 20, "maxrepeat": 3}
    )

    assert "# comment" in rendered
    assert "minlen = 20  # old" in rendered
    assert "maxrepeat = 3" in rendered
    with pytest.raises(ValueError, match="未知设置"):
        render_policy_update("future_rule = 1\n", {"minlen": 20})
    with pytest.raises(ValueError, match="重复"):
        render_policy_update("minlen=15\nminlen=16\n", {"minlen": 20})


def test_apply_and_restore_policy_with_verified_backup(tmp_path: Path) -> None:
    """修改后核对实际值，并可在配置未被再次改动时恢复原文件。"""

    config, backups, snapshot = _environment(tmp_path)
    original = config.read_bytes()

    result = apply_policy_changes(
        {"minlen": 20, "ucredit": -1, "maxrepeat": 3},
        config,
        backups,
        snapshot,
        owner_uid=os.getuid(),
        require_root=False,
    )

    assert result["changed"] is True
    assert snapshot()["settings"]["minlen"] == 20
    assert (backups / f"{result['backup_id']}.conf").read_bytes() == original
    restored = restore_policy_backup(
        result["backup_id"],
        config,
        backups,
        snapshot,
        owner_uid=os.getuid(),
        require_root=False,
    )
    assert restored["restored"] is True
    assert config.read_bytes() == original


def test_apply_rolls_back_when_effective_value_does_not_match(tmp_path: Path) -> None:
    """写入后的实际 PAM 值不匹配时自动恢复，并清理失败备份。"""

    config, backups, snapshot = _environment(tmp_path)
    original = config.read_bytes()
    before = snapshot()

    with pytest.raises(RuntimeError, match="验证失败"):
        apply_policy_changes(
            {"minlen": 20},
            config,
            backups,
            lambda: before,
            owner_uid=os.getuid(),
            require_root=False,
        )

    assert config.read_bytes() == original
    assert not list(backups.iterdir())


def test_restore_refuses_to_overwrite_later_manual_change(tmp_path: Path) -> None:
    """应用后发生人工修改时，旧备份不能覆盖新配置。"""

    config, backups, snapshot = _environment(tmp_path)
    result = apply_policy_changes(
        {"minlen": 20},
        config,
        backups,
        snapshot,
        owner_uid=os.getuid(),
        require_root=False,
    )
    config.write_text("minlen = 24\n", encoding="utf-8")

    with pytest.raises(ValueError, match="已变化"):
        restore_policy_backup(
            result["backup_id"],
            config,
            backups,
            snapshot,
            owner_uid=os.getuid(),
            require_root=False,
        )

    assert config.read_text(encoding="utf-8") == "minlen = 24\n"


def test_module_option_conflict_blocks_file_update(tmp_path: Path) -> None:
    """模块行参数优先级更高时，不能写入一个看似成功但不生效的主配置。"""

    config, backups, snapshot = _environment(tmp_path)
    common = tmp_path / "common-password"
    common.write_text("password requisite pam_pwquality.so minlen=24\n", encoding="utf-8")

    with pytest.raises(ValueError, match="模块参数覆盖"):
        apply_policy_changes(
            {"minlen": 20},
            config,
            backups,
            snapshot,
            owner_uid=os.getuid(),
            require_root=False,
        )
