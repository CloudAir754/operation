"""SQLite 初始化与最小持久化模型。"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 声明式模型基类。"""


class AppSetting(Base):
    """保存非敏感应用设置；系统事实不写入此表。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(1000))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class TaskRecord(Base):
    """保存可恢复长任务的状态，不存储明文凭据。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    parameters_hash: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict] = mapped_column(JSON)  # type: ignore[type-arg]
    target: Mapped[str] = mapped_column(String(500))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class TaskStepRecord(Base):
    """保存任务步骤和非敏感结果摘要。"""

    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(30))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ScanSnapshotRecord(Base):
    """保存有有效期的扫描摘要和 treemap 树。"""

    __tablename__ = "scan_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    summary: Mapped[dict] = mapped_column(JSON)  # type: ignore[type-arg]
    tree: Mapped[dict] = mapped_column(JSON)  # type: ignore[type-arg]


class OperationLogRecord(Base):
    """保存有限操作日志，不记录密码、公钥内容或用户文件内容。"""

    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(500))
    result: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


def _migration_config(database_path: Path) -> Config:
    """构造不依赖当前工作目录的 Alembic 配置。"""

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def _run_migrations(database_path: Path) -> None:
    """升级数据库；兼容只有 app_settings 的早期原型数据库。"""

    engine = create_engine(f"sqlite:///{database_path}")
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    config = _migration_config(database_path)
    if "app_settings" in tables and "alembic_version" not in tables:
        command.stamp(config, "0001")
    command.upgrade(config, "head")


def initialize_database(data_dir: Path) -> Path:
    """创建权限仅限启动用户的目录和 SQLite 数据库。"""

    if data_dir.is_symlink():
        raise ValueError("SQLite 数据目录不能是符号链接")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not data_dir.is_dir() or data_dir.is_symlink():
        raise ValueError("SQLite 数据目录不安全")
    os.chmod(data_dir, 0o700)
    database_path = data_dir / "labopspanel.db"
    flags = os.O_RDWR | os.O_NOFOLLOW
    if not database_path.exists():
        flags |= os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(database_path, flags, 0o600)
    except OSError as exc:
        raise ValueError("SQLite 数据库路径不安全或无法打开") from exc
    metadata = os.fstat(descriptor)
    os.close(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ValueError("SQLite 数据库必须是启动用户所有的普通文件")
    _run_migrations(database_path)
    os.chmod(database_path, 0o600)
    return database_path
