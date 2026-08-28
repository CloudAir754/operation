"""端口查询与受控进程终止测试。"""

import os
import socket
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from lab_ops_panel.infrastructure import processes


def _sleeping_process() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _cleanup(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def test_port_usage_identifies_tcp_process_and_user() -> None:
    """监听端口返回当前进程的稳定 PID、UID、用户名和启动时间。"""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        result = processes.port_usage(port, "tcp")
    finally:
        listener.close()

    row = next(item for item in result["connections"] if item["pid"] == os.getpid())
    assert row["uid"] == os.getuid()
    assert row["username"]
    assert row["create_time"] > 0
    assert row["protocol"] == "tcp"


def test_port_usage_rejects_invalid_port_and_protocol() -> None:
    """端口和协议必须来自严格范围。"""

    with pytest.raises(ValueError):
        processes.port_usage(0)
    with pytest.raises(ValueError):
        processes.port_usage(8080, "sctp")


def test_terminate_process_checks_identity_then_uses_term(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """单进程正常响应 TERM 时无需升级 KILL。"""

    child = _sleeping_process()
    try:
        identity = processes.process_identity(child.pid)
        monkeypatch.setattr(
            processes,
            "_requester_tree",
            lambda requester_pid: (psutil.Process(os.getpid()), {os.getpid()}),
        )
        result = processes.terminate_process(
            child.pid,
            identity["uid"],
            identity["create_time"],
            os.getpid(),
            grace_seconds=1,
            require_root=False,
        )
        child.wait(timeout=5)
    finally:
        _cleanup(child)

    assert result == {"terminated_pids": [child.pid], "escalated_pids": []}


def test_terminate_process_rejects_pid_reuse_and_panel_tree(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """启动时间变化或目标属于面板进程树时都不能发送信号。"""

    child = _sleeping_process()
    try:
        identity = processes.process_identity(child.pid)
        monkeypatch.setattr(
            processes,
            "_requester_tree",
            lambda requester_pid: (psutil.Process(os.getpid()), {os.getpid()}),
        )
        with pytest.raises(ValueError, match="已变化"):
            processes.terminate_process(
                child.pid,
                identity["uid"],
                identity["create_time"] + 10,
                os.getpid(),
                require_root=False,
            )
        monkeypatch.setattr(
            processes,
            "_requester_tree",
            lambda requester_pid: (psutil.Process(os.getpid()), {os.getpid(), child.pid}),
        )
        with pytest.raises(ValueError, match="LabOpsPanel"):
            processes.terminate_process(
                child.pid,
                identity["uid"],
                identity["create_time"],
                os.getpid(),
                require_root=False,
            )
        assert child.poll() is None
    finally:
        _cleanup(child)


def test_terminate_uid_processes_repeats_scan_until_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """按 UID 结束会处理全部目标并重扫确认没有残留。"""

    children = [_sleeping_process(), _sleeping_process()]

    class Requester:
        def uids(self):  # type: ignore[no-untyped-def]
            return type("Uids", (), {"real": os.getuid() + 1})()

    def targets(uid, protected):  # type: ignore[no-untyped-def]
        return [
            psutil.Process(child.pid)
            for child in children
            if child.poll() is None and child.pid not in protected
        ]

    try:
        monkeypatch.setattr(
            processes, "_requester_tree", lambda requester_pid: (Requester(), {os.getpid()})
        )
        monkeypatch.setattr(processes, "_uid_processes", targets)
        monkeypatch.setattr(processes, "minimum_regular_uid", lambda: 1)
        result = processes.terminate_uid_processes(
            os.getuid(), os.getpid(), grace_seconds=1, require_root=False
        )
    finally:
        for child in children:
            _cleanup(child)

    assert result["terminated_pids"] == sorted(child.pid for child in children)
    assert result["remaining_pids"] == []


def test_minimum_regular_uid_uses_login_defs_or_safe_default(tmp_path: Path) -> None:
    """普通用户边界来自显式系统配置，异常配置回退到 1000。"""

    config = tmp_path / "login.defs"
    config.write_text("UID_MIN 2000\n", encoding="utf-8")
    assert processes.minimum_regular_uid(config) == 2000
    config.write_text("UID_MIN invalid\n", encoding="utf-8")
    assert processes.minimum_regular_uid(config) == 1000
