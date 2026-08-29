"""受限 home 目录扫描后台任务。"""

from __future__ import annotations

import grp
import heapq
import os
import pwd
import re
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lab_ops_panel.infrastructure.tasks import TaskRepository

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
    target_uid: int | None = None
    include_hidden: bool = True
    cross_filesystems: bool = False
    include_unreadable: bool = True
    throttle_milliseconds: int = 0
    snapshot_ttl_seconds: int = 900
    state: str = "queued"
    nodes_scanned: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        """返回可发送到 Web 层的任务字段。"""

        expired = bool(
            self.result and datetime.now(UTC) >= datetime.fromisoformat(self.result["expires_at"])
        )
        return {
            "id": self.id,
            "username": self.username,
            "root": str(self.root),
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "target_uid": self.target_uid,
            "include_hidden": self.include_hidden,
            "cross_filesystems": self.cross_filesystems,
            "include_unreadable": self.include_unreadable,
            "throttle_milliseconds": self.throttle_milliseconds,
            "state": self.state,
            "nodes_scanned": self.nodes_scanned,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "snapshot_expired": expired,
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


def _local_identity(identity: str, passwd_path: Path = Path("/etc/passwd")) -> tuple[str, int]:
    """按用户名或十进制 UID 解析本机文件账号，返回稳定 UID。"""

    numeric_uid = int(identity) if identity.isdecimal() else None
    if numeric_uid is not None and numeric_uid > 4_294_967_294:
        raise ValueError("UID 超出本机有效范围")
    if numeric_uid is None and not USERNAME.fullmatch(identity):
        raise ValueError("用户名或 UID 格式无效")
    try:
        lines = passwd_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("无法读取本机账号来源") from exc
    for line in lines:
        fields = line.split(":")
        if len(fields) != 7:
            continue
        try:
            uid = int(fields[2])
        except ValueError:
            continue
        if (numeric_uid is not None and uid == numeric_uid) or fields[0] == identity:
            return fields[0], uid
    if numeric_uid is not None:
        return f"UID {numeric_uid}", numeric_uid
    raise ValueError("本机账号不存在")


class _TreeScanner:
    """实现不跟随符号链接、默认不跨文件系统的目录遍历。"""

    def __init__(self, task: ScanTask) -> None:
        self.task = task
        self.root_device = task.root.stat().st_dev
        self.seen_files: set[tuple[int, int]] = set()
        self.unreadable_count = 0
        self.changed_count = 0
        self.cross_filesystem_count = 0
        self._largest: list[tuple[int, str, dict[str, Any]]] = []

    def scan(self) -> dict[str, Any]:
        """返回可供 treemap 使用的聚合树。"""

        tree = self._visit(self.task.root, 0)
        if tree is None:
            tree = self._empty_root()
        scanned_at = datetime.now(UTC)
        return {
            "scanned_at": scanned_at.isoformat(),
            "expires_at": (
                scanned_at + timedelta(seconds=self.task.snapshot_ttl_seconds)
            ).isoformat(),
            "scope": str(self.task.root),
            "target_uid": self.task.target_uid,
            "cross_filesystems": self.task.cross_filesystems,
            "hardlink_policy": "同一设备和 inode 仅计算一次实际占用",
            "nodes_scanned": self.task.nodes_scanned,
            "unreadable_count": self.unreadable_count,
            "changed_count": self.changed_count,
            "cross_filesystem_count": self.cross_filesystem_count,
            "largest_paths": [item[2] for item in sorted(self._largest, reverse=True)],
            "tree": tree,
        }

    def _visit(self, path: Path, depth: int) -> dict[str, Any] | None:
        if self.task.cancel_event.is_set():
            raise ScanCancelled
        if self.task.nodes_scanned >= self.task.max_nodes:
            raise ScanLimitReached
        self.task.nodes_scanned += 1
        if self.task.throttle_milliseconds:
            time.sleep(self.task.throttle_milliseconds / 1000)
        try:
            metadata = path.lstat()
        except OSError as exc:
            self.unreadable_count += 1
            if not self.task.include_unreadable:
                return None
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
            "changed_during_scan": False,
            "children": [],
        }
        if node["cross_filesystem"]:
            self.cross_filesystem_count += 1
            if not self.task.cross_filesystems:
                if stat.S_ISDIR(metadata.st_mode):
                    node["type"] = "directory"
                elif stat.S_ISREG(metadata.st_mode):
                    node["type"] = "file"
                elif stat.S_ISLNK(metadata.st_mode):
                    node["type"] = "symlink"
                return node
        if (
            self.task.target_uid is not None
            and not stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid != self.task.target_uid
        ):
            return None
        if stat.S_ISLNK(metadata.st_mode):
            node["type"] = "symlink"
            return node
        if stat.S_ISREG(metadata.st_mode):
            if self.task.target_uid is not None and metadata.st_uid != self.task.target_uid:
                return None
            node["type"] = "file"
            node["logical_size"] = metadata.st_size
            node["file_count"] = 1
            identity = (metadata.st_dev, metadata.st_ino)
            if metadata.st_nlink > 1 and identity in self.seen_files:
                node["hardlink_duplicate"] = True
            else:
                self.seen_files.add(identity)
                node["allocated_size"] = metadata.st_blocks * 512
            largest = {
                "path": str(path),
                "logical_size": node["logical_size"],
                "allocated_size": node["allocated_size"],
                "owner_uid": metadata.st_uid,
            }
            candidate = (node["allocated_size"], str(path), largest)
            if len(self._largest) < 20:
                heapq.heappush(self._largest, candidate)
            elif candidate[:2] > self._largest[0][:2]:
                heapq.heapreplace(self._largest, candidate)
            return node
        if not stat.S_ISDIR(metadata.st_mode):
            return node
        node["type"] = "directory"
        if depth >= self.task.max_depth:
            return node
        children = []
        before_mtime = metadata.st_mtime_ns
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if not self.task.include_hidden and entry.name.startswith("."):
                        continue
                    child = self._visit(Path(entry.path), depth + 1)
                    if child is not None:
                        children.append(child)
        except OSError as exc:
            self.unreadable_count += 1
            if not self.task.include_unreadable:
                return None
            node["unreadable"] = True
            node["error"] = type(exc).__name__
            return node
        try:
            node["changed_during_scan"] = path.stat().st_mtime_ns != before_mtime
        except OSError:
            node["changed_during_scan"] = True
        if node["changed_during_scan"]:
            self.changed_count += 1
        children.sort(key=lambda child: child["allocated_size"], reverse=True)
        node["children"] = children
        node["logical_size"] = sum(child["logical_size"] for child in children)
        node["allocated_size"] = sum(child["allocated_size"] for child in children)
        node["file_count"] = sum(child["file_count"] for child in children)
        if self.task.target_uid is not None and depth > 0 and not children:
            return None
        return node

    def _empty_root(self) -> dict[str, Any]:
        """在过滤结果为空时仍返回明确范围节点。"""

        node = self._error_node(self.task.root, "no_matching_files")
        node["type"] = "directory"
        node["unreadable"] = False
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
            "changed_during_scan": False,
            "children": [],
            "error": error,
        }


class HomeScanManager:
    """以单工作线程限速执行扫描，并提供幂等启动和取消。"""

    def __init__(self, tasks: TaskRepository | None = None, task_kind: str = "home_scan") -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="home-scan")
        self._lock = threading.Lock()
        self._tasks: dict[str, ScanTask] = {}
        self._idempotency: dict[str, tuple[Any, ...]] = {}
        self._repository = tasks
        self._task_kind = task_kind

    def start(
        self,
        username: str,
        idempotency_key: str,
        max_depth: int,
        max_nodes: int,
        passwd_path: Path = Path("/etc/passwd"),
        throttle_milliseconds: int = 0,
        snapshot_ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        """校验本机账号后创建或复用同参数扫描任务。"""

        _, root = _local_home(username, passwd_path)
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            signature = (
                username,
                str(root),
                max_depth,
                max_nodes,
                throttle_milliseconds,
                snapshot_ttl_seconds,
            )
            if existing:
                if existing[1:] != signature:
                    raise ValueError("幂等键已用于不同扫描参数")
                return self._tasks[existing[0]].public()
            task_id = uuid.uuid4().hex
            if self._repository:
                persistent = self._repository.create_or_get(
                    self._task_kind,
                    idempotency_key,
                    {
                        "username": username,
                        "scope": str(root),
                        "max_depth": max_depth,
                        "max_nodes": max_nodes,
                        "throttle_milliseconds": throttle_milliseconds,
                        "snapshot_ttl_seconds": snapshot_ttl_seconds,
                    },
                    f"{username}:{root}",
                    ["扫描并生成限时空间快照"],
                )
                if persistent["id"] in self._tasks:
                    return self._tasks[persistent["id"]].public()
                if persistent["state"] != "queued":
                    raise ValueError("幂等键属于上次运行的扫描任务，请使用新幂等键")
                task_id = persistent["id"]
            task = ScanTask(
                id=task_id,
                idempotency_key=idempotency_key,
                username=username,
                root=root,
                max_depth=max_depth,
                max_nodes=max_nodes,
                throttle_milliseconds=throttle_milliseconds,
                snapshot_ttl_seconds=snapshot_ttl_seconds,
            )
            self._tasks[task.id] = task
            self._idempotency[idempotency_key] = (
                task.id,
                username,
                str(root),
                max_depth,
                max_nodes,
                throttle_milliseconds,
                snapshot_ttl_seconds,
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
            if self._repository:
                self._repository.transition(task.id, "prechecking")
                self._repository.update_step(task.id, 1, "running")
                self._repository.transition(task.id, "running")
            task.result = _TreeScanner(task).scan()
            task.state = "completed"
            if self._repository:
                self._repository.save_scan_snapshot(task.id, task.result)
                self._repository.update_step(
                    task.id,
                    1,
                    "succeeded",
                    {
                        "nodes_scanned": task.nodes_scanned,
                        "expires_at": task.result["expires_at"],
                    },
                )
                self._repository.transition(task.id, "succeeded")
        except ScanCancelled:
            task.state = "cancelled"
            if self._repository:
                self._repository.update_step(task.id, 1, "cancelled")
                self._repository.transition(task.id, "cancelled")
        except ScanLimitReached:
            task.state = "failed"
            task.error = f"扫描节点达到安全上限 {task.max_nodes}"
            self._persist_failure(task)
        except Exception as exc:
            task.state = "failed"
            task.error = f"扫描失败：{type(exc).__name__}"
            self._persist_failure(task)
        finally:
            task.finished_at = datetime.now(UTC).isoformat()

    def _persist_failure(self, task: ScanTask) -> None:
        """尽力把后台扫描失败写入持久化任务，不掩盖原始错误。"""

        if not self._repository:
            return
        try:
            self._repository.update_step(task.id, 1, "failed", {"reason": task.error})
            self._repository.transition(task.id, "failed", task.error)
        except (KeyError, ValueError):
            return

    def close(self) -> None:
        """停止接收新扫描并等待当前只读遍历安全结束。"""

        self._executor.shutdown(wait=True, cancel_futures=False)


class UserStorageScanManager(HomeScanManager):
    """在管理员选定的当前挂载点内按稳定 UID 汇总文件占用。"""

    def start(  # type: ignore[override]
        self,
        identity: str,
        mountpoint: str,
        idempotency_key: str,
        max_depth: int,
        max_nodes: int,
        include_hidden: bool,
        cross_filesystems: bool,
        include_unreadable: bool,
        throttle_milliseconds: int,
        snapshot_ttl_seconds: int,
        passwd_path: Path = Path("/etc/passwd"),
        allowed_mountpoints: set[str] | None = None,
    ) -> dict[str, Any]:
        """校验账号与挂载点后创建可取消、有限速的扫描任务。"""

        username, uid = _local_identity(identity, passwd_path)
        try:
            root = Path(mountpoint).resolve(strict=True)
        except OSError as exc:
            raise ValueError("挂载点不存在或无法解析") from exc
        if not root.is_dir():
            raise ValueError("扫描范围不是目录")
        allowed = allowed_mountpoints
        if allowed is None:
            from lab_ops_panel.infrastructure.system_info import mount_points

            allowed = {str(Path(item["mountpoint"]).resolve()) for item in mount_points()}
        if str(root) not in allowed:
            raise ValueError("扫描范围必须是当前可选择的完整挂载点")
        signature = (
            username,
            uid,
            str(root),
            max_depth,
            max_nodes,
            include_hidden,
            cross_filesystems,
            include_unreadable,
            throttle_milliseconds,
            snapshot_ttl_seconds,
        )
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing:
                if existing[1:] != signature:
                    raise ValueError("幂等键已用于不同扫描参数")
                return self._tasks[existing[0]].public()
            task_id = uuid.uuid4().hex
            if self._repository:
                persistent = self._repository.create_or_get(
                    self._task_kind,
                    idempotency_key,
                    {
                        "username": username,
                        "uid": uid,
                        "scope": str(root),
                        "max_depth": max_depth,
                        "max_nodes": max_nodes,
                        "include_hidden": include_hidden,
                        "cross_filesystems": cross_filesystems,
                        "include_unreadable": include_unreadable,
                        "throttle_milliseconds": throttle_milliseconds,
                        "snapshot_ttl_seconds": snapshot_ttl_seconds,
                    },
                    f"UID {uid}:{root}",
                    ["按 UID 扫描并生成限时空间快照"],
                )
                if persistent["id"] in self._tasks:
                    return self._tasks[persistent["id"]].public()
                if persistent["state"] != "queued":
                    raise ValueError("幂等键属于上次运行的扫描任务，请使用新幂等键")
                task_id = persistent["id"]
            task = ScanTask(
                id=task_id,
                idempotency_key=idempotency_key,
                username=username,
                root=root,
                max_depth=max_depth,
                max_nodes=max_nodes,
                target_uid=uid,
                include_hidden=include_hidden,
                cross_filesystems=cross_filesystems,
                include_unreadable=include_unreadable,
                throttle_milliseconds=throttle_milliseconds,
                snapshot_ttl_seconds=snapshot_ttl_seconds,
            )
            self._tasks[task.id] = task
            self._idempotency[idempotency_key] = (task.id, *signature)
            self._executor.submit(self._run, task)
            return task.public()


home_scan_manager = HomeScanManager()
user_storage_scan_manager = UserStorageScanManager()
