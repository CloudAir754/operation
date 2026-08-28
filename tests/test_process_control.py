"""进程终止持久化任务编排测试。"""

import time
from pathlib import Path

from lab_ops_panel.application.process_control import ProcessControlManager
from lab_ops_panel.database import initialize_database
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FakeExecutor:
    """记录固定进程动作并模拟 TERM 成功。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        pid = int(payload.get("pid", 1234))
        return {"terminated_pids": [pid], "escalated_pids": [], "remaining_pids": []}


def _wait(manager: ProcessControlManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("进程控制任务未及时结束")


def test_process_and_uid_termination_are_idempotent_tasks(tmp_path: Path) -> None:
    """单 PID 与按 UID 操作分别持久化，重复提交不重复发送信号。"""

    executor = FakeExecutor()
    manager = ProcessControlManager(TaskRepository(initialize_database(tmp_path)), executor)
    try:
        single = manager.start_process_termination(1234, 1001, 12345.5, 5, "process-stop-key-123")
        single_done = _wait(manager, str(single["id"]))
        repeated = manager.start_process_termination(1234, 1001, 12345.5, 5, "process-stop-key-123")
        all_task = manager.start_uid_termination("alice", 1001, 5, "uid-process-stop-key-123")
        all_done = _wait(manager, str(all_task["id"]))
    finally:
        manager.close()

    assert single_done["state"] == all_done["state"] == "succeeded"
    assert repeated["id"] == single["id"]
    assert [action for action, _ in executor.calls] == [
        "process-terminate",
        "uid-processes-terminate",
    ]
