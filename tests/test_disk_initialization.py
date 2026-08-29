"""空盘 GPT + ext4 纯预览、拒绝、恢复执行和 fstab 回滚测试。"""

import json
import os
from pathlib import Path

import pytest

from lab_ops_panel.infrastructure.disk_initialization import (
    disk_initialization_plan,
    disk_initialization_status,
    execute_disk_initialization,
)

UUID = "11111111-2222-3333-4444-555555555555"


class FakeDiskRunner:
    """模拟单块磁盘从空盘到单分区 ext4 的系统工具事实。"""

    def __init__(
        self,
        device: Path,
        partition: Path,
        mountpoint: Path,
        *,
        signatures: list[dict[str, object]] | None = None,
        system_disk: bool = False,
        mounted_initially: bool = False,
        child_initially: bool = False,
        fail_verify: bool = False,
        interrupt_after_partition: bool = False,
    ) -> None:
        self.device = device
        self.partition = partition
        self.mountpoint = mountpoint
        self.signatures = signatures or []
        self.system_disk = system_disk
        self.mounted_initially = mounted_initially
        self.partitioned = child_initially
        self.formatted = False
        self.mounted = False
        self.fail_verify = fail_verify
        self.interrupt_after_partition = interrupt_after_partition
        self.commands: list[list[str]] = []

    def _inventory(self) -> bytes:
        disk: dict[str, object] = {
            "name": "sdz",
            "kname": "sdz",
            "path": str(self.device),
            "type": "disk",
            "size": 100 * 1024**3,
            "fstype": None,
            "mountpoints": [str(self.mountpoint)] if self.mounted_initially else [None],
            "model": "构造磁盘",
            "serial": "SERIAL-TEST-001",
            "wwn": None,
            "ro": False,
            "rm": False,
            "tran": "sata",
            "uuid": None,
            "pkname": None,
            "parttype": None,
            "partuuid": None,
            "pttype": "gpt" if self.partitioned else None,
        }
        if self.partitioned:
            disk["children"] = [
                {
                    "name": "sdz1",
                    "kname": "sdz1",
                    "path": str(self.partition),
                    "type": "part",
                    "size": 99 * 1024**3,
                    "fstype": "ext4" if self.formatted else None,
                    "mountpoints": [str(self.mountpoint)] if self.mounted else [None],
                    "model": None,
                    "serial": None,
                    "wwn": None,
                    "ro": False,
                    "rm": False,
                    "tran": None,
                    "uuid": UUID if self.formatted else None,
                    "pkname": "sdz",
                    "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4",
                    "partuuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "pttype": None,
                }
            ]
        return json.dumps({"blockdevices": [disk]}).encode()

    def __call__(self, command: list[str], timeout: int = 120, check: bool = True) -> bytes:
        self.commands.append(command)
        executable = Path(command[0]).name
        if executable == "lsblk" and "--json" in command:
            return self._inventory()
        if executable == "lsblk" and "--inverse" in command:
            return (f"{self.device}\n" if self.system_disk else "/dev/root-test\n").encode()
        if executable == "findmnt" and "--verify" in command:
            if self.fail_verify:
                raise RuntimeError("构造的 fstab 验证失败")
            return b""
        if executable == "findmnt" and "--mountpoint" in command:
            return b""
        if executable == "findmnt" and "--target" in command:
            return f"{UUID} ext4 {self.mountpoint}\n".encode() if self.mounted else b""
        if executable == "findmnt":
            return b"/dev/root-test\n"
        if executable == "wipefs" and "--no-act" in command:
            return json.dumps({"signatures": self.signatures}).encode()
        if executable == "parted" and "mkpart" in command:
            self.partitioned = True
            return b""
        if executable == "udevadm" and self.interrupt_after_partition and self.partitioned:
            self.interrupt_after_partition = False
            raise KeyboardInterrupt("构造的进程硬中断")
        if executable == "mkfs.ext4":
            self.formatted = True
            return b""
        if executable == "blkid":
            return f"{UUID}\n".encode()
        if executable == "mount":
            self.mounted = True
            return b""
        if executable == "umount":
            self.mounted = False
            return b""
        return b""


def _environment(tmp_path: Path, **runner_options):  # type: ignore[no-untyped-def]
    device = tmp_path / "device"
    partition = tmp_path / "partition"
    device.touch()
    partition.touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "disk-test").symlink_to(device)
    (by_id / "disk-test-part1").symlink_to(partition)
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    mountpoint = mount_root / "research"
    sys_class = tmp_path / "sys-class"
    (sys_class / "sdz" / "holders").mkdir(parents=True)
    fstab = tmp_path / "fstab"
    fstab.write_text("# 构造 fstab\n", encoding="utf-8")
    fstab.chmod(0o600)
    journal = tmp_path / "journals"
    runner = FakeDiskRunner(device, partition, mountpoint, **runner_options)
    return {
        "device": device,
        "partition": partition,
        "by_id": by_id,
        "mount_root": mount_root,
        "mountpoint": mountpoint,
        "sys_class": sys_class,
        "fstab": fstab,
        "journal": journal,
        "runner": runner,
    }


def _plan(environment: dict[str, object]) -> dict[str, object]:
    return disk_initialization_plan(
        "serial:SERIAL-TEST-001",
        "research",
        str(environment["mountpoint"]),
        ["defaults", "nofail"],
        runner=environment["runner"],  # type: ignore[arg-type]
        by_id_directory=environment["by_id"],  # type: ignore[arg-type]
        sys_class_block=environment["sys_class"],  # type: ignore[arg-type]
        allowed_mount_roots=(environment["mount_root"],),  # type: ignore[arg-type]
        require_block_device=False,
        trusted_system_uid=os.getuid(),
    )


def _execute(environment: dict[str, object], operation_id: str = "a" * 32) -> dict[str, object]:
    return execute_disk_initialization(
        "serial:SERIAL-TEST-001",
        "SERIAL-TEST-001",
        "research",
        str(environment["mountpoint"]),
        ["defaults", "nofail"],
        operation_id,
        runner=environment["runner"],  # type: ignore[arg-type]
        by_id_directory=environment["by_id"],  # type: ignore[arg-type]
        sys_class_block=environment["sys_class"],  # type: ignore[arg-type]
        allowed_mount_roots=(environment["mount_root"],),  # type: ignore[arg-type]
        journal_directory=environment["journal"],  # type: ignore[arg-type]
        fstab_path=environment["fstab"],  # type: ignore[arg-type]
        require_block_device=False,
        require_partition_by_id=True,
        trusted_system_uid=os.getuid(),
    )


def test_disk_plan_is_pure_and_uses_stable_paths(tmp_path: Path) -> None:
    """计划预览只调用只读工具，并明确展示 wipefs、GPT、ext4、fstab 和挂载步骤。"""

    environment = _environment(tmp_path)
    plan = _plan(environment)
    runner = environment["runner"]
    assert isinstance(runner, FakeDiskRunner)

    assert plan["serial"] == "SERIAL-TEST-001"
    assert plan["command_device"].endswith("disk-test")  # type: ignore[union-attr]
    assert plan["fstab_preview"].startswith("UUID=<new-filesystem-uuid>")  # type: ignore[union-attr]
    assert [Path(command[0]).name for command in plan["commands"]] == [  # type: ignore[index]
        "wipefs",
        "parted",
        "parted",
        "udevadm",
        "mkfs.ext4",
        "mount",
    ]
    assert not any(
        Path(command[0]).name in {"parted", "mkfs.ext4", "mount"} for command in runner.commands
    )
    assert environment["fstab"].read_text(encoding="utf-8") == "# 构造 fstab\n"  # type: ignore[union-attr]


def test_disk_execution_formats_updates_fstab_and_verifies_mount(tmp_path: Path) -> None:
    """序列号确认后才执行，并验证 UUID、fstab 与最终挂载事实。"""

    environment = _environment(tmp_path)
    result = _execute(environment)
    fstab = environment["fstab"]
    assert isinstance(fstab, Path)

    assert result["phase"] == "succeeded"
    assert result["filesystem_uuid"] == UUID
    assert f"UUID={UUID} {environment['mountpoint']} ext4 defaults,nofail 0 2" in fstab.read_text(
        encoding="utf-8"
    )


def test_disk_execution_recovers_after_partition_process_interruption(tmp_path: Path) -> None:
    """分区创建后进程硬中断时，重试识别已有唯一分区且不再次清空磁盘。"""

    environment = _environment(tmp_path, interrupt_after_partition=True)
    operation_id = "b" * 32
    with pytest.raises(KeyboardInterrupt):
        _execute(environment, operation_id)
    status = disk_initialization_status(
        operation_id,
        journal_directory=environment["journal"],  # type: ignore[arg-type]
    )
    assert status["phase"] == "partitioning"

    resumed = _execute(environment, operation_id)
    assert resumed["phase"] == "succeeded"
    runner = environment["runner"]
    assert isinstance(runner, FakeDiskRunner)
    wipe_count = sum(
        Path(command[0]).name == "wipefs" and "--all" in command for command in runner.commands
    )
    assert wipe_count == 1


def test_disk_failure_restores_fstab(tmp_path: Path) -> None:
    """fstab 验证失败应恢复原文件，卸载目标并保留失败阶段诊断。"""

    environment = _environment(tmp_path, fail_verify=True)
    operation_id = "c" * 32
    with pytest.raises(RuntimeError, match="fstab 验证失败"):
        _execute(environment, operation_id)
    fstab = environment["fstab"]
    runner = environment["runner"]
    assert isinstance(fstab, Path) and isinstance(runner, FakeDiskRunner)
    assert fstab.read_text(encoding="utf-8") == "# 构造 fstab\n"
    assert runner.mounted is False
    status = disk_initialization_status(
        operation_id,
        journal_directory=environment["journal"],  # type: ignore[arg-type]
    )
    assert status["phase"] == "failed"
    assert status["fstab_restored"] is True


@pytest.mark.parametrize(
    "options, message",
    [
        ({"system_disk": True}, "系统盘"),
        ({"mounted_initially": True}, "已挂载"),
        ({"child_initially": True}, "已有"),
        ({"signatures": [{"type": "LVM2_member"}]}, "签名"),
        ({"signatures": [{"type": "linux_raid_member"}]}, "签名"),
        ({"signatures": [{"type": "zfs_member"}]}, "签名"),
    ],
)
def test_disk_plan_rejects_unsafe_device_facts(
    tmp_path: Path, options: dict[str, object], message: str
) -> None:
    """系统盘、挂载盘、已有分区和 LVM/RAID/ZFS 等签名盘均拒绝。"""

    environment = _environment(tmp_path, **options)
    with pytest.raises(ValueError, match=message):
        _plan(environment)


def test_disk_plan_rejects_kernel_holder_and_wrong_confirmation(tmp_path: Path) -> None:
    """内核 holder 阻断预览，序列号不匹配则不能触发任何破坏性命令。"""

    environment = _environment(tmp_path)
    holder = environment["sys_class"] / "sdz" / "holders" / "dm-0"  # type: ignore[operator]
    holder.touch()
    with pytest.raises(ValueError, match="占用"):
        _plan(environment)

    holder.unlink()
    with pytest.raises(ValueError, match="序列号"):
        execute_disk_initialization(
            "serial:SERIAL-TEST-001",
            "WRONG-SERIAL",
            "research",
            str(environment["mountpoint"]),
            ["defaults", "nofail"],
            "d" * 32,
            runner=environment["runner"],  # type: ignore[arg-type]
            by_id_directory=environment["by_id"],  # type: ignore[arg-type]
            sys_class_block=environment["sys_class"],  # type: ignore[arg-type]
            allowed_mount_roots=(environment["mount_root"],),  # type: ignore[arg-type]
            journal_directory=environment["journal"],  # type: ignore[arg-type]
            fstab_path=environment["fstab"],  # type: ignore[arg-type]
            require_block_device=False,
            trusted_system_uid=os.getuid(),
        )
    runner = environment["runner"]
    assert isinstance(runner, FakeDiskRunner)
    assert not any(Path(command[0]).name == "parted" for command in runner.commands)
