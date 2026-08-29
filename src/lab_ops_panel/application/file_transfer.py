"""部分文件迁移预检、复制发布和独立源清理的任务编排。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lab_ops_panel.application.user_creation import PrivilegedUserExecutor
from lab_ops_panel.infrastructure.tasks import TaskRepository


class FileTransferManager:
    """串行执行文件系统写操作，确保源清理永远是独立任务。"""

    def __init__(self, tasks: TaskRepository, executor: PrivilegedUserExecutor) -> None:
        self.tasks = tasks
        self.executor = executor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="file-transfer")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def start_preflight(
        self, source: str, target_directory: str, idempotency_key: str
    ) -> dict[str, Any]:
        """启动不修改源和目标的空间、inode 与边界预检。"""

        parameters = {"source": source, "target_directory": target_directory}
        task = self.tasks.create_or_get(
            "file_transfer_preflight",
            idempotency_key,
            parameters,
            source,
            ["核对路径与文件系统边界", "统计源数据、目标空间与 inode"],
        )
        self._schedule(task, "file-transfer-preflight", parameters)
        return task

    def start_copy(
        self,
        source: str,
        target_directory: str,
        expected_device: int,
        expected_inode: int,
        preflight_task_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """启动只复制不清理源数据的迁移任务。"""

        parameters = {
            "source": source,
            "target_directory": target_directory,
            "expected_device": expected_device,
            "expected_inode": expected_inode,
            "preflight_task_id": preflight_task_id,
        }
        task = self.tasks.create_or_get(
            "file_transfer_copy",
            idempotency_key,
            parameters,
            source,
            ["重新验证源身份与目标容量", "复制并保留全部元数据", "校验并发布目标路径"],
        )
        payload = {
            "source": source,
            "target_directory": target_directory,
            "operation_id": task["id"],
            "expected_device": expected_device,
            "expected_inode": expected_inode,
        }
        self._schedule(task, "file-transfer-copy", payload)
        return task

    def start_cleanup(
        self,
        source: str,
        destination: str,
        expected_device: int,
        expected_inode: int,
        copy_task_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """启动经独立人工确认的源数据校验与清理任务。"""

        parameters = {
            "source": source,
            "destination": destination,
            "expected_device": expected_device,
            "expected_inode": expected_inode,
            "copy_task_id": copy_task_id,
        }
        task = self.tasks.create_or_get(
            "file_transfer_cleanup",
            idempotency_key,
            parameters,
            source,
            ["重新比较源与目标", "隔离并清理源路径", "验证目标保留且源已移除"],
        )
        payload = {
            "source": source,
            "destination": destination,
            "operation_id": task["id"],
            "expected_device": expected_device,
            "expected_inode": expected_inode,
        }
        self._schedule(task, "file-transfer-cleanup", payload)
        return task

    def _schedule(self, task: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
        if task["state"] != "queued":
            return
        with self._lock:
            if task["id"] in self._scheduled:
                return
            self._scheduled.add(task["id"])
            self._pool.submit(self._run, task["id"], action, payload)

    def _run(self, task_id: str, action: str, payload: dict[str, Any]) -> None:
        current_step = 1
        try:
            self.tasks.transition(task_id, "prechecking")
            self.tasks.update_step(task_id, 1, "running")
            result = self.executor.execute(action, payload)
            if action == "file-transfer-preflight":
                self.tasks.update_step(
                    task_id,
                    1,
                    "succeeded",
                    {
                        "source": result["source"],
                        "destination": result["destination"],
                        "boundary_safe": True,
                    },
                )
                current_step = 2
                self.tasks.update_step(task_id, 2, "succeeded", result)
            else:
                self.tasks.update_step(task_id, 1, "succeeded", {"revalidated": True})
                self.tasks.transition(task_id, "running")
                for sequence in range(2, len(self.tasks.get(task_id)["steps"]) + 1):
                    current_step = sequence
                    self.tasks.update_step(task_id, sequence, "succeeded", result)
            if self.tasks.get(task_id)["state"] == "prechecking":
                self.tasks.transition(task_id, "running")
            self.tasks.transition(task_id, "succeeded")
            self.tasks.log(action, str(self.tasks.get(task_id)["target"]), "succeeded", task_id)
        except Exception as exc:
            task = self.tasks.get(task_id)
            step = next(item for item in task["steps"] if item["sequence"] == current_step)
            if step["state"] in {"pending", "running"}:
                self.tasks.update_step(task_id, current_step, "failed", {"reason": str(exc)})
            if self.tasks.get(task_id)["state"] in {"prechecking", "running"}:
                self.tasks.transition(task_id, "failed", str(exc))
            self.tasks.log(action, str(task["target"]), "failed", task_id)
        finally:
            with self._lock:
                self._scheduled.discard(task_id)

    def close(self) -> None:
        """等待正在执行的复制或清理安全收尾。"""

        self._pool.shutdown(wait=True, cancel_futures=False)
