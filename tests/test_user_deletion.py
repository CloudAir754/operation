"""用户删除预检、执行与失败恢复测试。"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab_ops_panel.infrastructure import user_deletion


def _runner(command, timeout=120):  # type: ignore[no-untyped-def]
    if "--status" in command:
        return SimpleNamespace(stdout=f"{command[-1]} P 2026-01-01 0 99999 7 -1\n")
    if "--inodes" in command:
        return SimpleNamespace(stdout="3\t/home\n")
    if Path(command[0]).name == "du":
        return SimpleNamespace(stdout="4096\t/home\n")
    return SimpleNamespace(stdout="")


def _passwd(path: Path, username: str, uid: int, home: Path) -> None:
    path.write_text(f"{username}:x:{uid}:{uid}:测试用户:{home}:/bin/bash\n", encoding="utf-8")


def test_deletion_preflight_reports_processes_schedules_and_disk_usage(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """预检统一展示稳定身份、进程、计划任务、sudo 与 home 占用。"""

    username = "deletetest"
    uid = os.getuid()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    (home / "data").write_text("test", encoding="utf-8")
    passwd = tmp_path / "passwd"
    _passwd(passwd, username, uid, home)
    crontabs = tmp_path / "crontabs"
    crontabs.mkdir(mode=0o700)
    crontabs.chmod(0o700)
    (crontabs / username).write_text("* * * * * true\n", encoding="utf-8")
    cron_file = tmp_path / "crontab"
    cron_file.write_text(f"* * * * * {username} true\n", encoding="utf-8")
    monkeypatch.setattr(user_deletion.psutil, "disk_partitions", lambda all=True: [])
    monkeypatch.setattr(user_deletion, "_open_home_pids", lambda path: ([4321], True))
    monkeypatch.setattr(
        user_deletion,
        "_uid_processes",
        lambda target_uid: [{"pid": 4321, "create_time": 1234.5}],
    )
    monkeypatch.setattr(user_deletion.psutil, "users", lambda: [])
    monkeypatch.setattr(
        user_deletion,
        "scan_sudoers_rules",
        lambda usernames: {"sources": {username: []}, "complete": True},
    )

    result = user_deletion.user_deletion_preflight(
        username,
        uid,
        passwd_path=passwd,
        crontab_directory=crontabs,
        at_directory=tmp_path / "missing-at",
        system_crontab=cron_file,
        cron_directory=tmp_path / "missing-cron.d",
        runner=_runner,
        require_root=False,
    )

    assert result["uid"] == uid
    assert result["disk_usage"] == {"allocated_bytes": 4096, "inodes": 3}
    assert result["processes"][0]["pid"] == 4321
    assert result["scheduled_tasks"]["user_crontab"] is True
    assert result["open_home_pids"] == [4321]
    assert "系统 cron" in "；".join(result["blockers"])


def test_preflight_detects_symlink_and_shared_home(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """符号链接或共享 home 不能进入递归删除模式。"""

    uid = os.getuid()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    logical = tmp_path / "logical"
    logical.symlink_to(target, target_is_directory=True)
    passwd = tmp_path / "passwd"
    passwd.write_text(
        f"alice:x:{uid}:{uid}:Alice:{logical}:/bin/bash\n"
        f"bob:x:{uid + 1}:{uid + 1}:Bob:{target}:/bin/bash\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(user_deletion.psutil, "disk_partitions", lambda all=True: [])
    monkeypatch.setattr(user_deletion, "_open_home_pids", lambda path: ([], True))
    monkeypatch.setattr(user_deletion, "_uid_processes", lambda target_uid: [])
    monkeypatch.setattr(user_deletion.psutil, "users", lambda: [])
    monkeypatch.setattr(
        user_deletion,
        "scan_sudoers_rules",
        lambda usernames: {"sources": {"alice": []}, "complete": True},
    )

    result = user_deletion.user_deletion_preflight(
        "alice", uid, passwd_path=passwd, runner=_runner, require_root=False
    )

    assert result["home_is_symlink"] is True
    assert result["shared_with"] == ["bob"]
    assert result["delete_home_supported"] is False
    assert "共享" in "；".join(result["home_delete_blockers"])
    assert result["blockers"] == []


def _deletion_facts(home: Path, uid: int) -> dict[str, object]:
    metadata = home.stat()
    return {
        "username": "alice",
        "uid": uid,
        "shell": "/bin/bash",
        "account_status": "P",
        "logical_home": str(home),
        "resolved_home": str(home),
        "home_is_symlink": False,
        "home_device": metadata.st_dev,
        "home_inode": metadata.st_ino,
        "delete_home_supported": True,
        "blockers": [],
        "home_delete_blockers": [],
        "processes": [],
        "open_home_pids": [],
    }


def test_delete_user_quarantines_then_removes_home(tmp_path: Path) -> None:
    """删除 home 时先在同一父目录 rename 隔离，账号删除成功后才递归清理。"""

    tmp_path.chmod(0o700)
    uid = os.getuid()
    home = tmp_path / "alice"
    home.mkdir(mode=0o700)
    (home / "important").write_text("artificial", encoding="utf-8")
    passwd = tmp_path / "passwd"
    _passwd(passwd, "alice", uid, home)
    facts = _deletion_facts(home, uid)

    def runner(command, timeout=120):  # type: ignore[no-untyped-def]
        if Path(command[0]).name == "userdel":
            passwd.write_text("", encoding="utf-8")
        return SimpleNamespace(stdout="")

    result = user_deletion.delete_local_user(
        "alice",
        uid,
        str(home),
        True,
        os.getpid(),
        passwd_path=passwd,
        crontab_directory=tmp_path / "missing-crontabs",
        at_directory=tmp_path / "missing-at",
        preflight_reader=lambda: facts,
        runner=runner,
        process_terminator=lambda *args, **kwargs: {
            "terminated_pids": [123],
            "escalated_pids": [],
        },
        sudo_revoker=lambda *args, **kwargs: {"changed": False},
        require_root=False,
    )

    assert result["account_deleted"] is True
    assert result["home_deleted"] is True
    assert not home.exists()
    assert not list(tmp_path.glob(".labopspanel-delete-*"))


def test_delete_failure_restores_home_login_and_managed_sudo(tmp_path: Path) -> None:
    """userdel 失败前的 home 隔离、登录禁用和托管 sudo 撤销都必须恢复。"""

    tmp_path.chmod(0o700)
    uid = os.getuid()
    home = tmp_path / "alice"
    home.mkdir(mode=0o700)
    passwd = tmp_path / "passwd"
    _passwd(passwd, "alice", uid, home)
    facts = _deletion_facts(home, uid)
    commands = []
    restored = []

    def runner(command, timeout=120):  # type: ignore[no-untyped-def]
        commands.append(command)
        if Path(command[0]).name == "userdel":
            raise RuntimeError("模拟 userdel 失败")
        return SimpleNamespace(stdout="")

    with pytest.raises(RuntimeError, match="模拟"):
        user_deletion.delete_local_user(
            "alice",
            uid,
            str(home),
            True,
            os.getpid(),
            passwd_path=passwd,
            preflight_reader=lambda: facts,
            runner=runner,
            process_terminator=lambda *args, **kwargs: {
                "terminated_pids": [],
                "escalated_pids": [],
            },
            sudo_revoker=lambda *args, **kwargs: {"changed": True},
            sudo_restorer=lambda *args, **kwargs: restored.append(args) or {"changed": True},
            require_root=False,
        )

    assert home.is_dir()
    assert restored
    assert any("--shell" in command for command in commands)
    assert any("--unlock" in command for command in commands)


def test_delete_rejects_home_identity_change_after_process_termination(tmp_path: Path) -> None:
    """禁用登录后仍须复核 home 设备和 inode，变化时恢复登录且不调用 userdel。"""

    uid = os.getuid()
    home = tmp_path / "alice"
    home.mkdir(mode=0o700)
    passwd = tmp_path / "passwd"
    _passwd(passwd, "alice", uid, home)
    initial = _deletion_facts(home, uid)
    changed = dict(initial, home_inode=int(initial["home_inode"]) + 1)
    facts = iter((initial, changed))
    commands = []

    def runner(command, timeout=120):  # type: ignore[no-untyped-def]
        commands.append(command)
        return SimpleNamespace(stdout="")

    with pytest.raises(RuntimeError, match="稳定身份已变化"):
        user_deletion.delete_local_user(
            "alice",
            uid,
            str(home),
            False,
            os.getpid(),
            passwd_path=passwd,
            preflight_reader=lambda: next(facts),
            runner=runner,
            process_terminator=lambda *args, **kwargs: {
                "terminated_pids": [],
                "escalated_pids": [],
            },
            sudo_revoker=lambda *args, **kwargs: {"changed": False},
            require_root=False,
        )

    assert not any(Path(command[0]).name == "userdel" for command in commands)
    assert any("--unlock" in command for command in commands)
