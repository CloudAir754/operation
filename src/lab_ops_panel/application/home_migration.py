"""home 迁移预检、恢复执行、回退和备份清理的持久化任务编排。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Any

from lab_ops_panel.application.user_creation import PrivilegedUserExecutor
from lab_ops_panel.infrastructure.tasks import TaskRepository

EXECUTION_STEPS = [
    "重新核对账号、源和目标身份",
    "锁定登录并终止 UID 全部进程",
    "原地改名保留原 home",
    "复制 ACL、扩展属性、硬链接和稀疏文件",
    "校验数量、大小与关键元数据",
    "发布目标 home",
    "切换逻辑 home 符号链接",
    "恢复账号并以用户身份读写试验",
    "进入原 home 备份观察期",
]
PHASE_SEQUENCE = {
    "preflighted": 1,
    "freezing": 2,
    "frozen": 2,
    "renaming_source": 3,
    "source_renamed": 3,
    "copying": 4,
    "copied": 5,
    "publishing_target": 6,
    "target_published": 6,
    "linking": 7,
    "linked": 7,
    "restoring_account": 8,
    "testing": 8,
    "observing": 9,
}


class HomeMigrationManager:
    """串行编排高风险 home 操作，并允许 interrupted 任务显式恢复。"""

    def __init__(self, tasks: TaskRepository, executor: PrivilegedUserExecutor) -> None:
        self.tasks = tasks
        self.executor = executor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="home-migration")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def start_preflight(
        self,
        username: str,
        uid: int,
        source: str,
        target_parent: str,
        safety_margin_percent: int,
        verify_contents: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """启动 ext4、路径、空间、inode 和源元数据预检。"""

        parameters = {
            "username": username,
            "uid": uid,
            "source": source,
            "target_parent": target_parent,
            "safety_margin_percent": safety_margin_percent,
            "verify_contents": verify_contents,
        }
        task = self.tasks.create_or_get(
            "home_migration_preflight",
            idempotency_key,
            parameters,
            f"UID:{uid}",
            ["核对本机账号和 ext4 边界", "统计空间、inode 与元数据", "写入可恢复迁移日志"],
        )
        payload = {
            "username": username,
            "expected_uid": uid,
            "target_parent": target_parent,
            "migration_id": task["id"],
            "safety_margin_percent": safety_margin_percent,
            "verify_contents": verify_contents,
        }
        self._schedule(task, "home-migration-preflight", payload)
        return task

    def start_execute(
        self, migration_id: str, preflight_task_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        """启动或首次执行可恢复迁移状态机。"""

        parameters = {
            "migration_id": migration_id,
            "preflight_task_id": preflight_task_id,
        }
        task = self.tasks.create_or_get(
            "home_migration_execute",
            idempotency_key,
            parameters,
            migration_id,
            EXECUTION_STEPS,
        )
        self._schedule(task, "home-migration-execute", {"migration_id": migration_id})
        return task

    def resume_execute(self, task_id: str) -> dict[str, Any]:
        """显式恢复因进程退出或可重试故障而 interrupted 的原执行任务。"""

        task = self.tasks.get(task_id)
        if task["kind"] != "home_migration_execute" or task["state"] != "interrupted":
            raise ValueError("只有中断的 home 迁移执行任务可以恢复")
        migration_id = str(task["parameters"]["migration_id"])
        task = self.tasks.transition(task_id, "running")
        self._schedule(task, "home-migration-execute", {"migration_id": migration_id})
        return self.tasks.get(task_id)

    def start_rollback(
        self, migration_id: str, username: str, idempotency_key: str
    ) -> dict[str, Any]:
        """启动重新冻结、移除链接、恢复原 home 和账号状态的回退。"""

        parameters = {"migration_id": migration_id, "username": username}
        task = self.tasks.create_or_get(
            "home_migration_rollback",
            idempotency_key,
            parameters,
            migration_id,
            ["核对迁移日志和原备份", "重新冻结账号并恢复原 home", "恢复账号并验证结果"],
        )
        self._schedule(task, "home-migration-rollback", {"migration_id": migration_id})
        return task

    def start_cleanup(
        self, migration_id: str, username: str, idempotency_key: str
    ) -> dict[str, Any]:
        """启动观察期后的独立原 home 备份清理。"""

        parameters = {"migration_id": migration_id, "username": username}
        task = self.tasks.create_or_get(
            "home_migration_cleanup",
            idempotency_key,
            parameters,
            migration_id,
            ["核对当前链接、目标和原备份", "清理原 home 备份", "验证目标保留且备份消失"],
        )
        self._schedule(task, "home-migration-cleanup", {"migration_id": migration_id})
        return task

    def _schedule(self, task: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
        if task["state"] not in {"queued", "interrupted", "running"}:
            return
        with self._lock:
            if task["id"] in self._scheduled:
                return
            self._scheduled.add(task["id"])
            self._pool.submit(self._run, task["id"], action, payload)

    def _run(self, task_id: str, action: str, payload: dict[str, Any]) -> None:
        task = self.tasks.get(task_id)
        current_step = 1
        try:
            if task["state"] == "queued":
                self.tasks.transition(task_id, "prechecking")
            elif task["state"] == "interrupted":
                self.tasks.transition(task_id, "running")
            self.tasks.update_step(task_id, 1, "running")
            if (
                self.tasks.get(task_id)["state"] == "prechecking"
                and action != "home-migration-preflight"
            ):
                self.tasks.transition(task_id, "running")
            result = self.executor.execute(action, payload)
            if action == "home-migration-preflight":
                for sequence in range(1, 4):
                    current_step = sequence
                    self.tasks.update_step(task_id, sequence, "succeeded", result)
                self.tasks.transition(task_id, "running")
            else:
                for sequence in range(1, len(self.tasks.get(task_id)["steps"]) + 1):
                    current_step = sequence
                    self.tasks.update_step(task_id, sequence, "succeeded", result)
            self.tasks.transition(task_id, "succeeded")
            self.tasks.log(action, str(self.tasks.get(task_id)["target"]), "succeeded", task_id)
        except Exception as exc:
            task = self.tasks.get(task_id)
            if action == "home-migration-execute" and task["state"] == "running":
                status: dict[str, Any] = {}
                with suppress(Exception):
                    status = self.executor.execute("home-migration-status", payload)
                current_step = PHASE_SEQUENCE.get(str(status.get("phase", "")), current_step)
                if status.get("phase") == "rolled_back":
                    self.tasks.update_step(
                        task_id,
                        current_step,
                        "failed",
                        {"reason": str(exc), "automatic_rollback": status},
                    )
                    self.tasks.transition(task_id, "failed", str(exc))
                else:
                    self.tasks.update_step(
                        task_id,
                        current_step,
                        "interrupted",
                        {"reason": str(exc), "recovery": status},
                    )
                    self.tasks.transition(task_id, "interrupted", str(exc))
            else:
                step = next(item for item in task["steps"] if item["sequence"] == current_step)
                if step["state"] in {"pending", "running", "interrupted"}:
                    self.tasks.update_step(task_id, current_step, "failed", {"reason": str(exc)})
                state = self.tasks.get(task_id)["state"]
                if state in {"prechecking", "running"}:
                    self.tasks.transition(task_id, "failed", str(exc))
            self.tasks.log(action, str(task["target"]), "failed", task_id)
        finally:
            with self._lock:
                self._scheduled.discard(task_id)

    def close(self) -> None:
        """等待当前 home 迁移动作落入可恢复阶段。"""

        self._pool.shutdown(wait=True, cancel_futures=False)
