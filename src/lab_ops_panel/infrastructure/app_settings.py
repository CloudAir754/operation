"""本机非敏感应用设置的受限持久化访问。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lab_ops_panel.database import AppSetting

DEFAULT_APP_SETTINGS = {
    "scan_throttle_milliseconds": 1,
    "scan_snapshot_ttl_seconds": 900,
}
SETTING_RANGES = {
    "scan_throttle_milliseconds": (0, 100),
    "scan_snapshot_ttl_seconds": (60, 86_400),
}


class AppSettingsRepository:
    """只允许读写显式白名单中的整数设置。"""

    def __init__(self, database_path: Path) -> None:
        self.engine = create_engine(f"sqlite:///{database_path}")

    def get_all(self) -> dict[str, int]:
        """返回经范围校验的有效值，损坏记录安全回退到默认值。"""

        values = dict(DEFAULT_APP_SETTINGS)
        with Session(self.engine) as session:
            records = session.scalars(
                select(AppSetting).where(AppSetting.key.in_(DEFAULT_APP_SETTINGS))
            )
            for record in records:
                try:
                    value = int(record.value)
                    self._validate(record.key, value)
                except (TypeError, ValueError):
                    continue
                values[record.key] = value
        return values

    def update(self, values: dict[str, int]) -> dict[str, int]:
        """原子更新已知设置，拒绝未知键和越界值。"""

        if not values:
            raise ValueError("至少提供一项设置")
        unknown = set(values) - set(DEFAULT_APP_SETTINGS)
        if unknown:
            raise ValueError(f"未知应用设置：{sorted(unknown)[0]}")
        for key, value in values.items():
            self._validate(key, value)
        with Session(self.engine) as session:
            now = datetime.now(UTC)
            for key, value in values.items():
                record = session.get(AppSetting, key)
                if record is None:
                    session.add(AppSetting(key=key, value=str(value), updated_at=now))
                else:
                    record.value = str(value)
                    record.updated_at = now
            session.commit()
        return self.get_all()

    @staticmethod
    def _validate(key: str, value: int) -> None:
        """验证单个设置的保守范围。"""

        minimum, maximum = SETTING_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
