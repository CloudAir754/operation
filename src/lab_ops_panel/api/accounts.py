"""账户生命周期 API：密码规则、系统策略、批量建用户、目录迁移与新空间分配。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse

from lab_ops_panel.api.deps import ContainerDep, get_server_or_404
from lab_ops_panel.errors import ApiError
from lab_ops_panel.remote.accounts import MIGRATION_TERMINAL_STATUSES
from lab_ops_panel.schemas_accounts import (
    AccountBatchListOut,
    AccountBatchOut,
    AccountBatchRequest,
    DatasetListOut,
    DatasetOut,
    DatasetRequest,
    GeneratorOut,
    GeneratorPreviewRequest,
    GeneratorSaveRequest,
    MigrationCleanupRequest,
    MigrationListOut,
    MigrationOut,
    MigrationPreflightOut,
    MigrationRequest,
    MigrationRollbackRequest,
    MigrationTargetRequest,
    PasswordPolicyOut,
    PolicyChangeListOut,
    PolicyChangeOut,
    PolicyChangeRequest,
    PolicyDiffOut,
    RevealPasswordsOut,
    StorageSettingsOut,
    StorageSettingsPut,
)
from lab_ops_panel.tasks.events import KEEPALIVE_FRAME, format_sse

router = APIRouter(tags=["accounts"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
KEEPALIVE_SECONDS = 15.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 密码生成规则
# ---------------------------------------------------------------------------


@router.get("/accounts/generator", response_model=GeneratorOut)
def read_generator(container: ContainerDep) -> GeneratorOut:
    return container.provisioning.generator()


@router.put("/accounts/generator", response_model=GeneratorOut)
def save_generator(payload: GeneratorSaveRequest, container: ContainerDep) -> GeneratorOut:
    return container.provisioning.save_generator(payload.rules)


@router.post("/accounts/generator/preview", response_model=GeneratorOut)
def preview_generator(payload: GeneratorPreviewRequest, container: ContainerDep) -> GeneratorOut:
    server = None
    if payload.server_id is not None:
        with container.session_factory() as session:
            server = get_server_or_404(session, payload.server_id)
    return container.provisioning.preview_generator(payload.rules, server)


# ---------------------------------------------------------------------------
# 系统密码策略
# ---------------------------------------------------------------------------


@router.get("/servers/{server_id}/accounts/policy", response_model=PasswordPolicyOut)
def read_policy(
    server_id: int,
    container: ContainerDep,
    refresh: bool = Query(default=False),
) -> PasswordPolicyOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.provisioning.policy(server, refresh=refresh)


@router.post("/servers/{server_id}/accounts/policy/preview", response_model=PolicyDiffOut)
def preview_policy(
    server_id: int, payload: PolicyChangeRequest, container: ContainerDep
) -> PolicyDiffOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.provisioning.policy_preview(server, payload.targets)


@router.put("/servers/{server_id}/accounts/policy", response_model=PolicyChangeOut)
def apply_policy(
    server_id: int, payload: PolicyChangeRequest, container: ContainerDep
) -> PolicyChangeOut:
    if not payload.confirm:
        raise ApiError(
            422,
            "confirmation_required",
            "修改系统密码策略会影响该主机所有本地用户，需要显式确认",
        )
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.provisioning.apply_policy(server, payload.targets)


@router.get("/servers/{server_id}/accounts/policy/changes", response_model=PolicyChangeListOut)
def list_policy_changes(
    server_id: int,
    container: ContainerDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> PolicyChangeListOut:
    with container.session_factory() as session:
        get_server_or_404(session, server_id)
    changes = container.provisioning.policy_changes(server_id, limit=limit)
    return PolicyChangeListOut(changes=changes, count=len(changes))


@router.post(
    "/servers/{server_id}/accounts/policy/changes/{change_id}/restore",
    response_model=PolicyChangeOut,
)
def restore_policy(server_id: int, change_id: str, container: ContainerDep) -> PolicyChangeOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.provisioning.restore_policy(server, change_id)


# ---------------------------------------------------------------------------
# 批量新建用户
# ---------------------------------------------------------------------------


@router.post(
    "/servers/{server_id}/account-batches",
    response_model=AccountBatchOut,
    status_code=status.HTTP_201_CREATED,
)
def create_account_batch(
    server_id: int, payload: AccountBatchRequest, container: ContainerDep
) -> AccountBatchOut:
    if not payload.confirm:
        raise ApiError(422, "confirmation_required", "批量新建用户需要目标确认")
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.provisioning.create_batch(server, payload)


@router.get("/servers/{server_id}/account-batches", response_model=AccountBatchListOut)
def list_account_batches(
    server_id: int,
    container: ContainerDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> AccountBatchListOut:
    with container.session_factory() as session:
        get_server_or_404(session, server_id)
    batches = container.provisioning.list_batches(server_id, limit=limit)
    return AccountBatchListOut(batches=batches, count=len(batches))


@router.get("/account-batches/{batch_id}", response_model=AccountBatchOut)
def read_account_batch(batch_id: str, container: ContainerDep) -> AccountBatchOut:
    return container.provisioning.get_batch(batch_id)


@router.post("/account-batches/{batch_id}/cancel", response_model=AccountBatchOut)
def cancel_account_batch(batch_id: str, container: ContainerDep) -> AccountBatchOut:
    return container.provisioning.cancel_batch(batch_id)


@router.post("/account-batches/{batch_id}/reveal", response_model=RevealPasswordsOut)
def reveal_account_passwords(batch_id: str, container: ContainerDep) -> RevealPasswordsOut:
    return container.provisioning.reveal_passwords(batch_id)


@router.get("/account-batches/{batch_id}/events")
async def account_batch_events(
    batch_id: str, request: Request, container: ContainerDep
) -> StreamingResponse:
    snapshot = container.provisioning.get_batch(batch_id)
    queue = container.event_bus.subscribe(batch_id)

    async def stream() -> AsyncIterator[str]:
        try:
            yield format_sse(
                {
                    "type": "progress",
                    "batchId": snapshot.id,
                    "status": snapshot.status,
                    "stage": snapshot.stage,
                    "progress": snapshot.progress,
                    "at": _now_iso(),
                }
            )
            if snapshot.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                yield format_sse(
                    {
                        "type": "done",
                        "batchId": snapshot.id,
                        "status": snapshot.status,
                        "progress": snapshot.progress,
                        "error": snapshot.error,
                    }
                )
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield KEEPALIVE_FRAME
                    continue
                yield format_sse(event)
                if event.get("type") == "done":
                    return
        finally:
            container.event_bus.unsubscribe(batch_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------------------------------------------------------------------------
# 用户目录迁移
# ---------------------------------------------------------------------------


@router.post("/servers/{server_id}/home-migrations/preflight", response_model=MigrationPreflightOut)
def preflight_migration(
    server_id: int, payload: MigrationTargetRequest, container: ContainerDep
) -> MigrationPreflightOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.provisioning.preflight_migration(
            server, payload.login, target_root=payload.target_root
        )


@router.post(
    "/servers/{server_id}/home-migrations",
    response_model=MigrationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_migration(
    server_id: int, payload: MigrationRequest, container: ContainerDep
) -> MigrationOut:
    if not payload.confirm:
        raise ApiError(422, "confirmation_required", "目录迁移会锁定账户并移动数据，需要显式确认")
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.provisioning.create_migration(server, payload)


@router.get("/servers/{server_id}/home-migrations", response_model=MigrationListOut)
def list_migrations(
    server_id: int,
    container: ContainerDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> MigrationListOut:
    with container.session_factory() as session:
        get_server_or_404(session, server_id)
    migrations = container.provisioning.list_migrations(server_id, limit=limit)
    return MigrationListOut(migrations=migrations, count=len(migrations))


@router.get("/home-migrations/{migration_id}", response_model=MigrationOut)
def read_migration(migration_id: str, container: ContainerDep) -> MigrationOut:
    return container.provisioning.get_migration(migration_id)


@router.post("/home-migrations/{migration_id}/cancel", response_model=MigrationOut)
def cancel_migration(migration_id: str, container: ContainerDep) -> MigrationOut:
    return container.provisioning.cancel_migration(migration_id)


@router.post("/home-migrations/{migration_id}/resume", response_model=MigrationOut)
def resume_migration(migration_id: str, container: ContainerDep) -> MigrationOut:
    return container.provisioning.resume_migration(migration_id)


@router.post("/home-migrations/{migration_id}/rollback", response_model=MigrationOut)
def rollback_migration(
    migration_id: str, payload: MigrationRollbackRequest, container: ContainerDep
) -> MigrationOut:
    return container.provisioning.rollback_migration(
        migration_id, discard_new_data=payload.discard_new_data
    )


@router.post("/home-migrations/{migration_id}/cleanup", response_model=MigrationOut)
def cleanup_migration(
    migration_id: str, payload: MigrationCleanupRequest, container: ContainerDep
) -> MigrationOut:
    return container.provisioning.cleanup_migration(migration_id, payload.confirm_path)


@router.get("/home-migrations/{migration_id}/events")
async def migration_events(
    migration_id: str, request: Request, container: ContainerDep
) -> StreamingResponse:
    snapshot = container.provisioning.get_migration(migration_id)
    queue = container.event_bus.subscribe(migration_id)

    async def stream() -> AsyncIterator[str]:
        try:
            terminal = snapshot.status in MIGRATION_TERMINAL_STATUSES
            yield format_sse(
                {
                    "type": "done" if terminal else "progress",
                    "migrationId": snapshot.id,
                    "status": snapshot.status,
                    "stage": snapshot.stage,
                    "stageIndex": snapshot.stage_index,
                    "progress": snapshot.progress,
                    "error": snapshot.error,
                    "at": _now_iso(),
                }
            )
            if terminal:
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield KEEPALIVE_FRAME
                    continue
                yield format_sse(event)
                if event.get("type") == "done":
                    return
        finally:
            container.event_bus.unsubscribe(migration_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------------------------------------------------------------------------
# 新空间分配
# ---------------------------------------------------------------------------


@router.get("/servers/{server_id}/storage-settings", response_model=StorageSettingsOut)
def read_storage_settings(server_id: int, container: ContainerDep) -> StorageSettingsOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return StorageSettingsOut(**container.provisioning.storage_settings(server))


@router.put("/servers/{server_id}/storage-settings", response_model=StorageSettingsOut)
def update_storage_settings(
    server_id: int, payload: StorageSettingsPut, container: ContainerDep
) -> StorageSettingsOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        result = container.provisioning.update_storage_settings(
            server, data_root=payload.data_root, migration_root=payload.migration_root
        )
        return StorageSettingsOut(**result)


@router.post(
    "/servers/{server_id}/datasets",
    response_model=DatasetOut,
    status_code=status.HTTP_201_CREATED,
)
def create_dataset(server_id: int, payload: DatasetRequest, container: ContainerDep) -> DatasetOut:
    with container.session_factory() as session:
        server = get_server_or_404(session, server_id)
        return container.provisioning.create_dataset(server, payload)


@router.get("/servers/{server_id}/datasets", response_model=DatasetListOut)
def list_datasets(
    server_id: int,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> DatasetListOut:
    with container.session_factory() as session:
        get_server_or_404(session, server_id)
    datasets = container.provisioning.list_datasets(server_id, limit=limit)
    return DatasetListOut(datasets=datasets, count=len(datasets))


@router.post("/datasets/{dataset_id}/unlink", response_model=DatasetOut)
def unlink_dataset(dataset_id: str, container: ContainerDep) -> DatasetOut:
    return container.provisioning.unlink_dataset(dataset_id)
