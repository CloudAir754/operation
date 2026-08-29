"""经 sudo 调用的结构化白名单特权助手入口。"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import Any

from lab_ops_panel.domain.ssh_keys import parse_public_key
from lab_ops_panel.domain.user_imports import USERNAME
from lab_ops_panel.infrastructure.authorized_keys import import_authorized_keys
from lab_ops_panel.infrastructure.disk_initialization import (
    disk_initialization_plan,
    disk_initialization_status,
    execute_disk_initialization,
)
from lab_ops_panel.infrastructure.file_transfer import (
    cleanup_file_transfer_source,
    execute_file_transfer,
    file_transfer_preflight,
)
from lab_ops_panel.infrastructure.home_migration import (
    cleanup_home_migration_backup,
    execute_home_migration,
    home_migration_preflight,
    home_migration_status,
    rollback_home_migration,
)
from lab_ops_panel.infrastructure.pam_rules import apply_policy_changes, restore_policy_backup
from lab_ops_panel.infrastructure.processes import (
    port_usage,
    terminate_process,
    terminate_uid_processes,
)
from lab_ops_panel.infrastructure.sudo_rules import apply_sudo_grant, revoke_sudo_grant
from lab_ops_panel.infrastructure.user_deletion import (
    delete_local_user,
    user_deletion_preflight,
)

SYSTEM_COMMANDS = {
    "useradd": Path("/usr/sbin/useradd"),
    "userdel": Path("/usr/sbin/userdel"),
    "chpasswd": Path("/usr/sbin/chpasswd"),
}


def _local_account(username: str, path: Path = Path("/etc/passwd")) -> pwd.struct_passwd | None:
    """只从本机 passwd 文件返回账号，不接受目录账号。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("无法读取本机账号来源") from exc
    for line in lines:
        fields = line.split(":")
        if len(fields) != 7 or fields[0] != username:
            continue
        try:
            return pwd.struct_passwd(
                (
                    fields[0],
                    fields[1],
                    int(fields[2]),
                    int(fields[3]),
                    fields[4],
                    fields[5],
                    fields[6],
                )
            )
        except ValueError as exc:
            raise ValueError("本机账号记录无效") from exc
    return None


def _run(command: list[str], input_value: str | None = None) -> None:
    """执行固定参数系统命令，不将 stdin 或 stderr 传播到结果。"""

    try:
        subprocess.run(
            command,
            input=input_value,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"系统动作 {Path(command[0]).name} 失败") from exc


def _create_user(payload: dict[str, Any]) -> dict[str, Any]:
    """创建单个本机账号、设置随机密码并验证事实状态。"""

    username = str(payload.get("username", ""))
    display_name = str(payload.get("display_name", ""))
    password = str(payload.get("password", ""))
    shell = str(payload.get("shell", "/bin/bash"))
    if not USERNAME.fullmatch(username):
        raise ValueError("用户名格式无效")
    invalid_display_name = any(value in display_name for value in ":\r\n\x00")
    if not display_name or len(display_name) > 100 or invalid_display_name:
        raise ValueError("中文姓名无效")
    if len(password) < 15 or any(value in password for value in ":\r\n\x00"):
        raise ValueError("初始密码不满足安全输入要求")
    if shell != "/bin/bash":
        raise ValueError("首版只允许 /bin/bash")
    if _local_account(username) is not None:
        return {"created": False, "reason": "账号已存在"}
    _run(
        [
            str(SYSTEM_COMMANDS["useradd"]),
            "--create-home",
            "--shell",
            shell,
            "--comment",
            display_name,
            "--",
            username,
        ]
    )
    try:
        account = _local_account(username)
        if account is None:
            raise RuntimeError("useradd 后未找到本机账号")
        _run([str(SYSTEM_COMMANDS["chpasswd"])], f"{username}:{password}\n")
        home = Path(account.pw_dir)
        if not home.is_dir() or home.stat().st_uid != account.pw_uid:
            raise RuntimeError("账号 home 创建结果验证失败")
        return {
            "created": True,
            "username": account.pw_name,
            "uid": account.pw_uid,
            "gid": account.pw_gid,
            "home": account.pw_dir,
            "shell": account.pw_shell,
        }
    except Exception:
        _run([str(SYSTEM_COMMANDS["userdel"]), "--remove", "--", username])
        raise


def _dispatch(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """将固定动作名映射到严格参数实现。"""

    if action == "create-user":
        return _create_user(payload)
    if action == "file-transfer-preflight":
        source = payload.get("source")
        target_directory = payload.get("target_directory")
        if not isinstance(source, str) or not isinstance(target_directory, str):
            raise ValueError("文件迁移预检参数无效")
        return file_transfer_preflight(source, target_directory)
    if action == "file-transfer-copy":
        source = payload.get("source")
        target_directory = payload.get("target_directory")
        operation_id = payload.get("operation_id")
        expected_device = payload.get("expected_device")
        expected_inode = payload.get("expected_inode")
        if (
            not isinstance(source, str)
            or not isinstance(target_directory, str)
            or not isinstance(operation_id, str)
            or not isinstance(expected_device, int)
            or not isinstance(expected_inode, int)
        ):
            raise ValueError("文件复制参数无效")
        return execute_file_transfer(
            source, target_directory, operation_id, expected_device, expected_inode
        )
    if action == "file-transfer-cleanup":
        source = payload.get("source")
        destination = payload.get("destination")
        operation_id = payload.get("operation_id")
        expected_device = payload.get("expected_device")
        expected_inode = payload.get("expected_inode")
        if (
            not isinstance(source, str)
            or not isinstance(destination, str)
            or not isinstance(operation_id, str)
            or not isinstance(expected_device, int)
            or not isinstance(expected_inode, int)
        ):
            raise ValueError("文件清理参数无效")
        return cleanup_file_transfer_source(
            source, destination, operation_id, expected_device, expected_inode
        )
    if action == "home-migration-preflight":
        username = payload.get("username")
        expected_uid = payload.get("expected_uid")
        target_parent = payload.get("target_parent")
        migration_id = payload.get("migration_id")
        safety_margin_percent = payload.get("safety_margin_percent", 10)
        verify_contents = payload.get("verify_contents", False)
        if (
            not isinstance(username, str)
            or not isinstance(expected_uid, int)
            or not isinstance(target_parent, str)
            or not isinstance(migration_id, str)
            or not isinstance(safety_margin_percent, int)
            or not isinstance(verify_contents, bool)
        ):
            raise ValueError("home 迁移预检参数无效")
        return home_migration_preflight(
            username,
            expected_uid,
            target_parent,
            migration_id,
            safety_margin_percent,
            verify_contents=verify_contents,
        )
    if action == "home-migration-status":
        migration_id = payload.get("migration_id")
        if not isinstance(migration_id, str):
            raise ValueError("home 迁移状态参数无效")
        return home_migration_status(migration_id)
    if action == "home-migration-execute":
        migration_id = payload.get("migration_id")
        requester_pid = payload.get("requester_pid")
        if not isinstance(migration_id, str) or not isinstance(requester_pid, int):
            raise ValueError("home 迁移执行参数无效")
        return execute_home_migration(migration_id, requester_pid)
    if action == "home-migration-rollback":
        migration_id = payload.get("migration_id")
        requester_pid = payload.get("requester_pid")
        if not isinstance(migration_id, str) or not isinstance(requester_pid, int):
            raise ValueError("home 迁移回退参数无效")
        return rollback_home_migration(migration_id, requester_pid)
    if action == "home-migration-cleanup":
        migration_id = payload.get("migration_id")
        if not isinstance(migration_id, str):
            raise ValueError("home 迁移备份清理参数无效")
        return cleanup_home_migration_backup(migration_id)
    if action in {"disk-initialization-plan", "disk-initialization-execute"}:
        stable_identity = payload.get("stable_identity")
        label = payload.get("label")
        mountpoint = payload.get("mountpoint")
        mount_options = payload.get("mount_options")
        if (
            not isinstance(stable_identity, str)
            or not isinstance(label, str)
            or not isinstance(mountpoint, str)
            or not isinstance(mount_options, list)
            or not all(isinstance(value, str) for value in mount_options)
        ):
            raise ValueError("磁盘初始化参数无效")
        if action == "disk-initialization-plan":
            return disk_initialization_plan(stable_identity, label, mountpoint, mount_options)
        confirmed_serial = payload.get("confirmed_serial")
        operation_id = payload.get("operation_id")
        if not isinstance(confirmed_serial, str) or not isinstance(operation_id, str):
            raise ValueError("磁盘初始化确认参数无效")
        return execute_disk_initialization(
            stable_identity,
            confirmed_serial,
            label,
            mountpoint,
            mount_options,
            operation_id,
        )
    if action == "disk-initialization-status":
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str):
            raise ValueError("磁盘初始化状态参数无效")
        return disk_initialization_status(operation_id)
    if action == "pam-update":
        changes = payload.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("PAM 策略参数无效")
        return apply_policy_changes(changes)
    if action == "pam-restore":
        return restore_policy_backup(str(payload.get("backup_id", "")))
    if action == "port-lookup":
        port = payload.get("port")
        protocol = payload.get("protocol", "any")
        if not isinstance(port, int) or not isinstance(protocol, str):
            raise ValueError("端口查询参数无效")
        return port_usage(port, protocol)
    if action == "process-terminate":
        pid = payload.get("pid")
        expected_uid = payload.get("uid")
        create_time = payload.get("create_time")
        requester_pid = payload.get("requester_pid")
        grace_seconds = payload.get("grace_seconds", 5)
        values = (pid, expected_uid, create_time, requester_pid, grace_seconds)
        if not all(isinstance(value, int | float) for value in values):
            raise ValueError("进程终止参数无效")
        return terminate_process(
            int(pid),
            int(expected_uid),
            float(create_time),
            int(requester_pid),
            float(grace_seconds),
        )
    if action == "uid-processes-terminate":
        uid = payload.get("uid")
        requester_pid = payload.get("requester_pid")
        grace_seconds = payload.get("grace_seconds", 5)
        if not all(isinstance(value, int | float) for value in (uid, requester_pid, grace_seconds)):
            raise ValueError("用户进程终止参数无效")
        return terminate_uid_processes(int(uid), int(requester_pid), float(grace_seconds))
    if action == "user-delete-preflight":
        username = payload.get("username")
        expected_uid = payload.get("uid")
        requester_pid = payload.get("requester_pid")
        if (
            not isinstance(username, str)
            or not isinstance(expected_uid, int)
            or not isinstance(requester_pid, int)
        ):
            raise ValueError("用户删除预检参数无效")
        return user_deletion_preflight(username, expected_uid, requester_pid=requester_pid)
    if action == "user-delete":
        username = payload.get("username")
        expected_uid = payload.get("expected_uid")
        expected_home = payload.get("expected_home")
        delete_home = payload.get("delete_home")
        requester_pid = payload.get("requester_pid")
        if (
            not isinstance(username, str)
            or not isinstance(expected_uid, int)
            or not isinstance(expected_home, str)
            or not isinstance(delete_home, bool)
            or not isinstance(requester_pid, int)
        ):
            raise ValueError("用户删除参数无效")
        return delete_local_user(
            username,
            expected_uid,
            expected_home,
            delete_home,
            requester_pid,
        )
    username = str(payload.get("username", ""))
    account = _local_account(username)
    if account is None:
        raise ValueError("本机账号不存在")
    expected_uid = payload.get("expected_uid")
    if not isinstance(expected_uid, int) or account.pw_uid != expected_uid:
        raise ValueError("账号 UID 已变化，请重新预检")
    if action == "ssh-import":
        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 100:
            raise ValueError("公钥列表无效")
        keys = [parse_public_key(str(value)) for value in raw_keys]
        return import_authorized_keys(Path(account.pw_dir), account.pw_uid, account.pw_gid, keys)
    if action == "sudo-grant":
        return apply_sudo_grant(username, account.pw_uid)
    if action == "sudo-revoke":
        return revoke_sudo_grant(username, account.pw_uid)
    raise ValueError("不支持的特权动作")


def main() -> None:
    """读取最大 1 MiB JSON 请求并只输出结构化结果。"""

    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "特权助手必须以 root 运行"}))
        raise SystemExit(1)
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "动作参数无效"}))
        raise SystemExit(2)
    value = sys.stdin.buffer.read(1_000_001)
    if len(value) > 1_000_000:
        print(json.dumps({"ok": False, "error": "请求过大"}))
        raise SystemExit(2)
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("请求必须是对象")
        result = _dispatch(sys.argv[1], payload)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
