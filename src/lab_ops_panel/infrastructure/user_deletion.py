"""本机普通用户删除的事实预检与受限执行。"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil

from lab_ops_panel.domain.user_imports import USERNAME
from lab_ops_panel.infrastructure.processes import (
    minimum_regular_uid,
    requester_context,
    terminate_uid_processes,
)
from lab_ops_panel.infrastructure.sudo_rules import apply_sudo_grant, revoke_sudo_grant
from lab_ops_panel.infrastructure.sudoers import scan_sudoers_rules

PASSWD_PATH = Path("/etc/passwd")
CRONTAB_DIRECTORY = Path("/var/spool/cron/crontabs")
AT_DIRECTORY = Path("/var/spool/cron/atjobs")
SYSTEM_CRONTAB = Path("/etc/crontab")
CRON_DIRECTORY = Path("/etc/cron.d")
COMMANDS = {
    "du": Path("/usr/bin/du"),
    "passwd": Path("/usr/bin/passwd"),
    "usermod": Path("/usr/sbin/usermod"),
    "userdel": Path("/usr/sbin/userdel"),
}
CRITICAL_HOMES = frozenset(
    {
        Path("/"),
        Path("/bin"),
        Path("/boot"),
        Path("/dev"),
        Path("/etc"),
        Path("/home"),
        Path("/lib"),
        Path("/lib64"),
        Path("/opt"),
        Path("/proc"),
        Path("/root"),
        Path("/run"),
        Path("/sbin"),
        Path("/srv"),
        Path("/sys"),
        Path("/tmp"),
        Path("/usr"),
        Path("/var"),
    }
)


def _local_accounts(path: Path = PASSWD_PATH) -> list[dict[str, Any]]:
    """只解析本机 passwd 文件，不通过 NSS 接受目录账号。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("无法读取本机账号文件") from exc
    accounts = []
    for line in lines:
        fields = line.split(":")
        if len(fields) != 7:
            continue
        try:
            uid = int(fields[2])
            gid = int(fields[3])
        except ValueError:
            continue
        accounts.append(
            {
                "username": fields[0],
                "uid": uid,
                "gid": gid,
                "display_name": fields[4].split(",", 1)[0],
                "home": fields[5],
                "shell": fields[6],
            }
        )
    return accounts


def local_account(username: str, path: Path = PASSWD_PATH) -> dict[str, Any] | None:
    """返回名称完全匹配的本机账号。"""

    if not USERNAME.fullmatch(username):
        raise ValueError("用户名格式无效")
    return next((item for item in _local_accounts(path) if item["username"] == username), None)


def _run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """执行固定绝对命令并隐藏 stderr 内容。"""

    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"系统动作 {Path(command[0]).name} 失败") from exc


def _account_status(username: str, runner: Callable[..., Any]) -> str | None:
    try:
        output = runner([str(COMMANDS["passwd"]), "--status", "--", username], 10).stdout
    except RuntimeError:
        return None
    fields = output.split()
    return fields[1] if len(fields) >= 2 else None


def _uid_processes(uid: int) -> list[dict[str, int | float]]:
    result = []
    for process in psutil.process_iter(["pid", "uids", "create_time"]):
        try:
            if process.info["uids"].real == uid:
                result.append(
                    {"pid": process.pid, "create_time": float(process.info["create_time"])}
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, TypeError):
            continue
    return sorted(result, key=lambda item: item["pid"])


def _open_home_pids(home: Path) -> tuple[list[int], bool]:
    """扫描 `/proc/*/fd` 指向 home 的描述符，最多返回 100 个 PID。"""

    result: set[int] = set()
    complete = True
    for process_directory in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(process_directory.name)
            descriptors = process_directory / "fd"
            for descriptor in descriptors.iterdir():
                try:
                    target = descriptor.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if target == home or target.is_relative_to(home):
                    result.add(pid)
                    break
        except PermissionError:
            complete = False
        except (FileNotFoundError, NotADirectoryError, ValueError):
            continue
        if len(result) >= 100:
            complete = False
            break
    return sorted(result), complete


def _scheduled_tasks(
    username: str,
    uid: int,
    home: Path,
    crontab_directory: Path,
    at_directory: Path,
    system_crontab: Path,
    cron_directory: Path,
) -> dict[str, Any]:
    user_crontab = crontab_directory / username
    try:
        crontab_metadata = user_crontab.lstat()
        has_user_crontab = stat.S_ISREG(crontab_metadata.st_mode)
    except OSError:
        has_user_crontab = False
    at_jobs = 0
    try:
        for path in at_directory.iterdir():
            try:
                if not path.is_symlink() and path.stat().st_uid == uid:
                    at_jobs += 1
            except OSError:
                continue
    except OSError:
        pass
    references = []
    candidates = [system_crontab]
    with suppress(OSError):
        candidates.extend(
            path for path in cron_directory.iterdir() if path.is_file() and not path.is_symlink()
        )
    pattern = re.compile(rf"(^|\s){re.escape(username)}(\s|$)")
    for path in candidates:
        try:
            if any(pattern.search(line.split("#", 1)[0]) for line in path.read_text().splitlines()):
                references.append(str(path))
        except OSError:
            continue
    unit_directory = home / ".config/systemd/user"
    try:
        user_units = sum(1 for path in unit_directory.iterdir() if path.is_file())
    except OSError:
        user_units = 0
    return {
        "user_crontab": has_user_crontab,
        "at_jobs": at_jobs,
        "system_cron_references": sorted(references),
        "systemd_user_units": user_units,
    }


def _disk_usage(home: Path, runner: Callable[..., Any]) -> dict[str, int | None]:
    """用 GNU du 固定参数统计同一文件系统的字节和 inode。"""

    try:
        bytes_output = runner(
            [
                str(COMMANDS["du"]),
                "--summarize",
                "--one-file-system",
                "--block-size=1",
                "--",
                str(home),
            ]
        ).stdout
        inode_output = runner(
            [str(COMMANDS["du"]), "--summarize", "--one-file-system", "--inodes", "--", str(home)]
        ).stdout
        allocated = int(bytes_output.split()[0])
        inodes = int(inode_output.split()[0])
        return {"allocated_bytes": allocated, "inodes": inodes}
    except (RuntimeError, ValueError, IndexError):
        return {"allocated_bytes": None, "inodes": None}


def user_deletion_preflight(
    username: str,
    expected_uid: int,
    passwd_path: Path = PASSWD_PATH,
    crontab_directory: Path = CRONTAB_DIRECTORY,
    at_directory: Path = AT_DIRECTORY,
    system_crontab: Path = SYSTEM_CRONTAB,
    cron_directory: Path = CRON_DIRECTORY,
    runner: Callable[..., Any] = _run,
    requester_pid: int | None = None,
    require_root: bool = True,
) -> dict[str, Any]:
    """收集账号、进程、会话、计划任务、sudo、home 边界和占用事实。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("用户删除预检只能由受限特权助手执行")
    account = local_account(username, passwd_path)
    if account is None or account["uid"] != expected_uid:
        raise ValueError("本机账号不存在或 UID 已变化")
    if expected_uid < minimum_regular_uid():
        raise ValueError("拒绝删除 root、系统账号或当前执行用户")
    if require_root:
        if requester_pid is None:
            raise ValueError("缺少可验证的调用进程")
        requester, _ = requester_context(requester_pid)
        if requester.uids().real == expected_uid:
            raise ValueError("拒绝删除启动 LabOpsPanel 的用户")
    logical_home = Path(str(account["home"]))
    blockers = []
    home_delete_blockers = []
    warnings = []
    if not logical_home.is_absolute() or logical_home in CRITICAL_HOMES:
        raise ValueError("账号 home 路径属于根路径或系统关键目录")
    try:
        logical_metadata = logical_home.lstat()
        resolved_home = logical_home.resolve(strict=True)
        resolved_metadata = resolved_home.stat()
    except OSError as exc:
        raise ValueError("账号 home 不存在或无法解析") from exc
    home_is_symlink = stat.S_ISLNK(logical_metadata.st_mode)
    if not stat.S_ISDIR(resolved_metadata.st_mode):
        home_delete_blockers.append("home 解析目标不是目录")
    if resolved_metadata.st_uid not in {0, expected_uid}:
        home_delete_blockers.append("home 所有者不是目标用户或 root")
    if resolved_metadata.st_mode & 0o022:
        home_delete_blockers.append("home 可被组或其他用户写入")
    same_home_users = sorted(
        item["username"]
        for item in _local_accounts(passwd_path)
        if item["username"] != username
        and Path(str(item["home"])).resolve(strict=False) == resolved_home
    )
    if same_home_users:
        home_delete_blockers.append("home 与其他本机账号共享")
    nested_mounts = []
    for mount in psutil.disk_partitions(all=True):
        mountpoint = Path(mount.mountpoint).resolve(strict=False)
        if mountpoint == resolved_home or mountpoint.is_relative_to(resolved_home):
            nested_mounts.append(str(mountpoint))
    nested_mounts = sorted(set(nested_mounts))
    if nested_mounts:
        home_delete_blockers.append("home 本身或内部包含挂载点")
    if home_is_symlink:
        warnings.append("逻辑 home 是符号链接，只允许仅删除账号并保留数据")
    open_pids, open_files_complete = _open_home_pids(resolved_home)
    if open_pids:
        warnings.append("检测到进程持有 home 内文件")
    if not open_files_complete:
        home_delete_blockers.append("无法完整检查持有 home 文件的进程")
    processes = _uid_processes(expected_uid)
    sessions = [
        {"pid": item.pid, "terminal": item.terminal, "started": item.started}
        for item in psutil.users()
        if item.name == username
    ]
    scheduled = _scheduled_tasks(
        username,
        expected_uid,
        resolved_home,
        crontab_directory,
        at_directory,
        system_crontab,
        cron_directory,
    )
    sudo_scan = scan_sudoers_rules({username})
    sudo_sources = sudo_scan["sources"].get(username, [])
    managed_sudo_source = f"/etc/sudoers.d/labopspanel-uid-{expected_uid}"
    unmanaged_sudo = [source for source in sudo_sources if source != managed_sudo_source]
    if unmanaged_sudo:
        blockers.append("存在非本程序托管的 sudoers 规则")
    if not sudo_scan["complete"]:
        blockers.append("无法完整检查 sudoers 规则")
    if scheduled["system_cron_references"]:
        blockers.append("系统 cron 配置仍引用该用户名")
    usage = _disk_usage(resolved_home, runner)
    if usage["allocated_bytes"] is None:
        warnings.append("无法完整统计 home 占用")
    account_status = _account_status(username, runner)
    if account_status not in {"P", "L", "NP"}:
        blockers.append("无法识别账号密码锁定状态")
    return {
        "username": username,
        "uid": expected_uid,
        "gid": account["gid"],
        "shell": account["shell"],
        "account_status": account_status,
        "logical_home": str(logical_home),
        "resolved_home": str(resolved_home),
        "home_is_symlink": home_is_symlink,
        "home_device": resolved_metadata.st_dev,
        "home_inode": resolved_metadata.st_ino,
        "home_mode": stat.S_IMODE(resolved_metadata.st_mode),
        "nested_mounts": nested_mounts,
        "shared_with": same_home_users,
        "disk_usage": usage,
        "processes": processes,
        "sessions": sessions,
        "open_home_pids": open_pids,
        "open_files_complete": open_files_complete,
        "scheduled_tasks": scheduled,
        "sudo_sources": sudo_sources,
        "sudo_scan_complete": sudo_scan["complete"],
        "delete_home_supported": not home_delete_blockers and not home_is_symlink,
        "blockers": sorted(set(blockers)),
        "home_delete_blockers": sorted(set(home_delete_blockers)),
        "warnings": sorted(set(warnings)),
    }


def _secure_parent_descriptor(home: Path, owner_uid: int = 0) -> int:
    """打开 root 所有且不可被组或其他用户写入的 home 父目录。"""

    try:
        descriptor = os.open(home.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("home 父目录必须是非符号链接目录") from exc
    metadata = os.fstat(descriptor)
    if metadata.st_uid != owner_uid or metadata.st_mode & 0o022:
        os.close(descriptor)
        raise ValueError("home 父目录所有者或权限不安全")
    return descriptor


def _restore_login(
    username: str,
    original_shell: str,
    original_status: str | None,
    runner: Callable[..., Any],
) -> None:
    """删除失败且账号仍存在时恢复原 shell 与锁定状态。"""

    runner([str(COMMANDS["usermod"]), "--shell", original_shell, "--", username], 30)
    if original_status == "P":
        runner([str(COMMANDS["usermod"]), "--unlock", "--", username], 30)
    elif original_status == "NP":
        runner([str(COMMANDS["passwd"]), "--delete", "--", username], 30)


def _cleanup_schedules(
    username: str,
    uid: int,
    crontab_directory: Path,
    at_directory: Path,
    owner_uid: int,
) -> None:
    """删除目标账号自己的用户 crontab 与 at 作业，不触碰系统 cron。"""

    for directory in (crontab_directory, at_directory):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_mode & 0o022
        ):
            raise ValueError(f"计划任务目录 {directory} 不安全")
    crontab = crontab_directory / username
    try:
        metadata = crontab.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("用户 crontab 类型异常")
        if metadata.st_uid not in {0, uid}:
            raise ValueError("用户 crontab 所有者异常")
        crontab.unlink()
    try:
        jobs = list(at_directory.iterdir())
    except FileNotFoundError:
        jobs = []
    for job in jobs:
        metadata = job.lstat()
        if (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == uid
        ):
            job.unlink()


def delete_local_user(
    username: str,
    expected_uid: int,
    expected_home: str,
    delete_home: bool,
    requester_pid: int,
    passwd_path: Path = PASSWD_PATH,
    crontab_directory: Path = CRONTAB_DIRECTORY,
    at_directory: Path = AT_DIRECTORY,
    preflight_reader: Callable[[], dict[str, Any]] | None = None,
    runner: Callable[..., Any] = _run,
    process_terminator: Callable[..., dict[str, Any]] = terminate_uid_processes,
    sudo_revoker: Callable[..., dict[str, Any]] = revoke_sudo_grant,
    sudo_restorer: Callable[..., dict[str, Any]] = apply_sudo_grant,
    require_root: bool = True,
) -> dict[str, Any]:
    """锁定登录、终止进程、删除账号，并可安全隔离后清理普通目录 home。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("用户删除只能由受限特权助手执行")

    def read_preflight() -> dict[str, Any]:
        if preflight_reader is not None:
            return preflight_reader()
        return user_deletion_preflight(
            username,
            expected_uid,
            passwd_path=passwd_path,
            crontab_directory=crontab_directory,
            at_directory=at_directory,
            requester_pid=requester_pid,
            require_root=require_root,
        )

    facts = read_preflight()
    if facts["uid"] != expected_uid or facts["logical_home"] != expected_home:
        raise ValueError("账号 UID 或 home 已变化，请重新预检")
    if facts["blockers"]:
        raise ValueError("用户删除存在阻断项：" + "；".join(facts["blockers"]))
    if delete_home and not facts["delete_home_supported"]:
        raise ValueError("当前 home 形态不支持自动删除")
    original_shell = str(facts["shell"])
    original_status = facts["account_status"]
    login_disabled = False
    sudo_revoked = False
    renamed_home: Path | None = None
    account_deleted = False
    logical_home = Path(expected_home)
    try:
        runner(
            [
                str(COMMANDS["usermod"]),
                "--lock",
                "--shell",
                "/usr/sbin/nologin",
                "--",
                username,
            ],
            30,
        )
        login_disabled = True
        process_result = process_terminator(
            expected_uid, requester_pid, grace_seconds=5, require_root=require_root
        )
        refreshed = read_preflight()
        if any(
            refreshed[key] != facts[key]
            for key in (
                "uid",
                "logical_home",
                "resolved_home",
                "home_device",
                "home_inode",
            )
        ):
            raise RuntimeError("执行前复检发现账号或 home 稳定身份已变化")
        if refreshed["blockers"]:
            raise RuntimeError("执行前复检出现阻断项：" + "；".join(refreshed["blockers"]))
        if delete_home and not refreshed["delete_home_supported"]:
            raise RuntimeError("执行前复检发现 home 已不再适合自动删除")
        if refreshed["processes"] or refreshed["open_home_pids"]:
            raise RuntimeError("终止后仍检测到进程或被占用的 home 文件")
        sudo_result = sudo_revoker(username, expected_uid, require_root=require_root)
        sudo_revoked = bool(sudo_result.get("changed"))
        if delete_home:
            if facts["home_is_symlink"] or Path(facts["resolved_home"]) != logical_home:
                raise ValueError("符号链接 home 不能自动递归删除")
            parent_descriptor = _secure_parent_descriptor(
                logical_home, 0 if require_root else os.getuid()
            )
            quarantine_name = f".labopspanel-delete-{expected_uid}-{uuid.uuid4().hex}"
            try:
                metadata = os.stat(
                    logical_home.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    metadata.st_dev != facts["home_device"]
                    or metadata.st_ino != facts["home_inode"]
                    or not stat.S_ISDIR(metadata.st_mode)
                ):
                    raise ValueError("home 目录身份已变化")
                os.rename(
                    logical_home.name,
                    quarantine_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                os.fsync(parent_descriptor)
                renamed_home = logical_home.parent / quarantine_name
            finally:
                os.close(parent_descriptor)
        runner([str(COMMANDS["userdel"]), "--", username], 60)
        account_deleted = True
        if local_account(username, passwd_path) is not None:
            raise RuntimeError("userdel 后本机账号仍存在")
        _cleanup_schedules(
            username,
            expected_uid,
            crontab_directory,
            at_directory,
            0 if require_root else os.getuid(),
        )
        if renamed_home is not None:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError("当前 Python 不支持抗符号链接攻击的目录删除")
            metadata = renamed_home.lstat()
            if metadata.st_dev != facts["home_device"] or metadata.st_ino != facts["home_inode"]:
                raise RuntimeError("隔离后的 home 目录身份验证失败")
            try:
                shutil.rmtree(renamed_home)
            except OSError as exc:
                raise RuntimeError(f"账号已删除，但 home 数据保留在 {renamed_home}") from exc
            renamed_home = None
        return {
            "account_deleted": True,
            "home_deleted": delete_home,
            "terminated_pids": process_result.get("terminated_pids", []),
            "escalated_pids": process_result.get("escalated_pids", []),
            "quarantine_path": None,
        }
    except Exception:
        if not account_deleted:
            if renamed_home is not None and renamed_home.exists() and not logical_home.exists():
                os.rename(renamed_home, logical_home)
            if sudo_revoked:
                sudo_restorer(username, expected_uid, require_root=require_root)
            if login_disabled and local_account(username, passwd_path) is not None:
                _restore_login(username, original_shell, original_status, runner)
        raise
