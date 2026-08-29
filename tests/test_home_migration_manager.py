"""home 迁移任务编排、中断恢复、回退和备份清理测试。"""

import time
from pathlib import Path

from lab_ops_panel.application.home_migration import HomeMigrationManager
from lab_ops_panel.database import initialize_database
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FakeExecutor:
    """返回阶段摘要，并在首次执行时构造可恢复中断。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_execute_once = True
        self.phase = "preflighted"

    def execute(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        migration_id = str(payload["migration_id"])
        common = {
            "migration_id": migration_id,
            "username": "alice",
            "uid": 2001,
            "source": "/home/alice",
            "backup": f"/home/.labopspanel-home-backup-{migration_id}",
            "target_parent": "/data/users",
            "destination": "/data/users/alice",
        }
        if action == "home-migration-preflight":
            return {
                **common,
                "phase": "preflighted",
                "source_summary": {"files": 3, "logical_bytes": 4096},
                "required_bytes": 4096,
                "safety_margin_bytes": 1024**3,
            }
        if action == "home-migration-execute" and self.fail_execute_once:
            self.fail_execute_once = False
            self.phase = "copying"
            raise RuntimeError("构造的复制中断")
        if action == "home-migration-status":
            return {**common, "phase": self.phase}
        if action == "home-migration-execute":
            self.phase = "observing"
        elif action == "home-migration-rollback":
            self.phase = "rolled_back"
        elif action == "home-migration-cleanup":
            self.phase = "cleaned"
        return {**common, "phase": self.phase}


def _wait(manager: HomeMigrationManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed", "interrupted"}:
            return task
        time.sleep(0.01)
    raise AssertionError("home 迁移任务未及时结束")


def test_home_migration_manager_resumes_same_interrupted_task(tmp_path: Path) -> None:
    """复制故障落入 interrupted，恢复时复用原任务且最终进入观察期。"""

    executor = FakeExecutor()
    manager = HomeMigrationManager(TaskRepository(initialize_database(tmp_path)), executor)
    try:
        preview = manager.start_preflight(
            "alice",
            2001,
            "/home/alice",
            "/data/users",
            10,
            True,
            "home-preview-key-123",
        )
        preview_done = _wait(manager, str(preview["id"]))
        execution = manager.start_execute(
            str(preview["id"]), str(preview["id"]), "home-execute-key-123"
        )
        interrupted = _wait(manager, str(execution["id"]))
        manager.resume_execute(str(execution["id"]))
        resumed = _wait(manager, str(execution["id"]))
    finally:
        manager.close()

    assert preview_done["state"] == "succeeded"
    assert interrupted["state"] == "interrupted"
    assert interrupted["steps"][3]["detail"]["recovery"]["phase"] == "copying"
    assert resumed["id"] == execution["id"]
    assert resumed["state"] == "succeeded"
    assert resumed["steps"][-1]["detail"]["phase"] == "observing"


def test_home_migration_rollback_and_cleanup_are_separate_tasks(tmp_path: Path) -> None:
    """回退与观察期清理使用独立幂等任务，不能混入首次执行。"""

    executor = FakeExecutor()
    executor.fail_execute_once = False
    manager = HomeMigrationManager(TaskRepository(initialize_database(tmp_path)), executor)
    try:
        rolled = manager.start_rollback("a" * 32, "alice", "home-rollback-key-123")
        rolled_done = _wait(manager, str(rolled["id"]))
        cleaned = manager.start_cleanup("b" * 32, "alice", "home-cleanup-key-123")
        cleaned_done = _wait(manager, str(cleaned["id"]))
    finally:
        manager.close()

    assert rolled_done["state"] == cleaned_done["state"] == "succeeded"
    assert rolled_done["steps"][-1]["detail"]["phase"] == "rolled_back"
    assert cleaned_done["steps"][-1]["detail"]["phase"] == "cleaned"
