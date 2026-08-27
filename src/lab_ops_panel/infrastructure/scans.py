"""受限 home 目录扫描后台任务。"""

from __future__ import annotations

import grp
import os
import pwd
import re
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USERNAME = re.compile(r"^[a-z_][a-z0-9_.-]*[$]?$", re.IGNORECASE)
TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})


class ScanCancelled(Exception):
    """表示管理员取消了只读扫描。"""


class ScanLimitReached(Exception):
    """表示扫描节点达到安全上限。"""


@dataclass(slots=True)
class ScanTask:
    """保存单次扫描的短期状态，不包含用户文件内容。"""

    id: str
    idempotency_key: str
    username: str
    root: Path
    max_depth: int
    max_nodes: int
    state: str = "queued"
    nodes_scanned: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        """返回可发送到 Web 层的任务字段。"""

        return {
            "id": self.id,
            "username": self.username,
            "root": str(self.root),
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "state": self.state,
            "nodes_scanned": self.nodes_scanned,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
        }


def _identity(uid: int, gid: int) -> tuple[str | None, str | None]:
    """将 UID/GID 转为仅用于展示的当前名称。"""

    try:
        owner = pwd.getpwuid(uid).pw_name
    except KeyError:
        owner = None
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = None
    return owner, group


def _local_home(username: str, passwd_path: Path = Path("/etc/passwd")) -> tuple[int, Path]:
    """只从本机 passwd 文件解析用户名和 home，拒绝目录账号来源。"""

    if not USERNAME.fullmatch(username):
        raise ValueError("用户名格式无效")
    try:
        lines = passwd_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("无法读取本机账号来源") from exc
    for line in lines:
        fields = line.split(":")
        if len(fields) != 7 or fields[0] != username:
            continue
        try:
            uid = int(fields[2])
        except ValueError as exc:
            raise ValueError("账号 UID 无效") from exc
        home = Path(fields[5])
        if not home.is_absolute() or home == Path("/"):
            raise ValueError("账号 home 路径不安全")
        try:
            resolved = home.resolve(strict=True)
        except OSError as exc:
            raise ValueError("账号 home 不存在或无法解析") from exc
        if not resolved.is_dir():
            raise ValueError("账号 home 不是目录")
        return uid, resolved
    raise ValueError("本机账号不存在")


class _TreeScanner:
    """实现不跟随符号链接、默认不跨文件系统的目录遍历。"""

    def __init__(self, task: ScanTask) -> None:
        self.task = task
        self.root_device = task.root.stat().st_dev
        self.seen_files: set[tuple[int, int]] = set()

    def scan(self) -> dict[str, Any]:
        """返回可供 treemap 使用的聚合树。"""

        tree = self._visit(self.task.root, 0)
        return {
            "scanned_at": datetime.now(UTC).isoformat(),
            "scope": str(self.task.root),
            "cross_filesystems": False,
            "hardlink_policy": "同一设备和 inode 仅计算一次实际占用",
            "nodes_scanned": self.task.nodes_scanned,
            "tree": tree,
        }

    def _visit(self, path: Path, depth: int) -> dict[str, Any]:
        if self.task.cancel_event.is_set():
            raise ScanCancelled
        if self.task.nodes_scanned >= self.task.max_nodes:
            raise ScanLimitReached
        self.task.nodes_scanned += 1
        try:
            metadata = path.lstat()
        except OSError as exc:
            return self._error_node(path, type(exc).__name__)
        owner, group = _identity(metadata.st_uid, metadata.st_gid)
        node: dict[str, Any] = {
            "name": path.name or str(path),
            "path": str(path),
            "type": "other",
            "logical_size": 0,
            "allocated_size": 0,
            "file_count": 0,
            "owner_uid": metadata.st_uid,
            "owner": owner,
            "group_gid": metadata.st_gid,
            "group": group,
            "mode": stat.S_IMODE(metadata.st_mode),
            "world_writable": bool(metadata.st_mode & stat.S_IWOTH),
            "unreadable": False,
            "cross_filesystem": metadata.st_dev != self.root_device,
            "hardlink_duplicate": False,
            "children": [],
        }
        if stat.S_ISLNK(metadata.st_mode):
            node["type"] = "symlink"
            return node
        if stat.S_ISREG(metadata.st_mode):
            node["type"] = "file"
            node["logical_size"] = metadata.st_size
            node["file_count"] = 1
            identity = (metadata.st_dev, metadata.st_ino)
            if metadata.st_nlink > 1 and identity in self.seen_files:
                node["hardlink_duplicate"] = True
            else:
                self.seen_files.add(identity)
                node["allocated_size"] = metadata.st_blocks * 512
            return node
        if not stat.S_ISDIR(metadata.st_mode):
            return node
        node["type"] = "directory"
        if node["cross_filesystem"] or depth >= self.task.max_depth:
            return node
        children = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    children.append(self._visit(Path(entry.path), depth + 1))
        except OSError as exc:
            node["unreadable"] = True
            node["error"] = type(exc).__name__
            return node
        children.sort(key=lambda child: child["allocated_size"], reverse=True)
        node["children"] = children
        node["logical_size"] = sum(child["logical_size"] for child in children)
        node["allocated_size"] = sum(child["allocated_size"] for child in children)
        node["file_count"] = sum(child["file_count"] for child in children)
        return node

    @staticmethod
    def _error_node(path: Path, error: str) -> dict[str, Any]:
        """表示扫描过程中消失或无权读取的节点。"""

        return {
            "name": path.name,
            "path": str(path),
            "type": "unknown",
            "logical_size": 0,
            "allocated_size": 0,
            "file_count": 0,
            "owner_uid": None,
            "owner": None,
            "group_gid": None,
            "group": None,
            "mode": None,
            "world_writable": False,
            "unreadable": True,
            "cross_filesystem": False,
            "hardlink_duplicate": False,
            "children": [],
            "error": error,
        }


class HomeScanManager:
    """以单工作线程限速执行扫描，并提供幂等启动和取消。"""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="home-scan")
        self._lock = threading.Lock()
        self._tasks: dict[str, ScanTask] = {}
        self._idempotency: dict[str, tuple[str, str, str, int, int]] = {}

    def start(
        self,
        username: str,
        idempotency_key: str,
        max_depth: int,
        max_nodes: int,
        passwd_path: Path = Path("/etc/passwd"),
    ) -> dict[str, Any]:
        """校验本机账号后创建或复用同参数扫描任务。"""

        _, root = _local_home(username, passwd_path)
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            signature = (username, str(root), max_depth, max_nodes)
            if existing:
                if existing[1:] != signature:
                    raise ValueError("幂等键已用于不同扫描参数")
                return self._tasks[existing[0]].public()
            task = ScanTask(
                id=uuid.uuid4().hex,
                idempotency_key=idempotency_key,
                username=username,
                root=root,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
            self._tasks[task.id] = task
            self._idempotency[idempotency_key] = (
                task.id,
                username,
                str(root),
                max_depth,
                max_nodes,
            )
            self._executor.submit(self._run, task)
            return task.public()

    def get(self, task_id: str) -> dict[str, Any]:
        """返回任务快照。"""

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            return task.public()

    def cancel(self, task_id: str) -> dict[str, Any]:
        """请求取消任务；重复取消保持幂等。"""

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            task.cancel_event.set()
            return task.public()

    def _run(self, task: ScanTask) -> None:
        task.state = "running"
        task.started_at = datetime.now(UTC).isoformat()
        try:
            task.result = _TreeScanner(task).scan()
            task.state = "completed"
        except ScanCancelled:
            task.state = "cancelled"
        except ScanLimitReached:
            task.state = "failed"
            task.error = f"扫描节点达到安全上限 {task.max_nodes}"
        except Exception as exc:
            task.state = "failed"
            task.error = f"扫描失败：{type(exc).__name__}"
        finally:
            task.finished_at = datetime.now(UTC).isoformat()


home_scan_manager = HomeScanManager()
