"""SSH 公钥与 sudo 授权的持久化任务编排。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lab_ops_panel.application.user_creation import PrivilegedUserExecutor
from lab_ops_panel.domain.ssh_keys import PublicKey
from lab_ops_panel.infrastructure.tasks import TaskRepository


class AccountAccessManager:
    """串行执行账号访问写操作，并记录不含密钥材料的任务摘要。"""

    def __init__(self, tasks: TaskRepository, executor: PrivilegedUserExecutor) -> None:
        self.tasks = tasks
        self.executor = executor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-access")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def start_ssh_import(
        self,
        username: str,
        uid: int,
        keys: list[PublicKey],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """按公钥指纹持久化幂等参数，密钥材料只传给当前工作线程。"""

        parameters = {
            "username": username,
            "uid": uid,
            "fingerprints": sorted({key.fingerprint for key in keys}),
        }
        task = self.tasks.create_or_get(
            "ssh_import",
            idempotency_key,
            parameters,
            f"UID:{uid}",
            ["重新预检账号", "原子导入公钥", "验证所有权、权限和 ACL"],
        )
        self._schedule(task, "ssh-import", username, uid, keys)
        return self.tasks.get(task["id"])

    def start_sudo_change(
        self,
        username: str,
        uid: int,
        grant: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """启动只管理本程序独立规则文件的 sudo 授权或撤销任务。"""

        action = "sudo-grant" if grant else "sudo-revoke"
        task = self.tasks.create_or_get(
            action.replace("-", "_"),
            idempotency_key,
            {"username": username, "uid": uid, "grant": grant},
            f"UID:{uid}",
            ["重新预检账号与 sudoers", "写入或撤销独立规则", "运行 visudo 结果验证"],
        )
        self._schedule(task, action, username, uid, None)
        return self.tasks.get(task["id"])

    def _schedule(
        self,
        task: dict[str, Any],
        action: str,
        username: str,
        uid: int,
        keys: list[PublicKey] | None,
    ) -> None:
        if task["state"] != "queued":
            return
        with self._lock:
            if task["id"] in self._scheduled:
                return
            self._scheduled.add(task["id"])
            self._pool.submit(self._run, task["id"], action, username, uid, keys)

    def _run(
        self,
        task_id: str,
        action: str,
        username: str,
        uid: int,
        keys: list[PublicKey] | None,
    ) -> None:
        current_step = 1
        try:
            self.tasks.transition(task_id, "prechecking")
            self.tasks.update_step(task_id, 1, "running")
            self.tasks.update_step(
                task_id, 1, "succeeded", {"username": username, "expected_uid": uid}
            )
            self.tasks.transition(task_id, "running")
            current_step = 2
            self.tasks.update_step(task_id, 2, "running")
            payload: dict[str, Any] = {"username": username, "expected_uid": uid}
            if keys is not None:
                payload["keys"] = [key.normalized() for key in keys]
            result = self.executor.execute(action, payload)
            self.tasks.update_step(task_id, 2, "succeeded", result)
            current_step = 3
            self.tasks.update_step(task_id, 3, "succeeded", {"helper_verified": True})
            self.tasks.transition(task_id, "succeeded")
            self.tasks.log(action, f"UID:{uid}", "succeeded", task_id)
        except Exception as exc:
            task = self.tasks.get(task_id)
            step = next(item for item in task["steps"] if item["sequence"] == current_step)
            if step["state"] in {"pending", "running"}:
                self.tasks.update_step(task_id, current_step, "failed", {"reason": str(exc)})
            task = self.tasks.get(task_id)
            if task["state"] in {"prechecking", "running"}:
                self.tasks.transition(task_id, "failed", str(exc))
            self.tasks.log(action, f"UID:{uid}", "failed", task_id)
        finally:
            with self._lock:
                self._scheduled.discard(task_id)

    def close(self) -> None:
        """等待当前访问控制任务安全收尾。"""

        self._pool.shutdown(wait=True, cancel_futures=False)
