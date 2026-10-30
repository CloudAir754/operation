"""账户生命周期 API 模型（阶段四）。

约定与主契约一致：JSON 使用 camelCase、请求体拒绝未知字段、时间带时区。
``PASSWORD_*`` 相关响应永远不包含明文密码；生成的目标账户密码只能在
``reveal`` 接口中一次性取回。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from lab_ops_panel.passwords import MAX_LENGTH, MIN_LENGTH, PasswordRules
from lab_ops_panel.remote.accounts import (
    MAX_BATCH_USERS,
    normalize_path,
    validate_comment,
    validate_groups,
    validate_link_name,
    validate_login,
    validate_password_text,
    validate_shell,
)
from lab_ops_panel.schemas import ApiModel, SectionStatus, StrictApiModel, ZonedDateTime

PolicyStatus = Literal["applied", "failed", "restored"]
BatchStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
MigrationStatus = Literal[
    "queued", "running", "succeeded", "failed", "cancelled", "interrupted", "rolled_back"
]
DatasetStatus = Literal["succeeded", "failed", "unlinked"]


class CheckOut(ApiModel):
    """一致性/预检检查项；``ok=false`` 且 ``blocking=true`` 时禁止继续。"""

    code: str
    label: str
    ok: bool
    message: str = ""
    blocking: bool = True


class AdjustmentOut(ApiModel):
    field: str
    label: str
    requested: int
    applied: int
    reason: str


class PolicyFloorOut(ApiModel):
    min_length: int
    min_upper: int
    min_lower: int
    min_digits: int
    min_symbols: int
    min_classes: int
    max_repeat: int
    max_sequence: int
    max_class_repeat: int
    reject_username: bool
    source: str
    notes: list[str] = Field(default_factory=list)


class GeneratorOut(ApiModel):
    configured: bool = False
    rules: PasswordRules
    effective_length: int
    adjustments: list[AdjustmentOut] = Field(default_factory=list)
    example: str = ""
    checks: list[CheckOut] = Field(default_factory=list)
    floor: PolicyFloorOut | None = None
    server_id: int | None = None
    updated_at: ZonedDateTime | None = None


class GeneratorPreviewRequest(StrictApiModel):
    rules: PasswordRules
    server_id: int | None = None


class GeneratorSaveRequest(StrictApiModel):
    rules: PasswordRules


class PasswordLengthOut(ApiModel):
    min: int = MIN_LENGTH
    max: int = MAX_LENGTH
    default: int = 20


# ---------------------------------------------------------------------------
# 系统密码策略
# ---------------------------------------------------------------------------


class PolicyFieldOut(ApiModel):
    key: str
    label: str
    value: int
    origin: Literal["pam", "conf", "default"]
    writable: bool
    min: int
    max: int
    reason: str | None = None


class PolicyCapabilitiesOut(ApiModel):
    pwquality_conf: bool = False
    pwquality_module: bool = False
    pwquality_module_editable: bool = False
    pam_retry_editable: bool = False
    pwscore: bool = False
    chage: bool = False
    useradd: bool = False
    rsync: bool = False
    drop_in_dir: bool = False
    writable_fields: list[str] = Field(default_factory=list)
    read_only: bool = True
    reasons: list[str] = Field(default_factory=list)


class PasswordPolicyOut(ApiModel):
    server_id: int
    collected_at: ZonedDateTime
    status: SectionStatus
    error: str | None = None
    os: dict[str, Any] = Field(default_factory=dict)
    fields: list[PolicyFieldOut] = Field(default_factory=list)
    capabilities: PolicyCapabilitiesOut = Field(default_factory=PolicyCapabilitiesOut)
    managed_lines: list[str] = Field(default_factory=list)
    unknown_keys: list[str] = Field(default_factory=list)
    login_defs: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    floor: PolicyFloorOut | None = None
    generator: GeneratorOut | None = None


class PolicyChangeRequest(StrictApiModel):
    targets: dict[str, int] = Field(default_factory=dict)
    confirm: bool = False

    @model_validator(mode="after")
    def _not_empty(self) -> PolicyChangeRequest:
        if not self.targets:
            raise ValueError("至少需要提供一个策略字段")
        return self


class PolicyDiffOut(ApiModel):
    server_id: int
    fields: dict[str, dict[str, int | None]] = Field(default_factory=dict)
    diff: str = ""
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    applicable: bool = False


class PolicyChangeOut(ApiModel):
    id: str
    server_id: int
    status: PolicyStatus
    targets: dict[str, Any] = Field(default_factory=dict)
    diff: str = ""
    backup_id: str = ""
    validation: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: ZonedDateTime
    restored_at: ZonedDateTime | None = None


class PolicyChangeListOut(ApiModel):
    changes: list[PolicyChangeOut]
    count: int


# ---------------------------------------------------------------------------
# 批量新建用户
# ---------------------------------------------------------------------------


class UserSpecIn(StrictApiModel):
    login: str
    comment: str = ""
    shell: str = "/bin/bash"
    groups: list[str] = Field(default_factory=list)
    uid: int | None = Field(default=None, ge=1000, le=4_000_000_000)
    password: str | None = Field(default=None, max_length=256)

    @field_validator("login")
    @classmethod
    def _login(cls, value: str) -> str:
        return validate_login(value)

    @field_validator("comment")
    @classmethod
    def _comment(cls, value: str) -> str:
        return validate_comment(value)

    @field_validator("shell")
    @classmethod
    def _shell(cls, value: str) -> str:
        return validate_shell(value)

    @field_validator("groups")
    @classmethod
    def _groups(cls, value: list[str]) -> list[str]:
        return validate_groups(value)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str | None) -> str | None:
        return validate_password_text(value) if value else None


class AccountBatchRequest(StrictApiModel):
    users: list[UserSpecIn] = Field(min_length=1, max_length=MAX_BATCH_USERS)
    rules: PasswordRules | None = None
    confirm: bool = False

    @model_validator(mode="after")
    def _unique_logins(self) -> AccountBatchRequest:
        seen: set[str] = set()
        for entry in self.users:
            if entry.login in seen:
                raise ValueError(f"用户名重复：{entry.login}")
            seen.add(entry.login)
        return self


class AccountBatchResultOut(ApiModel):
    login: str
    status: str
    error: str | None = None
    uid: int | None = None
    home: str | None = None
    password_generated: bool = False


class AccountBatchOut(ApiModel):
    id: str
    server_id: int
    server_alias: str = ""
    status: BatchStatus
    stage: str = "queued"
    progress: int = 0
    results: list[AccountBatchResultOut] = Field(default_factory=list)
    error: str | None = None
    has_generated_passwords: bool = False
    created_at: ZonedDateTime
    started_at: ZonedDateTime | None = None
    finished_at: ZonedDateTime | None = None


class AccountBatchListOut(ApiModel):
    batches: list[AccountBatchOut]
    count: int


class RevealPasswordsOut(ApiModel):
    batch_id: str
    passwords: dict[str, str] = Field(default_factory=dict)
    note: str = "密码只显示这一次；关闭后无法再次查看"


# ---------------------------------------------------------------------------
# 用户目录迁移
# ---------------------------------------------------------------------------


class MigrationTargetRequest(StrictApiModel):
    login: str
    target_root: str | None = Field(default=None, max_length=1024)

    @field_validator("login")
    @classmethod
    def _login(cls, value: str) -> str:
        return validate_login(value)

    @field_validator("target_root")
    @classmethod
    def _target_root(cls, value: str | None) -> str | None:
        return normalize_path(value, label="迁移根目录") if value else None


class MigrationRequest(MigrationTargetRequest):
    safety_margin_percent: int = Field(default=10, ge=0, le=100)
    full_checksum: bool = False
    retention_days: int = Field(default=7, ge=1, le=90)
    confirm: bool = False


class MigrationPathsOut(ApiModel):
    source: str
    target_root: str
    target: str
    staging: str
    backup: str


class MigrationChecksOut(ApiModel):
    allowed: bool = False
    checks: list[CheckOut] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class MigrationPreflightOut(ApiModel):
    server_id: int
    login: str
    paths: MigrationPathsOut
    source_info: dict[str, Any] = Field(default_factory=dict)
    target_info: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, Any] = Field(default_factory=dict)
    space: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckOut] = Field(default_factory=list)
    allowed: bool = False
    blockers: list[str] = Field(default_factory=list)
    requires_sudo: bool = True
    collected_at: ZonedDateTime


class MigrationOut(ApiModel):
    id: str
    server_id: int
    server_alias: str = ""
    login: str
    uid: int
    gid: int
    status: MigrationStatus
    stage: str
    stage_index: int = 0
    stages: list[str] = Field(default_factory=list)
    progress: int = 0
    paths: MigrationPathsOut
    counts: dict[str, Any] = Field(default_factory=dict)
    space: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckOut] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    account_status_before: str | None = None
    backup_retention_days: int = 7
    rollback_available: bool = False
    cleanup_available: bool = False
    cancel_requested: bool = False
    created_at: ZonedDateTime
    started_at: ZonedDateTime | None = None
    finished_at: ZonedDateTime | None = None
    cleaned_at: ZonedDateTime | None = None


class MigrationListOut(ApiModel):
    migrations: list[MigrationOut]
    count: int


class MigrationRollbackRequest(StrictApiModel):
    discard_new_data: bool = False


class MigrationCleanupRequest(StrictApiModel):
    confirm_path: str = Field(min_length=1, max_length=1024)


# ---------------------------------------------------------------------------
# 新空间分配
# ---------------------------------------------------------------------------


class StorageSettingsOut(ApiModel):
    server_id: int
    data_root: str | None = None
    migration_root: str | None = None
    default_data_root: str = "/data2"
    default_migration_root: str = "/data2/users"


class StorageSettingsPut(StrictApiModel):
    data_root: str | None = Field(default=None, max_length=1024)
    migration_root: str | None = Field(default=None, max_length=1024)

    @field_validator("data_root", "migration_root")
    @classmethod
    def _paths(cls, value: str | None) -> str | None:
        return normalize_path(value, label="挂载路径") if value else None


class DatasetRequest(StrictApiModel):
    login: str
    link_name: str = Field(default="data2_disk", max_length=32)
    data_root: str | None = Field(default=None, max_length=1024)
    reuse_existing: bool = False
    confirm: bool = False

    @field_validator("login")
    @classmethod
    def _login(cls, value: str) -> str:
        return validate_login(value)

    @field_validator("link_name")
    @classmethod
    def _link_name(cls, value: str) -> str:
        return validate_link_name(value)

    @field_validator("data_root")
    @classmethod
    def _data_root(cls, value: str | None) -> str | None:
        return normalize_path(value, label="数据盘路径") if value else None


class DatasetOut(ApiModel):
    id: str
    server_id: int
    login: str
    uid: int
    gid: int
    data_root: str
    data_path: str
    link_path: str
    status: DatasetStatus
    writable: bool = False
    error: str | None = None
    created_at: ZonedDateTime


class DatasetListOut(ApiModel):
    datasets: list[DatasetOut]
    count: int
