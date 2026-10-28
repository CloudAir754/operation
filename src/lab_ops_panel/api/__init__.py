"""REST API 路由集合。"""

from fastapi import APIRouter

from lab_ops_panel.api import audit, servers, snapshots, system, tasks

api_router = APIRouter(prefix="/api")
api_router.include_router(system.router)
api_router.include_router(servers.router)
api_router.include_router(snapshots.router)
api_router.include_router(tasks.router)
api_router.include_router(audit.router)

__all__ = ["api_router"]
