"""authorized_keys 的受限原子写入实现。"""

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

from lab_ops_panel.domain.ssh_keys import ALLOWED_KEY_TYPES, PublicKey, parse_public_key


def _default_acl_reader(path: Path) -> str:
    """用固定参数读取数字 ACL；缺少工具或失败时阻断写入。"""

    executable = shutil.which("getfacl")
    if executable is None:
        raise ValueError("系统缺少 getfacl，无法验证 SSH 路径 ACL")
    try:
        completed = subprocess.run(
            [executable, "--absolute-names", "--numeric", "--omit-header", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"无法读取 {path} 的 ACL") from exc
    return completed.stdout


def _assert_acl_no_extra_write(
    path: Path, target_uid: int, acl_reader: Callable[[Path], str]
) -> None:
    """拒绝通过命名用户、命名组或默认 ACL 授予额外写权限。"""

    for raw_line in acl_reader(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        default = line.startswith("default:")
        value = line.removeprefix("default:")
        fields = value.split(":")
        if len(fields) < 3:
            continue
        kind, qualifier, permissions = fields[:3]
        if "w" not in permissions:
            continue
        if default:
            raise ValueError(f"{path} 包含授予写权限的默认 ACL")
        if kind == "user" and qualifier and qualifier != str(target_uid):
            raise ValueError(f"{path} 额外授予其他用户写权限")
        if kind == "group" and qualifier:
            raise ValueError(f"{path} 额外授予命名组写权限")


def _assert_directory(path: Path, allowed_owners: set[int], label: str) -> os.stat_result:
    """拒绝符号链接、非目录、异常所有者和组/其他用户可写目录。"""

    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} 必须是非符号链接目录")
    if metadata.st_uid not in allowed_owners:
        raise ValueError(f"{label} 所有者异常")
    if metadata.st_mode & 0o022:
        raise ValueError(f"{label} 不能被组或其他用户写入")
    return metadata


def _existing_fingerprints(lines: list[str]) -> set[str]:
    """从已有 authorized_keys 行提取公钥，即使该行带已存在的选项。"""

    result = set()
    for line in lines:
        fields = line.split()
        key_index = next(
            (index for index, value in enumerate(fields) if value in ALLOWED_KEY_TYPES), None
        )
        if key_index is None or len(fields) <= key_index + 1:
            continue
        try:
            key = parse_public_key(" ".join(fields[key_index:]))
        except ValueError:
            continue
        result.add(key.fingerprint)
    return result


def import_authorized_keys(
    home: Path,
    target_uid: int,
    target_gid: int,
    keys: list[PublicKey],
    acl_reader: Callable[[Path], str] = _default_acl_reader,
    require_root: bool = True,
) -> dict[str, Any]:
    """校验路径后去重并原子替换 authorized_keys。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("SSH 公钥写入只能由受限特权助手执行")
    if not home.is_absolute() or home == Path("/"):
        raise ValueError("home 路径不安全")
    try:
        logical_metadata = home.lstat()
        resolved_home = home.resolve(strict=True)
    except OSError as exc:
        raise ValueError("home 不存在或无法解析") from exc
    if stat.S_ISLNK(logical_metadata.st_mode):
        # 迁移后的逻辑 home 可为符号链接，但只在完整解析到真实目录后继续。
        home = resolved_home
    _assert_directory(home, {0, target_uid}, "home")
    home_descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with suppress(FileExistsError):
            os.mkdir(".ssh", 0o700, dir_fd=home_descriptor)
        try:
            ssh_descriptor = os.open(
                ".ssh", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_descriptor
            )
        except OSError as exc:
            raise ValueError(".ssh 必须是非符号链接目录") from exc
    finally:
        os.close(home_descriptor)
    ssh_directory = home / ".ssh"
    authorized_keys = ssh_directory / "authorized_keys"
    temporary_name: str | None = None
    try:
        ssh_metadata = os.fstat(ssh_descriptor)
        if ssh_metadata.st_uid not in {0, target_uid}:
            raise ValueError(".ssh 所有者异常")
        if ssh_metadata.st_mode & 0o022:
            raise ValueError(".ssh 不能被组或其他用户写入")
        os.fchown(ssh_descriptor, target_uid, target_gid)
        os.fchmod(ssh_descriptor, 0o700)
        _assert_acl_no_extra_write(ssh_directory, target_uid, acl_reader)

        existing_lines: list[str] = []
        existing = False
        try:
            existing_descriptor = os.open(
                "authorized_keys", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=ssh_descriptor
            )
        except FileNotFoundError:
            existing_descriptor = None
        except OSError as exc:
            raise ValueError("authorized_keys 必须是非符号链接普通文件") from exc
        if existing_descriptor is not None:
            existing = True
            with os.fdopen(existing_descriptor, "r", encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("authorized_keys 必须是普通文件")
                if metadata.st_uid not in {0, target_uid}:
                    raise ValueError("authorized_keys 所有者异常")
                existing_lines = stream.read().splitlines()
            _assert_acl_no_extra_write(authorized_keys, target_uid, acl_reader)
        fingerprints = _existing_fingerprints(existing_lines)
        additions = [key for key in keys if key.fingerprint not in fingerprints]
        for key in additions:
            fingerprints.add(key.fingerprint)
        if additions:
            temporary_name = f".authorized_keys.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=ssh_descriptor,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                for line in existing_lines:
                    stream.write(f"{line}\n")
                for key in additions:
                    stream.write(f"{key.normalized()}\n")
                stream.flush()
                os.fchown(stream.fileno(), target_uid, target_gid)
                os.fchmod(stream.fileno(), 0o600)
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                "authorized_keys",
                src_dir_fd=ssh_descriptor,
                dst_dir_fd=ssh_descriptor,
            )
            temporary_name = None
        elif not existing:
            descriptor = os.open(
                "authorized_keys",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=ssh_descriptor,
            )
            try:
                os.fchown(descriptor, target_uid, target_gid)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        final_descriptor = os.open(
            "authorized_keys", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=ssh_descriptor
        )
        try:
            os.fchown(final_descriptor, target_uid, target_gid)
            os.fchmod(final_descriptor, 0o600)
        finally:
            os.close(final_descriptor)
        _assert_acl_no_extra_write(authorized_keys, target_uid, acl_reader)
        return {
            "added": len(additions),
            "duplicates": len(keys) - len(additions),
            "fingerprints": [key.fingerprint for key in additions],
            "path": str(authorized_keys),
        }
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=ssh_descriptor)
        os.close(ssh_descriptor)
