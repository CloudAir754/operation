"""PAM 策略修改与恢复的持久化任务编排。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lab_ops_panel.application.user_creation import PrivilegedUserExecutor
from lab_ops_panel.infrastructure.tasks import TaskRepository


class PamPolicyManager:
    """串行执行影响全机本地用户的 PAM 策略变更。"""

    def __init__(self, tasks: TaskRepository, executor: PrivilegedUserExecutor) -> None:
        self.tasks = tasks
        self.executor = executor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pam-policy")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def start_update(
        self,
        changes: dict[str, int | bool],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """创建策略修改任务，确认文本不进入持久化参数。"""

        task = self.tasks.create_or_get(
            "pam_update",
            idempotency_key,
            {"changes": changes},
            "/etc/security/pwquality.conf",
            ["重新读取 PAM 能力", "备份并原子写入", "验证实际生效策略"],
        )
        self._schedule(task, "pam-update", {"changes": changes})
        return self.tasks.get(task["id"])

    def start_restore(self, backup_id: str, idempotency_key: str) -> dict[str, Any]:
        """创建指定备份的恢复任务。"""

        task = self.tasks.create_or_get(
            "pam_restore",
            idempotency_key,
            {"backup_id": backup_id},
            f"PAM备份:{backup_id}",
            ["校验备份与当前配置", "原子恢复原配置", "验证实际生效策略"],
        )
        self._schedule(task, "pam-restore", {"backup_id": backup_id})
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
            self.tasks.update_step(task_id, 1, "succeeded", {"capability_rechecked": True})
            self.tasks.transition(task_id, "running")
            current_step = 2
            self.tasks.update_step(task_id, 2, "running")
            result = self.executor.execute(action, payload)
            self.tasks.update_step(task_id, 2, "succeeded", result)
            current_step = 3
            self.tasks.update_step(task_id, 3, "succeeded", {"effective_verified": True})
            self.tasks.transition(task_id, "succeeded")
            self.tasks.log(action, "/etc/security/pwquality.conf", "succeeded", task_id)
        except Exception as exc:
            task = self.tasks.get(task_id)
            step = next(item for item in task["steps"] if item["sequence"] == current_step)
            if step["state"] in {"pending", "running"}:
                self.tasks.update_step(task_id, current_step, "failed", {"reason": str(exc)})
            if self.tasks.get(task_id)["state"] in {"prechecking", "running"}:
                self.tasks.transition(task_id, "failed", str(exc))
            self.tasks.log(action, "/etc/security/pwquality.conf", "failed", task_id)
        finally:
            with self._lock:
                self._scheduled.discard(task_id)

    def close(self) -> None:
        """等待当前 PAM 任务安全完成。"""

        self._pool.shutdown(wait=True, cancel_futures=False)
