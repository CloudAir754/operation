"""阶段三资源观测 API：指标、GPU、用户、挂载点与空间扫描。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse

from lab_ops_panel.api.deps import ContainerDep, get_server_or_404
from lab_ops_panel.observability import TERMINAL_SCAN_STATUSES
from lab_ops_panel.remote.storage_scan import chunk_nodes
from lab_ops_panel.schemas_observability import (
    GpuMonitorOut,
    MetricsOut,
    MountsOut,
    StorageNodeOut,
    StorageScanListOut,
    StorageScanOut,
    StorageScanRequest,
    UsersOut,
    UserStorageScanRequest,
)
from lab_ops_panel.tasks.events import KEEPALIVE_FRAME, format_sse

router = APIRouter(tags=["observability"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
KEEPALIVE_SECONDS = 15.0
SCAN_CHUNK_SIZE = 200
METRICS_WINDOW_PATTERN = r"^(1m|5m|15m|1h)$"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.get("/servers/{server_id}/metrics", response_model=MetricsOut)
def read_metrics(
    server_id: int,
    container: ContainerDep,
    window: str = Query(default="5m", pattern=METRICS_WINDOW_PATTERN),
) -> MetricsOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.observability.metrics(server, window=window)


@router.get("/servers/{server_id}/gpus", response_model=GpuMonitorOut)
def read_gpus(
    server_id: int,
    container: ContainerDep,
    refresh: bool = Query(default=False),
) -> GpuMonitorOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.observability.gpu(server, refresh=refresh)


@router.get("/servers/{server_id}/users", response_model=UsersOut)
def read_users(
    server_id: int,
    container: ContainerDep,
    include_system: bool = Query(default=False, alias="includeSystem"),
) -> UsersOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.observability.users(server, include_system=include_system)


@router.get("/servers/{server_id}/mounts", response_model=MountsOut)
def read_mounts(
    server_id: int,
    container: ContainerDep,
    refresh: bool = Query(default=False),
) -> MountsOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.observability.mounts(server, refresh=refresh)


@router.get("/servers/{server_id}/storage/scans", response_model=StorageScanListOut)
def list_storage_scans(
    server_id: int,
    container: ContainerDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> StorageScanListOut:
    with container.session_factory() as session:
        get_server_or_404(session, server_id)
    scans = container.observability.list_scans(server_id=server_id, limit=limit)
    return StorageScanListOut(scans=scans, count=len(scans))


@router.post(
    "/servers/{server_id}/storage/scans",
    response_model=StorageScanOut,
    status_code=status.HTTP_201_CREATED,
)
def create_storage_scan(
    server_id: int,
    payload: StorageScanRequest,
    container: ContainerDep,
) -> StorageScanOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.observability.create_scan(server, payload)


@router.post(
    "/servers/{server_id}/users/storage-scan",
    response_model=StorageScanOut,
    status_code=status.HTTP_201_CREATED,
)
def create_user_storage_scan(
    server_id: int,
    payload: UserStorageScanRequest,
    container: ContainerDep,
) -> StorageScanOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.observability.create_user_scan(server, payload)


@router.get("/storage/scans/{scan_id}", response_model=StorageScanOut)
def read_storage_scan(scan_id: str, container: ContainerDep) -> StorageScanOut:
    return container.observability.get_scan(scan_id)


@router.post("/storage/scans/{scan_id}/cancel", response_model=StorageScanOut)
def cancel_storage_scan(scan_id: str, container: ContainerDep) -> StorageScanOut:
    return container.observability.cancel_scan(scan_id)


@router.get("/storage/scans/{scan_id}/children", response_model=list[StorageNodeOut])
def read_storage_children(
    scan_id: str,
    container: ContainerDep,
    path: str = Query(min_length=1, max_length=4096),
) -> list[StorageNodeOut]:
    return container.observability.scan_children(scan_id, path)


@router.get("/storage/scans/{scan_id}/events")
async def storage_scan_events(
    scan_id: str, request: Request, container: ContainerDep
) -> StreamingResponse:
    snapshot = container.observability.get_scan(scan_id)
    queue = container.event_bus.subscribe(scan_id)

    async def stream() -> AsyncIterator[str]:
        try:
            yield format_sse(
                {
                    "type": "progress",
                    "scanId": snapshot.scan_id,
                    "status": snapshot.status,
                    "stage": snapshot.stage,
                    "progress": snapshot.progress,
                    "at": _now_iso(),
                }
            )
            if snapshot.status in TERMINAL_SCAN_STATUSES:
                for offset, chunk in enumerate(chunk_nodes(snapshot.nodes, size=SCAN_CHUNK_SIZE)):
                    yield format_sse(
                        {
                            "type": "nodes",
                            "scanId": snapshot.scan_id,
                            "offset": offset * SCAN_CHUNK_SIZE,
                            "chunk": [node.model_dump(mode="json") for node in chunk],
                        }
                    )
                yield format_sse(
                    {
                        "type": "done",
                        "scanId": snapshot.scan_id,
                        "status": snapshot.status,
                        "progress": snapshot.progress,
                        "error": snapshot.error,
                        "nodeCount": snapshot.node_count,
                        "truncated": snapshot.truncated,
                        "incomplete": snapshot.incomplete,
                        "at": _now_iso(),
                    }
                )
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield KEEPALIVE_FRAME
                    continue
                yield format_sse(event)
                if event.get("type") == "done":
                    return
        finally:
            container.event_bus.unsubscribe(scan_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)
