"""磁盘初始化任务的序列号防持久化与中断恢复测试。"""

import time
from pathlib import Path

from lab_ops_panel.application.disk_initialization import DiskInitializationManager
from lab_ops_panel.database import initialize_database
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FakeExecutor:
    """首次执行构造分区阶段中断，随后返回成功挂载摘要。"""

    def __init__(self) -> None:
        self.fail_once = True
        self.phase = "partitioning"
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        if action == "disk-initialization-execute" and self.fail_once:
            self.fail_once = False
            raise RuntimeError("构造的分区后中断")
        if action == "disk-initialization-status":
            return {"operation_id": payload["operation_id"], "phase": self.phase}
        self.phase = "succeeded"
        return {
            "operation_id": payload["operation_id"],
            "phase": "succeeded",
            "stable_identity": payload["stable_identity"],
            "mountpoint": payload["mountpoint"],
            "filesystem_uuid": "11111111-2222-3333-4444-555555555555",
        }


def _wait(manager: DiskInitializationManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed", "interrupted"}:
            return task
        time.sleep(0.01)
    raise AssertionError("磁盘初始化任务未及时结束")


def test_disk_task_does_not_persist_confirmation_and_can_resume(tmp_path: Path) -> None:
    """序列号确认仅进入工作线程；恢复复用同一任务并要求再次提供确认。"""

    executor = FakeExecutor()
    manager = DiskInitializationManager(TaskRepository(initialize_database(tmp_path)), executor)
    try:
        task = manager.start(
            "serial:SENSITIVE-RUNTIME-VALUE",
            "SENSITIVE-RUNTIME-VALUE",
            "research",
            "/mnt/research",
            ["defaults", "nofail"],
            "disk-init-key-123",
        )
        interrupted = _wait(manager, str(task["id"]))
        manager.resume(str(task["id"]), "SENSITIVE-RUNTIME-VALUE")
        resumed = _wait(manager, str(task["id"]))
    finally:
        manager.close()

    assert interrupted["state"] == "interrupted"
    assert "confirmed_serial" not in interrupted["parameters"]
    assert resumed["id"] == task["id"] and resumed["state"] == "succeeded"
    execute_payloads = [payload for action, payload in executor.calls if action.endswith("execute")]
    assert len(execute_payloads) == 2
    assert all(
        payload["confirmed_serial"] == "SENSITIVE-RUNTIME-VALUE" for payload in execute_payloads
    )
