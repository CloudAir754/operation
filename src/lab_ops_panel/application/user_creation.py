"""批量用户创建任务编排。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from lab_ops_panel.application.credentials import export_initial_credentials
from lab_ops_panel.domain.passwords import PasswordPolicy, generate_password
from lab_ops_panel.infrastructure.pam import pam_policy_snapshot
from lab_ops_panel.infrastructure.system_info import local_users
from lab_ops_panel.infrastructure.tasks import TaskRepository


class PrivilegedUserExecutor(Protocol):
    """用户创建任务依赖的最小特权执行协议。"""

    def execute(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """执行固定结构化动作。"""


def _effective_policy(policy: PasswordPolicy, pam: dict[str, Any]) -> PasswordPolicy:
    """把应用规则提高到可证明的 PAM 字符类别与长度下限。"""

    minimums = pam["generation_minimums"]
    values = {
        "length": max(policy.length, minimums["length"]),
        "min_uppercase": max(policy.min_uppercase, minimums["min_uppercase"]),
        "min_lowercase": max(policy.min_lowercase, minimums["min_lowercase"]),
        "min_digits": max(policy.min_digits, minimums["min_digits"]),
        "min_symbols": max(policy.min_symbols, minimums["min_symbols"]),
    }
    required_classes = int(minimums.get("min_classes", 0))
    category_names = ("min_lowercase", "min_uppercase", "min_digits", "min_symbols")
    for name in category_names:
        if sum(values[item] > 0 for item in category_names) >= required_classes:
            break
        values[name] = max(1, values[name])
    effective = replace(policy, **values)
    effective.validate()
    return effective


class UserCreationManager:
    """单线程执行用户创建，明文密码仅存在于工作线程和导出文件。"""

    def __init__(
        self,
        tasks: TaskRepository,
        executor: PrivilegedUserExecutor,
        data_directory: Path,
    ) -> None:
        self.tasks = tasks
        self.executor = executor
        self.data_directory = data_directory
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="user-create")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def start(
        self,
        rows: list[dict[str, str]],
        policy: PasswordPolicy,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """创建幂等持久化任务，并只调度一次工作线程。"""

        parameters = {"rows": rows, "policy": policy.to_dict()}
        task = self.tasks.create_or_get(
            "create_users",
            idempotency_key,
            parameters,
            f"批量用户:{len(rows)}",
            ["重新预检", "逐个创建账号", "导出一次性凭据", "结果验证"],
        )
        if task["state"] == "queued":
            with self._lock:
                if task["id"] not in self._scheduled:
                    self._scheduled.add(task["id"])
                    self._pool.submit(self._run, task["id"], rows, policy)
        return self.tasks.get(task["id"])

    def close(self) -> None:
        """停止接收新任务，并等待当前用户创建安全收尾。"""

        self._pool.shutdown(wait=True, cancel_futures=False)

    def _run(self, task_id: str, rows: list[dict[str, str]], policy: PasswordPolicy) -> None:
        credentials: list[tuple[str, str, str]] = []
        current_step = 1
        try:
            self.tasks.transition(task_id, "prechecking")
            self.tasks.update_step(task_id, 1, "running")
            pam = pam_policy_snapshot()
            effective_policy = _effective_policy(policy, pam)
            self.tasks.update_step(
                task_id,
                1,
                "succeeded",
                {
                    "users": len(rows),
                    "generated_length": effective_policy.length,
                    "pam_pwquality_enabled": pam["pwquality_enabled"],
                },
            )
            self.tasks.transition(task_id, "running")
            current_step = 2
            self.tasks.update_step(task_id, 2, "running")
            created = []
            for row in rows:
                password = generate_password(effective_policy)
                result = self.executor.execute(
                    "create-user",
                    {
                        "username": row["username"],
                        "display_name": row["display_name"],
                        "password": password,
                        "shell": "/bin/bash",
                    },
                )
                if not result.get("created"):
                    raise RuntimeError(f"账号 {row['username']} 在执行期间已存在")
                credentials.append((row["username"], row["display_name"], password))
                created.append(row["username"])
            self.tasks.update_step(task_id, 2, "succeeded", {"created_usernames": created})
            current_step = 3
            self.tasks.update_step(task_id, 3, "running")
            export_path = export_initial_credentials(self.data_directory, task_id, credentials)
            credentials.clear()
            self.tasks.update_step(
                task_id, 3, "succeeded", {"export_path": str(export_path), "mode": "0600"}
            )
            current_step = 4
            facts = {str(user["username"]): user for user in local_users()}
            missing = [username for username in created if username not in facts]
            if missing:
                raise RuntimeError("创建结果验证未找到部分本机账号")
            self.tasks.update_step(task_id, 4, "succeeded", {"created_usernames": created})
            self.tasks.transition(task_id, "succeeded")
            self.tasks.log("批量创建用户", f"批量用户:{len(rows)}", "succeeded", task_id)
        except Exception as exc:
            if credentials:
                try:
                    export_path = export_initial_credentials(
                        self.data_directory, task_id, credentials
                    )
                    self.tasks.update_step(
                        task_id,
                        3,
                        "succeeded",
                        {
                            "export_path": str(export_path),
                            "mode": "0600",
                            "partial": True,
                        },
                    )
                except Exception:
                    pass
            credentials.clear()
            task = self.tasks.get(task_id)
            if task["state"] in {"prechecking", "running"}:
                step = next(item for item in task["steps"] if item["sequence"] == current_step)
                if step["state"] in {"pending", "running"}:
                    self.tasks.update_step(task_id, current_step, "failed", {"reason": str(exc)})
                self.tasks.transition(task_id, "failed", str(exc))
            self.tasks.log("批量创建用户", f"批量用户:{len(rows)}", "failed", task_id)
        finally:
            with self._lock:
                self._scheduled.discard(task_id)
