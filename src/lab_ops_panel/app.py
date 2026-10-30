"""FastAPI 应用装配与生命周期。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from lab_ops_panel import __version__
from lab_ops_panel.api import api_router
from lab_ops_panel.api.deps import AppContainer
from lab_ops_panel.config import Settings
from lab_ops_panel.db import create_db_engine, create_session_factory
from lab_ops_panel.errors import ApiError, api_error_handler
from lab_ops_panel.migrations_runner import upgrade_database
from lab_ops_panel.observability import ObservabilityService
from lab_ops_panel.provisioning import ProvisioningService
from lab_ops_panel.remote.actions import build_registry
from lab_ops_panel.secrets_store import CredentialStore, SecretProtector
from lab_ops_panel.snapshots import SnapshotService
from lab_ops_panel.ssh.transport import OpenSshTransport, SshTransport
from lab_ops_panel.tasks.events import EventBus
from lab_ops_panel.tasks.manager import TaskManager

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    transport: SshTransport | None = None,
    protector: SecretProtector | None = None,
) -> FastAPI:
    resolved = settings or Settings()
    resolved.ensure_directories()
    upgrade_database(resolved.db_path)

    engine = create_db_engine(resolved.db_path)
    session_factory = create_session_factory(engine)
    credentials = CredentialStore(session_factory, protector)
    ssh_config_path = resolved.resolved_ssh_config_path
    ssh_transport = transport or OpenSshTransport(
        ssh_binary=resolved.ssh_binary,
        config_path=ssh_config_path,
        max_output_bytes=resolved.max_output_bytes,
        default_timeout_seconds=resolved.command_timeout_seconds,
    )
    event_bus = EventBus()
    registry = build_registry(resolved)
    snapshots = SnapshotService(session_factory, ssh_transport, resolved)
    observability = ObservabilityService(
        session_factory=session_factory,
        transport=ssh_transport,
        settings=resolved,
        bus=event_bus,
    )
    tasks = TaskManager(
        session_factory=session_factory,
        transport=ssh_transport,
        settings=resolved,
        registry=registry,
        credentials=credentials,
        bus=event_bus,
        snapshots=snapshots,
    )
    provisioning = ProvisioningService(
        session_factory=session_factory,
        transport=ssh_transport,
        settings=resolved,
        credentials=credentials,
        bus=event_bus,
    )
    container = AppContainer(
        settings=resolved,
        engine=engine,
        session_factory=session_factory,
        transport=ssh_transport,
        credentials=credentials,
        registry=registry,
        event_bus=event_bus,
        tasks=tasks,
        snapshots=snapshots,
        observability=observability,
        provisioning=provisioning,
        version=__version__,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        container.event_bus.bind_loop(asyncio.get_running_loop())
        interrupted = container.tasks.recover_on_startup()
        if interrupted:
            logger.warning("启动时将 %d 个未完成任务标记为 interrupted", interrupted)
        interrupted_scans = container.observability.recover_scans()
        if interrupted_scans:
            logger.warning("启动时将 %d 个未完成空间扫描标记为 interrupted", interrupted_scans)
        recovered = container.provisioning.recover_on_startup()
        if recovered["batches"] or recovered["migrations"]:
            logger.warning("启动时中断账户生命周期作业：%s", recovered)
        logger.info(
            "LabOpsPanel %s 已启动，监听 %s:%s，SSH 配置：%s",
            __version__,
            resolved.bind_host,
            resolved.bind_port,
            ssh_config_path,
        )
        try:
            yield
        finally:
            container.tasks.shutdown()
            container.observability.shutdown()
            container.provisioning.shutdown()
            engine.dispose()

    app = FastAPI(
        title="LabOpsPanel",
        version=__version__,
        description="Windows 本地 Web 控制台，通过 SSH 管理 Linux 主机",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.container = container
    app.add_exception_handler(ApiError, api_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())[1:]) or "body",
                "message": error.get("msg", "参数不合法"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "validation_failed",
                    "message": "请求参数校验失败",
                    "fields": fields,
                }
            },
        )

    app.include_router(api_router)
    _mount_frontend(app, resolved.resolved_frontend_dist)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    index_file = dist / "index.html"
    if not index_file.is_file():

        @app.get("/", include_in_schema=False)
        def _placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "service": "LabOpsPanel",
                    "version": __version__,
                    "message": "前端尚未构建，请在 frontend/ 目录执行 npm install && npm run build",
                }
            )

        return

    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    resolved_dist = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def _spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise ApiError(404, "not_found", "接口不存在")
        if path:
            candidate = (resolved_dist / path).resolve()
            try:
                candidate.relative_to(resolved_dist)
            except ValueError:
                raise ApiError(404, "not_found", "路径不合法") from None
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index_file)
