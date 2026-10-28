"""应用配置。

所有可变路径与安全边界集中在这里，便于测试通过环境变量或构造参数覆盖。
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "LabOpsPanel"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def default_app_data_dir() -> Path:
    """Windows 用户应用数据目录；非 Windows 退回到 XDG 数据目录。"""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "labops-panel"
    return Path.home() / ".local" / "share" / "labops-panel"


def default_ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


class Settings(BaseSettings):
    """运行配置。

    环境变量前缀为 ``LABOPS_``，例如 ``LABOPS_APP_DATA_DIR``、
    ``LABOPS_SSH_CONFIG_PATH``。默认只绑定回环地址，违反时启动即失败。
    """

    model_config = SettingsConfigDict(env_prefix="LABOPS_", extra="ignore")

    app_data_dir: Path = Field(default_factory=default_app_data_dir)
    ssh_config_path: Path | None = None
    frontend_dist: Path | None = None

    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8765, ge=1, le=65535)
    allow_remote_bind: bool = False

    transport: str = "openssh"
    ssh_binary: str = "ssh"

    snapshot_ttl_seconds: int = Field(default=20, ge=0, le=300)
    max_output_bytes: int = Field(default=256 * 1024, ge=4096, le=8 * 1024 * 1024)
    ssh_connect_timeout: int = Field(default=10, ge=1, le=120)
    command_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    action_timeout_seconds: float = Field(default=90.0, gt=0, le=1800)
    connection_test_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    log_default_lines: int = Field(default=200, ge=1, le=2000)
    log_max_lines: int = Field(default=2000, ge=1, le=10000)

    max_concurrent_tasks: int = Field(default=3, ge=1, le=16)
    allow_arbitrary_commands: bool = False

    audit_limit: int = Field(default=200, ge=1, le=5000)

    @field_validator("bind_host")
    @classmethod
    def _bind_host_must_be_loopback(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("bind_host 不能为空")
        return normalized

    @model_validator(mode="after")
    def _reject_non_loopback_bind(self) -> Settings:
        if self.bind_host.lower() not in LOOPBACK_HOSTS and not self.allow_remote_bind:
            raise ValueError(
                "默认只允许绑定回环地址；如确需绑定其他地址，请显式设置 LABOPS_ALLOW_REMOTE_BIND=true"
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.app_data_dir / "labops.db"

    @property
    def resolved_ssh_config_path(self) -> Path:
        return self.ssh_config_path or default_ssh_config_path()

    @property
    def resolved_frontend_dist(self) -> Path:
        if self.frontend_dist is not None:
            return self.frontend_dist
        return Path(__file__).resolve().parents[2] / "frontend" / "dist"

    def ensure_directories(self) -> None:
        """创建应用数据目录，并保证只对当前用户可见（Windows 依赖用户配置目录 ACL）。"""

        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(self.app_data_dir, 0o700)
