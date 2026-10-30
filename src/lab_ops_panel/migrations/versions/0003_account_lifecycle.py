"""add account lifecycle tables

Revision ID: 0003_account_lifecycle
Revises: 0002_storage_scans
Create Date: 2026-10-30 22:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_account_lifecycle"
down_revision: str | None = "0002_storage_scans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("servers") as batch:
        batch.add_column(sa.Column("data_root", sa.String(length=1024), nullable=True))
        batch.add_column(sa.Column("migration_root", sa.String(length=1024), nullable=True))

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "password_policy_changes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("diff", sa.Text(), nullable=False),
        sa.Column("backup_id", sa.String(length=64), nullable=False),
        sa.Column("conf_before", sa.Text(), nullable=False),
        sa.Column("conf_after", sa.Text(), nullable=False),
        sa.Column("password_before", sa.Text(), nullable=True),
        sa.Column("password_after", sa.Text(), nullable=True),
        sa.Column("auth_before", sa.Text(), nullable=True),
        sa.Column("auth_after", sa.Text(), nullable=True),
        sa.Column("validation", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("restored_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_password_policy_changes_server_id",
        "password_policy_changes",
        ["server_id"],
        unique=False,
    )

    op.create_table(
        "account_batches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_account_batches_server_id", "account_batches", ["server_id"], unique=False)

    op.create_table(
        "home_migrations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(length=64), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=False),
        sa.Column("gid", sa.Integer(), nullable=False),
        sa.Column("source_home", sa.String(length=1024), nullable=False),
        sa.Column("target_root", sa.String(length=1024), nullable=False),
        sa.Column("target_path", sa.String(length=1024), nullable=False),
        sa.Column("staging_path", sa.String(length=1024), nullable=False),
        sa.Column("backup_path", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("preflight", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("account_status_before", sa.String(length=16), nullable=True),
        sa.Column("backup_retention_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_home_migrations_server_id", "home_migrations", ["server_id"], unique=False)

    op.create_table(
        "dataset_allocations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(length=64), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=False),
        sa.Column("gid", sa.Integer(), nullable=False),
        sa.Column("data_root", sa.String(length=1024), nullable=False),
        sa.Column("data_path", sa.String(length=1024), nullable=False),
        sa.Column("link_path", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dataset_allocations_server_id", "dataset_allocations", ["server_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_dataset_allocations_server_id", table_name="dataset_allocations")
    op.drop_table("dataset_allocations")
    op.drop_index("ix_home_migrations_server_id", table_name="home_migrations")
    op.drop_table("home_migrations")
    op.drop_index("ix_account_batches_server_id", table_name="account_batches")
    op.drop_table("account_batches")
    op.drop_index("ix_password_policy_changes_server_id", table_name="password_policy_changes")
    op.drop_table("password_policy_changes")
    op.drop_table("app_settings")
    with op.batch_alter_table("servers") as batch:
        batch.drop_column("migration_root")
        batch.drop_column("data_root")
