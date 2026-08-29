"""home 后台扫描安全规则测试。"""

import os
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lab_ops_panel.database import ScanSnapshotRecord, initialize_database
from lab_ops_panel.infrastructure.scans import (
    HomeScanManager,
    ScanTask,
    UserStorageScanManager,
    _TreeScanner,
)
from lab_ops_panel.infrastructure.tasks import TaskRepository


def _find(node: dict, name: str) -> dict:  # type: ignore[type-arg]
    if node["name"] == name:
        return node
    for child in node["children"]:
        try:
            return _find(child, name)
        except KeyError:
            continue
    raise KeyError(name)


def test_tree_scan_does_not_follow_symlinks_and_deduplicates_hardlinks(tmp_path: Path) -> None:
    """符号链接不越界，硬链接仅计算一次实际块占用。"""

    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.write_text("sensitive", encoding="utf-8")
    original = home / "data.bin"
    original.write_bytes(b"x" * 1024)
    os.link(original, home / "data-hardlink.bin")
    (home / "outside-link").symlink_to(outside)
    task = ScanTask("task", "key", "alice", home, max_depth=4, max_nodes=100)

    result = _TreeScanner(task).scan()

    link = _find(result["tree"], "outside-link")
    first = _find(result["tree"], "data.bin")
    second = _find(result["tree"], "data-hardlink.bin")
    assert link["type"] == "symlink"
    assert link["children"] == []
    assert {first["hardlink_duplicate"], second["hardlink_duplicate"]} == {False, True}
    assert result["tree"]["allocated_size"] == max(
        first["allocated_size"], second["allocated_size"]
    )


def test_home_scan_start_is_idempotent(tmp_path: Path) -> None:
    """相同幂等键和参数不得重复创建扫描任务。"""

    home = tmp_path / "alice"
    home.mkdir()
    (home / "file.txt").write_text("hello", encoding="utf-8")
    passwd = tmp_path / "passwd"
    passwd.write_text(f"alice:x:1000:1000::{home}:/bin/bash\n", encoding="utf-8")
    manager = HomeScanManager()

    first = manager.start("alice", "idempotency-key-123", 3, 100, passwd)
    second = manager.start("alice", "idempotency-key-123", 3, 100, passwd)

    assert first["id"] == second["id"]
    for _ in range(100):
        snapshot = manager.get(first["id"])
        if snapshot["state"] == "completed":
            break
        time.sleep(0.01)
    assert snapshot["state"] == "completed"
    assert snapshot["result"]["tree"]["file_count"] == 1


def test_home_scan_rejects_idempotency_key_reuse_with_other_parameters(tmp_path: Path) -> None:
    """幂等键冲突必须拒绝，不能返回参数不一致的旧结果。"""

    home = tmp_path / "alice"
    home.mkdir()
    passwd = tmp_path / "passwd"
    passwd.write_text(f"alice:x:1000:1000::{home}:/bin/bash\n", encoding="utf-8")
    manager = HomeScanManager()
    manager.start("alice", "idempotency-key-123", 3, 100, passwd)

    with pytest.raises(ValueError, match="幂等键"):
        manager.start("alice", "idempotency-key-123", 4, 100, passwd)


def test_home_scan_rejects_root_as_home(tmp_path: Path) -> None:
    """不明确的根路径不能成为扫描目标。"""

    passwd = tmp_path / "passwd"
    passwd.write_text("root:x:0:0::/:/bin/bash\n", encoding="utf-8")

    with pytest.raises(ValueError, match="不安全"):
        HomeScanManager().start("root", "idempotency-key-123", 3, 100, passwd)


def test_user_storage_scan_filters_hidden_and_reports_largest_paths(tmp_path: Path) -> None:
    """挂载点扫描按稳定 UID 汇总，并遵守隐藏文件开关。"""

    visible = tmp_path / "visible.bin"
    hidden = tmp_path / ".hidden.bin"
    visible.write_bytes(b"v" * 4096)
    hidden.write_bytes(b"h" * 8192)
    uid = os.getuid()
    passwd = tmp_path / "passwd"
    passwd.write_text(f"alice:x:{uid}:{os.getgid()}::/home/alice:/bin/bash\n", encoding="utf-8")
    manager = UserStorageScanManager()

    task = manager.start(
        str(uid),
        str(tmp_path),
        "user-storage-key-123",
        4,
        100,
        False,
        False,
        True,
        0,
        300,
        passwd,
        {str(tmp_path)},
    )
    for _ in range(100):
        snapshot = manager.get(task["id"])
        if snapshot["state"] == "completed":
            break
        time.sleep(0.01)

    assert snapshot["state"] == "completed"
    assert snapshot["target_uid"] == uid
    assert snapshot["result"]["tree"]["file_count"] == 2  # passwd 和可见文件
    largest = {item["path"] for item in snapshot["result"]["largest_paths"]}
    assert str(visible) in largest
    assert str(hidden) not in largest
    assert snapshot["result"]["expires_at"] > snapshot["result"]["scanned_at"]


def test_user_storage_scan_rejects_non_mount_scope(tmp_path: Path) -> None:
    """用户输入的任意子目录不能冒充管理员可选挂载点。"""

    passwd = tmp_path / "passwd"
    passwd.write_text(
        f"alice:x:{os.getuid()}:{os.getgid()}::/home/alice:/bin/bash\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="完整挂载点"):
        UserStorageScanManager().start(
            "alice",
            str(tmp_path),
            "user-storage-key-456",
            4,
            100,
            True,
            False,
            True,
            0,
            300,
            passwd,
            {"/"},
        )


def test_completed_scan_updates_persistent_task_and_snapshot(tmp_path: Path) -> None:
    """后台扫描成功后，任务步骤和限时快照必须一起持久化。"""

    home = tmp_path / "alice"
    home.mkdir()
    (home / "data.txt").write_text("data", encoding="utf-8")
    passwd = tmp_path / "passwd"
    passwd.write_text(f"alice:x:{os.getuid()}:{os.getgid()}::{home}:/bin/bash\n", encoding="utf-8")
    repository = TaskRepository(initialize_database(tmp_path / "state"))
    manager = HomeScanManager(repository)

    task = manager.start("alice", "persistent-scan-key-123", 3, 100, passwd)
    manager.close()
    snapshot = manager.get(task["id"])

    assert snapshot["state"] == "completed"
    persisted = repository.get(task["id"])
    assert persisted["state"] == "succeeded"
    assert persisted["steps"][0]["state"] == "succeeded"
    with Session(repository.engine) as session:
        assert session.scalar(select(func.count()).select_from(ScanSnapshotRecord)) == 1
