"""SQLite 权限与初始化测试。"""

import stat
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from lab_ops_panel.database import initialize_database


def test_database_has_private_permissions(tmp_path: Path) -> None:
    """数据库及其父目录只能由启动用户访问。"""

    data_dir = tmp_path / "data"
    database_path = initialize_database(data_dir)

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    engine = create_engine(f"sqlite:///{database_path}")
    assert set(inspect(engine).get_table_names()) >= {
        "alembic_version",
        "app_settings",
        "tasks",
        "task_steps",
        "scan_snapshots",
        "operation_logs",
    }
    engine.dispose()


def test_database_initialization_is_idempotent(tmp_path: Path) -> None:
    """重复启动不会重复建表或破坏已有数据库。"""

    first = initialize_database(tmp_path)
    second = initialize_database(tmp_path)
    assert first == second


def test_database_rejects_symlink_file(tmp_path: Path) -> None:
    """数据库路径不能通过符号链接逃逸到其他文件。"""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = tmp_path / "target"
    target.write_text("do not overwrite", encoding="utf-8")
    (data_dir / "labopspanel.db").symlink_to(target)

    with pytest.raises(ValueError, match="不安全"):
        initialize_database(data_dir)
    assert target.read_text(encoding="utf-8") == "do not overwrite"


def test_database_upgrades_legacy_app_settings_database(tmp_path: Path) -> None:
    """早期原型数据库应保留设置表并升级到最新任务模型。"""

    database_path = tmp_path / "labopspanel.db"
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE app_settings (key VARCHAR(100) PRIMARY KEY, "
            "value VARCHAR(1000) NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES ('retention', '7', '2026-08-27 00:00:00')"
        )
    engine.dispose()

    initialize_database(tmp_path)

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT value FROM app_settings WHERE key='retention'"
            ).scalar_one()
            == "7"
        )
        version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        assert version == "0003"
    assert "tasks" in inspect(engine).get_table_names()
    engine.dispose()
