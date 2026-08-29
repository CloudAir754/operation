"""受控文件复制、元数据校验、发布和独立源清理。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

RSYNC = Path("/usr/bin/rsync")
TRUSTED_TARGET_UID = 0
TRUSTED_ANCESTOR_UID = 0
CRITICAL_ROOTS = tuple(
    Path(value)
    for value in (
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/lib64",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/sys",
        "/usr",
        "/var",
    )
)


def _absolute_path(raw: str, label: str) -> Path:
    """接受规范绝对路径，拒绝根目录、点段和 NUL。"""

    if not raw or "\x00" in raw:
        raise ValueError(f"{label}路径无效")
    path = Path(raw)
    if not path.is_absolute() or path == Path("/") or path != Path(os.path.normpath(raw)):
        raise ValueError(f"{label}必须是规范绝对路径且不能为根目录")
    return path


def _reject_critical(path: Path, label: str) -> None:
    """拒绝系统关键目录本身及其后代作为迁移源或目标。"""

    if any(path == root or path.is_relative_to(root) for root in CRITICAL_ROOTS):
        raise ValueError(f"{label}位于系统关键目录")


def _existing_ancestors_are_not_symlinks(path: Path) -> None:
    """阻止任一已存在祖先通过符号链接改变路径边界。"""

    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"路径包含符号链接祖先：{current}")


def _target_ancestors_are_trusted(path: Path) -> None:
    """确保 root 助手使用目标路径期间，任一父目录都不能被不可信用户替换。"""

    current = path.parent
    while True:
        metadata = current.stat()
        sticky_root_directory = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if metadata.st_uid not in {0, TRUSTED_ANCESTOR_UID}:
            raise ValueError(f"目标路径父目录所有者不可信：{current}")
        if metadata.st_mode & 0o022 and not sticky_root_directory:
            raise ValueError(f"目标路径父目录可被其他用户改写：{current}")
        if current == Path("/"):
            break
        current = current.parent


def _entry_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    raise ValueError("源路径包含不支持的特殊文件")


def _tree_summary(path: Path) -> dict[str, Any]:
    """不跟随符号链接且不跨文件系统统计树和关键元数据摘要。"""

    root = path.lstat()
    root_device = root.st_dev
    digest = hashlib.sha256()
    seen_files: dict[tuple[int, int], str] = {}
    counts = {"files": 0, "directories": 0, "symlinks": 0}
    logical_bytes = 0
    allocated_bytes = 0

    def visit(current: Path, relative: str) -> None:
        nonlocal logical_bytes, allocated_bytes
        metadata = current.lstat()
        kind = _entry_kind(metadata.st_mode)
        if metadata.st_dev != root_device:
            raise ValueError(f"源路径包含跨文件系统条目：{relative or '.'}")
        hardlink = ""
        if kind == "file":
            counts["files"] += 1
            identity = (metadata.st_dev, metadata.st_ino)
            hardlink = seen_files.setdefault(identity, relative)
            if hardlink == relative:
                logical_bytes += metadata.st_size
                allocated_bytes += metadata.st_blocks * 512
        elif kind == "directory":
            counts["directories"] += 1
        else:
            counts["symlinks"] += 1
        target = os.readlink(current) if kind == "symlink" else ""
        xattrs = tuple(
            (
                name,
                hashlib.sha256(os.getxattr(current, name, follow_symlinks=False)).hexdigest(),
            )
            for name in sorted(os.listxattr(current, follow_symlinks=False))
        )
        record = (
            relative,
            kind,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_size if kind != "directory" else 0,
            metadata.st_mtime_ns if kind == "file" else 0,
            hardlink,
            target,
            xattrs,
        )
        digest.update(repr(record).encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        if kind == "directory":
            with os.scandir(current) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    child_relative = f"{relative}/{entry.name}" if relative else entry.name
                    visit(Path(entry.path), child_relative)

    visit(path, "")
    return {
        **counts,
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "device": root.st_dev,
        "inode": root.st_ino,
        "kind": _entry_kind(root.st_mode),
        "metadata_digest": digest.hexdigest(),
    }


def file_transfer_preflight(source_raw: str, target_directory_raw: str) -> dict[str, Any]:
    """验证迁移边界、目标冲突及空间/inode，并返回执行绑定事实。"""

    source = _absolute_path(source_raw, "源")
    target_directory = _absolute_path(target_directory_raw, "目标目录")
    _reject_critical(source, "源路径")
    _reject_critical(target_directory, "目标目录")
    _existing_ancestors_are_not_symlinks(source)
    _existing_ancestors_are_not_symlinks(target_directory)
    source_metadata = source.lstat()
    if stat.S_ISLNK(source_metadata.st_mode):
        raise ValueError("源路径本身不能是符号链接")
    if not target_directory.is_dir():
        raise ValueError("目标目录不存在或不是目录")
    target_metadata = target_directory.stat()
    if target_metadata.st_uid != TRUSTED_TARGET_UID or target_metadata.st_mode & 0o022:
        raise ValueError("目标目录必须由 root 所有且不可由组或其他用户写入")
    _target_ancestors_are_trusted(target_directory)
    if source == target_directory or target_directory.is_relative_to(source):
        raise ValueError("目标目录不能位于源路径内部")
    destination = target_directory / source.name
    if destination.exists() or destination.is_symlink():
        raise ValueError("目标路径已存在")
    summary = _tree_summary(source)
    filesystem = os.statvfs(target_directory)
    available_bytes = filesystem.f_bavail * filesystem.f_frsize
    available_inodes = filesystem.f_favail
    required_inodes = summary["files"] + summary["directories"] + summary["symlinks"]
    required_bytes = max(summary["logical_bytes"], summary["allocated_bytes"])
    if available_bytes < required_bytes:
        raise ValueError("目标文件系统可用空间不足")
    if available_inodes and available_inodes < required_inodes:
        raise ValueError("目标文件系统可用 inode 不足")
    return {
        "source": str(source),
        "target_directory": str(target_directory),
        "destination": str(destination),
        "source_summary": summary,
        "available_bytes": available_bytes,
        "available_inodes": available_inodes,
        "required_bytes": required_bytes,
        "required_inodes": required_inodes,
    }


def execute_file_transfer(
    source_raw: str,
    target_directory_raw: str,
    operation_id: str,
    expected_device: int,
    expected_inode: int,
) -> dict[str, Any]:
    """复制至目标临时目录，校验元数据后原子发布；不删除源路径。"""

    if len(operation_id) != 32 or any(value not in "0123456789abcdef" for value in operation_id):
        raise ValueError("操作 ID 无效")
    preview = file_transfer_preflight(source_raw, target_directory_raw)
    source_summary = preview["source_summary"]
    if (source_summary["device"], source_summary["inode"]) != (expected_device, expected_inode):
        raise ValueError("源路径身份已变化，请重新预检")
    source = Path(preview["source"])
    target_directory = Path(preview["target_directory"])
    staging = target_directory / f".labopspanel-transfer-{operation_id}"
    destination = Path(preview["destination"])
    if staging.exists() or staging.is_symlink():
        raise ValueError("目标临时路径已存在")
    staging.mkdir(mode=0o700)
    try:
        subprocess.run(
            [
                str(RSYNC),
                "--archive",
                "--hard-links",
                "--acls",
                "--xattrs",
                "--sparse",
                "--one-file-system",
                "--numeric-ids",
                "--protect-args",
                "--",
                str(source),
                str(staging),
            ],
            check=True,
            capture_output=True,
            timeout=3600,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        copied = staging / source.name
        destination_summary = _tree_summary(copied)
        source_after_copy = _tree_summary(source)
        comparable = ("files", "directories", "symlinks", "logical_bytes", "metadata_digest")
        if any(
            source_summary[key] != source_after_copy[key]
            or source_summary[key] != destination_summary[key]
            for key in comparable
        ):
            raise RuntimeError("复制后文件数量、大小或关键元数据校验失败")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("发布前目标路径出现冲突")
        os.rename(copied, destination)
        staging.rmdir()
        return {
            "source": str(source),
            "destination": str(destination),
            "source_device": source_summary["device"],
            "source_inode": source_summary["inode"],
            "summary": {key: source_summary[key] for key in comparable},
            "source_retained": True,
        }
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise


def _delete_directory_contents(descriptor: int, expected_device: int) -> None:
    """通过目录文件描述符递归清理，不跟随符号链接且不跨设备。"""

    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if metadata.st_dev != expected_device:
            raise RuntimeError("源清理遇到跨文件系统条目")
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                _delete_directory_contents(child, expected_device)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def cleanup_file_transfer_source(
    source_raw: str,
    destination_raw: str,
    operation_id: str,
    expected_device: int,
    expected_inode: int,
) -> dict[str, Any]:
    """再次校验源和目标后隔离并清理源；必须作为独立人工确认动作调用。"""

    source = _absolute_path(source_raw, "源")
    destination = _absolute_path(destination_raw, "目标")
    _reject_critical(source, "源路径")
    _reject_critical(destination, "目标路径")
    _existing_ancestors_are_not_symlinks(source)
    _existing_ancestors_are_not_symlinks(destination)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("源或目标路径本身不能是符号链接")
    source_summary = _tree_summary(source)
    destination_summary = _tree_summary(destination)
    if (source_summary["device"], source_summary["inode"]) != (expected_device, expected_inode):
        raise ValueError("源路径身份已变化，拒绝清理")
    comparable = ("files", "directories", "symlinks", "logical_bytes", "metadata_digest")
    if any(source_summary[key] != destination_summary[key] for key in comparable):
        raise ValueError("源与目标当前校验不一致，拒绝清理")
    parent = source.parent
    isolated_name = f".labopspanel-cleanup-{operation_id}"
    parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.path.lexists(parent / isolated_name):
            raise ValueError("源清理隔离路径已存在")
        os.rename(
            source.name,
            isolated_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        isolated = os.stat(isolated_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (isolated.st_dev, isolated.st_ino) != (expected_device, expected_inode):
            os.rename(
                isolated_name,
                source.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            raise RuntimeError("隔离后的源身份校验失败")
        if stat.S_ISDIR(isolated.st_mode):
            descriptor = os.open(
                isolated_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
            try:
                _delete_directory_contents(descriptor, expected_device)
            finally:
                os.close(descriptor)
            os.rmdir(isolated_name, dir_fd=parent_descriptor)
        else:
            os.unlink(isolated_name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return {"source": str(source), "destination": str(destination), "source_removed": True}
