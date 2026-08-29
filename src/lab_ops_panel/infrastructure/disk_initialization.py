"""空盘 GPT + ext4 初始化的纯预览、拒绝规则和可恢复执行。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from lab_ops_panel.infrastructure.storage import _by_id_links, _flatten_devices

LSBLK_COLUMNS = (
    "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL,WWN,RO,RM,TRAN,"
    "UUID,PKNAME,PARTTYPE,PARTUUID,PTTYPE"
)
LSBLK = Path("/usr/bin/lsblk")
FINDMNT = Path("/usr/bin/findmnt")
WIPEFS = Path("/usr/sbin/wipefs")
PARTED = Path("/usr/sbin/parted")
UDEVADM = Path("/usr/bin/udevadm")
MKFS_EXT4 = Path("/usr/sbin/mkfs.ext4")
BLKID = Path("/usr/sbin/blkid")
MOUNT = Path("/usr/bin/mount")
UMOUNT = Path("/usr/bin/umount")
DEFAULT_FSTAB = Path("/etc/fstab")
DEFAULT_BY_ID = Path("/dev/disk/by-id")
DEFAULT_SYS_CLASS_BLOCK = Path("/sys/class/block")
DEFAULT_JOURNAL_DIRECTORY = Path("/var/lib/labopspanel/disk-initializations")
ALLOWED_MOUNT_ROOTS = (Path("/mnt"), Path("/srv"), Path("/data"))
ALLOWED_MOUNT_OPTIONS = frozenset({"defaults", "nofail", "noatime", "nodev", "nosuid"})
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,16}$")


class CommandRunner(Protocol):
    """固定命令执行边界。"""

    def __call__(
        self, command: list[str], timeout: int = 120, check: bool = True
    ) -> bytes | None: ...


def _run(command: list[str], timeout: int = 120, check: bool = True) -> bytes:
    """执行固定参数系统工具，并仅返回机器可解析的 stdout。"""

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


def _inventory(runner: CommandRunner, by_id_directory: Path) -> list[dict[str, Any]]:
    """读取包含分区表字段的块设备树，并附加所有 by-id 稳定链接。"""

    output = runner([str(LSBLK), "--json", "--bytes", "--output", LSBLK_COLUMNS], timeout=30)
    try:
        payload = json.loads(output or b"")
        devices = payload["blockdevices"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("lsblk 输出无法安全解析") from exc
    if not isinstance(devices, list):
        raise ValueError("lsblk 输出缺少设备列表")
    flattened = _flatten_devices(devices, _by_id_links(by_id_directory))

    def attach_extra(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            path = node.get("path")
            item = next((value for value in flattened if value["path"] == path), None)
            if item is not None:
                item["partition_type"] = node.get("parttype")
                item["partition_uuid"] = node.get("partuuid")
                item["partition_table"] = node.get("pttype")
                item["kernel_name"] = node.get("kname")
            attach_extra(node.get("children") or [])

    attach_extra(devices)
    return flattened


def _select_disk(devices: list[dict[str, Any]], stable_identity: str) -> dict[str, Any]:
    """稳定身份必须唯一命中顶层磁盘，拒绝 UUID 或易变设备名。"""

    if not (
        stable_identity.startswith("wwn:")
        or stable_identity.startswith("serial:")
        or stable_identity.startswith("/dev/disk/by-id/")
    ):
        raise ValueError("磁盘必须使用 WWN、序列号或 /dev/disk/by-id 稳定身份")
    matches = [
        device
        for device in devices
        if device.get("stable_identity") == stable_identity
        or stable_identity in device.get("by_id", [])
    ]
    if len(matches) != 1 or matches[0].get("type") != "disk":
        raise ValueError("稳定身份未唯一匹配顶层磁盘")
    return matches[0]


def _safe_mountpoint(
    raw: str, allowed_roots: tuple[Path, ...], trusted_system_uid: int = 0
) -> Path:
    """挂载点必须位于显式白名单根下，不含点段、链接或系统关键路径。"""

    if not raw or "\x00" in raw:
        raise ValueError("挂载目录无效")
    path = Path(raw)
    if not path.is_absolute() or path == Path("/") or path != Path(os.path.normpath(raw)):
        raise ValueError("挂载目录必须是规范绝对路径且不能为根目录")
    if not any(path != root and path.is_relative_to(root) for root in allowed_roots):
        raise ValueError("挂载目录不在允许的 /mnt、/srv 或 /data 根下")
    if path.parent not in allowed_roots:
        raise ValueError("首版挂载目录必须是允许根目录的直接子目录")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", path.name):
        raise ValueError("挂载目录名称只允许字母、数字、点、下划线或连字符")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"挂载目录包含符号链接祖先：{current}")
    if path.exists():
        metadata = path.stat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != trusted_system_uid
            or metadata.st_mode & 0o022
        ):
            raise ValueError("已有挂载目录必须由 root 所有且不可被其他用户写入")
        if any(path.iterdir()):
            raise ValueError("已有挂载目录必须为空")
    parent_metadata = path.parent.stat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != trusted_system_uid
        or parent_metadata.st_mode & 0o022
    ):
        raise ValueError("挂载目录父目录必须由 root 所有且不可被其他用户写入")
    return path


def _mount_options(values: list[str]) -> str:
    """只接受无参数的固定 ext4 挂载选项白名单。"""

    if not values or len(values) != len(set(values)):
        raise ValueError("挂载选项不能为空或重复")
    if any(value not in ALLOWED_MOUNT_OPTIONS for value in values):
        raise ValueError("包含不支持的挂载选项")
    if "defaults" not in values:
        raise ValueError("挂载选项必须包含 defaults")
    return ",".join(values)


def _system_disk_paths(runner: CommandRunner) -> set[str]:
    """读取根文件系统来源及其全部父设备链。"""

    source = (
        (runner([str(FINDMNT), "--noheadings", "--output", "SOURCE", "/"]) or b"")
        .decode("utf-8", "strict")
        .strip()
    )
    if not source:
        raise ValueError("无法识别系统根文件系统设备")
    lineage = runner(
        [str(LSBLK), "--noheadings", "--paths", "--inverse", "--output", "PATH", source]
    )
    return {
        line.strip()
        for line in (lineage or b"").decode("utf-8", "strict").splitlines()
        if line.strip()
    }


def _signatures(device_path: str, runner: CommandRunner) -> list[dict[str, Any]]:
    """以 wipefs 只读 JSON 模式识别所有现有签名。"""

    output = runner([str(WIPEFS), "--no-act", "--json", "--", device_path], timeout=30)
    try:
        payload = json.loads(output or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("wipefs 输出无法安全解析") from exc
    signatures = payload.get("signatures", [])
    if not isinstance(signatures, list):
        raise ValueError("wipefs 输出缺少签名列表")
    return signatures


def _holders(kernel_name: str, sys_class_block: Path) -> list[str]:
    """检测 device-mapper、MD、LVM 等内核 holder。"""

    directory = sys_class_block / kernel_name / "holders"
    try:
        return sorted(item.name for item in directory.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError("无法读取块设备 holder") from exc


def _command_device(disk: dict[str, Any]) -> str:
    """破坏性命令只使用扫描时绑定到该磁盘的 by-id 链接。"""

    links = [value for value in disk.get("by_id", []) if "-part" not in Path(value).name]
    if not links:
        raise ValueError("磁盘缺少可用于执行的 /dev/disk/by-id 链接")
    return links[0]


def disk_initialization_plan(
    stable_identity: str,
    label: str,
    mountpoint_raw: str,
    mount_options: list[str],
    *,
    runner: CommandRunner = _run,
    by_id_directory: Path = DEFAULT_BY_ID,
    sys_class_block: Path = DEFAULT_SYS_CLASS_BLOCK,
    allowed_mount_roots: tuple[Path, ...] = ALLOWED_MOUNT_ROOTS,
    require_block_device: bool = True,
    trusted_system_uid: int = 0,
) -> dict[str, Any]:
    """纯只读生成 GPT + ext4 计划；任一风险事实不明确时拒绝。"""

    if not LABEL_PATTERN.fullmatch(label) or len(label.encode()) > 16:
        raise ValueError("ext4 卷标只允许 1-16 字节的字母、数字、点、下划线或连字符")
    mountpoint = _safe_mountpoint(mountpoint_raw, allowed_mount_roots, trusted_system_uid)
    options = _mount_options(mount_options)
    existing_mount = runner(
        [str(FINDMNT), "--noheadings", "--mountpoint", str(mountpoint)], check=False
    )
    if existing_mount:
        raise ValueError("目标挂载目录已被使用")
    devices = _inventory(runner, by_id_directory)
    disk = _select_disk(devices, stable_identity)
    device_path = str(disk["path"])
    if not disk.get("serial"):
        raise ValueError("磁盘缺少用于二次确认的序列号")
    if disk.get("read_only"):
        raise ValueError("磁盘为只读设备")
    if disk.get("removable"):
        raise ValueError("首版拒绝初始化可移除设备")
    if disk.get("filesystem") or disk.get("uuid") or disk.get("partition_table"):
        raise ValueError("磁盘已有文件系统、UUID 或分区表")
    children = [device for device in devices if device.get("parent") == disk.get("name")]
    if children:
        raise ValueError("磁盘已有分区或子设备")
    if disk.get("mountpoints"):
        raise ValueError("磁盘或其子设备已挂载")
    if device_path in _system_disk_paths(runner):
        raise ValueError("拒绝初始化系统盘")
    signatures = _signatures(device_path, runner)
    if signatures:
        raise ValueError("磁盘存在文件系统、RAID、LVM、ZFS 或其他签名")
    holders = _holders(str(disk.get("kernel_name") or disk.get("name")), sys_class_block)
    if holders:
        raise ValueError("磁盘正被 device-mapper、LVM、RAID 或其他设备占用")
    path = Path(device_path)
    if require_block_device:
        try:
            if not stat.S_ISBLK(path.stat().st_mode):
                raise ValueError("稳定身份目标不是块设备")
        except OSError as exc:
            raise ValueError("块设备路径不存在") from exc
    command_device = _command_device(disk)
    return {
        "stable_identity": stable_identity,
        "device_path": device_path,
        "command_device": command_device,
        "kernel_name": disk.get("kernel_name") or disk.get("name"),
        "model": disk.get("model"),
        "serial": disk["serial"],
        "wwn": disk.get("wwn"),
        "size": disk.get("size"),
        "label": label,
        "mountpoint": str(mountpoint),
        "mount_options": options,
        "signatures": [],
        "holders": [],
        "commands": [
            [str(WIPEFS), "--all", "--", command_device],
            [str(PARTED), "--script", command_device, "mklabel", "gpt"],
            [
                str(PARTED),
                "--script",
                command_device,
                "mkpart",
                "primary",
                "ext4",
                "1MiB",
                "100%",
            ],
            [str(UDEVADM), "settle"],
            [str(MKFS_EXT4), "-L", label, "-m", "0", "<partition-by-id>"],
            [str(MOUNT), str(mountpoint)],
        ],
        "fstab_preview": f"UUID=<new-filesystem-uuid> {mountpoint} ext4 {options} 0 2",
        "destructive": True,
    }


def _journal_path(operation_id: str, directory: Path) -> Path:
    if len(operation_id) != 32 or any(value not in "0123456789abcdef" for value in operation_id):
        raise ValueError("磁盘初始化操作 ID 无效")
    return directory / f"{operation_id}.json"


def _prepare_private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("磁盘初始化状态目录不能是符号链接")
    current = directory.parent
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("磁盘初始化状态目录父链包含符号链接")
        sticky_root = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if metadata.st_uid not in {0, os.geteuid()}:
            raise ValueError("磁盘初始化状态目录父链所有者不可信")
        if metadata.st_mode & 0o022 and not sticky_root:
            raise ValueError("磁盘初始化状态目录父链可被其他用户改写")
        if current == Path("/"):
            break
        current = current.parent
    metadata = directory.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError("磁盘初始化状态目录权限不安全")


def _write_journal(journal: dict[str, Any], directory: Path) -> None:
    """原子持久化磁盘初始化阶段。"""

    _prepare_private_directory(directory)
    path = _journal_path(str(journal["operation_id"]), directory)
    temporary = path.with_suffix(".tmp")
    if temporary.exists() and not temporary.is_symlink():
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        encoded = json.dumps(journal, ensure_ascii=False, sort_keys=True).encode()
        remaining = memoryview(encoded)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _read_journal(operation_id: str, directory: Path) -> dict[str, Any]:
    _prepare_private_directory(directory)
    path = _journal_path(operation_id, directory)
    if path.is_symlink():
        raise ValueError("磁盘初始化状态文件不能是符号链接")
    try:
        metadata = path.stat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("磁盘初始化状态不存在或损坏") from exc
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or not isinstance(value, dict)
        or value.get("operation_id") != operation_id
    ):
        raise ValueError("磁盘初始化状态文件不安全")
    return value


def _set_phase(journal: dict[str, Any], phase: str, directory: Path) -> None:
    journal["phase"] = phase
    journal["updated_at"] = datetime.now(UTC).isoformat()
    _write_journal(journal, directory)


def _partition_for_disk(
    plan: dict[str, Any], runner: CommandRunner, by_id_directory: Path, require_by_id: bool
) -> dict[str, Any]:
    """重新扫描并只接受该稳定磁盘下唯一的单分区。"""

    devices = _inventory(runner, by_id_directory)
    disk = _select_disk(devices, str(plan["stable_identity"]))
    partitions = [
        device
        for device in devices
        if device.get("parent") == disk.get("name") and device.get("type") == "part"
    ]
    if len(partitions) != 1:
        raise RuntimeError("分区后未找到唯一目标分区")
    partition = partitions[0]
    links = partition.get("by_id", [])
    if require_by_id and not links:
        raise RuntimeError("新分区缺少 /dev/disk/by-id 稳定链接")
    partition["command_path"] = links[0] if links else partition["path"]
    return partition


def _safe_fstab(path: Path) -> tuple[bytes, os.stat_result]:
    """读取 root 所有普通 fstab，拒绝链接和可写权限。"""

    if path.is_symlink():
        raise ValueError("fstab 不能是符号链接")
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise ValueError("fstab 所有者或权限不安全")
    return path.read_bytes(), metadata


def _replace_fstab(path: Path, content: bytes, metadata: os.stat_result) -> None:
    """同目录原子替换 fstab 并保留所有者和 mode。"""

    temporary = path.with_name(f".{path.name}.labopspanel.tmp")
    if temporary.exists() and not temporary.is_symlink():
        temporary_metadata = temporary.stat()
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_uid != os.geteuid()
            or temporary_metadata.st_mode & 0o077
        ):
            raise ValueError("fstab 临时文件权限不安全")
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        remaining = memoryview(content)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


@contextmanager
def _fstab_lock(path: Path):  # type: ignore[no-untyped-def]
    """使用同目录 root 私有锁文件串行化 fstab 检查和替换。"""

    lock_path = path.with_name(f".{path.name}.labopspanel.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise ValueError("fstab 锁文件权限不安全")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _assert_plan_disk(
    plan: dict[str, Any], runner: CommandRunner, by_id_directory: Path, sys_class_block: Path
) -> dict[str, Any]:
    """每次恢复前重新把稳定身份映射到同一序列号，并拒绝变成系统盘或 holder。"""

    disk = _select_disk(_inventory(runner, by_id_directory), str(plan["stable_identity"]))
    if disk.get("serial") != plan["serial"] or disk.get("wwn") != plan.get("wwn"):
        raise ValueError("稳定身份映射到的磁盘序列号或 WWN 已变化")
    if str(disk["path"]) in _system_disk_paths(runner):
        raise ValueError("目标磁盘已成为系统盘")
    holders = _holders(str(disk.get("kernel_name") or disk.get("name")), sys_class_block)
    if holders:
        raise ValueError("目标磁盘出现新的 holder")
    return disk


def _mounted_facts(mountpoint: str, runner: CommandRunner) -> list[str]:
    """读取指定路径当前挂载的 UUID、类型和目标；未挂载时返回空列表。"""

    output = runner(
        [
            str(FINDMNT),
            "--noheadings",
            "--output",
            "UUID,FSTYPE,TARGET",
            "--target",
            mountpoint,
        ],
        check=False,
    )
    return (output or b"").decode("utf-8", "strict").split()


def execute_disk_initialization(
    stable_identity: str,
    confirmed_serial: str,
    label: str,
    mountpoint: str,
    mount_options: list[str],
    operation_id: str,
    *,
    runner: CommandRunner = _run,
    by_id_directory: Path = DEFAULT_BY_ID,
    sys_class_block: Path = DEFAULT_SYS_CLASS_BLOCK,
    allowed_mount_roots: tuple[Path, ...] = ALLOWED_MOUNT_ROOTS,
    journal_directory: Path = DEFAULT_JOURNAL_DIRECTORY,
    fstab_path: Path = DEFAULT_FSTAB,
    require_block_device: bool = True,
    require_partition_by_id: bool = True,
    trusted_system_uid: int = 0,
) -> dict[str, Any]:
    """重做预检并执行可恢复初始化；失败时恢复 fstab 且保留诊断。"""

    journal_path = _journal_path(operation_id, journal_directory)
    if journal_path.exists():
        journal = _read_journal(operation_id, journal_directory)
        plan = dict(journal["plan"])
        if (
            plan["stable_identity"] != stable_identity
            or plan["label"] != label
            or plan["mountpoint"] != mountpoint
            or plan["mount_options"] != _mount_options(mount_options)
        ):
            raise ValueError("操作 ID 已绑定不同磁盘或初始化参数")
    else:
        plan = disk_initialization_plan(
            stable_identity,
            label,
            mountpoint,
            mount_options,
            runner=runner,
            by_id_directory=by_id_directory,
            sys_class_block=sys_class_block,
            allowed_mount_roots=allowed_mount_roots,
            require_block_device=require_block_device,
            trusted_system_uid=trusted_system_uid,
        )
        if confirmed_serial != plan["serial"]:
            raise ValueError("磁盘序列号二次确认不匹配")
        journal = {
            "operation_id": operation_id,
            "phase": "planned",
            "plan": plan,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _write_journal(journal, journal_directory)
    if confirmed_serial != plan["serial"]:
        raise ValueError("磁盘序列号二次确认不匹配")
    if journal["phase"] == "succeeded":
        return _public_status(journal)
    _assert_plan_disk(plan, runner, by_id_directory, sys_class_block)
    try:
        if journal["phase"] == "planned":
            # 破坏性命令前最后一次纯预检，随后所有恢复都依赖持久化计划和稳定身份。
            disk_initialization_plan(
                stable_identity,
                label,
                mountpoint,
                mount_options,
                runner=runner,
                by_id_directory=by_id_directory,
                sys_class_block=sys_class_block,
                allowed_mount_roots=allowed_mount_roots,
                require_block_device=require_block_device,
                trusted_system_uid=trusted_system_uid,
            )
            _set_phase(journal, "partitioning", journal_directory)
        if journal["phase"] == "partitioning":
            devices = _inventory(runner, by_id_directory)
            disk = _select_disk(devices, str(plan["stable_identity"]))
            children = [device for device in devices if device.get("parent") == disk.get("name")]
            if not children:
                runner([str(WIPEFS), "--all", "--", plan["command_device"]], timeout=60)
                runner([str(PARTED), "--script", plan["command_device"], "mklabel", "gpt"])
                runner(
                    [
                        str(PARTED),
                        "--script",
                        plan["command_device"],
                        "mkpart",
                        "primary",
                        "ext4",
                        "1MiB",
                        "100%",
                    ]
                )
            elif len(children) != 1 or children[0].get("type") != "part":
                raise RuntimeError("分区恢复阶段发现非预期子设备")
            runner([str(UDEVADM), "settle"], timeout=60)
            partition = _partition_for_disk(plan, runner, by_id_directory, require_partition_by_id)
            journal["partition"] = partition["command_path"]
            _set_phase(journal, "partitioned", journal_directory)
        if journal["phase"] in {"partitioned", "formatting"}:
            _set_phase(journal, "formatting", journal_directory)
            partition = _partition_for_disk(plan, runner, by_id_directory, require_partition_by_id)
            if partition.get("filesystem") not in {None, "ext4"}:
                raise RuntimeError("新分区出现非 ext4 签名")
            if partition.get("filesystem") is None:
                runner(
                    [
                        str(MKFS_EXT4),
                        "-F",
                        "-L",
                        plan["label"],
                        "-m",
                        "0",
                        partition["command_path"],
                    ],
                    timeout=3600,
                )
                runner([str(UDEVADM), "settle"], timeout=60)
            uuid = (
                (
                    runner(
                        [
                            str(BLKID),
                            "--output",
                            "value",
                            "--match-tag",
                            "UUID",
                            "--",
                            partition["command_path"],
                        ]
                    )
                    or b""
                )
                .decode("ascii", "strict")
                .strip()
            )
            if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", uuid):
                raise RuntimeError("无法读取新 ext4 UUID")
            journal["filesystem_uuid"] = uuid
            _set_phase(journal, "formatted", journal_directory)
        if journal["phase"] in {"formatted", "updating_fstab"}:
            _set_phase(journal, "updating_fstab", journal_directory)
            Path(plan["mountpoint"]).mkdir(mode=0o755, parents=False, exist_ok=True)
            marker = f"# LabOpsPanel disk-init {operation_id}\n".encode()
            entry = (
                f"UUID={journal['filesystem_uuid']} {plan['mountpoint']} ext4 "
                f"{plan['mount_options']} 0 2\n"
            ).encode()
            with _fstab_lock(fstab_path):
                current, metadata = _safe_fstab(fstab_path)
                current_hash = hashlib.sha256(current).hexdigest()
                if "fstab_after_sha256" in journal:
                    if current_hash == journal["fstab_after_sha256"]:
                        pass
                    elif current_hash == journal["fstab_before_sha256"]:
                        updated = str(journal["fstab_after"]).encode("utf-8", "surrogateescape")
                        _replace_fstab(fstab_path, updated, metadata)
                    else:
                        raise RuntimeError("fstab 在恢复期间被其他程序修改")
                else:
                    if marker in current or entry in current:
                        raise RuntimeError("fstab 已存在相同托管标记或挂载条目")
                    updated = (
                        current
                        + (b"" if current.endswith(b"\n") or not current else b"\n")
                        + marker
                        + entry
                    )
                    journal["fstab_before_sha256"] = current_hash
                    journal["fstab_before"] = current.decode("utf-8", "surrogateescape")
                    journal["fstab_after_sha256"] = hashlib.sha256(updated).hexdigest()
                    journal["fstab_after"] = updated.decode("utf-8", "surrogateescape")
                    _write_journal(journal, journal_directory)
                    _replace_fstab(fstab_path, updated, metadata)
            runner([str(FINDMNT), "--verify", "--tab-file", str(fstab_path)], timeout=60)
            _set_phase(journal, "fstab_updated", journal_directory)
        if journal["phase"] in {"fstab_updated", "mounting"}:
            _set_phase(journal, "mounting", journal_directory)
            mounted = _mounted_facts(str(plan["mountpoint"]), runner)
            expected_mount = [journal["filesystem_uuid"], "ext4", plan["mountpoint"]]
            if not mounted:
                runner([str(MOUNT), "--", plan["mountpoint"]], timeout=120)
            elif mounted != expected_mount:
                raise RuntimeError("挂载目录已被其他文件系统占用")
            _set_phase(journal, "mounted", journal_directory)
        if journal["phase"] == "mounted":
            source = _mounted_facts(str(plan["mountpoint"]), runner)
            if source != [journal["filesystem_uuid"], "ext4", plan["mountpoint"]]:
                raise RuntimeError("挂载结果的 UUID、文件系统或目标目录不匹配")
            _set_phase(journal, "succeeded", journal_directory)
        return _public_status(journal)
    except Exception as exc:
        journal["failure"] = str(exc)
        journal["failed_phase"] = journal["phase"]
        if "fstab_after_sha256" in journal:
            try:
                runner([str(UMOUNT), "--", plan["mountpoint"]], check=False)
                with _fstab_lock(fstab_path):
                    current, metadata = _safe_fstab(fstab_path)
                    if hashlib.sha256(current).hexdigest() == journal["fstab_after_sha256"]:
                        _replace_fstab(
                            fstab_path,
                            str(journal["fstab_before"]).encode("utf-8", "surrogateescape"),
                            metadata,
                        )
                        journal["fstab_restored"] = True
                    else:
                        journal["fstab_restored"] = False
            except Exception:
                journal["fstab_restored"] = False
        _set_phase(journal, "failed", journal_directory)
        raise


def disk_initialization_status(
    operation_id: str, *, journal_directory: Path = DEFAULT_JOURNAL_DIRECTORY
) -> dict[str, Any]:
    """返回不含 fstab 原文的磁盘初始化恢复摘要。"""

    return _public_status(_read_journal(operation_id, journal_directory))


def _public_status(journal: dict[str, Any]) -> dict[str, Any]:
    plan = journal["plan"]
    return {
        "operation_id": journal["operation_id"],
        "phase": journal["phase"],
        "stable_identity": plan["stable_identity"],
        "serial": plan["serial"],
        "model": plan["model"],
        "size": plan["size"],
        "label": plan["label"],
        "mountpoint": plan["mountpoint"],
        "mount_options": plan["mount_options"],
        "partition": journal.get("partition"),
        "filesystem_uuid": journal.get("filesystem_uuid"),
        "failed_phase": journal.get("failed_phase"),
        "failure": journal.get("failure"),
        "fstab_restored": journal.get("fstab_restored"),
        "created_at": journal["created_at"],
        "updated_at": journal["updated_at"],
    }
