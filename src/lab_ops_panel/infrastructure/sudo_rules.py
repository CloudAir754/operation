"""LabOpsPanel 独立 sudoers 规则的原子写入与撤销。"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from lab_ops_panel.domain.user_imports import USERNAME

MANAGED_MARKER = "# 由 LabOpsPanel 管理；请勿手工修改"


def _default_visudo(path: Path) -> None:
    """以固定参数验证指定 sudoers 文件。"""

    executable = shutil.which("visudo")
    if executable is None:
        raise ValueError("系统缺少 visudo")
    try:
        subprocess.run(
            [executable, "-c", "-f", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"visudo 验证 {path} 失败") from exc


def _rule_name(uid: int) -> str:
    if uid < 0:
        raise ValueError("UID 无效")
    return f"labopspanel-uid-{uid}"


def _rule_content(username: str, uid: int) -> str:
    if not USERNAME.fullmatch(username):
        raise ValueError("用户名格式无效")
    return f"{MANAGED_MARKER}\n# UID: {uid}\n{username} ALL=(ALL:ALL) ALL\n"


def _write_at(
    directory_descriptor: int,
    name: str,
    content: str,
    owner_uid: int,
    owner_gid: int,
) -> None:
    """在已打开目录内创建仅 root 可改的完整临时文件。"""

    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o440,
        dir_fd=directory_descriptor,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fchown(stream.fileno(), owner_uid, owner_gid)
        os.fchmod(stream.fileno(), 0o440)
        os.fsync(stream.fileno())


def _read_at(directory_descriptor: int, name: str, owner_uid: int) -> str | None:
    """不跟随符号链接读取已有托管规则。"""

    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("sudo 规则必须是非符号链接普通文件") from exc
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner_uid:
            raise ValueError("sudo 规则所有者或类型异常")
        if metadata.st_mode & 0o022:
            raise ValueError("sudo 规则不能被组或其他用户写入")
        return stream.read()


def _open_directory(path: Path, owner_uid: int) -> int:
    """打开 root 所有且不可被组或其他用户写入的 sudoers.d。"""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("sudoers.d 必须是非符号链接目录") from exc
    metadata = os.fstat(descriptor)
    if metadata.st_uid != owner_uid or metadata.st_mode & 0o022:
        os.close(descriptor)
        raise ValueError("sudoers.d 所有者或权限不安全")
    return descriptor


def apply_sudo_grant(
    username: str,
    uid: int,
    sudoers_path: Path = Path("/etc/sudoers"),
    sudoers_directory: Path = Path("/etc/sudoers.d"),
    validator: Callable[[Path], None] = _default_visudo,
    owner_uid: int = 0,
    owner_gid: int = 0,
    require_root: bool = True,
) -> dict[str, Any]:
    """写入独立规则并在替换前后运行 visudo，失败时恢复。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("sudo 授权只能由受限特权助手执行")
    desired = _rule_content(username, uid)
    validator(sudoers_path)
    name = _rule_name(uid)
    directory_descriptor = _open_directory(sudoers_directory, owner_uid)
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    previous: str | None = None
    try:
        previous = _read_at(directory_descriptor, name, owner_uid)
        if previous == desired:
            return {"changed": False, "path": str(sudoers_directory / name)}
        _write_at(directory_descriptor, temporary_name, desired, owner_uid, owner_gid)
        validator(sudoers_directory / temporary_name)
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
        temporary_name = ""
        try:
            validator(sudoers_path)
        except ValueError:
            _restore_rule(directory_descriptor, name, previous, owner_uid, owner_gid)
            validator(sudoers_path)
            raise
        return {"changed": True, "path": str(sudoers_directory / name)}
    finally:
        if temporary_name:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_descriptor)
        os.close(directory_descriptor)


def revoke_sudo_grant(
    username: str,
    uid: int,
    sudoers_path: Path = Path("/etc/sudoers"),
    sudoers_directory: Path = Path("/etc/sudoers.d"),
    validator: Callable[[Path], None] = _default_visudo,
    owner_uid: int = 0,
    owner_gid: int = 0,
    require_root: bool = True,
) -> dict[str, Any]:
    """只撤销内容与目标匹配的 LabOpsPanel 托管规则。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("sudo 撤销只能由受限特权助手执行")
    desired = _rule_content(username, uid)
    validator(sudoers_path)
    name = _rule_name(uid)
    directory_descriptor = _open_directory(sudoers_directory, owner_uid)
    try:
        previous = _read_at(directory_descriptor, name, owner_uid)
        if previous is None:
            return {"changed": False, "path": str(sudoers_directory / name)}
        if previous != desired or not previous.startswith(MANAGED_MARKER):
            raise ValueError("拒绝撤销非本程序创建或已被手工修改的 sudo 规则")
        os.unlink(name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
        try:
            validator(sudoers_path)
        except ValueError:
            _restore_rule(directory_descriptor, name, previous, owner_uid, owner_gid)
            validator(sudoers_path)
            raise
        return {"changed": True, "path": str(sudoers_directory / name)}
    finally:
        os.close(directory_descriptor)


def _restore_rule(
    directory_descriptor: int,
    name: str,
    previous: str | None,
    owner_uid: int,
    owner_gid: int,
) -> None:
    """原子恢复替换前规则，或删除本次新建规则。"""

    if previous is None:
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        return
    temporary_name = f".{name}.{uuid.uuid4().hex}.rollback"
    _write_at(directory_descriptor, temporary_name, previous, owner_uid, owner_gid)
    os.replace(
        temporary_name,
        name,
        src_dir_fd=directory_descriptor,
        dst_dir_fd=directory_descriptor,
    )
    os.fsync(directory_descriptor)
