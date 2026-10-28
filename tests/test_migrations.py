"""Alembic 迁移测试：建表、幂等升级与回滚。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from lab_ops_panel.migrations_runner import build_alembic_config, upgrade_database

CORE_TABLES = {
    "servers",
    "server_secrets",
    "server_snapshots",
    "tasks",
    "task_steps",
    "audit_logs",
}


@contextmanager
def engine_for(db_path: Path) -> Iterator:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_creates_core_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "labops.db"

    upgrade_database(db_path)

    with engine_for(db_path) as engine:
        tables = set(inspect(engine).get_table_names())
        assert tables >= CORE_TABLES
        assert "alembic_version" in tables


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "labops.db"

    upgrade_database(db_path)
    upgrade_database(db_path)

    with engine_for(db_path) as engine, engine.connect() as connection:
        versions = connection.execute(text("SELECT version_num FROM alembic_version")).all()
    assert len(versions) == 1


def test_downgrade_removes_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "labops.db"
    upgrade_database(db_path)

    command.downgrade(build_alembic_config(db_path), "base")

    with engine_for(db_path) as engine:
        tables = set(inspect(engine).get_table_names())
    assert not (CORE_TABLES & tables)


def test_foreign_keys_are_declared(tmp_path: Path) -> None:
    db_path = tmp_path / "labops.db"
    upgrade_database(db_path)

    with engine_for(db_path) as engine:
        inspector = inspect(engine)
        task_steps_fks = inspector.get_foreign_keys("task_steps")
        secrets_fks = inspector.get_foreign_keys("server_secrets")

    assert any(fk["referred_table"] == "tasks" for fk in task_steps_fks)
    assert any(fk["referred_table"] == "servers" for fk in secrets_fks)


def test_unique_indexes_are_created(tmp_path: Path) -> None:
    db_path = tmp_path / "labops.db"
    upgrade_database(db_path)

    with engine_for(db_path) as engine:
        inspector = inspect(engine)
        server_indexes = {index["name"]: index for index in inspector.get_indexes("servers")}
        task_uniques = inspector.get_unique_constraints("tasks")

    assert bool(server_indexes["ix_servers_alias"]["unique"]) is True
    assert any(unique["name"] == "uq_tasks_idempotency_key" for unique in task_uniques)
