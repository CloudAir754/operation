"""home 目录迁移的 root 侧可恢复状态机。"""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from lab_ops_panel.domain.user_imports import USERNAME
from lab_ops_panel.infrastructure.file_transfer import (
    RSYNC,
    _delete_directory_contents,
    _existing_ancestors_are_not_symlinks,
    _reject_critical,
    _target_ancestors_are_trusted,
    _tree_summary,
)
from lab_ops_panel.infrastructure.processes import terminate_uid_processes

DEFAULT_JOURNAL_DIRECTORY = Path("/var/lib/labopspanel/home-migrations")
DEFAULT_PASSWD = Path("/etc/passwd")
DEFAULT_MOUNTINFO = Path("/proc/self/mountinfo")
USERMOD = Path("/usr/sbin/usermod")
PASSWD = Path("/usr/bin/passwd")
LOGINCTL = Path("/usr/bin/loginctl")
RUNUSER = Path("/usr/sbin/runuser")
PYTHON = Path("/usr/bin/python3")
NOLOGIN = "/usr/sbin/nologin"
TERMINAL_PHASES = frozenset({"observing", "rolled_back", "cleaned"})


class CommandRunner(Protocol):
    """固定系统命令执行边界，便于故障注入测试。"""

    def __call__(
        self, command: list[str], timeout: int = 120, check: bool = True
    ) -> bytes | None: ...


def _run(command: list[str], timeout: int = 120, check: bool = True) -> bytes:
    """以固定环境执行结构化命令，不传播可能含用户内容的输出。"""

    try:
        completed = subprocess.run(
            command,
            check=check,
            capture_output=True,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"系统动作 {Path(command[0]).name} 失败") from exc
    return completed.stdout


def _local_account(username: str, passwd_path: Path = DEFAULT_PASSWD) -> pwd.struct_passwd:
    """只读取本机 passwd 文件并返回稳定 UID/GID/home。"""

    if not USERNAME.fullmatch(username):
        raise ValueError("用户名格式无效")
    try:
        lines = passwd_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("无法读取本机账号来源") from exc
    for line in lines:
        fields = line.split(":")
        if len(fields) == 7 and fields[0] == username:
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
    raise ValueError("本机账号不存在")


def _unescape_mountinfo(value: str) -> str:
    """解码 mountinfo 对空格、制表符、换行和反斜杠的八进制转义。"""

    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _mount_entries(path: Path = DEFAULT_MOUNTINFO) -> list[dict[str, str]]:
    """解析内核 mountinfo 中迁移判定所需的挂载点和文件系统类型。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("无法读取挂载信息") from exc
    entries: list[dict[str, str]] = []
    for line in lines:
        left, separator, right = line.partition(" - ")
        left_fields = left.split()
        right_fields = right.split()
        if not separator or len(left_fields) < 6 or len(right_fields) < 2:
            raise ValueError("挂载信息格式无法安全解析")
        entries.append(
            {
                "mountpoint": _unescape_mountinfo(left_fields[4]),
                "options": left_fields[5],
                "filesystem": right_fields[0],
                "source": _unescape_mountinfo(right_fields[1]),
            }
        )
    return entries


def _mount_for(path: Path, entries: list[dict[str, str]]) -> dict[str, str]:
    """以最长路径前缀定位覆盖指定目录的挂载。"""

    matches = [
        entry
        for entry in entries
        if path == Path(entry["mountpoint"]) or path.is_relative_to(Path(entry["mountpoint"]))
    ]
    if not matches:
        raise ValueError(f"无法识别路径所在挂载点：{path}")
    return max(matches, key=lambda item: len(Path(item["mountpoint"]).parts))


def _journal_path(migration_id: str, directory: Path) -> Path:
    """把迁移 ID 映射到固定目录内的 JSON 日志。"""

    if len(migration_id) != 32 or any(value not in "0123456789abcdef" for value in migration_id):
        raise ValueError("迁移 ID 无效")
    return directory / f"{migration_id}.json"


def _prepare_journal_directory(directory: Path) -> None:
    """创建并验证 root 私有状态目录，不接受符号链接或可写父链。"""

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _existing_ancestors_are_not_symlinks(directory)
    _target_ancestors_are_trusted(directory)
    if directory.is_symlink():
        raise ValueError("home 迁移状态目录不能是符号链接")
    metadata = directory.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError("home 迁移状态目录所有者或权限不安全")


def _write_journal(journal: dict[str, Any], directory: Path) -> None:
    """以同目录临时文件、fsync 和原子替换持久化迁移阶段。"""

    _prepare_journal_directory(directory)
    path = _journal_path(str(journal["migration_id"]), directory)
    temporary = path.with_suffix(".tmp")
    if temporary.exists() and not temporary.is_symlink():
        temporary_metadata = temporary.stat()
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_uid != os.geteuid()
            or temporary_metadata.st_mode & 0o077
        ):
            raise ValueError("home 迁移临时状态文件不安全")
        temporary.unlink()
    encoded = json.dumps(journal, ensure_ascii=False, sort_keys=True).encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("home 迁移状态写入不完整")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _read_journal(migration_id: str, directory: Path) -> dict[str, Any]:
    """读取权限安全且结构绑定迁移 ID 的状态日志。"""

    _prepare_journal_directory(directory)
    path = _journal_path(migration_id, directory)
    if path.is_symlink():
        raise ValueError("home 迁移状态文件不能是符号链接")
    try:
        metadata = path.stat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("home 迁移状态不存在或损坏") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or not isinstance(value, dict)
        or value.get("migration_id") != migration_id
    ):
        raise ValueError("home 迁移状态文件不安全")
    return value


def _set_phase(journal: dict[str, Any], phase: str, directory: Path) -> None:
    """在执行下一项不可逆变化前后持久化明确阶段。"""

    journal["phase"] = phase
    journal["updated_at"] = datetime.now(UTC).isoformat()
    _write_journal(journal, directory)


def _password_status(username: str) -> str:
    """读取 passwd -S 的单字符状态，不返回或持久化密码哈希。"""

    try:
        completed = subprocess.run(
            [str(PASSWD), "--status", username],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("无法读取账号密码状态") from exc
    fields = completed.stdout.split()
    if len(fields) < 2 or fields[1] not in {"P", "L", "NP"}:
        raise RuntimeError("账号密码状态无法安全识别")
    return fields[1]


def home_migration_preflight(
    username: str,
    expected_uid: int,
    target_parent_raw: str,
    migration_id: str,
    safety_margin_percent: int = 10,
    *,
    journal_directory: Path = DEFAULT_JOURNAL_DIRECTORY,
    passwd_path: Path = DEFAULT_PASSWD,
    mountinfo_path: Path = DEFAULT_MOUNTINFO,
    require_different_device: bool = True,
    trusted_system_uid: int = 0,
    verify_contents: bool = False,
) -> dict[str, Any]:
    """验证 ext4、身份、路径、空间和 inode，并创建可恢复迁移日志。"""

    if not 5 <= safety_margin_percent <= 100:
        raise ValueError("安全余量比例必须在 5% 到 100% 之间")
    account = _local_account(username, passwd_path)
    if account.pw_uid != expected_uid or expected_uid == 0:
        raise ValueError("账号 UID 已变化或不允许迁移 root")
    source = Path(account.pw_dir)
    if not source.is_absolute() or source == Path("/"):
        raise ValueError("home 路径无效")
    _existing_ancestors_are_not_symlinks(source)
    source_metadata = source.lstat()
    if not stat.S_ISDIR(source_metadata.st_mode) or source_metadata.st_uid != expected_uid:
        raise ValueError("home 必须是目标 UID 所有的普通目录")
    source_parent = source.parent
    _target_ancestors_are_trusted(source_parent)
    parent_metadata = source_parent.stat()
    if parent_metadata.st_uid != trusted_system_uid or parent_metadata.st_mode & 0o022:
        raise ValueError("home 父目录必须由 root 所有且不可被其他用户改写")

    target_parent = Path(target_parent_raw)
    if (
        not target_parent.is_absolute()
        or target_parent == Path("/")
        or target_parent != Path(os.path.normpath(target_parent_raw))
    ):
        raise ValueError("目标父目录必须是规范绝对路径且不能为根目录")
    _existing_ancestors_are_not_symlinks(target_parent)
    _reject_critical(target_parent, "目标父目录")
    if not target_parent.is_dir():
        raise ValueError("目标父目录不存在")
    target_metadata = target_parent.stat()
    if target_metadata.st_uid != trusted_system_uid or target_metadata.st_mode & 0o022:
        raise ValueError("目标父目录必须由 root 所有且不可被组或其他用户写入")
    _target_ancestors_are_trusted(target_parent)
    destination = target_parent / username
    if destination.exists() or destination.is_symlink():
        raise ValueError("目标 home 已存在")
    if target_parent == source or target_parent.is_relative_to(source):
        raise ValueError("目标父目录不能位于源 home 内部")

    mounts = _mount_entries(mountinfo_path)
    source_mount = _mount_for(source, mounts)
    target_mount = _mount_for(target_parent, mounts)
    if source_mount["filesystem"] != "ext4" or target_mount["filesystem"] != "ext4":
        raise ValueError("首版 home 迁移只支持本地 ext4 到 ext4")
    nested_mounts = [
        entry["mountpoint"]
        for entry in mounts
        if Path(entry["mountpoint"]) != source and Path(entry["mountpoint"]).is_relative_to(source)
    ]
    if nested_mounts:
        raise ValueError("home 内存在子挂载，拒绝迁移")
    if "ro" in target_mount["options"].split(","):
        raise ValueError("目标文件系统为只读")
    if require_different_device and source_metadata.st_dev == target_metadata.st_dev:
        raise ValueError("源与目标位于同一文件系统，无需执行 home 跨盘迁移")

    summary = _tree_summary(source)
    filesystem = os.statvfs(target_parent)
    available_bytes = filesystem.f_bavail * filesystem.f_frsize
    available_inodes = filesystem.f_favail
    required_inodes = summary["files"] + summary["directories"] + summary["symlinks"]
    base_bytes = max(summary["logical_bytes"], summary["allocated_bytes"])
    safety_margin_bytes = max(1024**3, base_bytes * safety_margin_percent // 100)
    if available_bytes < base_bytes + safety_margin_bytes:
        raise ValueError("目标空间不足以容纳数据和安全余量")
    if available_inodes and available_inodes < required_inodes:
        raise ValueError("目标 inode 不足")

    backup = source_parent / f".labopspanel-home-backup-{migration_id}"
    temporary = target_parent / f".labopspanel-home-{migration_id}"
    if backup.exists() or backup.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise ValueError("迁移备份或目标临时路径冲突")
    journal = {
        "migration_id": migration_id,
        "phase": "preflighted",
        "username": username,
        "uid": account.pw_uid,
        "gid": account.pw_gid,
        "original_shell": account.pw_shell,
        "source": str(source),
        "source_device": source_metadata.st_dev,
        "source_inode": source_metadata.st_ino,
        "source_parent_device": parent_metadata.st_dev,
        "source_parent_inode": parent_metadata.st_ino,
        "source_summary": summary,
        "source_mount": source_mount,
        "target_mount": target_mount,
        "target_parent": str(target_parent),
        "target_parent_device": target_metadata.st_dev,
        "target_parent_inode": target_metadata.st_ino,
        "destination": str(destination),
        "temporary": str(temporary),
        "backup": str(backup),
        "available_bytes": available_bytes,
        "available_inodes": available_inodes,
        "required_bytes": base_bytes,
        "required_inodes": required_inodes,
        "safety_margin_bytes": safety_margin_bytes,
        "verify_contents": verify_contents,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _prepare_journal_directory(journal_directory)
    existing_path = _journal_path(migration_id, journal_directory)
    if existing_path.exists():
        existing = _read_journal(migration_id, journal_directory)
        identity_fields = ("username", "uid", "source", "target_parent")
        if any(existing.get(field) != journal[field] for field in identity_fields):
            raise ValueError("迁移 ID 已绑定不同参数")
        return _public_status(existing)
    _write_journal(journal, journal_directory)
    return _public_status(journal)


def _assert_journal_identity(journal: dict[str, Any], passwd_path: Path) -> pwd.struct_passwd:
    """每次恢复或执行前重新核对用户名、UID 和逻辑 home。"""

    account = _local_account(str(journal["username"]), passwd_path)
    if account.pw_uid != journal["uid"] or account.pw_dir != journal["source"]:
        raise ValueError("账号 UID 或逻辑 home 已变化")
    return account


def _assert_path_identities(journal: dict[str, Any]) -> None:
    """阻止预检后 home 父目录或目标父目录被替换、重新挂载或链接逃逸。"""

    source_parent = Path(str(journal["source"])).parent
    target_parent = Path(str(journal["target_parent"]))
    _existing_ancestors_are_not_symlinks(source_parent)
    _existing_ancestors_are_not_symlinks(target_parent)
    source_parent_metadata = source_parent.stat()
    target_parent_metadata = target_parent.stat()
    if (source_parent_metadata.st_dev, source_parent_metadata.st_ino) != (
        journal["source_parent_device"],
        journal["source_parent_inode"],
    ):
        raise ValueError("home 父目录身份已变化")
    if (target_parent_metadata.st_dev, target_parent_metadata.st_ino) != (
        journal["target_parent_device"],
        journal["target_parent_inode"],
    ):
        raise ValueError("目标父目录身份已变化")


def _freeze_account(
    journal: dict[str, Any], requester_pid: int, runner: CommandRunner, journal_directory: Path
) -> None:
    """记录原状态后锁定登录、终止会话和 UID 全部进程。"""

    username = str(journal["username"])
    if "original_password_status" not in journal:
        journal["original_password_status"] = _password_status(username)
        _set_phase(journal, "freezing", journal_directory)
    runner([str(PASSWD), "--lock", username])
    runner([str(USERMOD), "--shell", NOLOGIN, "--", username])
    runner([str(LOGINCTL), "terminate-user", username], check=False)
    terminate_uid_processes(int(journal["uid"]), requester_pid, 5)
    _set_phase(journal, "frozen", journal_directory)


def _restore_account(journal: dict[str, Any], runner: CommandRunner) -> None:
    """恢复迁移前 shell，并仅在原状态可登录时解锁密码。"""

    username = str(journal["username"])
    runner([str(USERMOD), "--shell", str(journal["original_shell"]), "--", username])
    if journal.get("original_password_status") == "P":
        runner([str(PASSWD), "--unlock", username])


def _copy_and_verify(journal: dict[str, Any], runner: CommandRunner) -> None:
    """从稳定备份复制到目标临时目录并比较完整元数据摘要。"""

    backup = Path(str(journal["backup"]))
    temporary = Path(str(journal["temporary"]))
    if not temporary.exists():
        temporary.mkdir(mode=0o700)
    runner(
        [
            str(RSYNC),
            "--archive",
            "--hard-links",
            "--acls",
            "--xattrs",
            "--sparse",
            "--one-file-system",
            "--numeric-ids",
            "--delete",
            "--protect-args",
            "--",
            f"{backup}/",
            f"{temporary}/",
        ],
        timeout=7200,
    )
    source_summary = _tree_summary(backup)
    target_summary = _tree_summary(temporary)
    comparable = ("files", "directories", "symlinks", "logical_bytes", "metadata_digest")
    if any(source_summary[key] != target_summary[key] for key in comparable):
        raise RuntimeError("home 复制后数量、大小或关键元数据校验失败")
    journal["verified_summary"] = {key: source_summary[key] for key in comparable}
    if journal.get("verify_contents"):
        differences = runner(
            [
                str(RSYNC),
                "--archive",
                "--hard-links",
                "--acls",
                "--xattrs",
                "--sparse",
                "--one-file-system",
                "--numeric-ids",
                "--delete",
                "--checksum",
                "--dry-run",
                "--itemize-changes",
                "--protect-args",
                "--",
                f"{backup}/",
                f"{temporary}/",
            ],
            timeout=7200,
        )
        if differences:
            raise RuntimeError("home 全量内容校验失败")


def _trial_as_user(journal: dict[str, Any], runner: CommandRunner) -> None:
    """以目标用户身份在逻辑 home 完成创建、读写和删除试验。"""

    source = str(journal["source"])
    marker = f".labopspanel-migration-test-{journal['migration_id']}"
    program = (
        "from pathlib import Path; import sys; "
        "p=Path(sys.argv[1]); p.write_text('ok', encoding='utf-8'); "
        "assert p.read_text(encoding='utf-8') == 'ok'; p.unlink()"
    )
    runner(
        [
            str(RUNUSER),
            "--user",
            str(journal["username"]),
            "--",
            str(PYTHON),
            "-I",
            "-c",
            program,
            str(Path(source) / marker),
        ]
    )


def execute_home_migration(
    migration_id: str,
    requester_pid: int,
    *,
    journal_directory: Path = DEFAULT_JOURNAL_DIRECTORY,
    passwd_path: Path = DEFAULT_PASSWD,
    runner: CommandRunner = _run,
) -> dict[str, Any]:
    """从任一已持久化中间阶段幂等继续，成功后进入备份观察期。"""

    journal = _read_journal(migration_id, journal_directory)
    if journal["phase"] in {"rolled_back", "cleaned"}:
        raise ValueError("迁移已经回退或完成清理，不能继续")
    if journal["phase"] == "observing":
        return _public_status(journal)
    _assert_journal_identity(journal, passwd_path)
    _assert_path_identities(journal)
    source = Path(str(journal["source"]))
    backup = Path(str(journal["backup"]))
    temporary = Path(str(journal["temporary"]))
    destination = Path(str(journal["destination"]))
    try:
        if journal["phase"] in {"preflighted", "freezing"}:
            _freeze_account(journal, requester_pid, runner, journal_directory)
        if journal["phase"] == "frozen":
            _set_phase(journal, "renaming_source", journal_directory)
        if journal["phase"] == "renaming_source":
            if source.exists() and not backup.exists():
                metadata = source.lstat()
                if (metadata.st_dev, metadata.st_ino) != (
                    journal["source_device"],
                    journal["source_inode"],
                ):
                    raise ValueError("改名前 home 身份已变化")
                os.rename(source, backup)
            if source.exists() or not backup.is_dir() or backup.is_symlink():
                raise RuntimeError("无法确认 home 原地改名结果")
            _set_phase(journal, "source_renamed", journal_directory)
        if journal["phase"] in {"source_renamed", "copying"}:
            _set_phase(journal, "copying", journal_directory)
            _copy_and_verify(journal, runner)
            _set_phase(journal, "copied", journal_directory)
        if journal["phase"] in {"copied", "publishing_target"}:
            _set_phase(journal, "publishing_target", journal_directory)
            if temporary.exists() and not destination.exists():
                os.rename(temporary, destination)
            if temporary.exists() or not destination.is_dir() or destination.is_symlink():
                raise RuntimeError("无法确认目标 home 发布结果")
            _set_phase(journal, "target_published", journal_directory)
        if journal["phase"] in {"target_published", "linking"}:
            _set_phase(journal, "linking", journal_directory)
            if not source.exists() and not source.is_symlink():
                link_temporary = source.parent / f".labopspanel-home-link-{migration_id}"
                if link_temporary.exists() or link_temporary.is_symlink():
                    link_temporary.unlink()
                link_temporary.symlink_to(destination, target_is_directory=True)
                os.rename(link_temporary, source)
            if not source.is_symlink() or source.resolve(strict=True) != destination:
                raise RuntimeError("逻辑 home 符号链接验证失败")
            _set_phase(journal, "linked", journal_directory)
        if journal["phase"] in {"linked", "restoring_account", "testing"}:
            _set_phase(journal, "restoring_account", journal_directory)
            _restore_account(journal, runner)
            _set_phase(journal, "testing", journal_directory)
            _trial_as_user(journal, runner)
            journal["observation_started_at"] = datetime.now(UTC).isoformat()
            _set_phase(journal, "observing", journal_directory)
        return _public_status(journal)
    except Exception as exc:
        if journal["phase"] in {"linked", "restoring_account", "testing"}:
            try:
                rollback_home_migration(
                    migration_id,
                    requester_pid,
                    journal_directory=journal_directory,
                    passwd_path=passwd_path,
                    runner=runner,
                )
            except Exception as rollback_exc:
                raise RuntimeError(
                    "用户读写试验失败且自动回退失败，请立即人工处理"
                ) from rollback_exc
            raise RuntimeError("用户读写试验失败，已自动恢复原 home") from exc
        # 已经改名后保留恢复所需事实；调用方可立即触发显式回退。
        if journal["phase"] in {"freezing", "frozen"} or (source.exists() and not backup.exists()):
            _restore_account(journal, runner)
        raise


def rollback_home_migration(
    migration_id: str,
    requester_pid: int,
    *,
    journal_directory: Path = DEFAULT_JOURNAL_DIRECTORY,
    passwd_path: Path = DEFAULT_PASSWD,
    runner: CommandRunner = _run,
) -> dict[str, Any]:
    """重新冻结账号，移除受控链接并把同父目录备份原子恢复为 home。"""

    journal = _read_journal(migration_id, journal_directory)
    if journal["phase"] == "rolled_back":
        return _public_status(journal)
    if journal["phase"] == "cleaned":
        raise ValueError("原 home 备份已清理，不能自动回退")
    _assert_journal_identity(journal, passwd_path)
    _assert_path_identities(journal)
    source = Path(str(journal["source"]))
    backup = Path(str(journal["backup"]))
    if not backup.exists() and source.is_dir() and not source.is_symlink():
        restored = source.lstat()
        if (restored.st_dev, restored.st_ino) == (
            journal["source_device"],
            journal["source_inode"],
        ):
            _restore_account(journal, runner)
            _set_phase(journal, "rolled_back", journal_directory)
            return _public_status(journal)
    if not backup.is_dir() or backup.is_symlink():
        raise ValueError("原 home 备份不存在或类型异常")
    _set_phase(journal, "rolling_back", journal_directory)
    _freeze_account(journal, requester_pid, runner, journal_directory)
    if source.is_symlink():
        if source.resolve(strict=True) != Path(str(journal["destination"])):
            raise ValueError("当前 home 链接不再指向迁移目标")
        source.unlink()
    elif source.exists():
        raise ValueError("逻辑 home 已被其他路径占用")
    os.rename(backup, source)
    restored = source.lstat()
    if (restored.st_dev, restored.st_ino) != (
        journal["source_device"],
        journal["source_inode"],
    ):
        raise RuntimeError("回退后的原 home 身份校验失败")
    _restore_account(journal, runner)
    _set_phase(journal, "rolled_back", journal_directory)
    return _public_status(journal)


def cleanup_home_migration_backup(
    migration_id: str,
    *,
    journal_directory: Path = DEFAULT_JOURNAL_DIRECTORY,
) -> dict[str, Any]:
    """观察期后再次验证链接和目标，再独立清理原 home 备份。"""

    journal = _read_journal(migration_id, journal_directory)
    if journal["phase"] == "cleaned":
        return _public_status(journal)
    if journal["phase"] not in {"observing", "cleaning_backup"}:
        raise ValueError("迁移尚未进入可清理备份的观察期")
    source = Path(str(journal["source"]))
    backup = Path(str(journal["backup"]))
    destination = Path(str(journal["destination"]))
    if not source.is_symlink() or source.resolve(strict=True) != destination:
        raise ValueError("当前逻辑 home 链接关系不匹配")
    if journal["phase"] == "observing" and not backup.exists():
        raise ValueError("观察期内原 home 备份意外缺失")
    if backup.exists():
        metadata = backup.lstat()
        if (metadata.st_dev, metadata.st_ino) != (
            journal["source_device"],
            journal["source_inode"],
        ):
            raise ValueError("原 home 备份身份已变化")
    if journal["phase"] == "observing":
        backup_summary = _tree_summary(backup)
        destination_summary = _tree_summary(destination)
        comparable = ("files", "directories", "symlinks", "logical_bytes", "metadata_digest")
        if any(backup_summary[key] != destination_summary[key] for key in comparable):
            raise ValueError("备份与目标当前校验不一致，拒绝清理")
        _set_phase(journal, "cleaning_backup", journal_directory)
    if backup.exists():
        parent_descriptor = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(
                backup.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
            try:
                _delete_directory_contents(descriptor, int(journal["source_device"]))
            finally:
                os.close(descriptor)
            os.rmdir(backup.name, dir_fd=parent_descriptor)
        finally:
            os.close(parent_descriptor)
    if backup.exists():
        raise RuntimeError("原 home 备份清理结果验证失败")
    _set_phase(journal, "cleaned", journal_directory)
    return _public_status(journal)


def home_migration_status(
    migration_id: str, *, journal_directory: Path = DEFAULT_JOURNAL_DIRECTORY
) -> dict[str, Any]:
    """返回不含用户文件名或内容的持久化迁移阶段摘要。"""

    return _public_status(_read_journal(migration_id, journal_directory))


def _public_status(journal: dict[str, Any]) -> dict[str, Any]:
    """只公开路径、容量、计数和阶段，不返回文件树或账号凭据。"""

    keys = (
        "migration_id",
        "phase",
        "username",
        "uid",
        "source",
        "backup",
        "target_parent",
        "destination",
        "temporary",
        "source_summary",
        "verified_summary",
        "source_mount",
        "target_mount",
        "available_bytes",
        "available_inodes",
        "required_bytes",
        "required_inodes",
        "safety_margin_bytes",
        "verify_contents",
        "created_at",
        "updated_at",
        "observation_started_at",
    )
    return {key: journal[key] for key in keys if key in journal}
