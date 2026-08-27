"""一次性初始凭据导出测试。"""

import stat
from pathlib import Path

from lab_ops_panel.application.credentials import (
    delete_initial_credentials,
    export_initial_credentials,
    initial_credentials_path,
)


def test_credentials_export_is_private_and_not_overwritten(tmp_path: Path) -> None:
    """凭据文件权限为 0600，同一任务不能覆盖旧文件。"""

    destination = export_initial_credentials(
        tmp_path, "task123", [("alice", "张三", "temporary-password")]
    )

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert "temporary-password" in destination.read_text(encoding="utf-8")
    try:
        export_initial_credentials(tmp_path, "task123", [("bob", "李四", "another-password")])
    except FileExistsError:
        pass
    else:
        raise AssertionError("重复导出应拒绝覆盖")
    assert "another-password" not in destination.read_text(encoding="utf-8")

    assert initial_credentials_path(tmp_path, "task123") == destination
    delete_initial_credentials(tmp_path, "task123")
    assert not destination.exists()
