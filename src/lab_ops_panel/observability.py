"""阶段三资源观测服务。

职责：
- 指标采样与内存环形缓存（CPU/内存/Swap、磁盘 I/O、网络 I/O、GPU 摘要）。
- GPU / 用户 / 挂载点短 TTL 缓存。
- 空间扫描任务：挂载点 allowlist、后台执行、取消、SSE 事件与重启恢复。
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from lab_ops_panel.audit import record_audit
from lab_ops_panel.db import session_scope
from lab_ops_panel.errors import ApiError, ConflictError, NotFoundError
from lab_ops_panel.models import Server, StorageScan
from lab_ops_panel.remote.gpu import normalize_gpu_monitor
from lab_ops_panel.remote.metrics import (
    build_point,
    derive_metrics,
    empty_sections,
    summarize_status,
)
from lab_ops_panel.remote.probe import run_probe
from lab_ops_panel.remote.storage_scan import (
    ScanFilters,
    build_scan_view,
    chunk_nodes,
    mount_matches,
    normalize_mount,
    normalize_scan_path,
    parse_nodes,
    scan_children,
)
from lab_ops_panel.remote.targets import target_from_server
from lab_ops_panel.remote.users import normalize_users
from lab_ops_panel.schemas_observability import (
    WINDOW_SECONDS,
    GpuMonitorOut,
    MetricPointOut,
    MetricsOut,
    MountOut,
    MountsOut,
    StorageNodeOut,
    StorageScanOut,
    StorageScanRequest,
    StorageUidSummaryOut,
    UsersOut,
    UserStorageScanRequest,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from lab_ops_panel.config import Settings
    from lab_ops_panel.ssh.transport import SshTransport
    from lab_ops_panel.tasks.events import EventBus

logger = logging.getLogger(__name__)

TERMINAL_SCAN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
MAX_MOUNTS = 100
MAX_RAW_DEVICES = 100
MAX_RAW_INTERFACES = 100
SCAN_CHUNK_SIZE = 200
MAX_VIEW_NODES = 3000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _bounded_mapping(value: Any, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in list(value.items())[:limit]}


def _age_seconds(stamp: datetime | None, now: datetime) -> float:
    if stamp is None:
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (now - stamp).total_seconds())


@dataclass
class _MetricsState:
    points: deque[MetricPointOut]
    last_raw: dict[str, Any] | None = None
    last_received_at: datetime | None = None
    last_status: str = "ok"
    last_error: str | None = None
    last_requires_sudo: bool = False


@dataclass
class _GpuCache:
    stamp: datetime | None = None
    value: GpuMonitorOut | None = None


@dataclass
class _UsersCache:
    entries: dict[bool, tuple[datetime, UsersOut]] = field(default_factory=dict)


class ObservabilityService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        transport: SshTransport,
        settings: Settings,
        bus: EventBus,
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport
        self._settings = settings
        self._bus = bus
        self._lock = threading.RLock()
        self._metrics: dict[int, _MetricsState] = {}
        self._gpu_cache: dict[int, _GpuCache] = defaultdict(_GpuCache)
        self._users_cache: dict[int, _UsersCache] = defaultdict(_UsersCache)
        self._mounts_cache: dict[int, tuple[datetime, MountsOut]] = {}
        self._scan_cancel: dict[str, threading.Event] = {}
        self._scan_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="storage-scan")
        self._closed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            events = list(self._scan_cancel.values())
        for event in events:
            event.set()
        self._scan_pool.shutdown(wait=False, cancel_futures=True)

    def recover_scans(self) -> int:
        """应用重启后，未完成的扫描必须标记 interrupted，绝不伪造成功。"""

        now = _utcnow()
        with session_scope(self._session_factory) as session:
            rows = session.scalars(
                select(StorageScan).where(StorageScan.status.in_(["queued", "running"]))
            ).all()
            for row in rows:
                row.status = "interrupted"
                row.stage = "interrupted"
                row.error = "应用重启，扫描已中断；请重新发起扫描"
                row.finished_at = now
                row.cancel_requested = False
            count = len(rows)
        if count:
            logger.warning("已将 %d 个未完成空间扫描标记为 interrupted", count)
        return count

    # ------------------------------------------------------------------
    # 指标
    # ------------------------------------------------------------------

    def metrics(self, server: Server, *, window: str) -> MetricsOut:
        window_seconds = WINDOW_SECONDS.get(window, 300)
        received_at = _utcnow()
        try:
            payload = run_probe(
                self._transport,
                target_from_server(server),
                mode="metrics",
                timeout=min(20.0, max(10.0, self._settings.command_timeout_seconds)),
                max_output_bytes=self._settings.max_output_bytes,
                operation="资源指标采集",
            )
        except Exception as exc:  # noqa: BLE001 - 远端失败以状态返回
            return self._metrics_failure(server.id, received_at, window_seconds, "error", str(exc))

        section = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        status = section.get("status")
        if status not in {"ok", "permission_denied", "unavailable", "error"}:
            status = "error"
        if status != "ok":
            return self._metrics_failure(
                server.id,
                received_at,
                window_seconds,
                status,
                section.get("error") or "指标采集失败",
            )

        data = section.get("data") if isinstance(section.get("data"), dict) else {}
        if not data:
            return self._metrics_failure(
                server.id, received_at, window_seconds, "error", "探针未返回指标数据"
            )

        state = self._metrics_state(server.id)
        with self._lock:
            interval_ms = 1000
            if state.last_received_at is not None:
                interval_ms = max(
                    1, int((received_at - state.last_received_at).total_seconds() * 1000)
                )
            remote_time = _parse_iso(payload.get("collectedAt")) or received_at
            requires_sudo = bool(section.get("requiresSudo", False))
            sections, reset_reason = derive_metrics(
                previous=state.last_raw,
                current=data,
                interval_ms=interval_ms,
                collected_at=received_at,
                requires_sudo=requires_sudo,
            )
            point = build_point(
                sections,
                collected_at=remote_time,
                received_at=received_at,
                interval_ms=interval_ms,
                reset_reason=reset_reason,
            )
            state.points.append(point)
            state.last_raw = data
            state.last_received_at = received_at
            state.last_status, state.last_error = summarize_status(sections)
            state.last_requires_sudo = requires_sudo
            self._trim_points(state, received_at)
            points = self._window_points(state, received_at, window_seconds)
            stale = self._is_stale(state, received_at, interval_ms)

        overall, overall_error = summarize_status(sections)
        return MetricsOut(
            server_id=server.id,
            collected_at=received_at,
            window_seconds=window_seconds,
            retention_seconds=self._settings.metrics_retention_seconds,
            sample_interval_ms=interval_ms,
            stale=stale,
            status=overall,  # type: ignore[arg-type]
            error=overall_error,
            requires_sudo=requires_sudo,
            current=sections,
            points=points,
            raw=self._bounded_raw(data),
        )

    def _metrics_state(self, server_id: int) -> _MetricsState:
        with self._lock:
            state = self._metrics.get(server_id)
            if state is None:
                state = _MetricsState(points=deque(maxlen=self._settings.metrics_max_points))
                self._metrics[server_id] = state
            return state

    def _trim_points(self, state: _MetricsState, now: datetime) -> None:
        cutoff = now.timestamp() - self._settings.metrics_retention_seconds
        while state.points and state.points[0].received_at.timestamp() < cutoff:
            state.points.popleft()

    def _window_points(
        self, state: _MetricsState, now: datetime, window_seconds: int
    ) -> list[MetricPointOut]:
        cutoff = now.timestamp() - window_seconds
        return [point for point in state.points if point.received_at.timestamp() >= cutoff]

    def _is_stale(self, state: _MetricsState, now: datetime, interval_ms: int) -> bool:
        if not state.points:
            return True
        threshold = max(10.0, interval_ms / 1000.0 * 3)
        return (
            _age_seconds(state.points[-1].received_at, now) > threshold or state.last_status != "ok"
        )

    def _metrics_failure(
        self,
        server_id: int,
        received_at: datetime,
        window_seconds: int,
        status: str,
        error: str | None,
    ) -> MetricsOut:
        state = self._metrics_state(server_id)
        with self._lock:
            points = self._window_points(state, received_at, window_seconds)
            sample_interval = points[-1].sample_interval_ms if points else 0
            stale = True
        message = (error or "指标采集失败")[:300]
        return MetricsOut(
            server_id=server_id,
            collected_at=received_at,
            window_seconds=window_seconds,
            retention_seconds=self._settings.metrics_retention_seconds,
            sample_interval_ms=sample_interval,
            stale=stale,
            status=status,  # type: ignore[arg-type]
            error=message,
            requires_sudo=state.last_requires_sudo,
            current=empty_sections(
                received_at,
                status=status,
                error=message,  # type: ignore[arg-type]
            ),
            points=points,
            raw={},
        )

    def _bounded_raw(self, data: dict[str, Any]) -> dict[str, Any]:
        memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
        vmstat = data.get("vmstat") if isinstance(data.get("vmstat"), dict) else {}
        return {
            "collectedAt": data.get("collectedAt"),
            "cpu": data.get("cpu") if isinstance(data.get("cpu"), dict) else {},
            "memory": {
                key: memory.get(key)
                for key in (
                    "MemTotal",
                    "MemAvailable",
                    "MemFree",
                    "Buffers",
                    "Cached",
                    "CommitLimit",
                    "Committed_AS",
                    "SwapTotal",
                    "SwapFree",
                )
            },
            "vmstat": {key: vmstat.get(key) for key in ("pswpin", "pswpout", "pgpgin", "pgpgout")},
            "disk": _bounded_mapping(data.get("disk"), MAX_RAW_DEVICES),
            "network": _bounded_mapping(data.get("network"), MAX_RAW_INTERFACES),
            "mounts": list(data.get("mounts") or [])[:MAX_MOUNTS],
            "gpu": data.get("gpu"),
            "gpuStatus": data.get("gpuStatus"),
        }

    # ------------------------------------------------------------------
    # GPU / 用户 / 挂载点
    # ------------------------------------------------------------------

    def gpu(self, server: Server, *, refresh: bool = False) -> GpuMonitorOut:
        now = _utcnow()
        with self._lock:
            cached = self._gpu_cache[server.id]
            if (
                not refresh
                and cached.value is not None
                and _age_seconds(cached.stamp, now) <= self._settings.gpu_cache_ttl_seconds
            ):
                return cached.value
        try:
            payload = run_probe(
                self._transport,
                target_from_server(server),
                mode="gpu",
                timeout=min(30.0, max(10.0, self._settings.command_timeout_seconds)),
                max_output_bytes=self._settings.max_output_bytes,
                operation="GPU 采集",
            )
            result = normalize_gpu_monitor(
                payload.get("gpu"), server_id=server.id, collected_at=now
            )
        except Exception as exc:  # noqa: BLE001
            result = normalize_gpu_monitor(
                {
                    "status": "error",
                    "error": str(exc)[:300],
                    "data": {
                        "processesStatus": "unavailable",
                        "capabilities": {"processes": "unavailable"},
                    },
                },
                server_id=server.id,
                collected_at=now,
            )
        with self._lock:
            self._gpu_cache[server.id] = _GpuCache(stamp=now, value=result)
        return result

    def users(self, server: Server, *, include_system: bool = False) -> UsersOut:
        now = _utcnow()
        with self._lock:
            cache = self._users_cache[server.id]
            hit = cache.entries.get(include_system)
            if (
                hit is not None
                and _age_seconds(hit[0], now) <= self._settings.users_cache_ttl_seconds
            ):
                return hit[1]
        try:
            payload = run_probe(
                self._transport,
                target_from_server(server),
                mode="users",
                probe_args=["include-system"] if include_system else [],
                timeout=min(45.0, max(20.0, self._settings.command_timeout_seconds)),
                max_output_bytes=self._settings.max_output_bytes,
                operation="用户信息采集",
            )
            result = normalize_users(
                payload.get("users"),
                server_id=server.id,
                collected_at=now,
                include_system=include_system,
            )
        except Exception as exc:  # noqa: BLE001
            result = normalise_users_error(server.id, now, include_system, str(exc))
        with self._lock:
            self._users_cache[server.id].entries[include_system] = (now, result)
        return result

    def mounts(self, server: Server, *, refresh: bool = False) -> MountsOut:
        now = _utcnow()
        with self._lock:
            cached = self._mounts_cache.get(server.id)
            if (
                not refresh
                and cached is not None
                and _age_seconds(cached[0], now) <= self._settings.mounts_cache_ttl_seconds
            ):
                return cached[1]
        try:
            payload = run_probe(
                self._transport,
                target_from_server(server),
                mode="mounts",
                timeout=min(20.0, max(10.0, self._settings.command_timeout_seconds)),
                max_output_bytes=self._settings.max_output_bytes,
                operation="挂载点采集",
            )
            result = self._normalize_mounts(payload.get("mounts"), server.id, now)
        except Exception as exc:  # noqa: BLE001
            result = MountsOut(
                server_id=server.id,
                collected_at=now,
                status="error",
                error=str(exc)[:300],
                mounts=[],
            )
        with self._lock:
            self._mounts_cache[server.id] = (now, result)
        return result

    @staticmethod
    def _normalize_mounts(section: Any, server_id: int, collected_at: datetime) -> MountsOut:
        raw = section if isinstance(section, dict) else {}
        status = raw.get("status")
        if status not in {"ok", "permission_denied", "unavailable", "error"}:
            status = "error"
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        mounts: list[MountOut] = []
        seen: set[str] = set()
        for item in data.get("mounts") or []:
            if not isinstance(item, dict):
                continue
            mount = item.get("mount")
            if not isinstance(mount, str) or not mount.startswith("/"):
                continue
            try:
                normalized = normalize_mount(mount)
            except ValueError:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            mounts.append(
                MountOut(
                    mount=normalized,
                    device=str(item.get("device"))[:512] if item.get("device") else None,
                    filesystem=str(item.get("filesystem"))[:64] if item.get("filesystem") else None,
                    options=str(item.get("options"))[:512] if item.get("options") else None,
                    uuid=str(item.get("uuid"))[:128] if item.get("uuid") else None,
                    major_minor=str(item.get("majorMinor"))[:32]
                    if item.get("majorMinor")
                    else None,
                    physical_device=str(item.get("physicalDevice"))[:128]
                    if item.get("physicalDevice")
                    else None,
                    read_only=bool(item.get("readOnly", False)),
                )
            )
            if len(mounts) >= MAX_MOUNTS:
                break
        mounts.sort(key=lambda entry: (entry.mount != "/", entry.mount))
        return MountsOut(
            server_id=server_id,
            collected_at=collected_at,
            status=status,  # type: ignore[arg-type]
            error=str(raw.get("error"))[:300] if raw.get("error") else None,
            requires_sudo=bool(raw.get("requiresSudo", False)),
            mounts=mounts,
        )

    # ------------------------------------------------------------------
    # 空间扫描
    # ------------------------------------------------------------------

    def create_scan(self, server: Server, request: StorageScanRequest) -> StorageScanOut:
        return self._create_scan(
            server,
            mount=request.mount,
            one_file_system=request.one_file_system,
            follow_symlinks=request.follow_symlinks,
            max_depth=request.max_depth,
            user_filter=request.user_filter,
            min_file_size=request.min_file_size,
            extension=request.extension,
            modified_after=request.modified_after,
            include_deleted_open=request.include_deleted_open,
        )

    def create_user_scan(self, server: Server, request: UserStorageScanRequest) -> StorageScanOut:
        users = self.users(server, include_system=True)
        if users.status != "ok":
            raise ApiError(502, "users_unavailable", users.error or "无法读取可登录用户")
        if not any(user.uid == request.uid for user in users.users):
            raise NotFoundError("user_not_found", "可登录用户不存在或不可见")
        return self._create_scan(
            server,
            mount=request.mount,
            one_file_system=True,
            follow_symlinks=False,
            max_depth=request.max_depth,
            user_filter=request.uid,
            min_file_size=request.min_file_size,
            extension=None,
            modified_after=None,
            include_deleted_open=request.include_deleted_open,
        )

    def _create_scan(
        self,
        server: Server,
        *,
        mount: str,
        one_file_system: bool,
        follow_symlinks: bool,
        max_depth: int,
        user_filter: int | None,
        min_file_size: int,
        extension: str | None,
        modified_after: datetime | None,
        include_deleted_open: bool,
    ) -> StorageScanOut:
        if self._closed:
            raise ApiError(503, "observability_unavailable", "服务正在关闭，无法创建扫描")
        if not server.enabled:
            raise ApiError(400, "server_disabled", "主机已停用，无法创建扫描")
        mounts = self.mounts(server)
        if mounts.status != "ok":
            raise ApiError(
                502,
                "mounts_unavailable",
                mounts.error or "无法读取挂载点清单，扫描已拒绝（fail closed）",
            )
        allowed = [entry.mount for entry in mounts.mounts]
        resolved_mount = mount_matches(mount, allowed)
        if resolved_mount is None:
            raise ApiError(
                422,
                "mount_not_allowed",
                "扫描只能从已发现的挂载点开始；请重新选择挂载点",
            )
        with self._lock:
            active = self._active_scan_count(server.id)
        if active >= self._settings.scan_max_active_per_server:
            raise ConflictError(
                "scan_limit_reached",
                "该主机已有进行中的扫描，请等待完成或取消后再试",
            )
        scan_id = uuid.uuid4().hex
        request_payload = {
            "mount": resolved_mount,
            "one_file_system": one_file_system,
            "follow_symlinks": follow_symlinks,
            "max_depth": max_depth,
            "user_filter": user_filter,
            "min_file_size": min_file_size,
            "extension": extension,
            "modified_after": modified_after.isoformat() if modified_after else None,
            "include_deleted_open": include_deleted_open,
        }
        with session_scope(self._session_factory) as session:
            row = StorageScan(
                id=scan_id,
                server_id=server.id,
                mount=resolved_mount,
                request=request_payload,
                status="queued",
                stage="queued",
                progress=0,
            )
            session.add(row)
            session.flush()
            self._scan_cancel[scan_id] = threading.Event()
            result = self._row_to_out(row, server.alias)
        record_audit(
            self._session_factory,
            action="storage_scan",
            target=f"{server.alias}:{resolved_mount}",
            outcome="queued",
            detail=(
                f"depth={max_depth} oneFileSystem={one_file_system} "
                f"user={user_filter if user_filter is not None else 'all'}"
            ),
        )
        self._scan_pool.submit(self._run_scan, scan_id)
        return result

    def _active_scan_count(self, server_id: int) -> int:
        with self._session_factory() as session:
            return len(
                session.scalars(
                    select(StorageScan.id).where(
                        StorageScan.server_id == server_id,
                        StorageScan.status.in_(["queued", "running"]),
                    )
                ).all()
            )

    def _run_scan(self, scan_id: str) -> None:
        cancel_event = self._scan_cancel.get(scan_id) or threading.Event()
        try:
            cancelled_before_start: StorageScanOut | None = None
            with session_scope(self._session_factory) as session:
                row = session.get(StorageScan, scan_id)
                if row is None:
                    return
                if row.status in TERMINAL_SCAN_STATUSES:
                    return
                if row.cancel_requested or cancel_event.is_set():
                    self._mark_scan(row, "cancelled", "cancelled", "扫描已取消", 100)
                    cancelled_before_start = self._row_to_out(
                        row, self._server_alias(session, row.server_id)
                    )
                else:
                    server = session.get(Server, row.server_id)
                    if server is None:
                        self._mark_scan(row, "failed", "failed", "主机记录已删除", 100)
                        return
                    request = dict(row.request or {})
                    row.status = "running"
                    row.stage = "connecting"
                    row.progress = 10
                    row.started_at = _utcnow()
                    alias = server.alias
                    mount = row.mount
                    server_obj = server
            if cancelled_before_start is not None:
                self._publish_done(cancelled_before_start, [])
                return
            self._publish_progress(scan_id, "running", "connecting", 10)

            probe_args = [mount, str(int(request.get("max_depth", 3)))]
            if request.get("follow_symlinks"):
                probe_args.append("follow-symlinks")
            if not request.get("one_file_system", True):
                probe_args.append("no-one-file-system")
            if not request.get("include_deleted_open", True):
                probe_args.append("no-deleted-open")

            self._publish_progress(scan_id, "running", "scanning", 35)
            try:
                payload = run_probe(
                    self._transport,
                    target_from_server(server_obj),
                    mode="storage",
                    probe_args=probe_args,
                    timeout=self._settings.scan_timeout_seconds,
                    cancel_event=cancel_event,
                    max_output_bytes=self._settings.max_output_bytes,
                    operation="磁盘空间扫描",
                )
            except Exception as exc:  # noqa: BLE001
                self._finish_scan(
                    scan_id,
                    status="cancelled" if cancel_event.is_set() else "failed",
                    stage="cancelled" if cancel_event.is_set() else "failed",
                    error="扫描已取消" if cancel_event.is_set() else str(exc)[:300],
                    alias=alias,
                )
                return

            if cancel_event.is_set():
                self._finish_scan(
                    scan_id, status="cancelled", stage="cancelled", error="扫描已取消", alias=alias
                )
                return

            section = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
            status = section.get("status")
            if status != "ok":
                error = str(section.get("error") or "扫描失败")[:300]
                self._finish_scan(
                    scan_id, status="failed", stage="failed", error=error, alias=alias
                )
                return

            self._publish_progress(scan_id, "running", "aggregating", 80)
            data = section.get("data") if isinstance(section.get("data"), dict) else {}
            raw_nodes = parse_nodes(data.get("nodes"), mount=mount)
            filters = ScanFilters(
                min_file_size=int(request.get("min_file_size", 0) or 0),
                uid=request.get("user_filter"),
                extension=request.get("extension"),
                modified_after=_parse_iso(request.get("modified_after")),
            )
            view = build_scan_view(
                raw_nodes,
                mount=mount,
                filters=filters,
                probe_incomplete=bool(data.get("incomplete", False)),
                probe_truncated=bool(data.get("truncated", False)),
                max_nodes=MAX_VIEW_NODES,
            )
            result = {
                "mount_info": data.get("mountInfo")
                if isinstance(data.get("mountInfo"), dict)
                else {},
                "nodes": [node.model_dump(mode="json") for node in view.nodes],
                "largest_files": [node.model_dump(mode="json") for node in view.largest_files],
                "summary_by_uid": [item.model_dump(mode="json") for item in view.summary],
                "node_count": view.node_count,
                "truncated": view.truncated,
                "incomplete": view.incomplete,
                "cross_device_count": int(data.get("crossDeviceCount", 0) or 0),
                "deleted_open_bytes": view.deleted_open_bytes,
                "deleted_open_count": view.deleted_open_count,
            }
            self._publish_progress(scan_id, "running", "chunking", 92)
            self._complete_scan(
                scan_id,
                result=result,
                alias=alias,
                mount=mount,
                request=request,
                nodes=view.nodes,
                summary=view.summary,
                incomplete=view.incomplete,
                truncated=view.truncated,
            )
        finally:
            with self._lock:
                self._scan_cancel.pop(scan_id, None)

    def _complete_scan(
        self,
        scan_id: str,
        *,
        result: dict[str, Any],
        alias: str,
        mount: str,
        request: dict[str, Any],
        nodes: list[StorageNodeOut],
        summary: list[StorageUidSummaryOut],
        incomplete: bool,
        truncated: bool,
    ) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(StorageScan, scan_id)
            if row is None:
                return
            row.status = "succeeded"
            row.stage = "done"
            row.progress = 100
            row.result = result
            row.node_count = int(result.get("node_count", 0))
            row.truncated = truncated
            row.error = None
            row.finished_at = _utcnow()
            out = self._row_to_out(row, alias)
        self._prune_scans(out.server_id)
        for offset, chunk in enumerate(chunk_nodes(nodes, size=SCAN_CHUNK_SIZE)):
            self._publish(
                scan_id,
                {
                    "type": "nodes",
                    "scanId": scan_id,
                    "offset": offset * SCAN_CHUNK_SIZE,
                    "chunk": [node.model_dump(mode="json") for node in chunk],
                },
            )
        self._publish_done(out, summary)
        record_audit(
            self._session_factory,
            action="storage_scan",
            target=f"{alias}:{mount}",
            outcome="succeeded",
            detail=f"{out.node_count} 节点，incomplete={incomplete}，truncated={truncated}",
        )

    def _finish_scan(
        self,
        scan_id: str,
        *,
        status: str,
        stage: str,
        error: str | None,
        alias: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(StorageScan, scan_id)
            if row is None:
                return
            if row.status in TERMINAL_SCAN_STATUSES:
                return
            self._mark_scan(row, status, stage, error, 100)
            out = self._row_to_out(row, alias)
            mount = row.mount
        self._publish_done(out, [])
        record_audit(
            self._session_factory,
            action="storage_scan",
            target=f"{alias}:{mount}",
            outcome=status,
            detail=(error or "")[:200],
        )

    @staticmethod
    def _mark_scan(
        row: StorageScan, status: str, stage: str, error: str | None, progress: int
    ) -> None:
        row.status = status
        row.stage = stage
        row.error = error
        row.progress = progress
        row.finished_at = _utcnow()

    def get_scan(self, scan_id: str) -> StorageScanOut:
        with self._session_factory() as session:
            row = session.get(StorageScan, scan_id)
            if row is None:
                raise NotFoundError("storage_scan_not_found", "扫描任务不存在")
            alias = self._server_alias(session, row.server_id)
            return self._row_to_out(row, alias)

    def list_scans(self, *, server_id: int | None = None, limit: int = 20) -> list[StorageScanOut]:
        with self._session_factory() as session:
            statement = (
                select(StorageScan)
                .order_by(StorageScan.created_at.desc(), StorageScan.id.desc())
                .limit(limit)
            )
            if server_id is not None:
                statement = statement.where(StorageScan.server_id == server_id)
            rows = session.scalars(statement).all()
            aliases = {
                server.id: server.alias
                for server in session.scalars(
                    select(Server).where(Server.id.in_({row.server_id for row in rows}))
                )
            }
            return [self._row_to_out(row, aliases.get(row.server_id, "")) for row in rows]

    def cancel_scan(self, scan_id: str) -> StorageScanOut:
        with session_scope(self._session_factory) as session:
            row = session.get(StorageScan, scan_id)
            if row is None:
                raise NotFoundError("storage_scan_not_found", "扫描任务不存在")
            if row.status in TERMINAL_SCAN_STATUSES:
                raise ConflictError("scan_finished", "扫描已结束，无法取消")
            alias = self._server_alias(session, row.server_id)
            if row.status == "queued":
                self._mark_scan(row, "cancelled", "cancelled", "已取消（尚未开始）", 100)
                out = self._row_to_out(row, alias)
                finished_immediately = True
            else:
                row.cancel_requested = True
                out = self._row_to_out(row, alias)
                finished_immediately = False
        with self._lock:
            event = self._scan_cancel.get(scan_id)
        if event is not None:
            event.set()
        if finished_immediately:
            self._publish_done(out, [])
            record_audit(
                self._session_factory,
                action="storage_scan_cancel",
                target=f"{alias}:{out.mount}",
                outcome="cancelled",
                detail="取消排队中的扫描",
            )
        return out

    def scan_children(self, scan_id: str, path: str) -> list[StorageNodeOut]:
        scan = self.get_scan(scan_id)
        if scan.status != "succeeded":
            raise ConflictError("scan_not_finished", "扫描尚未完成，无法浏览子目录")
        try:
            normalized = normalize_scan_path(scan.mount, path)
        except ValueError as exc:
            raise ApiError(422, "path_not_allowed", str(exc)) from exc
        children = scan_children(scan.nodes, normalized)
        if (
            not children
            and normalized != scan.mount
            and not any(node.path == normalized for node in scan.nodes)
        ):
            raise NotFoundError("scan_path_not_found", "扫描结果中不存在该路径")
        return children

    # ------------------------------------------------------------------
    # 输出与事件
    # ------------------------------------------------------------------

    def _row_to_out(self, row: StorageScan, alias: str) -> StorageScanOut:
        request = dict(row.request or {})
        result = row.result if isinstance(row.result, dict) else {}
        nodes = self._parse_nodes(result.get("nodes"))
        largest = self._parse_nodes(result.get("largest_files"))
        summary = []
        for item in result.get("summary_by_uid") or []:
            try:
                summary.append(StorageUidSummaryOut.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
        return StorageScanOut(
            scan_id=row.id,
            server_id=row.server_id,
            server_alias=alias,
            mount=row.mount,
            mount_info=result.get("mount_info")
            if isinstance(result.get("mount_info"), dict)
            else {},
            status=row.status,  # type: ignore[arg-type]
            stage=row.stage,
            progress=row.progress,
            one_file_system=bool(request.get("one_file_system", True)),
            follow_symlinks=bool(request.get("follow_symlinks", False)),
            max_depth=int(request.get("max_depth", 3) or 0),
            user_filter=request.get("user_filter"),
            min_file_size=int(request.get("min_file_size", 0) or 0),
            extension=request.get("extension"),
            node_count=row.node_count,
            truncated=row.truncated,
            incomplete=bool(result.get("incomplete", False)),
            cross_device_count=int(result.get("cross_device_count", 0) or 0),
            deleted_open_bytes=int(result.get("deleted_open_bytes", 0) or 0),
            deleted_open_count=int(result.get("deleted_open_count", 0) or 0),
            nodes=nodes,
            largest_files=largest,
            summary_by_uid=summary,
            error=row.error,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    @staticmethod
    def _parse_nodes(value: Any) -> list[StorageNodeOut]:
        nodes: list[StorageNodeOut] = []
        for item in value or []:
            try:
                nodes.append(StorageNodeOut.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
        return nodes

    def _prune_scans(self, server_id: int) -> None:
        """每个主机只保留最近的扫描历史，避免结果 JSON 无限增长。"""

        limit = self._settings.scan_history_limit
        with session_scope(self._session_factory) as session:
            keep = session.scalars(
                select(StorageScan.id)
                .where(StorageScan.server_id == server_id)
                .order_by(StorageScan.created_at.desc(), StorageScan.id.desc())
                .limit(limit)
            ).all()
            if len(keep) < limit:
                return
            session.execute(
                delete(StorageScan).where(
                    StorageScan.server_id == server_id,
                    StorageScan.id.not_in(keep),
                )
            )

    def _publish(self, scan_id: str, event: dict[str, Any]) -> None:
        self._bus.publish(scan_id, event)

    def _publish_progress(self, scan_id: str, status: str, stage: str, progress: int) -> None:
        self._publish(
            scan_id,
            {
                "type": "progress",
                "scanId": scan_id,
                "status": status,
                "stage": stage,
                "progress": progress,
                "at": _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
        )

    def _publish_done(self, out: StorageScanOut, summary: list[StorageUidSummaryOut]) -> None:
        self._publish(
            out.scan_id,
            {
                "type": "done",
                "scanId": out.scan_id,
                "status": out.status,
                "progress": out.progress,
                "error": out.error,
                "nodeCount": out.node_count,
                "truncated": out.truncated,
                "incomplete": out.incomplete,
                "summary": [item.model_dump(mode="json") for item in summary],
                "at": _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
        )

    @staticmethod
    def _server_alias(session: Session, server_id: int) -> str:
        server = session.get(Server, server_id)
        return server.alias if server is not None else ""


def normalise_users_error(
    server_id: int, collected_at: datetime, include_system: bool, message: str
) -> UsersOut:
    return UsersOut(
        server_id=server_id,
        collected_at=collected_at,
        include_system=include_system,
        status="error",
        error=(message or "用户信息采集失败")[:300],
        users=[],
    )
