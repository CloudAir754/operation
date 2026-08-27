"""pwquality 主配置的备份、保守修改、结果验证与人工恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lab_ops_panel.infrastructure.pam import KNOWN_SETTINGS, pam_policy_snapshot

CONFIG_PATH = Path("/etc/security/pwquality.conf")
BACKUP_DIRECTORY = Path("/var/lib/labopspanel/pam-backups")
MANAGED_MARKER = "# 由 LabOpsPanel 更新；修改前已保存可验证备份"
VALUE_RANGES = {
    "minlen": (15, 128),
    "dcredit": (-20, 0),
    "ucredit": (-20, 0),
    "lcredit": (-20, 0),
    "ocredit": (-20, 0),
    "minclass": (0, 4),
    "maxrepeat": (0, 128),
    "maxsequence": (0, 128),
    "maxclassrepeat": (0, 128),
    "retry": (1, 10),
    "difok": (0, 128),
}


def validate_policy_changes(changes: dict[str, int | bool]) -> dict[str, int | bool]:
    """只接受首版能够明确解释且不低于安全基线的 pwquality 值。"""

    if not changes:
        raise ValueError("至少选择一项 PAM 策略修改")
    if set(changes) - KNOWN_SETTINGS:
        raise ValueError("包含不支持的 PAM 策略字段")
    validated: dict[str, int | bool] = {}
    for key, value in changes.items():
        if key == "usercheck":
            if not isinstance(value, bool):
                raise ValueError("usercheck 必须是布尔值")
            validated[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} 必须是整数")
        minimum, maximum = VALUE_RANGES[key]
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
        validated[key] = value
    return validated


def _value_text(value: int | bool) -> str:
    return "1" if value is True else "0" if value is False else str(value)


def render_policy_update(content: str, changes: dict[str, int | bool]) -> str:
    """保留注释和顺序修改已知唯一键；未知、重复或异常行直接拒绝。"""

    validated = validate_policy_changes(changes)
    seen: set[str] = set()
    rendered: list[str] = []
    for number, raw_line in enumerate(content.splitlines(), 1):
        body, separator, comment = raw_line.partition("#")
        stripped = body.strip()
        if not stripped:
            rendered.append(raw_line)
            continue
        match = re.fullmatch(r"([A-Za-z0-9_]+)\s*=\s*(\S+)\s*", stripped)
        if match is None:
            raise ValueError(f"pwquality.conf 第 {number} 行无法安全理解")
        key = match.group(1).lower()
        if key not in KNOWN_SETTINGS:
            raise ValueError(f"pwquality.conf 第 {number} 行包含未知设置 {key}")
        if key in seen:
            raise ValueError(f"pwquality.conf 中 {key} 重复定义")
        seen.add(key)
        if key not in validated:
            rendered.append(raw_line)
            continue
        suffix = f"  # {comment.strip()}" if separator and comment.strip() else ""
        rendered.append(f"{key} = {_value_text(validated[key])}{suffix}")
    missing = [key for key in validated if key not in seen]
    if missing:
        if rendered and rendered[-1]:
            rendered.append("")
        if MANAGED_MARKER not in rendered:
            rendered.append(MANAGED_MARKER)
        rendered.extend(f"{key} = {_value_text(validated[key])}" for key in missing)
    return "\n".join(rendered).rstrip() + "\n"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _assert_directory(path: Path, owner_uid: int, mode: int, enforce_mode: bool = False) -> None:
    """创建或验证不可由非所有者改写的非符号链接目录。"""

    existed = path.exists()
    path.mkdir(parents=True, mode=mode, exist_ok=True)
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_mode & 0o022
    ):
        raise ValueError(f"目录 {path} 的类型、所有者或权限不安全")
    if enforce_mode or not existed:
        os.chmod(path, mode)


def _assert_trusted_ancestors(path: Path, owner_uid: int) -> None:
    """验证绝对路径的全部既有父级均无符号链接且不可被非所有者改写。"""

    if not path.is_absolute():
        raise ValueError("PAM 配置与备份路径必须是绝对路径")
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_mode & 0o022
        ):
            raise ValueError(f"路径父级 {current} 的类型、所有者或权限不安全")


def _read_config(path: Path, owner_uid: int) -> tuple[bytes, bool, int]:
    """不跟随符号链接读取主配置，并返回是否原先存在及原 mode。"""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b"", False, 0o644
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_mode & 0o022
    ):
        raise ValueError("pwquality.conf 的类型、所有者或权限不安全")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read(), True, stat.S_IMODE(metadata.st_mode)


def _atomic_write(path: Path, content: bytes, owner_uid: int, mode: int) -> None:
    """在目标目录内创建受限临时文件并原子替换。"""

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchown(stream.fileno(), owner_uid, owner_uid)
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _delete_and_sync(path: Path) -> None:
    """删除精确文件并同步父目录元数据。"""

    path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _snapshot() -> dict[str, Any]:
    return pam_policy_snapshot()


def apply_policy_changes(
    changes: dict[str, int | bool],
    config_path: Path = CONFIG_PATH,
    backup_directory: Path = BACKUP_DIRECTORY,
    snapshot_reader: Callable[[], dict[str, Any]] = _snapshot,
    owner_uid: int = 0,
    require_root: bool = True,
) -> dict[str, Any]:
    """备份后修改配置，核对实际生效值；任一步失败即恢复原文件。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("PAM 策略修改只能由受限特权助手执行")
    validated = validate_policy_changes(changes)
    if require_root:
        _assert_trusted_ancestors(config_path.parent, owner_uid)
        _assert_trusted_ancestors(backup_directory.parent, owner_uid)
    _assert_directory(config_path.parent, owner_uid, 0o755)
    _assert_directory(backup_directory, owner_uid, 0o700, enforce_mode=True)
    before = snapshot_reader()
    if not before.get("modification_supported"):
        raise ValueError(str(before.get("modification_reason") or "PAM 配置不可安全修改"))
    conflicts = set(validated) & set(before.get("module_settings", {}))
    if conflicts:
        raise ValueError(f"PAM 模块参数覆盖了待修改字段：{', '.join(sorted(conflicts))}")
    original, existed, original_mode = _read_config(config_path, owner_uid)
    try:
        current_text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("pwquality.conf 不是 UTF-8 文本") from exc
    updated = render_policy_update(current_text, validated).encode()
    if updated == original:
        return {"changed": False, "backup_id": None, "settings": before["settings"]}

    backup_id = uuid.uuid4().hex
    backup_path = backup_directory / f"{backup_id}.conf"
    manifest_path = backup_directory / f"{backup_id}.json"
    backup_collision = backup_path.exists() or backup_path.is_symlink()
    manifest_collision = manifest_path.exists() or manifest_path.is_symlink()
    if backup_collision or manifest_collision:
        raise RuntimeError("PAM 备份 ID 冲突")
    _atomic_write(backup_path, original, owner_uid, 0o600)
    manifest = {
        "version": 1,
        "config_path": str(config_path),
        "original_existed": existed,
        "original_mode": original_mode,
        "original_sha256": _digest(original),
        "applied_sha256": _digest(updated),
        "changes": validated,
        "before_settings": {key: before.get("settings", {}).get(key) for key in validated},
    }
    _atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(),
        owner_uid,
        0o600,
    )
    try:
        _atomic_write(config_path, updated, owner_uid, original_mode)
        after = snapshot_reader()
        mismatches = {
            key: value
            for key, value in validated.items()
            if after.get("settings", {}).get(key) != value
        }
        if mismatches or not after.get("modification_supported"):
            raise RuntimeError("PAM 策略写入后的实际生效值验证失败")
        return {
            "changed": True,
            "backup_id": backup_id,
            "settings": {key: after["settings"][key] for key in validated},
        }
    except Exception:
        if existed:
            _atomic_write(config_path, original, owner_uid, original_mode)
        else:
            _delete_and_sync(config_path)
        _delete_and_sync(backup_path)
        _delete_and_sync(manifest_path)
        raise


def restore_policy_backup(
    backup_id: str,
    config_path: Path = CONFIG_PATH,
    backup_directory: Path = BACKUP_DIRECTORY,
    snapshot_reader: Callable[[], dict[str, Any]] = _snapshot,
    owner_uid: int = 0,
    require_root: bool = True,
) -> dict[str, Any]:
    """只在当前文件仍等于该次应用结果时恢复，避免覆盖后续人工修改。"""

    if require_root and os.geteuid() != 0:
        raise PermissionError("PAM 策略恢复只能由受限特权助手执行")
    if not re.fullmatch(r"[0-9a-f]{32}", backup_id):
        raise ValueError("PAM 备份 ID 无效")
    if require_root:
        _assert_trusted_ancestors(config_path.parent, owner_uid)
        _assert_trusted_ancestors(backup_directory.parent, owner_uid)
    _assert_directory(backup_directory, owner_uid, 0o700, enforce_mode=True)
    manifest_bytes, _, _ = _read_config(backup_directory / f"{backup_id}.json", owner_uid)
    backup, _, _ = _read_config(backup_directory / f"{backup_id}.conf", owner_uid)
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("PAM 备份清单损坏") from exc
    if not isinstance(manifest, dict):
        raise ValueError("PAM 备份清单不是对象")
    if manifest.get("config_path") != str(config_path):
        raise ValueError("PAM 备份目标路径不匹配")
    if _digest(backup) != manifest.get("original_sha256"):
        raise ValueError("PAM 备份内容校验失败")
    current, current_existed, current_mode = _read_config(config_path, owner_uid)
    if not current_existed or _digest(current) != manifest.get("applied_sha256"):
        raise ValueError("当前 PAM 配置已变化，拒绝覆盖后续人工修改")
    before_settings = manifest.get("before_settings")
    changes = manifest.get("changes")
    if not isinstance(before_settings, dict) or not isinstance(changes, dict):
        raise ValueError("PAM 备份清单缺少策略摘要")
    if manifest.get("original_existed"):
        mode = int(manifest.get("original_mode", 0o644))
        if mode < 0 or mode > 0o777 or mode & 0o022:
            raise ValueError("PAM 备份记录的原权限不安全")
        _atomic_write(config_path, backup, owner_uid, mode)
    else:
        _delete_and_sync(config_path)
    after = snapshot_reader()
    mismatches = {
        key: expected
        for key, expected in before_settings.items()
        if after.get("settings", {}).get(key) != expected
    }
    if mismatches or not after.get("modification_supported"):
        _atomic_write(config_path, current, owner_uid, current_mode)
        raise RuntimeError("PAM 策略恢复后的实际生效值验证失败，已重新应用恢复前配置")
    return {"restored": True, "backup_id": backup_id, "settings": after.get("settings", {})}
