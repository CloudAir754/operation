"""阶段三（资源观测）API 模型。

约定与主契约一致：JSON 使用 camelCase；每个分区都带
``status`` / ``error`` / ``requiresSudo`` / ``collectedAt``，未知值一律为 ``null``，
不允许用 0 冒充未知。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from lab_ops_panel.schemas import ApiModel, SectionStatus, StrictApiModel, ZonedDateTime

CapabilityStatus = Literal["ok", "partial", "unavailable"]
SudoEffective = Literal["true", "false", "unknown"]
GpuProcessesStatus = Literal["ok", "partial", "unavailable"]
ScanStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
MetricsWindow = Literal["1m", "5m", "15m", "1h"]

WINDOW_SECONDS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
RETENTION_SECONDS = 3600
MAX_POINTS = 2400


class SectionMetaOut(ApiModel):
    status: SectionStatus = "ok"
    error: str | None = None
    requires_sudo: bool = False
    collected_at: ZonedDateTime | None = None


# ---------------------------------------------------------------------------
# 指标：当前点
# ---------------------------------------------------------------------------


class CpuMetricsOut(ApiModel):
    total_percent: float | None = None
    user_percent: float | None = None
    system_percent: float | None = None
    iowait_percent: float | None = None
    steal_percent: float | None = None
    idle_percent: float | None = None
    logical_cpus: int | None = None
    load1: float | None = None
    load5: float | None = None
    load15: float | None = None


class MemoryMetricsOut(ApiModel):
    total_bytes: int | None = None
    available_bytes: int | None = None
    used_bytes: int | None = None
    used_percent: float | None = None
    buffers_bytes: int | None = None
    cached_bytes: int | None = None
    commit_bytes: int | None = None
    swap_total_bytes: int | None = None
    swap_used_bytes: int | None = None
    swap_free_bytes: int | None = None
    swap_in_per_second: float | None = None
    swap_out_per_second: float | None = None


class DiskDeviceMetricsOut(ApiModel):
    name: str
    kind: Literal["disk", "partition", "virtual", "unknown"] = "unknown"
    parent: str | None = None
    model: str | None = None
    size_bytes: int | None = None
    hidden: bool = False
    read_bytes_per_second: float | None = None
    write_bytes_per_second: float | None = None
    read_iops: float | None = None
    write_iops: float | None = None
    await_ms: float | None = None
    util_percent: float | None = None
    reset: bool = False


class DiskMountMetricsOut(ApiModel):
    mount: str
    device: str | None = None
    filesystem: str | None = None
    device_name: str | None = None
    read_bytes_per_second: float | None = None
    write_bytes_per_second: float | None = None


class DiskMetricsOut(ApiModel):
    devices: list[DiskDeviceMetricsOut] = Field(default_factory=list)
    mounts: list[DiskMountMetricsOut] = Field(default_factory=list)


class NetworkInterfaceMetricsOut(ApiModel):
    name: str
    kind: Literal["physical", "bond", "bridge", "vlan", "virtual", "loopback", "unknown"] = (
        "unknown"
    )
    state: str | None = None
    speed_mbps: int | None = None
    addresses: list[str] = Field(default_factory=list)
    rx_bytes_per_second: float | None = None
    tx_bytes_per_second: float | None = None
    rx_packets_per_second: float | None = None
    tx_packets_per_second: float | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None
    rx_drops: int | None = None
    tx_drops: int | None = None
    reset: bool = False


class NetworkMetricsOut(ApiModel):
    default_interface: str | None = None
    interfaces: list[NetworkInterfaceMetricsOut] = Field(default_factory=list)


class GpuSampleOut(ApiModel):
    index: int
    uuid: str | None = None
    name: str | None = None
    utilization_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    encoder_percent: float | None = None
    decoder_percent: float | None = None
    temperature_c: float | None = None
    power_draw_w: float | None = None


class GpuMetricsOut(ApiModel):
    devices: list[GpuSampleOut] = Field(default_factory=list)


class MetricsCurrentOut(ApiModel):
    cpu: CpuMetricsOut
    memory: MemoryMetricsOut
    disk: DiskMetricsOut
    network: NetworkMetricsOut
    gpu: GpuMetricsOut


class CpuSectionOut(SectionMetaOut):
    data: CpuMetricsOut | None = None

    @model_validator(mode="after")
    def _drop_data_when_not_ok(self) -> CpuSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class MemorySectionOut(SectionMetaOut):
    data: MemoryMetricsOut | None = None

    @model_validator(mode="after")
    def _drop_data_when_not_ok(self) -> MemorySectionOut:
        if self.status != "ok":
            self.data = None
        return self


class DiskSectionOut(SectionMetaOut):
    data: DiskMetricsOut | None = None

    @model_validator(mode="after")
    def _drop_data_when_not_ok(self) -> DiskSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class NetworkSectionOut(SectionMetaOut):
    data: NetworkMetricsOut | None = None

    @model_validator(mode="after")
    def _drop_data_when_not_ok(self) -> NetworkSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class GpuMetricsSectionOut(SectionMetaOut):
    data: GpuMetricsOut | None = None

    @model_validator(mode="after")
    def _drop_data_when_not_ok(self) -> GpuMetricsSectionOut:
        if self.status != "ok":
            self.data = None
        return self


class MetricsSectionsOut(ApiModel):
    cpu: CpuSectionOut
    memory: MemorySectionOut
    disk: DiskSectionOut
    network: NetworkSectionOut
    gpu: GpuMetricsSectionOut


class MetricPointOut(ApiModel):
    """曲线点：同时保留远端采样时间与本机接收时间。

    ``collected_at`` 来自探针（远端时钟），``received_at`` 是本机接收时间，
    两者都返回以便前端区分时钟偏差与真实采样间隔。
    """

    collected_at: ZonedDateTime
    received_at: ZonedDateTime
    sample_interval_ms: int
    reset: bool = False
    reset_reason: str | None = None
    cpu_percent: float | None = None
    user_percent: float | None = None
    system_percent: float | None = None
    iowait_percent: float | None = None
    memory_used_percent: float | None = None
    swap_used_percent: float | None = None
    disk: dict[str, dict[str, float | bool | None]] = Field(default_factory=dict)
    network: dict[str, dict[str, float | bool | None]] = Field(default_factory=dict)
    gpu: list[GpuSampleOut] = Field(default_factory=list)


class MetricsOut(ApiModel):
    server_id: int
    collected_at: ZonedDateTime
    window_seconds: int
    retention_seconds: int
    sample_interval_ms: int
    stale: bool = False
    status: SectionStatus
    error: str | None = None
    requires_sudo: bool = False
    current: MetricsSectionsOut
    points: list[MetricPointOut] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------


class GpuDeviceDetailOut(ApiModel):
    index: int
    uuid: str | None = None
    pci_bus_id: str | None = None
    name: str | None = None
    driver_version: str | None = None
    memory_total_mb: float | None = None
    memory_used_mb: float | None = None
    utilization_gpu_percent: float | None = None
    utilization_memory_percent: float | None = None
    encoder_percent: float | None = None
    decoder_percent: float | None = None
    temperature_c: float | None = None
    power_draw_w: float | None = None
    power_limit_w: float | None = None
    clock_graphics_mhz: float | None = None
    clock_memory_mhz: float | None = None
    mig_mode: str | None = None
    compute_mode: str | None = None
    mig_devices: list[str] = Field(default_factory=list)


class GpuProcessOut(ApiModel):
    pid: int
    user: str | None = None
    name: str | None = None
    command: str | None = None
    memory_used_mb: float | None = None
    utilization_percent: float | None = None
    gpu_uuid: str | None = None
    gpu_index: int | None = None
    program_path: str | None = None
    cwd: str | None = None
    started_at: ZonedDateTime | None = None
    stale: bool = False
    error: str | None = None


class GpuCapabilitiesOut(ApiModel):
    nvml: bool = False
    processes: CapabilityStatus = "unavailable"
    mig: bool = False
    per_process_utilization: bool = False


class GpuMonitorOut(ApiModel):
    server_id: int
    collected_at: ZonedDateTime
    status: SectionStatus
    error: str | None = None
    requires_sudo: bool = False
    capabilities: GpuCapabilitiesOut = Field(default_factory=GpuCapabilitiesOut)
    devices: list[GpuDeviceDetailOut] = Field(default_factory=list)
    processes: list[GpuProcessOut] = Field(default_factory=list)
    processes_status: GpuProcessesStatus = "unavailable"
    processes_error: str | None = None
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 用户与 sudo
# ---------------------------------------------------------------------------


class SudoStateOut(ApiModel):
    group_member: bool | None = None
    policy_present: bool | None = None
    effective: SudoEffective = "unknown"
    checked_at: ZonedDateTime | None = None
    error: str | None = None


class HomeStorageOut(ApiModel):
    allocated_bytes: int | None = None
    apparent_bytes: int | None = None
    file_count: int | None = None
    directory_count: int | None = None
    depth: int | None = None
    truncated: bool = False
    incomplete: bool = False
    error: str | None = None
    collected_at: ZonedDateTime | None = None


class UserOut(ApiModel):
    login: str
    uid: int
    gid: int
    primary_group: str | None = None
    supplementary_groups: list[str] = Field(default_factory=list)
    home: str
    shell: str
    system: bool = False
    recent_login: str | None = None
    sudo: SudoStateOut = Field(default_factory=SudoStateOut)
    home_storage: HomeStorageOut | None = None


class UsersOut(ApiModel):
    server_id: int
    collected_at: ZonedDateTime
    include_system: bool = False
    status: SectionStatus
    error: str | None = None
    requires_sudo: bool = False
    users: list[UserOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 挂载点与空间扫描
# ---------------------------------------------------------------------------


class MountOut(ApiModel):
    mount: str
    device: str | None = None
    filesystem: str | None = None
    options: str | None = None
    uuid: str | None = None
    major_minor: str | None = None
    physical_device: str | None = None
    read_only: bool = False


class MountsOut(ApiModel):
    server_id: int
    collected_at: ZonedDateTime
    status: SectionStatus
    error: str | None = None
    requires_sudo: bool = False
    mounts: list[MountOut] = Field(default_factory=list)


class StorageScanRequest(StrictApiModel):
    mount: str = Field(min_length=1, max_length=4096)
    one_file_system: bool = True
    follow_symlinks: bool = False
    max_depth: int = Field(default=3, ge=0, le=8)
    user_filter: int | None = Field(default=None, ge=0)
    min_file_size: int = Field(default=0, ge=0)
    extension: str | None = Field(default=None, max_length=32)
    modified_after: ZonedDateTime | None = None
    include_deleted_open: bool = True

    @field_validator("mount")
    @classmethod
    def _validate_mount(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("挂载点必须是绝对路径")
        if "\x00" in normalized or "\n" in normalized:
            raise ValueError("挂载点包含非法字符")
        parts = [part for part in normalized.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            raise ValueError("挂载点不允许包含 . 或 .. 路径段")
        return "/" + "/".join(parts) if parts else "/"

    @field_validator("extension")
    @classmethod
    def _validate_extension(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if not normalized.startswith("."):
            normalized = f".{normalized}"
        if len(normalized) > 32 or any(char in normalized for char in "/\\*\x00"):
            raise ValueError("扩展名过滤条件不合法")
        return normalized


class UserStorageScanRequest(StrictApiModel):
    uid: int = Field(ge=0)
    mount: str = Field(default="/", min_length=1, max_length=4096)
    max_depth: int = Field(default=8, ge=0, le=8)
    min_file_size: int = Field(default=0, ge=0)
    include_deleted_open: bool = True

    @field_validator("mount")
    @classmethod
    def _validate_mount(cls, value: str) -> str:
        return StorageScanRequest._validate_mount(value)


class StorageNodeOut(ApiModel):
    id: str
    path: str
    parent_path: str | None = None
    name: str
    kind: Literal["dir", "file", "symlink", "other"]
    allocated_bytes: int | None = None
    apparent_bytes: int | None = None
    file_count: int | None = None
    directory_count: int | None = None
    uid: int | None = None
    gid: int | None = None
    inode: int | None = None
    hardlink_duplicate_of: str | None = None
    deleted_open: bool = False
    unscanned: bool = False
    incomplete: bool = False
    error: str | None = None
    modified_at: ZonedDateTime | None = None


class StorageUidSummaryOut(ApiModel):
    uid: int
    allocated_bytes: int
    apparent_bytes: int
    file_count: int
    directory_count: int
    incomplete: bool = False


class StorageScanOut(ApiModel):
    scan_id: str
    server_id: int
    server_alias: str = ""
    mount: str
    mount_info: dict[str, Any] = Field(default_factory=dict)
    status: ScanStatus
    stage: str = "queued"
    progress: int = Field(default=0, ge=0, le=100)
    one_file_system: bool = True
    follow_symlinks: bool = False
    max_depth: int = 3
    user_filter: int | None = None
    min_file_size: int = 0
    extension: str | None = None
    node_count: int = 0
    truncated: bool = False
    incomplete: bool = False
    cross_device_count: int = 0
    deleted_open_bytes: int = 0
    deleted_open_count: int = 0
    nodes: list[StorageNodeOut] = Field(default_factory=list)
    largest_files: list[StorageNodeOut] = Field(default_factory=list)
    summary_by_uid: list[StorageUidSummaryOut] = Field(default_factory=list)
    error: str | None = None
    created_at: ZonedDateTime
    started_at: ZonedDateTime | None = None
    finished_at: ZonedDateTime | None = None


class StorageScanListOut(ApiModel):
    scans: list[StorageScanOut]
    count: int
