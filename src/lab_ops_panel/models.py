"""SQLAlchemy 数据模型。

时间统一以 UTC 存储，读取时总是带有 tzinfo，避免缓存过期判断出现 naive/aware 混用。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lab_ops_panel.db import Base

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_INTERRUPTED = "interrupted"

TASK_TERMINAL_STATUSES = frozenset(
    {
        TASK_STATUS_SUCCEEDED,
        TASK_STATUS_FAILED,
        TASK_STATUS_CANCELLED,
        TASK_STATUS_INTERRUPTED,
    }
)

STEP_STATUS_PENDING = "pending"
STEP_STATUS_RUNNING = "running"
STEP_STATUS_OK = "ok"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_SKIPPED = "skipped"


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite 不保存时区，这里统一按 UTC 往返转换。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Server(Base):
    """主机清单。Host 别名是稳定标识。"""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255))
    user: Mapped[str | None] = mapped_column(String(128))
    port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    identity_file: Mapped[str | None] = mapped_column(String(512))
    proxy_jump: Mapped[str | None] = mapped_column(String(512))
    proxy_command: Mapped[str | None] = mapped_column(String(512))
    connect_timeout: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="ssh_config", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    last_connect_status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    last_connect_error: Mapped[str | None] = mapped_column(Text)
    last_connect_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    secret: Mapped[ServerSecret | None] = relationship(
        back_populates="server", cascade="all, delete-orphan", uselist=False
    )
    snapshots: Mapped[list[ServerSnapshot]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[Task]] = relationship(back_populates="server")


class ServerSecret(Base):
    """DPAPI 加密后的 sudo 密码；数据库里只有密文。"""

    __tablename__ = "server_secrets"

    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cipher_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    server: Mapped[Server] = relationship(back_populates="secret")


class ServerSnapshot(Base):
    """只读采集结果的缓存；过期数据必须标注非实时。"""

    __tablename__ = "server_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    server: Mapped[Server] = relationship(back_populates="snapshots")


class Task(Base):
    """后台维护任务。"""

    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    action_label: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    risk: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=TASK_STATUS_QUEUED, nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    output_tail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    server: Mapped[Server] = relationship(back_populates="tasks")
    steps: Mapped[list[TaskStep]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskStep.seq"
    )


class TaskStep(Base):
    __tablename__ = "task_steps"
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_task_steps_task_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=STEP_STATUS_PENDING, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    output_tail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    command: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    task: Mapped[Task] = relationship(back_populates="steps")


class AuditLog(Base):
    """审计只记录动作、目标、结果摘要，不记录凭据与完整输出。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    operator: Mapped[str] = mapped_column(String(32), default="local-admin", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, index=True, nullable=False
    )
