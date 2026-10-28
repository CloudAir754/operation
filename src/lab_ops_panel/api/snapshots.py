"""只读状态：快照、服务列表与日志。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from lab_ops_panel.api.deps import ContainerDep, get_server_or_404
from lab_ops_panel.errors import ValidationFailedError
from lab_ops_panel.remote.services import UnitValidationError, normalize_unit_name
from lab_ops_panel.schemas import LogsOut, ServicesOut, SnapshotOut

router = APIRouter(tags=["snapshots"])


@router.get("/servers/{server_id}/snapshot", response_model=SnapshotOut)
def read_snapshot(
    server_id: int,
    container: ContainerDep,
    refresh: bool = Query(default=False, description="强制重新采集"),
) -> SnapshotOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.snapshots.get(server, refresh=refresh)


@router.get("/servers/{server_id}/services", response_model=ServicesOut)
def read_services(
    server_id: int,
    container: ContainerDep,
    refresh: bool = Query(default=False, description="忽略缓存重新查询"),
) -> ServicesOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.snapshots.services(server, refresh=refresh)


@router.get("/servers/{server_id}/logs", response_model=LogsOut)
def read_logs(
    server_id: int,
    container: ContainerDep,
    unit: str | None = Query(default=None, max_length=200),
    lines: int | None = Query(default=None, ge=1),
    use_sudo: bool = Query(default=False, alias="useSudo"),
) -> LogsOut:
    limit = min(lines or container.settings.log_default_lines, container.settings.log_max_lines)
    normalized_unit: str | None = None
    if unit and unit.strip():
        try:
            normalized_unit = normalize_unit_name(unit.strip())
        except UnitValidationError as exc:
            raise ValidationFailedError(
                str(exc), fields=[{"field": "unit", "message": str(exc)}]
            ) from exc
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        password = None
        if use_sudo:
            password = container.credentials.get_password(server_id)
        return container.snapshots.logs(
            server,
            unit=normalized_unit,
            lines=limit,
            use_sudo=use_sudo,
            sudo_password=password,
        )
