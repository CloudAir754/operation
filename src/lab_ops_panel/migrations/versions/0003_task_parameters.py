"""保存已验证且不含凭据的任务恢复参数。"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加用于中断恢复的非敏感结构化参数。"""

    op.add_column(
        "tasks",
        sa.Column("parameters", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    """删除任务恢复参数。"""

    op.drop_column("tasks", "parameters")
