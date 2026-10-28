"""主机清单、SSH 配置导入、连接测试与 sudo 密码维护。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response
from sqlalchemy import select

from lab_ops_panel.api.deps import ContainerDep, get_server_or_404, password_timestamps
from lab_ops_panel.audit import record_audit
from lab_ops_panel.db import session_scope
from lab_ops_panel.errors import ApiError
from lab_ops_panel.models import Server, ServerSecret
from lab_ops_panel.remote.diagnosis import diagnose_result
from lab_ops_panel.remote.targets import target_from_server
from lab_ops_panel.schemas import (
    ConnectionTestOut,
    ImportRequest,
    ImportResultOut,
    SecretPut,
    SecretStatusOut,
    ServerListOut,
    ServerOut,
    ServerPatch,
    SkippedHostOut,
    SshConfigOut,
    SshHostOut,
    SshIncludeOut,
    SshIssueOut,
)
from lab_ops_panel.secrets_store import SecretBackendError
from lab_ops_panel.ssh.config_parser import SshConfigParser
from lab_ops_panel.ssh.transport import TransportError

router = APIRouter(tags=["servers"])

CONNECTION_TEST_COMMAND = "printf 'labops-ok'"


def _serialize(server: Server, has_password: bool) -> ServerOut:
    return ServerOut.from_server(server, has_sudo_password=has_password)


def _secret_backend_error(exc: SecretBackendError) -> ApiError:
    return ApiError(503, "secret_backend_unavailable", f"凭据后端不可用：{exc}")


@router.get("/servers", response_model=ServerListOut)
def list_servers(container: ContainerDep) -> ServerListOut:
    with container.session_factory() as session:
        servers = session.scalars(select(Server).order_by(Server.alias)).all()
        secrets = password_timestamps(session)
        items = [_serialize(server, server.id in secrets) for server in servers]
    return ServerListOut(servers=items, count=len(items))


@router.get("/ssh-config", response_model=SshConfigOut)
def read_ssh_config(container: ContainerDep) -> SshConfigOut:
    path = container.settings.resolved_ssh_config_path
    parsed = SshConfigParser(path).parse()
    with container.session_factory() as session:
        imported = set(session.scalars(select(Server.alias)).all())
    return SshConfigOut(
        path=str(path),
        exists=parsed.exists,
        hosts=[
            SshHostOut(
                alias=entry.alias,
                hostname=entry.hostname,
                user=entry.user,
                port=entry.port,
                identity_file=entry.identity_file,
                proxy_jump=entry.proxy_jump,
                proxy_command=entry.proxy_command,
                connect_timeout=entry.connect_timeout,
                imported=entry.alias in imported,
                line_number=entry.line_number,
            )
            for entry in parsed.hosts
        ],
        includes=[
            SshIncludeOut(
                pattern=include.pattern,
                resolved=[str(item) for item in include.resolved],
                error=include.error,
            )
            for include in parsed.includes
        ],
        errors=[
            SshIssueOut(path=str(issue.path), line=issue.line, message=issue.message)
            for issue in parsed.issues
        ],
    )


@router.post("/servers/import", response_model=ImportResultOut)
def import_servers(payload: ImportRequest, container: ContainerDep) -> ImportResultOut:
    path = container.settings.resolved_ssh_config_path
    parsed = SshConfigParser(path).parse()
    selected = set(payload.aliases) if payload.aliases else None
    imported: list[str] = []
    updated: list[str] = []
    skipped: list[SkippedHostOut] = []

    with session_scope(container.session_factory) as session:
        existing = {server.alias: server for server in session.scalars(select(Server))}
        configured: set[str] = set()
        for entry in parsed.hosts:
            if selected is not None and entry.alias not in selected:
                continue
            configured.add(entry.alias)
            if not entry.hostname:
                skipped.append(SkippedHostOut(alias=entry.alias, reason="配置中缺少 HostName"))
                continue
            server = existing.get(entry.alias)
            if server is None:
                server = Server(alias=entry.alias, enabled=True, notes="", source="ssh_config")
                session.add(server)
                existing[entry.alias] = server
                imported.append(entry.alias)
            else:
                updated.append(entry.alias)
            server.hostname = entry.hostname
            server.user = entry.user
            server.port = entry.port
            server.identity_file = entry.identity_file
            server.proxy_jump = entry.proxy_jump
            server.proxy_command = entry.proxy_command
            server.connect_timeout = entry.connect_timeout
            server.source = "ssh_config"
        if selected is not None:
            missing = sorted(selected - configured)
            skipped.extend(
                SkippedHostOut(alias=alias, reason="SSH 配置中不存在该 Host") for alias in missing
            )
        elif payload.prune_missing:
            for alias, server in existing.items():
                if server.source == "ssh_config" and alias not in configured and server.enabled:
                    server.enabled = False
                    updated.append(alias)

    record_audit(
        container.session_factory,
        action="import_ssh_config",
        target=str(path),
        outcome="succeeded",
        detail=f"新增 {len(imported)}，更新 {len(updated)}，跳过 {len(skipped)}",
    )
    return ImportResultOut(
        imported=imported,
        updated=updated,
        skipped=skipped,
        errors=[
            SshIssueOut(path=str(issue.path), line=issue.line, message=issue.message)
            for issue in parsed.issues
        ],
    )


@router.patch("/servers/{server_id}", response_model=ServerOut)
def update_server(server_id: int, payload: ServerPatch, container: ContainerDep) -> ServerOut:
    with session_scope(container.session_factory) as session:
        server = get_server_or_404(session, server_id)
        if payload.enabled is not None:
            server.enabled = payload.enabled
        if payload.notes is not None:
            server.notes = payload.notes
        has_password = server.secret is not None
        return _serialize(server, has_password)


@router.post("/servers/{server_id}/connection-test", response_model=ConnectionTestOut)
def connection_test(server_id: int, container: ContainerDep) -> ConnectionTestOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        target = target_from_server(server)
        alias = server.alias
    try:
        result = container.transport.run(
            target,
            CONNECTION_TEST_COMMAND,
            timeout=container.settings.connection_test_timeout_seconds,
            max_output_bytes=8192,
        )
    except TransportError as exc:
        raise ApiError(503, "ssh_unavailable", str(exc)) from exc

    ok = result.ok and "labops-ok" in result.stdout
    error = None
    if not ok:
        error = diagnose_result(result, operation="连接测试") or "连接测试失败"
    tested_at = datetime.now(UTC)
    with session_scope(container.session_factory) as session:
        server = get_server_or_404(session, server_id)
        server.last_connect_status = "ok" if ok else "failed"
        server.last_connect_error = error
        server.last_connect_at = tested_at
    record_audit(
        container.session_factory,
        action="connection_test",
        target=alias,
        outcome="ok" if ok else "failed",
        detail=error or "",
    )
    return ConnectionTestOut(
        ok=ok,
        status="ok" if ok else "failed",
        error=error,
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        tested_at=tested_at,
    )


@router.get("/servers/{server_id}/secret", response_model=SecretStatusOut)
def read_secret_status(server_id: int, container: ContainerDep) -> SecretStatusOut:
    with container.session_factory() as session:
        get_server_or_404(session, server_id)
        secret = session.get(ServerSecret, server_id)
        return SecretStatusOut(
            server_id=server_id,
            has_password=secret is not None,
            updated_at=secret.updated_at if secret is not None else None,
        )


@router.put("/servers/{server_id}/secret", response_model=SecretStatusOut)
def put_secret(server_id: int, payload: SecretPut, container: ContainerDep) -> SecretStatusOut:
    with container.session_factory() as session:
        get_server_or_404(session, server_id)
    try:
        updated_at = container.credentials.set_password(server_id, payload.password)
    except SecretBackendError as exc:
        raise _secret_backend_error(exc) from exc
    record_audit(
        container.session_factory,
        action="update_sudo_password",
        target=str(server_id),
        outcome="succeeded",
        detail="已更新 sudo 密码（内容不记录）",
    )
    return SecretStatusOut(server_id=server_id, has_password=True, updated_at=updated_at)


@router.delete("/servers/{server_id}/secret", status_code=204)
def delete_secret(server_id: int, container: ContainerDep) -> Response:
    with container.session_factory() as session:
        get_server_or_404(session, server_id)
    container.credentials.clear_password(server_id)
    record_audit(
        container.session_factory,
        action="clear_sudo_password",
        target=str(server_id),
        outcome="succeeded",
        detail="已清除 sudo 密码",
    )
    return Response(status_code=204)


@router.get("/servers/{server_id}", response_model=ServerOut)
def read_server(server_id: int, container: ContainerDep) -> ServerOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return _serialize(server, server.secret is not None)
