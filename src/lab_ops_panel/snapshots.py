"""只读状态采集与缓存。

缓存策略：快照 20 秒内复用；过期后仍可返回旧数据但必须标注 ``stale``，
绝不把过期数据伪装成实时数据。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from lab_ops_panel.models import ServerSnapshot
from lab_ops_panel.remote.diagnosis import diagnose_result
from lab_ops_panel.remote.logs import build_journalctl_command
from lab_ops_panel.remote.parsing import (
    error_snapshot_data,
    normalize_services_section,
    normalize_snapshot_sections,
    snapshot_status,
)
from lab_ops_panel.remote.probe import ProbeError, run_probe
from lab_ops_panel.remote.targets import target_from_server
from lab_ops_panel.schemas import (
    LogsOut,
    ServicesOut,
    ServiceUnitOut,
    SnapshotDataOut,
    SnapshotOut,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from lab_ops_panel.config import Settings
    from lab_ops_panel.models import Server
    from lab_ops_panel.ssh.transport import SshTransport

logger = logging.getLogger(__name__)

MAX_SNAPSHOTS_PER_SERVER = 50


class SnapshotService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        transport: SshTransport,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport
        self._settings = settings

    # -- 缓存读写 -----------------------------------------------------------

    def latest(self, server_id: int) -> ServerSnapshot | None:
        session = self._session_factory()
        try:
            return session.scalars(
                select(ServerSnapshot)
                .where(ServerSnapshot.server_id == server_id)
                .order_by(ServerSnapshot.collected_at.desc(), ServerSnapshot.id.desc())
                .limit(1)
            ).first()
        finally:
            session.close()

    def get(self, server: Server, *, refresh: bool = False) -> SnapshotOut:
        cached = self.latest(server.id)
        if cached is not None and not refresh:
            age = self._age_seconds(cached.collected_at)
            if (
                self._settings.snapshot_ttl_seconds > 0
                and age <= self._settings.snapshot_ttl_seconds
            ):
                return self._to_out(server.id, cached, stale=False)
            return self._to_out(server.id, cached, stale=True)
        return self.collect(server)

    def collect(self, server: Server) -> SnapshotOut:
        target = target_from_server(server)
        error: str | None = None
        try:
            payload = run_probe(
                self._transport,
                target,
                mode="snapshot",
                timeout=max(self._settings.command_timeout_seconds, 45.0),
                max_output_bytes=self._settings.max_output_bytes,
                operation="状态采集",
            )
            data = normalize_snapshot_sections(payload.get("sections"))
            status = snapshot_status(data)
        except Exception as exc:  # noqa: BLE001 - 采集失败必须转成字段级状态
            message = str(exc) or exc.__class__.__name__
            if not isinstance(exc, ProbeError):
                logger.warning("采集状态失败：%s", message, exc_info=True)
            error = message
            data = error_snapshot_data(message)
            status = "failed"
        return self.store(server, data, status=status, error=error)

    def store(
        self,
        server: Server,
        data: SnapshotDataOut,
        *,
        status: str,
        error: str | None,
    ) -> SnapshotOut:
        collected_at = datetime.now(UTC)
        session = self._session_factory()
        try:
            row = ServerSnapshot(
                server_id=server.id,
                collected_at=collected_at,
                status=status,
                error=error,
                payload=data.model_dump(mode="json", by_alias=True),
            )
            session.add(row)
            session.commit()
            self._prune(session, server.id)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return SnapshotOut(
            server_id=server.id,
            collected_at=collected_at,
            age_seconds=0,
            stale=False,
            status=status,
            error=error,
            data=data,
        )

    def _prune(self, session: Session, server_id: int) -> None:
        keep = session.scalars(
            select(ServerSnapshot.id)
            .where(ServerSnapshot.server_id == server_id)
            .order_by(ServerSnapshot.collected_at.desc(), ServerSnapshot.id.desc())
            .limit(MAX_SNAPSHOTS_PER_SERVER)
        ).all()
        if len(keep) < MAX_SNAPSHOTS_PER_SERVER:
            return
        session.execute(
            delete(ServerSnapshot).where(
                ServerSnapshot.server_id == server_id,
                ServerSnapshot.id.not_in(keep),
            )
        )
        session.commit()

    def _age_seconds(self, collected_at: datetime) -> int:
        if collected_at.tzinfo is None:
            collected_at = collected_at.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - collected_at
        return max(0, int(delta.total_seconds()))

    def _to_out(self, server_id: int, row: ServerSnapshot, *, stale: bool) -> SnapshotOut:
        try:
            data = SnapshotDataOut.model_validate(row.payload)
        except Exception:  # noqa: BLE001 - 旧缓存损坏时按错误快照返回
            message = "本地缓存已损坏，请刷新采集"
            return SnapshotOut(
                server_id=server_id,
                collected_at=row.collected_at,
                age_seconds=self._age_seconds(row.collected_at),
                stale=True,
                status="failed",
                error=message,
                data=error_snapshot_data(message),
            )
        return SnapshotOut(
            server_id=server_id,
            collected_at=row.collected_at,
            age_seconds=self._age_seconds(row.collected_at),
            stale=stale,
            status=row.status,
            error=row.error,
            data=data,
        )

    # -- 服务与日志 ---------------------------------------------------------

    def services(self, server: Server, *, refresh: bool = False) -> ServicesOut:
        if not refresh:
            cached = self.latest(server.id)
            if cached is not None:
                age = self._age_seconds(cached.collected_at)
                if (
                    self._settings.snapshot_ttl_seconds > 0
                    and age <= self._settings.snapshot_ttl_seconds
                ):
                    try:
                        data = SnapshotDataOut.model_validate(cached.payload)
                    except Exception:  # noqa: BLE001
                        data = None
                    if data is not None and data.services.data is not None:
                        section = data.services
                        return ServicesOut(
                            server_id=server.id,
                            collected_at=cached.collected_at,
                            status=section.status,
                            error=section.error,
                            units=section.data.units,
                        )
        collected_at = datetime.now(UTC)
        try:
            payload = run_probe(
                self._transport,
                target_from_server(server),
                mode="services",
                timeout=min(self._settings.command_timeout_seconds, 30.0),
                max_output_bytes=self._settings.max_output_bytes,
                operation="服务列表查询",
            )
        except Exception as exc:  # noqa: BLE001 - 远端失败以状态返回
            return ServicesOut(
                server_id=server.id,
                collected_at=collected_at,
                status="error",
                error=str(exc) or exc.__class__.__name__,
                units=[],
            )
        section = normalize_services_section(payload.get("services"))
        units = section.data.units if section.data is not None else []
        return ServicesOut(
            server_id=server.id,
            collected_at=collected_at,
            status=section.status,
            error=section.error,
            units=units,
        )

    def logs(
        self,
        server: Server,
        *,
        unit: str | None,
        lines: int,
        use_sudo: bool = False,
        sudo_password: str | None = None,
    ) -> LogsOut:
        command = build_journalctl_command(unit, lines)
        collected_at = datetime.now(UTC)
        stdin_data = None
        if use_sudo:
            if sudo_password is None:
                return LogsOut(
                    server_id=server.id,
                    unit=unit,
                    collected_at=collected_at,
                    lines=[],
                    error="未保存 sudo 密码，无法以提权方式读取日志",
                    command=command,
                )
            command = f"sudo -S -p '' {command}"
            stdin_data = (sudo_password + "\n").encode("utf-8")
        try:
            result = self._transport.run(
                target_from_server(server),
                command,
                timeout=min(self._settings.command_timeout_seconds, 45.0),
                stdin_data=stdin_data,
                max_output_bytes=self._settings.max_output_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            return LogsOut(
                server_id=server.id,
                unit=unit,
                collected_at=collected_at,
                lines=[],
                error=str(exc) or exc.__class__.__name__,
                command=command,
            )
        if result.cancelled or result.timed_out or result.exit_code != 0:
            return LogsOut(
                server_id=server.id,
                unit=unit,
                collected_at=collected_at,
                lines=[],
                exit_code=result.exit_code,
                truncated=result.truncated,
                error=diagnose_result(result, operation="日志读取"),
                command=command,
            )
        lines_out = [line for line in result.stdout.splitlines() if line.strip()]
        return LogsOut(
            server_id=server.id,
            unit=unit,
            collected_at=collected_at,
            lines=lines_out,
            exit_code=result.exit_code,
            truncated=result.truncated,
            error=None,
            command=command,
        )


def protected_units(units: list[ServiceUnitOut]) -> list[str]:
    return [unit.name for unit in units if unit.protected]
