"""SSH 与 sudo 持久化任务编排测试。"""

import base64
import struct
import time
from pathlib import Path

from lab_ops_panel.application.account_access import AccountAccessManager
from lab_ops_panel.database import initialize_database
from lab_ops_panel.domain.ssh_keys import parse_public_key
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FakeExecutor:
    """返回可持久化的特权动作校验摘要。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        if action == "ssh-import":
            return {"added": 1, "duplicates": 0, "fingerprints": ["SHA256:test"]}
        return {"changed": True, "path": "/etc/sudoers.d/labopspanel-uid-1000"}


def _key() -> str:
    key_type = "ssh-ed25519"
    blob = struct.pack(">I", len(key_type)) + key_type.encode() + b"test-key-material"
    return f"{key_type} {base64.b64encode(blob).decode()} test@example"


def _wait(manager: AccountAccessManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("账号访问任务未及时结束")


def test_ssh_task_persists_fingerprint_but_not_key_material(tmp_path: Path) -> None:
    """SSH 任务数据库只保存指纹，并向助手传递稳定 UID。"""

    executor = FakeExecutor()
    tasks = TaskRepository(initialize_database(tmp_path))
    manager = AccountAccessManager(tasks, executor)
    key = parse_public_key(_key())
    try:
        started = manager.start_ssh_import("alice", 1000, [key], "ssh-import-key-123")
        completed = _wait(manager, str(started["id"]))
        repeated = manager.start_ssh_import("alice", 1000, [key], "ssh-import-key-123")
    finally:
        manager.close()

    assert completed["state"] == "succeeded"
    assert repeated["id"] == started["id"]
    assert key.encoded not in str(completed)
    assert completed["parameters"]["fingerprints"] == [key.fingerprint]
    assert executor.calls[0][1]["expected_uid"] == 1000
    assert len(executor.calls) == 1


def test_sudo_grant_and_revoke_are_distinct_idempotent_tasks(tmp_path: Path) -> None:
    """sudo 新增和撤销使用不同动作，并保留明确的 UID 目标。"""

    executor = FakeExecutor()
    manager = AccountAccessManager(TaskRepository(initialize_database(tmp_path)), executor)
    try:
        granted = manager.start_sudo_change("alice", 1000, True, "sudo-grant-key-123")
        grant_result = _wait(manager, str(granted["id"]))
        revoked = manager.start_sudo_change("alice", 1000, False, "sudo-revoke-key-123")
        revoke_result = _wait(manager, str(revoked["id"]))
    finally:
        manager.close()

    assert grant_result["state"] == revoke_result["state"] == "succeeded"
    assert [call[0] for call in executor.calls] == ["sudo-grant", "sudo-revoke"]
    assert grant_result["target"] == revoke_result["target"] == "UID:1000"
