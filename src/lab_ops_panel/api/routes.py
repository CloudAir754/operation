"""只读 HTTP API。"""

from typing import Any

from fastapi import APIRouter

from lab_ops_panel.infrastructure.gpu import gpu_snapshot
from lab_ops_panel.infrastructure.system_info import (
    local_users,
    mount_points,
    resource_snapshot,
    system_snapshot,
)

router = APIRouter(prefix="/api/v1")


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


@router.get("/users")
def users() -> list[dict[str, Any]]:
    """列出 NSS 可见账号及可识别的 sudo 组来源。"""

    return local_users()
