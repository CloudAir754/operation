"""本机账号只读采集规则测试。"""

from pathlib import Path

from lab_ops_panel.infrastructure.system_info import local_users


def test_local_users_omit_nologin_shell(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """不展示明确禁止登录的 nologin 系统账号。"""

    passwd_path = tmp_path / "passwd"
    passwd_path.write_text(
        "alice:x:1000:1000:Alice:/home/alice:/bin/bash\n"
        "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
        "service:x:999:999:Service:/srv/service:/bin/false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("lab_ops_panel.infrastructure.system_info._sudo_group_members", set)

    users = local_users(passwd_path)

    assert [user["username"] for user in users] == ["service", "alice"]


def test_local_users_ignore_malformed_record(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """异常 passwd 行不会破坏整个账号列表。"""

    passwd_path = tmp_path / "passwd"
    passwd_path.write_text(
        "broken-record\nvalid:x:1000:1000::/home/valid:/bin/bash\n", encoding="utf-8"
    )
    monkeypatch.setattr("lab_ops_panel.infrastructure.system_info._sudo_group_members", set)

    assert [user["username"] for user in local_users(passwd_path)] == ["valid"]
