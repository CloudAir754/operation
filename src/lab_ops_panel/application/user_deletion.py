"""用户删除预检与执行的持久化任务编排。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lab_ops_panel.application.user_creation import PrivilegedUserExecutor
from lab_ops_panel.infrastructure.tasks import TaskRepository


class UserDeletionManager:
    """串行执行不可逆用户删除，预检和删除均使用稳定 UID/home。"""

    def __init__(self, tasks: TaskRepository, executor: PrivilegedUserExecutor) -> None:
        self.tasks = tasks
        self.executor = executor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="user-deletion")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def start_preflight(
        self, username: str, uid: int, home: str, idempotency_key: str
    ) -> dict[str, Any]:
        """启动包含 home 占用统计的只读删除预检。"""

        parameters = {"username": username, "uid": uid, "home": home}
        task = self.tasks.create_or_get(
            "user_delete_preflight",
            idempotency_key,
            parameters,
            f"UID:{uid}",
            ["核对本机账号身份", "检查进程、会话、计划任务与 sudo", "检查 home 边界与占用"],
        )
        self._schedule(task, "user-delete-preflight", parameters)
        # 调度线程可能立即取得 SQLite 写锁；创建事务返回的快照已经完整，
        # 无需为“尽量新”的状态与后台任务争用数据库。
        return task

    def start_delete(
        self,
        username: str,
        uid: int,
        home: str,
        delete_home: bool,
        preflight_task_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """启动已关联成功预检的账号删除任务。"""

        parameters = {
            "username": username,
            "uid": uid,
            "home": home,
            "delete_home": delete_home,
            "preflight_task_id": preflight_task_id,
        }
        task = self.tasks.create_or_get(
            "user_delete",
            idempotency_key,
            parameters,
            f"UID:{uid}",
            [
                "重新验证账号与预检条件",
                "禁用登录并终止 UID 全部进程",
                "撤销托管 sudo 并删除账号",
                "按选择保留或安全删除 home",
                "验证账号、进程和路径结果",
            ],
        )
        payload = {
            "username": username,
            "expected_uid": uid,
            "expected_home": home,
            "delete_home": delete_home,
        }
        self._schedule(task, "user-delete", payload)
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
            self.tasks.update_step(task_id, 1, "succeeded", {"identity_rechecked": True})
            self.tasks.transition(task_id, "running")
            current_step = 2
            self.tasks.update_step(task_id, 2, "running")
            result = self.executor.execute(action, payload)
            if action == "user-delete-preflight":
                dependency_detail = {
                    key: result[key]
                    for key in (
                        "processes",
                        "sessions",
                        "open_home_pids",
                        "scheduled_tasks",
                        "sudo_sources",
                        "sudo_scan_complete",
                    )
                }
                self.tasks.update_step(task_id, 2, "succeeded", dependency_detail)
                current_step = 3
                home_detail = {
                    key: result[key]
                    for key in (
                        "logical_home",
                        "resolved_home",
                        "home_is_symlink",
                        "home_device",
                        "home_inode",
                        "home_mode",
                        "nested_mounts",
                        "shared_with",
                        "disk_usage",
                        "delete_home_supported",
                        "blockers",
                        "home_delete_blockers",
                        "warnings",
                        "account_status",
                        "shell",
                    )
                }
                self.tasks.update_step(task_id, 3, "succeeded", home_detail)
            else:
                self.tasks.update_step(
                    task_id,
                    2,
                    "succeeded",
                    {
                        "terminated_pids": result.get("terminated_pids", []),
                        "escalated_pids": result.get("escalated_pids", []),
                    },
                )
                for sequence in (3, 4, 5):
                    current_step = sequence
                    self.tasks.update_step(task_id, sequence, "succeeded", result)
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
        """等待当前用户删除任务安全收尾。"""

        self._pool.shutdown(wait=True, cancel_futures=False)
