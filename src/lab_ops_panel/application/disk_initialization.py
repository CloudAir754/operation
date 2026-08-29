"""磁盘初始化持久化执行和中断恢复编排。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Any

from lab_ops_panel.application.user_creation import PrivilegedUserExecutor
from lab_ops_panel.infrastructure.tasks import TaskRepository

PHASE_SEQUENCE = {
    "planned": 1,
    "partitioning": 2,
    "partitioned": 3,
    "formatting": 4,
    "formatted": 4,
    "updating_fstab": 5,
    "fstab_updated": 5,
    "mounting": 6,
    "mounted": 6,
    "succeeded": 6,
}
STEPS = [
    "重新核对稳定身份、系统盘、签名和 holder",
    "清除空盘签名并创建 GPT",
    "创建唯一 ext4 分区并等待稳定链接",
    "格式化 ext4 并读取 UUID",
    "原子更新并验证 fstab",
    "挂载并核对 UUID、类型和目标目录",
]


class DiskInitializationManager:
    """串行执行磁盘破坏性任务，确认序列号不写入 SQLite。"""

    def __init__(self, tasks: TaskRepository, executor: PrivilegedUserExecutor) -> None:
        self.tasks = tasks
        self.executor = executor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="disk-initialization")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def start(
        self,
        stable_identity: str,
        confirmed_serial: str,
        label: str,
        mountpoint: str,
        mount_options: list[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """创建任务并仅在工作线程内携带序列号确认文本。"""

        parameters = {
            "stable_identity": stable_identity,
            "label": label,
            "mountpoint": mountpoint,
            "mount_options": mount_options,
        }
        task = self.tasks.create_or_get(
            "disk_initialization",
            idempotency_key,
            parameters,
            stable_identity,
            STEPS,
        )
        payload = {
            **parameters,
            "confirmed_serial": confirmed_serial,
            "operation_id": task["id"],
        }
        self._schedule(task, payload)
        return task

    def resume(self, task_id: str, confirmed_serial: str) -> dict[str, Any]:
        """再次输入序列号后恢复原 interrupted 任务。"""

        task = self.tasks.get(task_id)
        if task["kind"] != "disk_initialization" or task["state"] != "interrupted":
            raise ValueError("只有中断的磁盘初始化任务可以恢复")
        task = self.tasks.transition(task_id, "running")
        payload = {
            **task["parameters"],
            "confirmed_serial": confirmed_serial,
            "operation_id": task_id,
        }
        self._schedule(task, payload)
        return self.tasks.get(task_id)

    def _schedule(self, task: dict[str, Any], payload: dict[str, Any]) -> None:
        if task["state"] not in {"queued", "interrupted", "running"}:
            return
        with self._lock:
            if task["id"] in self._scheduled:
                return
            self._scheduled.add(task["id"])
            self._pool.submit(self._run, task["id"], payload)

    def _run(self, task_id: str, payload: dict[str, Any]) -> None:
        task = self.tasks.get(task_id)
        current_step = 1
        try:
            if task["state"] == "queued":
                self.tasks.transition(task_id, "prechecking")
                self.tasks.transition(task_id, "running")
            elif task["state"] == "interrupted":
                self.tasks.transition(task_id, "running")
            self.tasks.update_step(task_id, 1, "running")
            result = self.executor.execute("disk-initialization-execute", payload)
            for sequence in range(1, len(STEPS) + 1):
                current_step = sequence
                self.tasks.update_step(task_id, sequence, "succeeded", result)
            self.tasks.transition(task_id, "succeeded")
            self.tasks.log(
                "disk-initialization-execute",
                str(self.tasks.get(task_id)["target"]),
                "succeeded",
                task_id,
            )
        except Exception as exc:
            status: dict[str, Any] = {}
            with suppress(Exception):
                status = self.executor.execute(
                    "disk-initialization-status", {"operation_id": task_id}
                )
            current_step = PHASE_SEQUENCE.get(str(status.get("phase", "")), current_step)
            if status.get("phase") == "failed":
                self.tasks.update_step(
                    task_id,
                    current_step,
                    "failed",
                    {"reason": str(exc), "recovery": status},
                )
                if self.tasks.get(task_id)["state"] == "running":
                    self.tasks.transition(task_id, "failed", str(exc))
            else:
                self.tasks.update_step(
                    task_id,
                    current_step,
                    "interrupted",
                    {"reason": str(exc), "recovery": status},
                )
                if self.tasks.get(task_id)["state"] == "running":
                    self.tasks.transition(task_id, "interrupted", str(exc))
            self.tasks.log(
                "disk-initialization-execute",
                str(task["target"]),
                "failed",
                task_id,
            )
        finally:
            with self._lock:
                self._scheduled.discard(task_id)

    def close(self) -> None:
        """等待磁盘命令落入已持久化阶段。"""

        self._pool.shutdown(wait=True, cancel_futures=False)
