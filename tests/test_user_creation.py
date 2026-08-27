"""批量用户创建任务编排测试。"""

import csv
import time
from pathlib import Path

from lab_ops_panel.application.user_creation import UserCreationManager
from lab_ops_panel.database import initialize_database
from lab_ops_panel.domain.passwords import PasswordPolicy
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FakeExecutor:
    """记录固定动作，并可在指定用户处模拟失败。"""

    def __init__(self, fail_username: str | None = None) -> None:
        self.fail_username = fail_username
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        if payload["username"] == self.fail_username:
            raise RuntimeError("模拟创建失败")
        return {"created": True}


def _wait(manager: UserCreationManager, task_id: str) -> dict[str, object]:
    """等待短任务进入终态。"""

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("用户创建任务未及时结束")


def _manager(tmp_path: Path, executor: FakeExecutor) -> UserCreationManager:
    database = initialize_database(tmp_path)
    return UserCreationManager(TaskRepository(database), executor, tmp_path)


def _pam_snapshot() -> dict[str, object]:
    return {
        "generation_minimums": {
            "length": 15,
            "min_uppercase": 0,
            "min_lowercase": 0,
            "min_digits": 0,
            "min_symbols": 0,
            "min_classes": 0,
        },
        "pwquality_enabled": False,
    }


def test_user_creation_exports_credentials_without_persisting_passwords(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """成功任务导出 0600 CSV，SQLite 任务内容不含明文密码。"""

    monkeypatch.setattr(
        "lab_ops_panel.application.user_creation.pam_policy_snapshot", _pam_snapshot
    )
    monkeypatch.setattr(
        "lab_ops_panel.application.user_creation.local_users",
        lambda: [{"username": "alice"}],
    )
    executor = FakeExecutor()
    manager = _manager(tmp_path, executor)
    try:
        task = manager.start(
            [{"username": "alice", "display_name": "张三"}],
            PasswordPolicy(),
            "create-users-key-123",
        )
        completed = _wait(manager, str(task["id"]))
        repeated = manager.start(
            [{"username": "alice", "display_name": "张三"}],
            PasswordPolicy(),
            "create-users-key-123",
        )
    finally:
        manager.close()

    assert completed["state"] == "succeeded"
    assert repeated["id"] == task["id"]
    assert len(executor.calls) == 1
    password = str(executor.calls[0][1]["password"])
    assert password not in str(completed)
    exported = tmp_path / "credential-exports" / f"initial-credentials-{task['id']}.csv"
    assert exported.stat().st_mode & 0o777 == 0o600
    assert password in exported.read_text(encoding="utf-8")


def test_user_creation_failure_exports_only_completed_rows(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """中途失败保留已创建账号的凭据，且任务明确失败。"""

    monkeypatch.setattr(
        "lab_ops_panel.application.user_creation.pam_policy_snapshot", _pam_snapshot
    )
    manager = _manager(tmp_path, FakeExecutor(fail_username="bob"))
    try:
        task = manager.start(
            [
                {"username": "alice", "display_name": "张三"},
                {"username": "bob", "display_name": "李四"},
            ],
            PasswordPolicy(),
            "partial-users-key-123",
        )
        completed = _wait(manager, str(task["id"]))
    finally:
        manager.close()

    exported = tmp_path / "credential-exports" / f"initial-credentials-{task['id']}.csv"
    with exported.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    assert completed["state"] == "failed"
    assert rows[1][0] == "alice"
    assert all(row[0] != "bob" for row in rows[1:])
    assert completed["steps"][1]["state"] == "failed"
