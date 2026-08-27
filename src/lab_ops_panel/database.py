"""SQLite 初始化与最小持久化模型。"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, String, create_engine
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
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    os.chmod(database_path, 0o600)
    engine.dispose()
    return database_path
