"""创建任务、步骤、扫描快照和有限操作日志。"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建可恢复任务所需的持久化表。"""

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_tasks_kind", "tasks", ["kind"])
    op.create_index("ix_tasks_state", "tasks", ["state"])
    op.create_table(
        "task_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_steps_task_id", "task_steps", ["task_id"])
    op.create_table(
        "scan_snapshots",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("tree", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scan_snapshots_task_id", "scan_snapshots", ["task_id"])
    op.create_index("ix_scan_snapshots_expires_at", "scan_snapshots", ["expires_at"])
    op.create_table(
        "operation_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column("result", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operation_logs_task_id", "operation_logs", ["task_id"])
    op.create_index("ix_operation_logs_created_at", "operation_logs", ["created_at"])


def downgrade() -> None:
    """按外键依赖逆序删除持久化表。"""

    op.drop_index("ix_operation_logs_created_at", table_name="operation_logs")
    op.drop_index("ix_operation_logs_task_id", table_name="operation_logs")
    op.drop_table("operation_logs")
    op.drop_index("ix_scan_snapshots_expires_at", table_name="scan_snapshots")
    op.drop_index("ix_scan_snapshots_task_id", table_name="scan_snapshots")
    op.drop_table("scan_snapshots")
    op.drop_index("ix_task_steps_task_id", table_name="task_steps")
    op.drop_table("task_steps")
    op.drop_index("ix_tasks_state", table_name="tasks")
    op.drop_index("ix_tasks_kind", table_name="tasks")
    op.drop_table("tasks")
