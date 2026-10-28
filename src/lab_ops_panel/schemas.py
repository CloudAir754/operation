"""API 请求/响应模型与快照数据结构。

约定：JSON 使用 camelCase，Python 内部使用 snake_case；
敏感请求模型禁止未知字段，避免前端误传凭据类字段。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

SectionStatus = Literal["ok", "permission_denied", "unavailable", "error"]
TaskStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
StepStatus = Literal["pending", "running", "ok", "failed", "skipped"]
RiskLevel = Literal["low", "medium", "high"]
ConnectStatus = Literal["ok", "failed", "unknown"]


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


ZonedDateTime = Annotated[
    datetime, PlainSerializer(_format_datetime, return_type=str, when_used="json")
]


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        ser_json_timedelta="float",
    )


class StrictApiModel(ApiModel):
    """请求体基类：拒绝未知字段，避免凭据被静默忽略或误存。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# 系统
# ---------------------------------------------------------------------------


class HealthOut(ApiModel):
    status: str
    version: str
    time: ZonedDateTime


class SettingsOut(ApiModel):
    allow_arbitrary_commands: bool
    snapshot_ttl_seconds: int
    max_output_bytes: int
    ssh_config_path: str
    app_data_dir: str
    bind_host: str
    bind_port: int
    transport: str


class ActionFieldOut(ApiModel):
    name: str
    required: bool
    type: str


class ActionSpecOut(ApiModel):
    id: str
    label: str
    description: str
    risk: RiskLevel
    requires_confirmation: bool
    requires_idempotency_key: bool
    fields: list[ActionFieldOut]


class ActionsOut(ApiModel):
    actions: list[ActionSpecOut]


# ---------------------------------------------------------------------------
# SSH 配置
# ---------------------------------------------------------------------------


class SshHostOut(ApiModel):
    alias: str
    hostname: str | None
    user: str | None
    port: int
    identity_file: str | None
    proxy_jump: str | None
    proxy_command: str | None
    connect_timeout: int
    imported: bool
    line_number: int


class SshIncludeOut(ApiModel):
    pattern: str
    resolved: list[str]
    error: str | None = None


class SshIssueOut(ApiModel):
    path: str
    line: int
    message: str


class SshConfigOut(ApiModel):
    path: str
    exists: bool
    hosts: list[SshHostOut]
    includes: list[SshIncludeOut]
    errors: list[SshIssueOut]


class ImportRequest(StrictApiModel):
    aliases: list[str] | None = None
    prune_missing: bool = False

    @field_validator("aliases")
    @classmethod
    def _strip_aliases(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [alias.strip() for alias in value if alias and alias.strip()]
        return cleaned or None


class SkippedHostOut(ApiModel):
    alias: str
    reason: str


class ImportResultOut(ApiModel):
    imported: list[str]
    updated: list[str]
    skipped: list[SkippedHostOut]
    errors: list[SshIssueOut]


# ---------------------------------------------------------------------------
# 主机
# ---------------------------------------------------------------------------


class LastConnectOut(ApiModel):
    status: ConnectStatus
    error: str | None = None
    at: ZonedDateTime | None = None


class ServerOut(ApiModel):
    id: int
    alias: str
    hostname: str | None
    user: str | None
    port: int
    identity_file: str | None
    proxy_jump: str | None
    proxy_command: str | None
    connect_timeout: int
    source: str
    enabled: bool
    notes: str
    has_sudo_password: bool
    last_connect: LastConnectOut

    @classmethod
    def from_server(cls, server: Any, *, has_sudo_password: bool) -> ServerOut:
        return cls(
            id=server.id,
            alias=server.alias,
            hostname=server.hostname,
            user=server.user,
            port=server.port,
            identity_file=server.identity_file,
            proxy_jump=server.proxy_jump,
            proxy_command=server.proxy_command,
            connect_timeout=server.connect_timeout,
            source=server.source,
            enabled=server.enabled,
            notes=server.notes,
            has_sudo_password=has_sudo_password,
            last_connect=LastConnectOut(
                status=server.last_connect_status,
                error=server.last_connect_error,
                at=server.last_connect_at,
            ),
        )


class ServerListOut(ApiModel):
    servers: list[ServerOut]
    count: int


class ServerPatch(StrictApiModel):
    enabled: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> ServerPatch:
        if self.enabled is None and self.notes is None:
            raise ValueError("至少需要提供 enabled 或 notes")
        return self


class ConnectionTestOut(ApiModel):
    ok: bool
    status: ConnectStatus
    error: str | None
    duration_ms: int
    exit_code: int | None = None
    tested_at: ZonedDateTime


# ---------------------------------------------------------------------------
# 快照数据
# ---------------------------------------------------------------------------


class SystemInfoOut(ApiModel):
    hostname: str | None = None
    os: str | None = None
    kernel: str | None = None
    arch: str | None = None
    uptime_seconds: int | None = None
    boot_time: str | None = None


class CpuInfoOut(ApiModel):
    model: str | None = None
    cores: int | None = None
    usage_percent: float | None = None
    load1: float | None = None
    load5: float | None = None
    load15: float | None = None


class MemoryInfoOut(ApiModel):
    total_bytes: int | None = None
    available_bytes: int | None = None
    used_percent: float | None = None
    swap_total_bytes: int | None = None
    swap_used_bytes: int | None = None


class DiskMountOut(ApiModel):
    mount: str
    filesystem: str | None = None
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    used_percent: float | None = None


class DiskInfoOut(ApiModel):
    mounts: list[DiskMountOut] = Field(default_factory=list)


class NetworkInterfaceOut(ApiModel):
    name: str
    state: str | None = None
    speed_mbps: int | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None
    addresses: list[str] = Field(default_factory=list)


class NetworkInfoOut(ApiModel):
    default_interface: str | None = None
    gateway: str | None = None
    interfaces: list[NetworkInterfaceOut] = Field(default_factory=list)


class GpuDeviceOut(ApiModel):
    index: int
    name: str | None = None
    memory_total_mb: int | None = None
    memory_used_mb: int | None = None
    utilization_percent: float | None = None
    temperature_c: float | None = None
    driver_version: str | None = None


class GpuInfoOut(ApiModel):
    devices: list[GpuDeviceOut] = Field(default_factory=list)


class ServiceUnitOut(ApiModel):
    name: str
    load: str | None = None
    active: str | None = None
    sub: str | None = None
    description: str | None = None
    protected: bool = False


class ServiceInfoOut(ApiModel):
    running_count: int | None = None
    failed_count: int | None = None
    failed_units: list[str] = Field(default_factory=list)
    units: list[ServiceUnitOut] = Field(default_factory=list)


class SystemSectionOut(ApiModel):
    status: SectionStatus
    error: str | None = None
    data: SystemInfoOut | None = None

    @model_validator(mode="after")
    def _data_matches_status(self) -> SystemSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class CpuSectionOut(ApiModel):
    status: SectionStatus
    error: str | None = None
    data: CpuInfoOut | None = None

    @model_validator(mode="after")
    def _data_matches_status(self) -> CpuSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class MemorySectionOut(ApiModel):
    status: SectionStatus
    error: str | None = None
    data: MemoryInfoOut | None = None

    @model_validator(mode="after")
    def _data_matches_status(self) -> MemorySectionOut:
        if self.status != "ok":
            self.data = None
        return self


class DiskSectionOut(ApiModel):
    status: SectionStatus
    error: str | None = None
    data: DiskInfoOut | None = None

    @model_validator(mode="after")
    def _data_matches_status(self) -> DiskSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class NetworkSectionOut(ApiModel):
    status: SectionStatus
    error: str | None = None
    data: NetworkInfoOut | None = None

    @model_validator(mode="after")
    def _data_matches_status(self) -> NetworkSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class GpuSectionOut(ApiModel):
    status: SectionStatus
    error: str | None = None
    data: GpuInfoOut | None = None

    @model_validator(mode="after")
    def _data_matches_status(self) -> GpuSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class ServiceSectionOut(ApiModel):
    status: SectionStatus
    error: str | None = None
    data: ServiceInfoOut | None = None

    @model_validator(mode="after")
    def _data_matches_status(self) -> ServiceSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class SnapshotDataOut(ApiModel):
    system: SystemSectionOut
    cpu: CpuSectionOut
    memory: MemorySectionOut
    disks: DiskSectionOut
    network: NetworkSectionOut
    gpu: GpuSectionOut
    services: ServiceSectionOut


class SnapshotOut(ApiModel):
    server_id: int
    collected_at: ZonedDateTime
    age_seconds: int
    stale: bool
    status: str
    error: str | None = None
    data: SnapshotDataOut


class ServicesOut(ApiModel):
    server_id: int
    collected_at: ZonedDateTime
    status: SectionStatus
    error: str | None = None
    units: list[ServiceUnitOut] = Field(default_factory=list)


class LogsOut(ApiModel):
    server_id: int
    unit: str | None
    collected_at: ZonedDateTime
    lines: list[str]
    exit_code: int | None = None
    truncated: bool = False
    error: str | None = None
    command: str


class SecretStatusOut(ApiModel):
    server_id: int
    has_password: bool
    updated_at: ZonedDateTime | None = None


class SecretPut(StrictApiModel):
    password: str = Field(min_length=1, max_length=512)


# ---------------------------------------------------------------------------
# 动作参数
# ---------------------------------------------------------------------------


class EmptyParams(StrictApiModel):
    pass


class UnitParams(StrictApiModel):
    unit: str = Field(min_length=1, max_length=200)


class LogsParams(StrictApiModel):
    unit: str | None = Field(default=None, max_length=200)
    lines: int = Field(default=200, ge=1, le=2000)
    use_sudo: bool = False


class CommandParams(StrictApiModel):
    command: str = Field(min_length=1, max_length=2000)
    use_sudo: bool = False


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


class TaskCreate(StrictApiModel):
    server_id: int
    action: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False
    idempotency_key: str | None = Field(default=None, max_length=128)


class TaskStepOut(ApiModel):
    seq: int
    name: str
    status: StepStatus
    exit_code: int | None = None
    output_tail: str = ""
    started_at: ZonedDateTime | None = None
    finished_at: ZonedDateTime | None = None


class TaskOut(ApiModel):
    id: str
    server_id: int
    server_alias: str
    action: str
    action_label: str
    risk: RiskLevel
    params: dict[str, Any]
    status: TaskStatus
    progress: int
    exit_code: int | None = None
    error: str | None = None
    output_tail: str = ""
    idempotency_key: str | None = None
    created_at: ZonedDateTime
    started_at: ZonedDateTime | None = None
    finished_at: ZonedDateTime | None = None
    steps: list[TaskStepOut] = Field(default_factory=list)


class TaskListOut(ApiModel):
    tasks: list[TaskOut]
    count: int


class TaskCancelOut(ApiModel):
    task: TaskOut


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


class AuditEntryOut(ApiModel):
    id: int
    action: str
    target: str
    outcome: str
    detail: str
    created_at: ZonedDateTime


class AuditListOut(ApiModel):
    entries: list[AuditEntryOut]
