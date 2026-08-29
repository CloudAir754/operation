"""从本机只读接口采集系统、账号、挂载与资源信息。"""

from __future__ import annotations

import grp
import os
import platform
import pwd
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import psutil

from lab_ops_panel.infrastructure.sudoers import scan_sudoers_rules


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
    required_commands = (
        "chage",
        "findmnt",
        "getfacl",
        "getent",
        "lsblk",
        "passwd",
        "rsync",
        "ssh-keygen",
        "sudo",
        "useradd",
        "userdel",
        "visudo",
    )
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
        "supported_architecture": platform.machine().lower() in {"amd64", "x86_64"},
        "addresses": addresses,
        "capabilities": {
            "proc": Path("/proc").is_dir(),
            "sys": Path("/sys").is_dir(),
            "systemd": Path("/run/systemd/system").is_dir(),
            "commands": {
                command: shutil.which(command) is not None for command in required_commands
            },
            "directory_account_sources": _directory_account_sources(),
        },
    }


def _directory_account_sources(path: Path = Path("/etc/nsswitch.conf")) -> list[str]:
    """从 passwd 的 NSS 配置识别 LDAP、NIS、SSSD 等目录来源。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line.startswith("passwd:"):
            continue
        sources = [value for value in line.split(":", 1)[1].split() if not value.startswith("[")]
        return [value for value in sources if value not in {"files", "systemd"}]
    return []


def mount_points() -> list[dict[str, Any]]:
    """列出当前挂载点；设备名仅用于展示，不作为破坏性身份。"""

    result = []
    for item in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(item.mountpoint)
            filesystem_stats = os.statvfs(item.mountpoint)
        except OSError:
            usage = None
            filesystem_stats = None
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
                "inodes_total": filesystem_stats.f_files if filesystem_stats else None,
                "inodes_free": filesystem_stats.f_ffree if filesystem_stats else None,
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


def _home_storage(path: Path) -> dict[str, Any] | None:
    """按最长挂载点匹配 home 的当前文件系统关系。"""

    resolved = path.resolve(strict=False)
    matches = []
    for mount in psutil.disk_partitions(all=True):
        mountpoint = Path(mount.mountpoint).resolve(strict=False)
        if resolved == mountpoint or resolved.is_relative_to(mountpoint):
            matches.append((len(mountpoint.parts), mount, mountpoint))
    if not matches:
        return None
    _, mount, mountpoint = max(matches, key=lambda item: item[0])
    try:
        usage = psutil.disk_usage(str(mountpoint))
    except OSError:
        usage = None
    return {
        "device": mount.device,
        "filesystem": mount.fstype,
        "mountpoint": str(mountpoint),
        "total": usage.total if usage else None,
        "free": usage.free if usage else None,
        "read_only": "ro" in mount.opts.split(","),
    }


def _nss_passwd_lines() -> list[str]:
    """以固定命令和超时读取 NSS 账号，目录服务异常时安全降级。"""

    try:
        completed = subprocess.run(
            ["getent", "passwd"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    return completed.stdout.splitlines()


def local_users(
    passwd_path: Path = Path("/etc/passwd"), nss_lines: list[str] | None = None
) -> list[dict[str, Any]]:
    """从本机 passwd 文件读取账号并基于 UID/GID 返回稳定关联信息。"""

    sudo_members = _sudo_group_members()
    users = []
    try:
        passwd_lines = passwd_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return users
    local_names = {fields[0] for line in passwd_lines if len(fields := line.split(":")) == 7}
    if nss_lines is None and passwd_path == Path("/etc/passwd"):
        nss_lines = _nss_passwd_lines()
    merged_lines = passwd_lines + [
        line for line in (nss_lines or []) if line.split(":", 1)[0] not in local_names
    ]
    for line in merged_lines:
        fields = line.split(":")
        if len(fields) != 7:
            continue
        username, _, uid, gid, gecos, home, shell = fields
        if Path(shell).name == "nologin":
            continue
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
                "account_source": "local" if username in local_names else "NSS 目录服务",
                "account_status": "N/A",
                "home_storage": _home_storage(Path(home)),
                "sudo_sources": [],
            }
        )
    sudoers_scan = scan_sudoers_rules({str(user["username"]) for user in users})
    for user in users:
        username = str(user["username"])
        sources = ["sudo 组"] if username in sudo_members else []
        sources.extend(sudoers_scan["sources"].get(username, []))
        user["sudo_sources"] = sources
        user["sudo_sources_complete"] = sudoers_scan["complete"]
    return sorted(users, key=lambda item: (item["uid"], item["username"]))
