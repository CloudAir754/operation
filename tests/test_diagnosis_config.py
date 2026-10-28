"""失败诊断与配置边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lab_ops_panel.config import Settings
from lab_ops_panel.remote.diagnosis import diagnose_result
from lab_ops_panel.ssh.transport import CommandResult


def _result(**overrides) -> CommandResult:
    defaults = {
        "argv": [],
        "command": "x",
        "exit_code": 1,
        "stdout": "",
        "stderr": "",
        "duration_ms": 1,
    }
    defaults.update(overrides)
    return CommandResult(**defaults)


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Permission denied (publickey).", "认证失败"),
        ("Host key verification failed.", "主机密钥校验失败"),
        ("ssh: Could not resolve hostname lab-01", "无法解析主机名"),
        ("ssh: connect to host 10.0.0.1 port 22: Connection refused", "连接被拒绝"),
        ("ssh: connect to host 10.0.0.1 port 22: Connection timed out", "连接超时"),
        ("ssh: connect to host 10.0.0.1 port 22: No route to host", "主机不可达"),
        ("sudo: a password is required", "sudo 需要密码"),
        ("sudo: 1 incorrect password attempt", "sudo 密码错误"),
        ("bash: journalctl: command not found", "远端缺少所需命令"),
        ("System has not been booted with systemd as init system", "未使用 systemd"),
    ],
)
def test_known_failures_are_translated(stderr: str, expected: str) -> None:
    assert expected in diagnose_result(_result(stderr=stderr))


def test_timeout_and_cancel_have_priority() -> None:
    assert "超时" in diagnose_result(_result(exit_code=None, timed_out=True))
    assert "取消" in diagnose_result(_result(exit_code=None, cancelled=True))


def test_successful_result_has_empty_diagnosis() -> None:
    assert diagnose_result(_result(exit_code=0)) == ""


def test_unknown_failure_keeps_exit_code() -> None:
    message = diagnose_result(_result(exit_code=3, stderr="weird failure"))

    assert "退出码 3" in message
    assert "weird failure" in message


def test_failure_without_output_reports_exit_code_only() -> None:
    assert "无输出" in diagnose_result(_result(exit_code=5))


def test_long_output_is_truncated() -> None:
    message = diagnose_result(_result(exit_code=1, stderr="x" * 2000))

    assert len(message) < 500


def test_non_loopback_bind_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(app_data_dir=tmp_path / "data", bind_host="0.0.0.0")


def test_remote_bind_allowed_when_explicit(tmp_path: Path) -> None:
    settings = Settings(
        app_data_dir=tmp_path / "data",
        bind_host="0.0.0.0",
        allow_remote_bind=True,
    )

    assert settings.bind_host == "0.0.0.0"


def test_empty_bind_host_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(app_data_dir=tmp_path / "data", bind_host="   ")


def test_snapshot_ttl_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(app_data_dir=tmp_path / "data", snapshot_ttl_seconds=1000)


def test_db_path_lives_in_app_data_dir(tmp_path: Path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data")

    assert settings.db_path == tmp_path / "data" / "labops.db"


def test_ensure_directories_creates_app_data_dir(tmp_path: Path) -> None:
    settings = Settings(app_data_dir=tmp_path / "nested" / "data")

    settings.ensure_directories()

    assert settings.app_data_dir.is_dir()


def test_env_overrides_are_applied(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LABOPS_BIND_PORT", "9911")
    monkeypatch.setenv("LABOPS_SNAPSHOT_TTL_SECONDS", "42")
    monkeypatch.setenv("LABOPS_APP_DATA_DIR", str(tmp_path / "env-data"))

    settings = Settings()

    assert settings.bind_port == 9911
    assert settings.snapshot_ttl_seconds == 42
    assert settings.app_data_dir == tmp_path / "env-data"


def test_ssh_config_path_defaults_to_user_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("lab_ops_panel.config.default_ssh_config_path", lambda: tmp_path / "cfg")

    settings = Settings(app_data_dir=tmp_path / "data")

    assert settings.resolved_ssh_config_path == tmp_path / "cfg"
