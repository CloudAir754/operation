"""用户删除预检与执行任务编排测试。"""

import time
from pathlib import Path

from lab_ops_panel.application.user_deletion import UserDeletionManager
from lab_ops_panel.database import initialize_database
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FakeExecutor:
    """返回不含用户文件内容的删除预检和结果摘要。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        if action == "user-delete-preflight":
            return {
                "processes": [],
                "sessions": [],
                "open_home_pids": [],
                "scheduled_tasks": {},
                "sudo_sources": [],
                "sudo_scan_complete": True,
                "logical_home": "/home/alice",
                "resolved_home": "/home/alice",
                "home_is_symlink": False,
                "home_device": 1,
                "home_inode": 2,
                "home_mode": 0o700,
                "nested_mounts": [],
                "shared_with": [],
                "disk_usage": {"allocated_bytes": 4096, "inodes": 3},
                "delete_home_supported": True,
                "blockers": [],
                "home_delete_blockers": [],
                "warnings": [],
                "account_status": "P",
                "shell": "/bin/bash",
            }
        return {
            "account_deleted": True,
            "home_deleted": payload["delete_home"],
            "terminated_pids": [1234],
            "escalated_pids": [],
            "quarantine_path": None,
        }


def _wait(manager: UserDeletionManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("用户删除任务未及时结束")


def test_user_deletion_preflight_and_delete_are_persisted(tmp_path: Path) -> None:
    """预检事实和不可逆删除分别持久化，并携带同一稳定 UID/home。"""

    executor = FakeExecutor()
    manager = UserDeletionManager(TaskRepository(initialize_database(tmp_path)), executor)
    try:
        preflight = manager.start_preflight(
            "alice", 1001, "/home/alice", "delete-preflight-key-123"
        )
        preflight_done = _wait(manager, str(preflight["id"]))
        deletion = manager.start_delete(
            "alice",
            1001,
            "/home/alice",
            True,
            str(preflight["id"]),
            "delete-user-key-123",
        )
        deletion_done = _wait(manager, str(deletion["id"]))
    finally:
        manager.close()

    assert preflight_done["state"] == deletion_done["state"] == "succeeded"
    assert preflight_done["steps"][2]["detail"]["delete_home_supported"] is True
    assert deletion_done["steps"][3]["detail"]["home_deleted"] is True
    assert [action for action, _ in executor.calls] == [
        "user-delete-preflight",
        "user-delete",
    ]
