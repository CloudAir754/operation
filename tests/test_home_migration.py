"""home 迁移状态机、故障恢复、回退与独立备份清理测试。"""

import os
import subprocess
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

import lab_ops_panel.infrastructure.file_transfer as file_transfer_module
import lab_ops_panel.infrastructure.home_migration as home_migration_module
from lab_ops_panel.infrastructure.home_migration import (
    cleanup_home_migration_backup,
    execute_home_migration,
    home_migration_preflight,
    home_migration_status,
    rollback_home_migration,
)


class FakeRunner:
    """模拟固定系统命令，并可在第一次复制或用户试验时注入故障。"""

    def __init__(self, fail_copy_once: bool = False, fail_trial: bool = False) -> None:
        self.fail_copy_once = fail_copy_once
        self.fail_trial = fail_trial
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], timeout: int = 120, check: bool = True) -> bytes:
        self.calls.append(command)
        executable = Path(command[0]).name
        if executable == "rsync" and "--dry-run" not in command:
            destination = Path(command[-1].removesuffix("/"))
            if self.fail_copy_once:
                self.fail_copy_once = False
                (destination / "partial.txt").write_text("中断", encoding="utf-8")
                raise RuntimeError("构造的复制中断")
            completed = subprocess.run(command, check=True, capture_output=True, timeout=timeout)
            return completed.stdout
        if executable == "rsync" and "--dry-run" in command:
            completed = subprocess.run(command, check=True, capture_output=True, timeout=timeout)
            return completed.stdout
        elif executable == "runuser" and "-c" in command:
            if self.fail_trial:
                raise RuntimeError("构造的用户读写试验失败")
            marker = Path(command[-1])
            marker.write_text("ok", encoding="utf-8")
            assert marker.read_text(encoding="utf-8") == "ok"
            marker.unlink()
        return b""


@pytest.fixture(autouse=True)
def safe_test_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """系统事实均由临时文件和替身提供，不锁定或终止开发机真实账号。"""

    monkeypatch.setattr(file_transfer_module, "TRUSTED_ANCESTOR_UID", os.getuid())
    monkeypatch.setattr(home_migration_module, "_password_status", lambda username: "P")
    monkeypatch.setattr(
        home_migration_module,
        "terminate_uid_processes",
        lambda uid, requester_pid, grace: {
            "terminated_pids": [],
            "escalated_pids": [],
            "remaining_pids": [],
        },
    )


def _environment(tmp_path: Path) -> dict[str, object]:
    source_parent = tmp_path / "home"
    source_parent.mkdir(mode=0o700)
    source = source_parent / "alice"
    source.mkdir(mode=0o700)
    project = source / "project.txt"
    project.write_text("构造数据", encoding="utf-8")
    os.link(project, source / "project-hardlink.txt")
    sparse = source / "sparse.bin"
    with sparse.open("wb") as stream:
        stream.seek(1024 * 1024)
        stream.write(b"end")
    (source / "project-link").symlink_to("project.txt")
    with suppress(OSError):
        os.setxattr(project, "user.labopspanel-test", b"metadata")
    target_parent = tmp_path / "data-users"
    target_parent.mkdir(mode=0o700)
    passwd_path = tmp_path / "passwd"
    passwd_path.write_text(
        f"alice:x:{os.getuid()}:{os.getgid()}:测试账号:{source}:/bin/bash\n",
        encoding="utf-8",
    )
    mountinfo_path = tmp_path / "mountinfo"
    mountinfo_path.write_text(
        "1 0 0:1 / / rw,relatime - ext4 /dev/loop-test rw\n",
        encoding="utf-8",
    )
    journal_directory = tmp_path / "journals"
    return {
        "source": source,
        "target_parent": target_parent,
        "passwd_path": passwd_path,
        "mountinfo_path": mountinfo_path,
        "journal_directory": journal_directory,
    }


def _preflight(environment: dict[str, object], migration_id: str = "a" * 32) -> dict[str, object]:
    return home_migration_preflight(
        "alice",
        os.getuid(),
        str(environment["target_parent"]),
        migration_id,
        journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        passwd_path=environment["passwd_path"],  # type: ignore[arg-type]
        mountinfo_path=environment["mountinfo_path"],  # type: ignore[arg-type]
        require_different_device=False,
        trusted_system_uid=os.getuid(),
    )


def test_home_migration_enters_observation_then_cleans_backup(tmp_path: Path) -> None:
    """成功切换后保留原 home，只有独立清理动作才删除备份。"""

    environment = _environment(tmp_path)
    migration_id = "a" * 32
    preview = _preflight(environment, migration_id)
    runner = FakeRunner()
    migrated = execute_home_migration(
        migration_id,
        99999,
        journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        passwd_path=environment["passwd_path"],  # type: ignore[arg-type]
        runner=runner,
    )

    source = environment["source"]
    destination = Path(str(preview["destination"]))
    backup = Path(str(preview["backup"]))
    assert migrated["phase"] == "observing"
    assert isinstance(source, Path) and source.is_symlink()
    assert source.resolve() == destination
    assert backup.is_dir() and (backup / "project.txt").exists()
    assert (destination / "project.txt").read_text(encoding="utf-8") == "构造数据"
    assert (destination / "project.txt").stat().st_ino == (
        destination / "project-hardlink.txt"
    ).stat().st_ino
    assert (destination / "project-link").is_symlink()
    assert (destination / "sparse.bin").stat().st_blocks < (
        destination / "sparse.bin"
    ).stat().st_size // 512
    if "user.labopspanel-test" in os.listxattr(destination / "project.txt"):
        assert os.getxattr(destination / "project.txt", "user.labopspanel-test") == b"metadata"

    cleaned = cleanup_home_migration_backup(
        migration_id,
        journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
    )
    assert cleaned["phase"] == "cleaned"
    assert not backup.exists()
    assert source.is_symlink() and destination.is_dir()


def test_home_migration_resumes_after_copy_interruption(tmp_path: Path) -> None:
    """复制中断后的日志保持 copying，重试应从稳定备份安全继续。"""

    environment = _environment(tmp_path)
    migration_id = "b" * 32
    _preflight(environment, migration_id)
    runner = FakeRunner(fail_copy_once=True)
    with pytest.raises(RuntimeError, match="复制中断"):
        execute_home_migration(
            migration_id,
            99999,
            journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
            passwd_path=environment["passwd_path"],  # type: ignore[arg-type]
            runner=runner,
        )
    assert (
        home_migration_status(
            migration_id,
            journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        )["phase"]
        == "copying"
    )

    resumed = execute_home_migration(
        migration_id,
        99999,
        journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        passwd_path=environment["passwd_path"],  # type: ignore[arg-type]
        runner=runner,
    )
    assert resumed["phase"] == "observing"


def test_home_migration_trial_failure_can_restore_original_home(tmp_path: Path) -> None:
    """切换后的用户试验失败时，显式回退应恢复原 inode 和登录路径。"""

    environment = _environment(tmp_path)
    migration_id = "c" * 32
    preview = _preflight(environment, migration_id)
    runner = FakeRunner(fail_trial=True)
    with pytest.raises(RuntimeError, match="试验失败"):
        execute_home_migration(
            migration_id,
            99999,
            journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
            passwd_path=environment["passwd_path"],  # type: ignore[arg-type]
            runner=runner,
        )

    runner.fail_trial = False
    rolled_back = rollback_home_migration(
        migration_id,
        99999,
        journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        passwd_path=environment["passwd_path"],  # type: ignore[arg-type]
        runner=runner,
    )
    source = environment["source"]
    assert rolled_back["phase"] == "rolled_back"
    assert isinstance(source, Path) and source.is_dir() and not source.is_symlink()
    assert source.stat().st_ino == preview["source_summary"]["inode"]  # type: ignore[index]
    assert (source / "project.txt").read_text(encoding="utf-8") == "构造数据"
    assert Path(str(preview["destination"])).is_dir()


def test_home_migration_cleanup_rejects_changed_target(tmp_path: Path) -> None:
    """观察期内目标发生变化时，原 home 备份不得被删除。"""

    environment = _environment(tmp_path)
    migration_id = "d" * 32
    preview = _preflight(environment, migration_id)
    execute_home_migration(
        migration_id,
        99999,
        journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        passwd_path=environment["passwd_path"],  # type: ignore[arg-type]
        runner=FakeRunner(),
    )
    Path(str(preview["destination"]), "project.txt").write_text("发生变化", encoding="utf-8")
    with pytest.raises(ValueError, match="校验不一致"):
        cleanup_home_migration_backup(
            migration_id,
            journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        )
    assert Path(str(preview["backup"])).is_dir()


def test_home_migration_resumes_partially_interrupted_backup_cleanup(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """清理阶段已持久化后即使删除部分文件中断，也应能继续完成。"""

    environment = _environment(tmp_path)
    migration_id = "9" * 32
    preview = _preflight(environment, migration_id)
    execute_home_migration(
        migration_id,
        99999,
        journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        passwd_path=environment["passwd_path"],  # type: ignore[arg-type]
        runner=FakeRunner(),
    )
    original_delete = home_migration_module._delete_directory_contents

    def interrupt_after_one_file(descriptor: int, expected_device: int) -> None:
        os.unlink("project.txt", dir_fd=descriptor)
        raise RuntimeError("构造的备份清理中断")

    monkeypatch.setattr(
        home_migration_module, "_delete_directory_contents", interrupt_after_one_file
    )
    with pytest.raises(RuntimeError, match="清理中断"):
        cleanup_home_migration_backup(
            migration_id,
            journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        )
    assert (
        home_migration_status(
            migration_id,
            journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
        )["phase"]
        == "cleaning_backup"
    )

    monkeypatch.setattr(home_migration_module, "_delete_directory_contents", original_delete)
    cleaned = cleanup_home_migration_backup(
        migration_id,
        journal_directory=environment["journal_directory"],  # type: ignore[arg-type]
    )
    assert cleaned["phase"] == "cleaned"
    assert not Path(str(preview["backup"])).exists()


def test_home_migration_rejects_unknown_filesystem_and_nested_mount(tmp_path: Path) -> None:
    """非 ext4 或 home 子挂载均不进入迁移状态机。"""

    environment = _environment(tmp_path)
    mountinfo = environment["mountinfo_path"]
    assert isinstance(mountinfo, Path)
    mountinfo.write_text("1 0 0:1 / / rw - xfs /dev/loop-test rw\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ext4"):
        _preflight(environment, "e" * 32)

    source = environment["source"]
    assert isinstance(source, Path)
    mountinfo.write_text(
        f"1 0 0:1 / / rw - ext4 /dev/loop-test rw\n"
        f"2 1 0:2 / {source / 'nested'} rw - ext4 /dev/loop-nested rw\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="子挂载"):
        _preflight(environment, "f" * 32)


def test_home_migration_rejects_insufficient_space_and_inodes(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """目标空间或 inode 任一不足都必须在冻结账号前阻断。"""

    environment = _environment(tmp_path)
    monkeypatch.setattr(
        home_migration_module.os,
        "statvfs",
        lambda path: SimpleNamespace(f_bavail=1, f_frsize=4096, f_favail=1_000_000),
    )
    with pytest.raises(ValueError, match="空间不足"):
        _preflight(environment, "1" * 32)

    monkeypatch.setattr(
        home_migration_module.os,
        "statvfs",
        lambda path: SimpleNamespace(
            f_bavail=10 * 1024**3 // 4096,
            f_frsize=4096,
            f_favail=1,
        ),
    )
    with pytest.raises(ValueError, match="inode 不足"):
        _preflight(environment, "2" * 32)
