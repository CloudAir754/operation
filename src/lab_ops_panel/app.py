"""FastAPI 应用工厂。"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from lab_ops_panel.api.routes import router
from lab_ops_panel.application.account_access import AccountAccessManager
from lab_ops_panel.application.file_transfer import FileTransferManager
from lab_ops_panel.application.home_migration import HomeMigrationManager
from lab_ops_panel.application.pam_policy import PamPolicyManager
from lab_ops_panel.application.process_control import ProcessControlManager
from lab_ops_panel.application.user_creation import UserCreationManager
from lab_ops_panel.application.user_deletion import UserDeletionManager
from lab_ops_panel.config import Settings, ensure_unprivileged
from lab_ops_panel.database import initialize_database
from lab_ops_panel.infrastructure.privileged import PrivilegedExecutor
from lab_ops_panel.infrastructure.tasks import TaskRepository


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建带启动令牌校验和 SQLite 生命周期初始化的本地应用。"""

    active_settings = settings or Settings()
    active_settings.validate()
    ensure_unprivileged()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.database_path = initialize_database(active_settings.data_dir)
        app.state.tasks = TaskRepository(app.state.database_path)
        app.state.cleaned_operation_logs = app.state.tasks.cleanup_operation_logs()
        app.state.interrupted_tasks = app.state.tasks.mark_running_interrupted()
        app.state.privileged = PrivilegedExecutor()
        app.state.user_creation = UserCreationManager(
            app.state.tasks, app.state.privileged, active_settings.data_dir
        )
        app.state.account_access = AccountAccessManager(app.state.tasks, app.state.privileged)
        app.state.pam_policy = PamPolicyManager(app.state.tasks, app.state.privileged)
        app.state.process_control = ProcessControlManager(app.state.tasks, app.state.privileged)
        app.state.user_deletion = UserDeletionManager(app.state.tasks, app.state.privileged)
        app.state.file_transfer = FileTransferManager(app.state.tasks, app.state.privileged)
        app.state.home_migration = HomeMigrationManager(app.state.tasks, app.state.privileged)
        try:
            yield
        finally:
            app.state.home_migration.close()
            app.state.file_transfer.close()
            app.state.user_deletion.close()
            app.state.process_control.close()
            app.state.pam_policy.close()
            app.state.account_access.close()
            app.state.user_creation.close()

    app = FastAPI(title="LabOpsPanel", version="0.1.0", lifespan=lifespan)
    app.state.settings = active_settings

    @app.middleware("http")
    async def require_session_token(request: Request, call_next):  # type: ignore[no-untyped-def]
        """校验每次启动生成的令牌，阻止其他本机网页跨站调用。"""

        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("X-LabOps-Token", "")
            if not secrets.compare_digest(supplied, active_settings.session_token):
                return JSONResponse({"detail": "缺少或无效的启动会话令牌"}, status_code=403)
        return await call_next(request)

    app.include_router(router)
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app
