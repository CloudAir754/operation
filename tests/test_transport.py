"""SSH 传输层测试：参数构造、超时、取消、输出上限与退出码。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from lab_ops_panel.ssh.transport import (
    CommandResult,
    OpenSshTransport,
    SshTarget,
    TransportError,
    build_ssh_argv,
    expand_identity_file,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="使用 Windows 批处理模拟 ssh 客户端")

MANUAL_TARGET = SshTarget(
    alias="lab-gpu-01",
    hostname="10.0.0.11",
    user="labadmin",
    port=2222,
    identity_file="~/.ssh/id_ed25519",
    proxy_jump="bastion",
    connect_timeout=7,
)


def _bat(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_ssh.bat"
    path.write_text("@echo off\r\n" + body, encoding="ascii", newline="")
    return path


def _target() -> SshTarget:
    return SshTarget(alias="lab-gpu-01", hostname="10.0.0.11", user="labadmin", port=22)


def test_config_alias_argv_relies_on_openssh_config(tmp_path: Path) -> None:
    config = tmp_path / "ssh_config"
    target = SshTarget(alias="lab-gpu-01", connect_timeout=7, use_config_alias=True)

    argv = build_ssh_argv(target, "true", config_path=config)

    assert argv[0] == "ssh"
    assert argv[1] == "-F"
    assert argv[2] == str(config)
    assert "-p" not in argv
    assert argv[-2:] == ["lab-gpu-01", "true"]
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "ConnectTimeout=7" in argv


def test_manual_target_argv_carries_explicit_options() -> None:
    argv = build_ssh_argv(MANUAL_TARGET, "systemctl status nginx", ssh_binary="ssh")

    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-i") + 1] == expand_identity_file("~/.ssh/id_ed25519")
    assert argv[argv.index("-J") + 1] == "bastion"
    assert "labadmin@10.0.0.11" in argv
    assert argv[-1] == "systemctl status nginx"


def test_proxy_command_is_forwarded_as_option() -> None:
    target = SshTarget(
        alias="jump",
        hostname="10.0.0.12",
        proxy_command="ssh -W %h:%p bastion",
        use_config_alias=False,
    )

    argv = build_ssh_argv(target, "true")

    assert "ProxyCommand=ssh -W %h:%p bastion" in argv


def test_connect_timeout_is_at_least_one_second() -> None:
    target = SshTarget(alias="a", hostname="h", connect_timeout=0)

    argv = build_ssh_argv(target, "true")

    assert "ConnectTimeout=1" in argv


def test_successful_command_returns_exit_code(tmp_path: Path) -> None:
    transport = OpenSshTransport(ssh_binary=str(_bat(tmp_path, "echo %*\r\nexit /b 0\r\n")))

    result = transport.run(_target(), "echo hello", timeout=15)

    assert result.exit_code == 0
    assert result.ok is True
    assert "echo hello" in result.stdout
    assert result.timed_out is False
    assert result.cancelled is False


def test_nonzero_exit_code_is_reported(tmp_path: Path) -> None:
    transport = OpenSshTransport(ssh_binary=str(_bat(tmp_path, "echo boom 1>&2\r\nexit /b 7\r\n")))

    result = transport.run(_target(), "false", timeout=15)

    assert result.exit_code == 7
    assert result.ok is False
    assert "boom" in result.stderr


def test_timeout_kills_process(tmp_path: Path) -> None:
    transport = OpenSshTransport(
        ssh_binary=str(_bat(tmp_path, "ping -n 12 127.0.0.1 > nul\r\nexit /b 0\r\n"))
    )
    started = time.monotonic()

    result = transport.run(_target(), "sleep", timeout=0.5)

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.ok is False
    assert time.monotonic() - started < 8


def test_cancel_event_stops_command(tmp_path: Path) -> None:
    transport = OpenSshTransport(
        ssh_binary=str(_bat(tmp_path, "ping -n 12 127.0.0.1 > nul\r\nexit /b 0\r\n"))
    )
    cancel_event = threading.Event()

    def trigger() -> None:
        time.sleep(0.3)
        cancel_event.set()

    thread = threading.Thread(target=trigger)
    thread.start()
    started = time.monotonic()
    result = transport.run(_target(), "sleep", timeout=30, cancel_event=cancel_event)
    thread.join()

    assert result.cancelled is True
    assert result.exit_code is None
    assert time.monotonic() - started < 8


def test_output_is_truncated_at_limit(tmp_path: Path) -> None:
    body = "for /l %%i in (1,1,2000) do @echo 0123456789012345678901234567890123456789\r\nexit /b 0\r\n"
    transport = OpenSshTransport(ssh_binary=str(_bat(tmp_path, body)))

    result = transport.run(_target(), "big", timeout=30, max_output_bytes=500)

    assert result.truncated is True
    assert len(result.stdout.encode()) <= 500


def test_stdin_is_forwarded_for_sudo_password(tmp_path: Path) -> None:
    transport = OpenSshTransport(ssh_binary=str(_bat(tmp_path, "more\r\n")))

    result = transport.run(_target(), "sudo -S true", timeout=15, stdin_data=b"secret\n")

    assert result.exit_code == 0
    assert "secret" in result.stdout


def test_missing_ssh_binary_raises_transport_error(tmp_path: Path) -> None:
    transport = OpenSshTransport(ssh_binary=str(tmp_path / "does-not-exist.exe"))

    with pytest.raises(TransportError, match="找不到 SSH 客户端"):
        transport.run(_target(), "true", timeout=5)


def test_command_result_combined_output() -> None:
    result = CommandResult(
        argv=[],
        command="x",
        exit_code=1,
        stdout="out",
        stderr="err",
        duration_ms=1,
    )

    assert result.combined_output == "out\nerr"
    assert result.ok is False


def test_command_result_ok_requires_zero_exit() -> None:
    assert (
        CommandResult(argv=[], command="x", exit_code=0, stdout="", stderr="", duration_ms=0).ok
        is True
    )
    assert (
        CommandResult(
            argv=[],
            command="x",
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=0,
            timed_out=True,
        ).ok
        is False
    )
