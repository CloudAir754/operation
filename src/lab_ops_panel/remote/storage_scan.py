"""磁盘空间扫描结果处理：路径校验、硬链接去重、聚合、过滤与分块。

探针只负责 ``lstat`` 与遍历；所有口径（硬链接、稀疏文件、incomplete 传播、
过滤后的聚合）在本地实现，便于用临时目录做确定性测试。
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lab_ops_panel.schemas_observability import (
    StorageNodeOut,
    StorageUidSummaryOut,
)

MAX_RAW_NODES = 6000
MAX_VIEW_NODES = 3000
MAX_LARGEST_FILES = 100
MAX_DEPTH = 8
MAX_TEXT = 300
NODE_KINDS = {"dir", "file", "symlink", "other"}


@dataclass(frozen=True)
class RawNode:
    path: str
    name: str
    kind: str
    allocated_bytes: int | None
    apparent_bytes: int | None
    uid: int | None
    gid: int | None
    device: int | None
    inode: int | None
    modified_at: datetime | None
    hardlink_duplicate_of: str | None
    deleted_open: bool
    unscanned: bool
    incomplete: bool
    error: str | None

    @property
    def counted(self) -> bool:
        return self.hardlink_duplicate_of is None

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"


@dataclass
class ScanFilters:
    min_file_size: int = 0
    uid: int | None = None
    extension: str | None = None
    modified_after: datetime | None = None

    def normalized_extension(self) -> str | None:
        if not self.extension:
            return None
        value = self.extension.strip().lower()
        if not value:
            return None
        return value if value.startswith(".") else f".{value}"

    def matches_file(self, node: RawNode) -> bool:
        size = node.apparent_bytes or node.allocated_bytes or 0
        if size < self.min_file_size:
            return False
        if self.uid is not None and node.uid != self.uid:
            return False
        extension = self.normalized_extension()
        if extension is not None and not node.name.lower().endswith(extension):
            return False
        if self.modified_after is not None:
            if node.modified_at is None:
                return False
            cutoff = self.modified_after
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=UTC)
            if node.modified_at < cutoff:
                return False
        return True


@dataclass
class AggregatedNode:
    raw: RawNode
    allocated_bytes: int = 0
    apparent_bytes: int = 0
    file_count: int = 0
    directory_count: int = 0
    incomplete: bool = False
    children: list[str] = field(default_factory=list)


@dataclass
class ScanView:
    nodes: list[StorageNodeOut]
    largest_files: list[StorageNodeOut]
    summary: list[StorageUidSummaryOut]
    node_count: int
    truncated: bool
    incomplete: bool
    deleted_open_bytes: int
    deleted_open_count: int


def normalize_mount(mount: str) -> str:
    text = (mount or "").strip()
    if not text.startswith("/"):
        raise ValueError("挂载点必须是绝对路径")
    normalized = posixpath.normpath(text)
    if any(part in {".", ".."} for part in text.split("/")):
        raise ValueError("挂载点不允许包含 . 或 .. 路径段")
    return "/" if normalized in {"", "."} else normalized


def normalize_scan_path(mount: str, path: str) -> str:
    """校验扫描路径必须位于挂载点内（供 children 接口复用）。"""

    base = normalize_mount(mount)
    candidate = (path or "").strip()
    if not candidate.startswith("/"):
        raise ValueError("路径必须是绝对路径")
    if "\x00" in candidate or "\n" in candidate:
        raise ValueError("路径包含非法字符")
    parts = [part for part in candidate.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("路径不允许包含 . 或 .. 路径段")
    resolved = "/" + "/".join(parts) if parts else "/"
    if resolved != base and not resolved.startswith(base.rstrip("/") + "/"):
        raise ValueError("路径不在扫描挂载点内")
    return resolved


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    return text[:limit] if text else None


def _kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"dir", "directory"}:
        return "dir"
    if text in {"file", "symlink", "other"}:
        return text
    return "other"


def _modified_at(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    if epoch <= 0 or epoch > 4102444800:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


def parse_nodes(raw_nodes: Any, *, mount: str) -> list[RawNode]:
    """校验并规范化探针返回的节点列表。"""

    base = normalize_mount(mount)
    nodes: list[RawNode] = []
    seen: set[str] = set()
    for item in raw_nodes or []:
        if not isinstance(item, dict):
            continue
        raw_path = _text(item.get("path"), 4096)
        if not raw_path:
            continue
        try:
            path = normalize_scan_path(base, raw_path)
        except ValueError:
            continue
        if path in seen:
            continue
        seen.add(path)
        nodes.append(
            RawNode(
                path=path,
                name=_text(item.get("name"), 255) or posixpath.basename(path) or path,
                kind=_kind(item.get("kind")),
                allocated_bytes=_int(item.get("allocatedBytes")),
                apparent_bytes=_int(item.get("apparentBytes")),
                uid=_int(item.get("uid")),
                gid=_int(item.get("gid")),
                device=_int(item.get("device")),
                inode=_int(item.get("inode")),
                modified_at=_modified_at(item.get("modifiedAtEpoch")),
                hardlink_duplicate_of=_text(item.get("hardlinkDuplicateOf"), 4096),
                deleted_open=bool(item.get("deletedOpen", False)),
                unscanned=bool(item.get("unscanned", False)),
                incomplete=bool(item.get("incomplete", False)),
                error=_text(item.get("error"), 240),
            )
        )
        if len(nodes) >= MAX_RAW_NODES:
            break
    return nodes


def _ensure_root(nodes: list[RawNode], mount: str) -> list[RawNode]:
    if any(node.path == mount for node in nodes):
        return nodes
    root = RawNode(
        path=mount,
        name=posixpath.basename(mount) or mount,
        kind="dir",
        allocated_bytes=0,
        apparent_bytes=0,
        uid=None,
        gid=None,
        device=None,
        inode=None,
        modified_at=None,
        hardlink_duplicate_of=None,
        deleted_open=False,
        unscanned=False,
        incomplete=False,
        error=None,
    )
    return [root, *nodes]


def _ancestors(path: str, mount: str) -> list[str]:
    chain: list[str] = []
    current = path
    while current != mount and current.startswith(mount.rstrip("/") + "/"):
        current = posixpath.dirname(current)
        chain.append(current)
    return chain


def _node_id(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def build_scan_view(
    raw_nodes: list[RawNode],
    *,
    mount: str,
    filters: ScanFilters | None = None,
    probe_incomplete: bool = False,
    probe_truncated: bool = False,
    max_nodes: int = MAX_VIEW_NODES,
) -> ScanView:
    base = normalize_mount(mount)
    nodes = _ensure_root(raw_nodes, base)
    applied = filters or ScanFilters()
    by_path = {node.path: node for node in nodes}

    # 1. 选中要保留的文件；目录保留其祖先链，保证树可展开。
    #    硬链接重复项仍然展示（标记 hardlinkDuplicateOf），但不计入任何聚合。
    kept_files: list[RawNode] = []
    displayed_files: list[RawNode] = []
    kept_dirs: set[str] = {base}
    for node in nodes:
        if node.is_dir:
            if node.incomplete or node.error or node.unscanned:
                kept_dirs.add(node.path)
            continue
        if not applied.matches_file(node):
            continue
        displayed_files.append(node)
        if not node.counted:
            continue
        kept_files.append(node)
        for ancestor in _ancestors(node.path, base):
            kept_dirs.add(ancestor)

    aggregates: dict[str, AggregatedNode] = {}

    def ensure(path: str) -> AggregatedNode:
        existing = aggregates.get(path)
        if existing is not None:
            return existing
        raw = by_path.get(path)
        if raw is None:
            raw = RawNode(
                path=path,
                name=posixpath.basename(path) or path,
                kind="dir",
                allocated_bytes=0,
                apparent_bytes=0,
                uid=None,
                gid=None,
                device=None,
                inode=None,
                modified_at=None,
                hardlink_duplicate_of=None,
                deleted_open=False,
                unscanned=False,
                incomplete=False,
                error=None,
            )
        item = AggregatedNode(raw=raw)
        if raw.incomplete or raw.error or raw.unscanned:
            item.incomplete = True
        aggregates[path] = item
        return item

    ensure(base)
    for path in kept_dirs:
        ensure(path)
    for node in displayed_files:
        ensure(node.path)
    for node in kept_files:
        item = ensure(node.path)
        item.allocated_bytes = node.allocated_bytes or 0
        item.apparent_bytes = node.apparent_bytes or node.allocated_bytes or 0
        item.file_count = 1
        # 目录自身不计入文件空间；逐级累加。
        for ancestor in _ancestors(node.path, base):
            parent = ensure(ancestor)
            parent.allocated_bytes += item.allocated_bytes
            parent.apparent_bytes += item.apparent_bytes
            parent.file_count += 1
    for path in kept_dirs:
        ensure(path).directory_count = 1
    for path in sorted(kept_dirs, key=len, reverse=True):
        if path == base:
            continue
        for ancestor in _ancestors(path, base):
            ensure(ancestor).directory_count += 1
    for node in displayed_files:
        if node.counted:
            continue
        item = ensure(node.path)
        item.allocated_bytes = node.allocated_bytes or 0
        item.apparent_bytes = node.apparent_bytes or node.allocated_bytes or 0
        item.file_count = 0

    # 2. 子节点索引（只保留保留集合内的边）。
    children: dict[str, list[str]] = {path: [] for path in aggregates}
    for path in aggregates:
        if path == base:
            continue
        parent = posixpath.dirname(path)
        if parent in children:
            children[parent].append(path)

    for _path, kids in children.items():
        kids.sort(
            key=lambda child: (
                not aggregates[child].raw.is_dir,
                -(aggregates[child].allocated_bytes or 0),
                aggregates[child].raw.name,
            )
        )

    # 3. incomplete 自底向上传播。
    for path in sorted(aggregates, key=lambda item: len(item), reverse=True):
        item = aggregates[path]
        if any(aggregates[child].incomplete for child in children.get(path, ())):
            item.incomplete = True
        if item.raw.unscanned:
            item.incomplete = True

    ordered: list[str] = []

    def walk(path: str) -> None:
        if len(ordered) >= max_nodes:
            return
        ordered.append(path)
        for child in children.get(path, ()):  # type: ignore[arg-type]
            walk(child)

    walk(base)

    def to_out(path: str) -> StorageNodeOut:
        item = aggregates[path]
        raw = item.raw
        return StorageNodeOut(
            id=_node_id(path),
            path=path,
            parent_path=None if path == base else posixpath.dirname(path),
            name=raw.name,
            kind=raw.kind,  # type: ignore[arg-type]
            allocated_bytes=item.allocated_bytes,
            apparent_bytes=item.apparent_bytes,
            file_count=item.file_count,
            directory_count=item.directory_count,
            uid=raw.uid,
            gid=raw.gid,
            inode=raw.inode,
            hardlink_duplicate_of=raw.hardlink_duplicate_of,
            deleted_open=raw.deleted_open,
            unscanned=raw.unscanned,
            incomplete=item.incomplete,
            error=raw.error,
            modified_at=raw.modified_at,
        )

    view_nodes = [to_out(path) for path in ordered]
    truncated = len(ordered) >= max_nodes and len(ordered) < len(aggregates)
    largest = sorted(
        kept_files,
        key=lambda node: (
            -(node.allocated_bytes or node.apparent_bytes or 0),
            node.path,
        ),
    )[:MAX_LARGEST_FILES]
    largest_out = [to_out(node.path) for node in largest]

    summary: dict[int, dict[str, Any]] = {}
    for node in kept_files:
        if node.uid is None:
            continue
        entry = summary.setdefault(
            node.uid,
            {
                "allocated": 0,
                "apparent": 0,
                "files": 0,
                "dirs": 0,
                "incomplete": False,
            },
        )
        entry["allocated"] += node.allocated_bytes or 0
        entry["apparent"] += node.apparent_bytes or node.allocated_bytes or 0
        entry["files"] += 1
        if node.incomplete or node.error:
            entry["incomplete"] = True
    for path in kept_dirs:
        item = aggregates.get(path)
        if item is None or item.raw.uid is None:
            continue
        entry = summary.setdefault(
            item.raw.uid,
            {"allocated": 0, "apparent": 0, "files": 0, "dirs": 0, "incomplete": False},
        )
        entry["dirs"] += 1
        if item.incomplete:
            entry["incomplete"] = True

    summary_out = [
        StorageUidSummaryOut(
            uid=uid,
            allocated_bytes=entry["allocated"],
            apparent_bytes=entry["apparent"],
            file_count=entry["files"],
            directory_count=entry["dirs"],
            incomplete=entry["incomplete"],
        )
        for uid, entry in sorted(summary.items())
        if entry["files"] or entry["allocated"] or entry["apparent"]
    ]

    deleted_nodes = [node for node in nodes if node.deleted_open and node.counted]
    incomplete = (
        probe_incomplete or probe_truncated or any(item.incomplete for item in aggregates.values())
    )
    return ScanView(
        nodes=view_nodes,
        largest_files=largest_out,
        summary=summary_out,
        node_count=len(aggregates),
        truncated=truncated or probe_truncated,
        incomplete=incomplete,
        deleted_open_bytes=sum(node.allocated_bytes or 0 for node in deleted_nodes),
        deleted_open_count=len(deleted_nodes),
    )


def chunk_nodes(nodes: list[StorageNodeOut], *, size: int = 200) -> list[list[StorageNodeOut]]:
    if size <= 0:
        raise ValueError("分块大小必须为正数")
    return [nodes[index : index + size] for index in range(0, len(nodes), size)]


def scan_children(nodes: list[StorageNodeOut], path: str) -> list[StorageNodeOut]:
    """从已完成扫描结果中取直接子节点，避免额外远端调用。"""

    return [node for node in nodes if node.parent_path == path]


def mount_matches(requested: str, allowed: list[str]) -> str | None:
    """在挂载点 allowlist 中匹配请求路径；返回规范化后的挂载点。"""

    try:
        normalized = normalize_mount(requested)
    except ValueError:
        return None
    for mount in allowed:
        try:
            candidate = normalize_mount(mount)
        except ValueError:
            continue
        if normalized == candidate:
            return candidate
    return None


def scan_is_stale(finished_at: datetime | None, *, ttl_seconds: int) -> bool:
    if finished_at is None:
        return True
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - finished_at).total_seconds()
    return age > ttl_seconds
