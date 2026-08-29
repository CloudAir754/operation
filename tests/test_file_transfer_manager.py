"""部分文件迁移持久化任务编排测试。"""

import time
from pathlib import Path

from lab_ops_panel.application.file_transfer import FileTransferManager
from lab_ops_panel.database import initialize_database
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FakeExecutor:
    """模拟三个固定特权动作，不读取真实用户文件。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        if action == "file-transfer-preflight":
            return {
                "source": "/data/source",
                "target_directory": "/mnt/archive",
                "destination": "/mnt/archive/source",
                "source_summary": {
                    "device": 10,
                    "inode": 20,
                    "files": 2,
                    "directories": 1,
                    "symlinks": 0,
                    "logical_bytes": 100,
                    "allocated_bytes": 4096,
                    "kind": "directory",
                    "metadata_digest": "a" * 64,
                },
                "available_bytes": 10000,
                "available_inodes": 100,
                "required_bytes": 4096,
                "required_inodes": 3,
            }
        if action == "file-transfer-copy":
            return {
                "source": "/data/source",
                "destination": "/mnt/archive/source",
                "source_device": 10,
                "source_inode": 20,
                "summary": {"files": 2, "logical_bytes": 100},
                "source_retained": True,
            }
        return {
            "source": "/data/source",
            "destination": "/mnt/archive/source",
            "source_removed": True,
        }


def _wait(manager: FileTransferManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("文件迁移任务未及时结束")


def test_file_transfer_uses_separate_copy_and_cleanup_tasks(tmp_path: Path) -> None:
    """复制成功只标记源保留，清理必须通过另一幂等任务执行。"""

    executor = FakeExecutor()
    manager = FileTransferManager(TaskRepository(initialize_database(tmp_path)), executor)
    try:
        preview = manager.start_preflight("/data/source", "/mnt/archive", "file-preview-key-123")
        preview_done = _wait(manager, str(preview["id"]))
        copied = manager.start_copy(
            "/data/source",
            "/mnt/archive",
            10,
            20,
            str(preview["id"]),
            "file-copy-key-123",
        )
        copied_done = _wait(manager, str(copied["id"]))
        cleaned = manager.start_cleanup(
            "/data/source",
            "/mnt/archive/source",
            10,
            20,
            str(copied["id"]),
            "file-clean-key-123",
        )
        cleaned_done = _wait(manager, str(cleaned["id"]))
    finally:
        manager.close()

    assert preview_done["state"] == copied_done["state"] == cleaned_done["state"] == "succeeded"
    assert copied_done["steps"][-1]["detail"]["source_retained"] is True
    assert cleaned_done["steps"][-1]["detail"]["source_removed"] is True
    assert [action for action, _ in executor.calls] == [
        "file-transfer-preflight",
        "file-transfer-copy",
        "file-transfer-cleanup",
    ]
