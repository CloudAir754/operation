"""一次性初始凭据文件的安全导出。"""

from __future__ import annotations

import csv
import os
import stat
import uuid
from pathlib import Path


def export_initial_credentials(
    data_directory: Path, task_id: str, rows: list[tuple[str, str, str]]
) -> Path:
    """原子创建仅启动用户可读的 CSV，明文只写该临时文件。"""

    if not task_id.isalnum() or len(task_id) > 64:
        raise ValueError("任务 ID 无效")
    export_directory = data_directory / "credential-exports"
    if export_directory.is_symlink():
        raise ValueError("凭据导出目录不能是符号链接")
    export_directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(export_directory, 0o700)
    destination = export_directory / f"initial-credentials-{task_id}.csv"
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("该任务的凭据文件已经存在")
    temporary = export_directory / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["username", "中文姓名", "初始密码"])
            writer.writerows(rows)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def initial_credentials_path(data_directory: Path, task_id: str) -> Path:
    """解析并验证凭据文件，拒绝符号链接、异常所有者和宽松权限。"""

    if not task_id.isalnum() or len(task_id) > 64:
        raise ValueError("任务 ID 无效")
    path = data_directory / "credential-exports" / f"initial-credentials-{task_id}.csv"
    if path.is_symlink():
        raise ValueError("凭据文件不能是符号链接")
    try:
        metadata = path.stat()
    except FileNotFoundError:
        raise FileNotFoundError("凭据文件不存在或已删除") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("凭据文件所有者或权限不安全")
    return path


def delete_initial_credentials(data_directory: Path, task_id: str) -> None:
    """只删除由任务 ID 精确定位且通过安全校验的凭据文件。"""

    initial_credentials_path(data_directory, task_id).unlink()
