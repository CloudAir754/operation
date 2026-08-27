"""应用运行配置与本地安全约束。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

LOOPBACK_HOSTS = frozenset({"127.0.0.1"})


def default_data_dir() -> Path:
    """返回用户级数据目录，不依赖当前工作目录。"""

    return Path.home() / ".local" / "share" / "labopspanel"


@dataclass(slots=True)
class Settings:
    """描述单次启动的配置；会话令牌只存在于内存中。"""

    host: str = "127.0.0.1"
    port: int = 0
    data_dir: Path = field(default_factory=default_data_dir)
    session_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def validate(self) -> None:
        """拒绝对外监听以及无效端口，保留本地应用安全边界。"""

        if self.host not in LOOPBACK_HOSTS:
            raise ValueError("LabOpsPanel 当前仅允许监听回环地址")
        if not 0 <= self.port <= 65535:
            raise ValueError("端口必须在 0 到 65535 之间")
