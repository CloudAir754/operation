"""add storage scans

Revision ID: 0002_storage_scans
Revises: 35c740dde4aa
Create Date: 2026-10-30 20:10:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_storage_scans"
down_revision: str | None = "35c740dde4aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "storage_scans",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("mount", sa.String(length=1024), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_storage_scans_server_id", "storage_scans", ["server_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_storage_scans_server_id", table_name="storage_scans")
    op.drop_table("storage_scans")
