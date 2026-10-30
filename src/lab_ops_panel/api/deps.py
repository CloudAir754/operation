"""依赖注入容器与会话辅助。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from sqlalchemy import select

from lab_ops_panel.errors import NotFoundError

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from lab_ops_panel.config import Settings
    from lab_ops_panel.models import Server
    from lab_ops_panel.observability import ObservabilityService
    from lab_ops_panel.provisioning import ProvisioningService
    from lab_ops_panel.remote.actions import ActionRegistry
    from lab_ops_panel.secrets_store import CredentialStore
    from lab_ops_panel.snapshots import SnapshotService
    from lab_ops_panel.ssh.transport import SshTransport
    from lab_ops_panel.tasks.events import EventBus
    from lab_ops_panel.tasks.manager import TaskManager


@dataclass
class AppContainer:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    transport: SshTransport
    credentials: CredentialStore
    registry: ActionRegistry
    event_bus: EventBus
    tasks: TaskManager
    snapshots: SnapshotService
    observability: ObservabilityService
    provisioning: ProvisioningService
    version: str


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - 应用未初始化
        raise RuntimeError("应用容器尚未初始化")
    return container


ContainerDep = Annotated[AppContainer, Depends(get_container)]


def get_server_or_404(session: Session, server_id: int) -> Server:
    from lab_ops_panel.models import Server as ServerModel

    server = session.get(ServerModel, server_id)
    if server is None:
        raise NotFoundError("server_not_found", "主机不存在")
    return server


def password_timestamps(session: Session) -> dict[int, object]:
    from lab_ops_panel.models import ServerSecret

    return dict(session.execute(select(ServerSecret.server_id, ServerSecret.updated_at)).all())
