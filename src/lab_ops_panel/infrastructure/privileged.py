"""普通 Web 进程调用 root 所有特权助手的唯一适配器。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_HELPER = Path("/usr/local/libexec/labopspanel-privileged")
SUDO = Path("/usr/bin/sudo")
ALLOWED_ACTIONS = frozenset(
    {
        "create-user",
        "ssh-import",
        "sudo-grant",
        "sudo-revoke",
        "pam-update",
        "pam-restore",
        "port-lookup",
        "process-terminate",
        "uid-processes-terminate",
        "user-delete-preflight",
        "user-delete",
        "file-transfer-preflight",
        "file-transfer-copy",
        "file-transfer-cleanup",
        "home-migration-preflight",
        "home-migration-execute",
        "home-migration-status",
        "home-migration-rollback",
        "home-migration-cleanup",
        "disk-initialization-plan",
        "disk-initialization-execute",
        "disk-initialization-status",
    }
)
ACTION_TIMEOUTS = {
    # home 占用统计和同文件系统递归清理可能明显超过普通账号动作。
    "user-delete-preflight": 600,
    "user-delete": 3600,
    "file-transfer-preflight": 3600,
    "file-transfer-copy": 7200,
    "file-transfer-cleanup": 3600,
    "home-migration-preflight": 3600,
    "home-migration-execute": 14_400,
    "home-migration-status": 120,
    "home-migration-rollback": 3600,
    "home-migration-cleanup": 7200,
    "disk-initialization-plan": 300,
    "disk-initialization-execute": 7200,
    "disk-initialization-status": 120,
}


def helper_capability(path: Path = DEFAULT_HELPER) -> dict[str, Any]:
    """验证助手及全部父目录均由 root 控制且不可被其他用户改写。"""

    try:
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise ValueError
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError
        parent = path.parent
        while True:
            parent_metadata = parent.stat()
            if parent_metadata.st_uid != 0 or parent_metadata.st_mode & 0o022:
                raise ValueError
            if parent == Path("/"):
                break
            parent = parent.parent
    except (OSError, ValueError):
        return {
            "available": False,
            "reason": "特权助手不存在，或文件/父目录并非 root 所有且不可写",
            "path": str(path),
        }
    return {"available": True, "reason": None, "path": str(path)}


class PrivilegedExecutor:
    """只允许固定动作名，通过 stdin 发送结构化参数。"""

    def __init__(self, helper_path: Path = DEFAULT_HELPER) -> None:
        self.helper_path = helper_path

    def execute(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """调用可信助手；参数不进入 argv、日志或异常文本。"""

        if action not in ALLOWED_ACTIONS:
            raise ValueError("不支持的特权动作")
        capability = helper_capability(self.helper_path)
        if not capability["available"]:
            raise RuntimeError(str(capability["reason"]))
        request_payload = dict(payload)
        if action in {
            "process-terminate",
            "uid-processes-terminate",
            "user-delete-preflight",
            "user-delete",
            "home-migration-execute",
            "home-migration-rollback",
        }:
            request_payload["requester_pid"] = os.getpid()
        encoded = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > 1_000_000:
            raise ValueError("特权动作参数过大")
        try:
            completed = subprocess.run(
                [str(SUDO), "--non-interactive", str(self.helper_path), action],
                input=encoded,
                capture_output=True,
                text=True,
                check=False,
                timeout=ACTION_TIMEOUTS.get(action, 120),
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError("特权助手无法执行") from exc
        try:
            result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("特权助手返回无法解析的结果") from exc
        if completed.returncode != 0 or not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "特权动作失败"))
        return dict(result["result"])
