"""用户口径与 sudo 三态测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from lab_ops_panel.remote.users import (
    included_by_scope,
    is_login_shell,
    normalize_users,
)

COLLECTED = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


def user_row(
    login: str = "alice",
    *,
    uid: int = 1001,
    shell: str = "/bin/bash",
    sudo_group: bool | None = False,
    sudo_policy: bool | None = None,
    sudo_effective: str = "unknown",
    home_storage: dict | None = None,
) -> dict:
    return {
        "login": login,
        "uid": uid,
        "gid": uid,
        "home": f"/home/{login}",
        "shell": shell,
        "system": uid < 1000,
        "primaryGroup": login,
        "supplementaryGroups": ["sudo", "docker"],
        "sudoGroupMember": sudo_group,
        "sudoPolicyPresent": sudo_policy,
        "sudoEffective": sudo_effective,
        "recentLogin": None,
        "homeStorage": home_storage
        if home_storage is not None
        else {
            "allocatedBytes": 1024,
            "apparentBytes": 2048,
            "fileCount": 3,
            "directoryCount": 1,
            "depth": 2,
            "truncated": False,
            "incomplete": False,
            "error": None,
        },
    }


def section(users: list[dict], **overrides) -> dict:
    data = {"thresholdUid": 1000, "users": users, "notes": ["note"]}
    data.update(overrides)
    return {"status": "ok", "error": None, "requiresSudo": False, "data": data}


def test_login_shell_filter() -> None:
    assert is_login_shell("/bin/bash") is True
    assert is_login_shell("/usr/sbin/nologin") is False
    assert is_login_shell("/bin/false") is False
    assert is_login_shell("") is False


def test_scope_excludes_system_accounts_by_default() -> None:
    assert included_by_scope(1000, "/bin/bash", threshold=1000, include_system=False) is True
    assert included_by_scope(999, "/bin/bash", threshold=1000, include_system=False) is False
    assert included_by_scope(0, "/bin/bash", threshold=1000, include_system=True) is True
    assert (
        included_by_scope(1001, "/usr/sbin/nologin", threshold=1000, include_system=True) is False
    )


def test_normalize_users_filters_and_sorts() -> None:
    result = normalize_users(
        section(
            [
                user_row("bob", uid=1002),
                user_row("root", uid=0),
                user_row("alice", uid=1001),
                user_row("daemon", uid=2, shell="/usr/sbin/nologin"),
            ]
        ),
        server_id=1,
        collected_at=COLLECTED,
        include_system=False,
    )

    assert [user.login for user in result.users] == ["alice", "bob"]
    assert result.status == "ok"
    assert result.notes == ["note"]


def test_normalize_users_include_system_keeps_root() -> None:
    result = normalize_users(
        section([user_row("root", uid=0), user_row("alice")]),
        server_id=1,
        collected_at=COLLECTED,
        include_system=True,
    )

    assert [user.login for user in result.users] == ["root", "alice"]
    assert result.users[0].system is True


def test_invalid_and_duplicate_rows_are_skipped() -> None:
    broken_login = user_row("bad login")
    missing_uid = user_row("missing")
    missing_uid["uid"] = None
    result = normalize_users(
        section([user_row("alice"), user_row("alice"), broken_login, missing_uid, "junk"]),
        server_id=1,
        collected_at=COLLECTED,
        include_system=False,
    )

    assert [user.login for user in result.users] == ["alice"]


def test_sudo_tristate_is_preserved() -> None:
    result = normalize_users(
        section(
            [
                user_row("alice", sudo_group=True, sudo_policy=None, sudo_effective="unknown"),
                user_row(
                    "bob", uid=1002, sudo_group=False, sudo_policy=False, sudo_effective="false"
                ),
                user_row(
                    "carol", uid=1003, sudo_group=None, sudo_policy=True, sudo_effective="true"
                ),
            ]
        ),
        server_id=1,
        collected_at=COLLECTED,
        include_system=False,
    )

    by_login = {user.login: user for user in result.users}
    assert by_login["alice"].sudo.group_member is True
    assert by_login["alice"].sudo.policy_present is None
    assert by_login["alice"].sudo.effective == "unknown"
    assert by_login["bob"].sudo.effective == "false"
    assert by_login["carol"].sudo.group_member is None
    assert by_login["carol"].sudo.effective == "true"
    assert by_login["alice"].sudo.checked_at is not None


def test_invalid_sudo_effective_falls_back_to_unknown() -> None:
    row = user_row("alice", sudo_effective="maybe")
    result = normalize_users(
        section([row]), server_id=1, collected_at=COLLECTED, include_system=False
    )

    assert result.users[0].sudo.effective == "unknown"


def test_home_storage_fields_are_normalized() -> None:
    row = user_row(
        "alice",
        home_storage={
            "allocatedBytes": 4096,
            "apparentBytes": 8192,
            "fileCount": 2,
            "directoryCount": 1,
            "depth": 2,
            "truncated": True,
            "incomplete": True,
            "error": "部分目录不可读",
        },
    )

    result = normalize_users(
        section([row]), server_id=1, collected_at=COLLECTED, include_system=False
    )

    storage = result.users[0].home_storage
    assert storage is not None
    assert storage.allocated_bytes == 4096
    assert storage.apparent_bytes == 8192
    assert storage.truncated is True
    assert storage.incomplete is True
    assert storage.error == "部分目录不可读"


def test_missing_home_storage_stays_none() -> None:
    row = user_row("alice")
    row["homeStorage"] = None

    result = normalize_users(
        section([row]), server_id=1, collected_at=COLLECTED, include_system=False
    )

    assert result.users[0].home_storage is None


def test_error_sections_are_passed_through() -> None:
    result = normalize_users(
        {"status": "permission_denied", "error": "Access denied", "data": None},
        server_id=1,
        collected_at=COLLECTED,
        include_system=False,
    )

    assert result.status == "permission_denied"
    assert result.error == "Access denied"
    assert result.users == []


def test_group_lists_are_bounded() -> None:
    row = user_row("alice")
    row["supplementaryGroups"] = [f"group-{index}" for index in range(100)]

    result = normalize_users(
        section([row]), server_id=1, collected_at=COLLECTED, include_system=False
    )

    assert len(result.users[0].supplementary_groups) == 64
