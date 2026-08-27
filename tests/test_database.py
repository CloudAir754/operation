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
    assert "app_settings" in inspect(engine).get_table_names()
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
