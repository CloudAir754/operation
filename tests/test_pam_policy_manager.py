"""PAM 策略持久化任务编排测试。"""

import time
from pathlib import Path

from lab_ops_panel.application.pam_policy import PamPolicyManager
from lab_ops_panel.database import initialize_database
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FakeExecutor:
    """记录 PAM 固定动作并返回验证摘要。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        if action == "pam-update":
            return {
                "changed": True,
                "backup_id": "a" * 32,
                "settings": payload["changes"],
            }
        return {"restored": True, "backup_id": payload["backup_id"], "settings": {}}


def _wait(manager: PamPolicyManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("PAM 任务未及时结束")


def test_pam_update_and_restore_are_idempotent_persisted_tasks(tmp_path: Path) -> None:
    """修改与恢复分别持久化，重复修改不会再次调用特权助手。"""

    executor = FakeExecutor()
    manager = PamPolicyManager(TaskRepository(initialize_database(tmp_path)), executor)
    try:
        update = manager.start_update({"minlen": 20}, "pam-update-key-123")
        completed = _wait(manager, str(update["id"]))
        repeated = manager.start_update({"minlen": 20}, "pam-update-key-123")
        restore = manager.start_restore("a" * 32, "pam-restore-key-123")
        restored = _wait(manager, str(restore["id"]))
    finally:
        manager.close()

    assert completed["state"] == restored["state"] == "succeeded"
    assert repeated["id"] == update["id"]
    assert [action for action, _ in executor.calls] == ["pam-update", "pam-restore"]
    assert completed["steps"][1]["detail"]["backup_id"] == "a" * 32
