"""端口占用查询与基于 PID 启动时间、UID 的受控进程终止。"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Any

import psutil


def minimum_regular_uid(path: Path = Path("/etc/login.defs")) -> int:
    """读取系统普通用户 UID 下限；无法识别时采用 Ubuntu 常见默认值。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 1000
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        fields = line.split()
        if len(fields) == 2 and fields[0] == "UID_MIN":
            try:
                value = int(fields[1])
            except ValueError:
                return 1000
            return value if value > 0 else 1000
    return 1000


def _address(value: Any) -> tuple[str | None, int | None]:
    """统一 psutil 的 IPv4/IPv6 地址元组。"""

    if not value:
        return None, None
    return str(value.ip), int(value.port)


def process_identity(pid: int) -> dict[str, Any]:
    """按 PID 返回 UID 与启动时间，供提交前防 PID 复用校验。"""

    if pid <= 1:
        raise ValueError("不能操作 PID 1 或无效 PID")
    try:
        process = psutil.Process(pid)
        with process.oneshot():
            uid = process.uids().real
            create_time = process.create_time()
            username = process.username()
            command = " ".join(process.cmdline()) or process.name()
        return {
            "pid": pid,
            "uid": uid,
            "username": username,
            "create_time": create_time,
            "command": command,
        }
    except psutil.NoSuchProcess as exc:
        raise ValueError("进程已退出") from exc
    except psutil.AccessDenied as exc:
        raise ValueError("无权读取进程身份") from exc


def port_usage(port: int, protocol: str = "any") -> dict[str, Any]:
    """查询 TCP/UDP 本地端口占用；非 root 结果明确标记可能不完整。"""

    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    if protocol not in {"tcp", "udp", "any"}:
        raise ValueError("协议只允许 tcp、udp 或 any")
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError) as exc:
        raise RuntimeError("无法读取本机网络连接") from exc
    rows = []
    for connection in connections:
        actual_protocol = (
            "tcp"
            if connection.type == socket.SOCK_STREAM
            else "udp"
            if connection.type == socket.SOCK_DGRAM
            else "other"
        )
        local_address, local_port = _address(connection.laddr)
        if local_port != port or (protocol != "any" and actual_protocol != protocol):
            continue
        remote_address, remote_port = _address(connection.raddr)
        identity = None
        if connection.pid is not None:
            try:
                identity = process_identity(connection.pid)
            except ValueError:
                identity = None
        rows.append(
            {
                "protocol": actual_protocol,
                "family": "IPv6" if connection.family == socket.AF_INET6 else "IPv4",
                "local_address": local_address,
                "local_port": local_port,
                "remote_address": remote_address,
                "remote_port": remote_port,
                "status": connection.status or None,
                "pid": connection.pid,
                "uid": identity["uid"] if identity else None,
                "username": identity["username"] if identity else None,
                "create_time": identity["create_time"] if identity else None,
                "command": identity["command"] if identity else None,
            }
        )
    rows.sort(key=lambda row: (row["protocol"], row["pid"] or -1, row["local_address"] or ""))
    complete = os.geteuid() == 0
    return {
        "port": port,
        "protocol": protocol,
        "complete": complete,
        "reason": None if complete else "普通用户权限下其他用户的 PID 或连接可能不可见",
        "connections": rows,
    }


def _verified_process(pid: int, expected_uid: int, expected_create_time: float) -> psutil.Process:
    """同时核对 PID、UID 与启动时间，阻止 PID 复用后误杀。"""

    if pid <= 1 or expected_uid <= 0:
        raise ValueError("拒绝终止 root、PID 1 或无效目标")
    try:
        process = psutil.Process(pid)
        actual_uid = process.uids().real
        actual_create_time = process.create_time()
    except psutil.NoSuchProcess as exc:
        raise ValueError("目标进程已退出") from exc
    if actual_uid != expected_uid or abs(actual_create_time - expected_create_time) > 0.01:
        raise ValueError("进程 PID、UID 或启动时间已变化，请重新查询")
    return process


def requester_context(requester_pid: int) -> tuple[psutil.Process, set[int]]:
    """确认 Web 调用进程是助手祖先，并保护面板及其全部子进程。"""

    try:
        current = psutil.Process(os.getpid())
        ancestors = {process.pid for process in current.parents()}
        if requester_pid not in ancestors:
            raise ValueError("调用进程身份无法验证")
        requester = psutil.Process(requester_pid)
        protected = {requester.pid}
        protected.update(process.pid for process in requester.children(recursive=True))
        protected.add(os.getpid())
        return requester, protected
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        raise ValueError("无法验证调用进程树") from exc


def terminate_process(
    pid: int,
    expected_uid: int,
    expected_create_time: float,
    requester_pid: int,
    grace_seconds: float = 5,
    require_root: bool = True,
) -> dict[str, Any]:
    """先 TERM 再按需 KILL 单进程，并验证目标已消失。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("进程终止只能由受限特权助手执行")
    if not 1 <= grace_seconds <= 30:
        raise ValueError("TERM 等待时间必须在 1 到 30 秒之间")
    _, protected = requester_context(requester_pid)
    if pid in protected:
        raise ValueError("拒绝终止 LabOpsPanel 进程树")
    process = _verified_process(pid, expected_uid, expected_create_time)
    process.terminate()
    _, alive = psutil.wait_procs([process], timeout=grace_seconds)
    escalated = False
    if alive:
        process = _verified_process(pid, expected_uid, expected_create_time)
        process.kill()
        _, alive = psutil.wait_procs([process], timeout=5)
        escalated = True
    if alive:
        raise RuntimeError("发送 KILL 后目标进程仍存在")
    return {"terminated_pids": [pid], "escalated_pids": [pid] if escalated else []}


def _uid_processes(uid: int, protected: set[int]) -> list[psutil.Process]:
    """以实时 UID 扫描目标进程，忽略已退出项。"""

    result = []
    for process in psutil.process_iter(["pid", "uids"]):
        try:
            if process.pid not in protected and process.info["uids"].real == uid:
                result.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    return result


def terminate_uid_processes(
    uid: int,
    requester_pid: int,
    grace_seconds: float = 5,
    require_root: bool = True,
) -> dict[str, Any]:
    """按稳定 UID 终止全部进程，重复扫描以发现短暂重生进程。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("用户进程终止只能由受限特权助手执行")
    if uid < minimum_regular_uid() or not 1 <= grace_seconds <= 30:
        raise ValueError("只允许终止普通用户的全部进程")
    requester, protected = requester_context(requester_pid)
    if requester.uids().real == uid:
        raise ValueError("拒绝终止启动 LabOpsPanel 用户的全部进程")
    terminated: set[int] = set()
    escalated: set[int] = set()
    for _ in range(3):
        targets = _uid_processes(uid, protected)
        if not targets:
            return {
                "terminated_pids": sorted(terminated),
                "escalated_pids": sorted(escalated),
                "remaining_pids": [],
            }
        identities = {process.pid: process.create_time() for process in targets}
        for process in targets:
            process.terminate()
            terminated.add(process.pid)
        _, alive = psutil.wait_procs(targets, timeout=grace_seconds)
        for process in alive:
            try:
                verified = _verified_process(process.pid, uid, identities[process.pid])
                verified.kill()
                escalated.add(process.pid)
            except ValueError:
                continue
        if alive:
            psutil.wait_procs(alive, timeout=5)
        time.sleep(0.05)
    remaining = [process.pid for process in _uid_processes(uid, protected)]
    if remaining:
        raise RuntimeError(f"用户 UID {uid} 仍有进程持续重生")
    return {
        "terminated_pids": sorted(terminated),
        "escalated_pids": sorted(escalated),
        "remaining_pids": [],
    }
