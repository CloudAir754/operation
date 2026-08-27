"""只读 HTTP API。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lab_ops_panel.infrastructure.gpu import gpu_snapshot
from lab_ops_panel.infrastructure.resources import resource_snapshot
from lab_ops_panel.infrastructure.scans import TERMINAL_STATES, home_scan_manager
from lab_ops_panel.infrastructure.storage import block_device_snapshot
from lab_ops_panel.infrastructure.system_info import (
    local_users,
    mount_points,
    system_snapshot,
)

router = APIRouter(prefix="/api/v1")


class HomeScanRequest(BaseModel):
    """启动受限 home 扫描所需的结构化参数。"""

    username: str = Field(pattern=r"^[a-z_][a-z0-9_.-]*[$]?$", max_length=32)
    idempotency_key: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    max_depth: int = Field(default=5, ge=1, le=8)
    max_nodes: int = Field(default=20_000, ge=100, le=50_000)


@router.get("/health")
def health() -> dict[str, str]:
    """返回进程存活状态。"""

    return {"status": "ok"}


@router.get("/overview")
def overview() -> dict[str, Any]:
    """返回总览页的一次性只读快照。"""

    return {
        "system": system_snapshot(),
        "resources": resource_snapshot(),
        "gpu": gpu_snapshot(),
        "mounts": mount_points(),
    }


@router.get("/resources")
def resources() -> dict[str, Any]:
    """返回供页面短周期刷新的 CPU 与内存快照。"""

    return resource_snapshot()


@router.get("/storage")
def storage() -> dict[str, Any]:
    """返回块设备拓扑和可获得的稳定身份。"""

    return block_device_snapshot()


@router.get("/gpu")
def gpu() -> dict[str, Any]:
    """返回可短周期刷新的 GPU 与当前进程快照。"""

    return gpu_snapshot()


@router.get("/users")
def users() -> list[dict[str, Any]]:
    """列出允许交互式登录的本机账号及可识别的 sudo 组来源。"""

    return local_users()


@router.post("/home-scans", status_code=202)
def start_home_scan(request: HomeScanRequest) -> dict[str, Any]:
    """创建可取消的 home 后台扫描，相同幂等键复用原任务。"""

    try:
        return home_scan_manager.start(
            request.username,
            request.idempotency_key,
            request.max_depth,
            request.max_nodes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/home-scans/{task_id}")
def get_home_scan(task_id: str) -> dict[str, Any]:
    """返回 home 扫描任务和完成后的 treemap 数据。"""

    try:
        return home_scan_manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc


@router.post("/home-scans/{task_id}/cancel")
def cancel_home_scan(task_id: str) -> dict[str, Any]:
    """幂等请求取消 home 扫描。"""

    try:
        return home_scan_manager.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc


@router.get("/home-scans/{task_id}/events")
def home_scan_events(task_id: str) -> StreamingResponse:
    """以 SSE 推送扫描进度，仍由统一启动令牌中间件保护。"""

    try:
        home_scan_manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc

    async def events() -> AsyncIterator[str]:
        previous = ""
        while True:
            snapshot = home_scan_manager.get(task_id)
            payload = json.dumps(snapshot, ensure_ascii=False)
            if payload != previous:
                yield f"event: progress\ndata: {payload}\n\n"
                previous = payload
            if snapshot["state"] in TERMINAL_STATES:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
