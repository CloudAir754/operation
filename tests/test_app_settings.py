"""本机应用设置白名单与持久化测试。"""

from pathlib import Path

import pytest

from lab_ops_panel.database import initialize_database
from lab_ops_panel.infrastructure.app_settings import AppSettingsRepository


def test_app_settings_use_defaults_and_persist_updates(tmp_path: Path) -> None:
    """首次使用返回安全默认值，更新后由新仓库实例读取。"""

    database_path = initialize_database(tmp_path)
    repository = AppSettingsRepository(database_path)

    assert repository.get_all() == {
        "scan_throttle_milliseconds": 1,
        "scan_snapshot_ttl_seconds": 900,
    }
    repository.update({"scan_throttle_milliseconds": 4, "scan_snapshot_ttl_seconds": 1800})

    assert AppSettingsRepository(database_path).get_all() == {
        "scan_throttle_milliseconds": 4,
        "scan_snapshot_ttl_seconds": 1800,
    }


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"unknown": 1}, "未知"),
        ({"scan_throttle_milliseconds": 101}, "0 到 100"),
        ({"scan_snapshot_ttl_seconds": 59}, "60 到 86400"),
    ],
)
def test_app_settings_reject_unknown_or_out_of_range_values(
    tmp_path: Path, values: dict[str, int], message: str
) -> None:
    """设置接口不能成为任意键值存储，也不能关闭资源边界。"""

    repository = AppSettingsRepository(initialize_database(tmp_path))

    with pytest.raises(ValueError, match=message):
        repository.update(values)
