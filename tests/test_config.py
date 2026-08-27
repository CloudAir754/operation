"""运行配置安全约束测试。"""

from pathlib import Path

import pytest

from lab_ops_panel.config import Settings, ensure_unprivileged


def test_default_host_is_loopback(tmp_path: Path) -> None:
    """默认配置只监听 IPv4 回环地址。"""

    settings = Settings(data_dir=tmp_path)
    settings.validate()
    assert settings.host == "127.0.0.1"
    assert len(settings.session_token) >= 32


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "example.test", "localhost"])
def test_external_host_is_rejected(host: str, tmp_path: Path) -> None:
    """首版没有认证和 HTTPS，必须拒绝外部监听。"""

    with pytest.raises(ValueError, match="回环地址"):
        Settings(host=host, data_dir=tmp_path).validate()


@pytest.mark.parametrize("port", [-1, 65536])
def test_invalid_port_is_rejected(port: int, tmp_path: Path) -> None:
    """拒绝操作系统端口范围以外的边界值。"""

    with pytest.raises(ValueError, match="端口"):
        Settings(port=port, data_dir=tmp_path).validate()


def test_root_process_is_rejected() -> None:
    """整个 Web 服务不能以 root 身份运行。"""

    with pytest.raises(PermissionError, match="root"):
        ensure_unprivileged(0)


def test_regular_user_process_is_accepted() -> None:
    """普通 UID 满足最小权限运行前提。"""

    ensure_unprivileged(1000)
