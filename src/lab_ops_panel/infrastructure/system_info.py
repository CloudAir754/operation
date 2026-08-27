"""从本机只读接口采集系统、账号、挂载与资源信息。"""

from __future__ import annotations

import grp
import os
import platform
import pwd
import socket
from pathlib import Path
from typing import Any

import psutil


def _read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    """解析 os-release，忽略格式异常的扩展行。"""

    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value.strip().strip('"')
    return result


def system_snapshot() -> dict[str, Any]:
    """采集不会触发外部网络访问的本机基本信息。"""

    os_release = _read_os_release()
    addresses = sorted(
        {
            address.address
            for values in psutil.net_if_addrs().values()
            for address in values
            if address.family in {socket.AF_INET, socket.AF_INET6}
        }
    )
    return {
        "hostname": socket.gethostname(),
        "operating_system": os_release.get("PRETTY_NAME", platform.platform()),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "addresses": addresses,
        "capabilities": {
            "proc": Path("/proc").is_dir(),
            "sys": Path("/sys").is_dir(),
            "systemd": Path("/run/systemd/system").is_dir(),
        },
    }


def resource_snapshot() -> dict[str, Any]:
    """读取 CPU 和内存当前值，不在数据库中持久化高频样本。"""

    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "cpu": {
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "load_average": list(os.getloadavg()),
            "total_percent": psutil.cpu_percent(interval=None),
            "per_core_percent": psutil.cpu_percent(interval=None, percpu=True),
        },
        "memory": {
            "total": memory.total,
            "available": memory.available,
            "used": memory.used,
            "cached": getattr(memory, "cached", None),
            "percent": memory.percent,
            "swap_total": swap.total,
            "swap_used": swap.used,
        },
    }


def mount_points() -> list[dict[str, Any]]:
    """列出当前挂载点；设备名仅用于展示，不作为破坏性身份。"""

    result = []
    for item in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(item.mountpoint)
        except OSError:
            usage = None
        result.append(
            {
                "device": item.device,
                "mountpoint": item.mountpoint,
                "filesystem": item.fstype,
                "options": item.opts.split(","),
                "total": usage.total if usage else None,
                "used": usage.used if usage else None,
                "free": usage.free if usage else None,
                "percent": usage.percent if usage else None,
                "read_only": "ro" in item.opts.split(","),
            }
        )
    return result


def _sudo_group_members() -> set[str]:
    """读取 sudo 组成员；不存在该组时安全返回空集合。"""

    try:
        sudo_group = grp.getgrnam("sudo")
    except KeyError:
        return set()
    members = set(sudo_group.gr_mem)
    members.update(entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == sudo_group.gr_gid)
    return members


def local_users() -> list[dict[str, Any]]:
    """从本机 passwd 文件读取账号并基于 UID/GID 返回稳定关联信息。"""

    sudo_members = _sudo_group_members()
    users = []
    try:
        passwd_lines = Path("/etc/passwd").read_text(encoding="utf-8").splitlines()
    except OSError:
        return users
    for line in passwd_lines:
        fields = line.split(":")
        if len(fields) != 7:
            continue
        username, _, uid, gid, gecos, home, shell = fields
        try:
            numeric_uid, numeric_gid = int(uid), int(gid)
        except ValueError:
            continue
        users.append(
            {
                "username": username,
                "uid": numeric_uid,
                "gid": numeric_gid,
                "display_name": gecos.split(",", 1)[0],
                "home": home,
                "resolved_home": str(Path(home).resolve(strict=False)),
                "shell": shell,
                "sudo_sources": ["sudo 组"] if username in sudo_members else [],
            }
        )
    return sorted(users, key=lambda item: (item["uid"], item["username"]))
