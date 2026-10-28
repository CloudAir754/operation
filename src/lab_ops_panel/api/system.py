"""系统信息、设置与动作注册表。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from lab_ops_panel import __version__
from lab_ops_panel.api.deps import ContainerDep
from lab_ops_panel.schemas import ActionsOut, HealthOut, SettingsOut

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(status="ok", version=__version__, time=datetime.now(UTC))


@router.get("/settings", response_model=SettingsOut)
def read_settings(container: ContainerDep) -> SettingsOut:
    settings = container.settings
    return SettingsOut(
        allow_arbitrary_commands=settings.allow_arbitrary_commands,
        snapshot_ttl_seconds=settings.snapshot_ttl_seconds,
        max_output_bytes=settings.max_output_bytes,
        ssh_config_path=str(settings.resolved_ssh_config_path),
        app_data_dir=str(settings.app_data_dir),
        bind_host=settings.bind_host,
        bind_port=settings.bind_port,
        transport=settings.transport,
    )


@router.get("/actions", response_model=ActionsOut)
def list_actions(container: ContainerDep) -> ActionsOut:
    return container.registry.describe()
