"""单进程与按 UID 全部进程终止的持久化任务编排。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lab_ops_panel.application.user_creation import PrivilegedUserExecutor
from lab_ops_panel.infrastructure.tasks import TaskRepository


class ProcessControlManager:
    """串行执行进程终止动作，防止并发操作同一 PID 或 UID。"""

    def __init__(self, tasks: TaskRepository, executor: PrivilegedUserExecutor) -> None:
        self.tasks = tasks
        self.executor = executor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="process-control")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def start_process_termination(
        self,
        pid: int,
        uid: int,
        create_time: float,
        grace_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """启动绑定 PID、UID 和启动时间的单进程终止任务。"""

        parameters = {
            "pid": pid,
            "uid": uid,
            "create_time": create_time,
            "grace_seconds": grace_seconds,
        }
        task = self.tasks.create_or_get(
            "process_terminate",
            idempotency_key,
            parameters,
            f"PID:{pid}",
            ["重新核对 PID、UID 与启动时间", "发送 TERM 并等待", "按需 KILL 并验证退出"],
        )
        self._schedule(task, "process-terminate", parameters)
        return self.tasks.get(task["id"])

    def start_uid_termination(
        self,
        username: str,
        uid: int,
        grace_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """启动按稳定 UID 反复扫描并结束全部进程的任务。"""

        parameters = {
            "username": username,
            "uid": uid,
            "grace_seconds": grace_seconds,
        }
        task = self.tasks.create_or_get(
            "uid_processes_terminate",
            idempotency_key,
            parameters,
            f"UID:{uid}",
            ["重新核对本机账号与 UID", "发送 TERM 并等待", "按需 KILL、重扫并验证"],
        )
        self._schedule(task, "uid-processes-terminate", parameters)
        return self.tasks.get(task["id"])

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
            self.tasks.update_step(task_id, 1, "succeeded", {"identity_rechecked": True})
            self.tasks.transition(task_id, "running")
            current_step = 2
            self.tasks.update_step(task_id, 2, "running")
            result = self.executor.execute(action, payload)
            self.tasks.update_step(
                task_id,
                2,
                "succeeded",
                {"terminated_pids": result.get("terminated_pids", [])},
            )
            current_step = 3
            self.tasks.update_step(
                task_id,
                3,
                "succeeded",
                {
                    "escalated_pids": result.get("escalated_pids", []),
                    "remaining_pids": result.get("remaining_pids", []),
                },
            )
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
        """等待当前进程控制任务完成。"""

        self._pool.shutdown(wait=True, cancel_futures=False)
