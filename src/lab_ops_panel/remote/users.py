"""可登录用户与 sudo 三态归一化。

sudo 组成员、sudoers 策略存在、非交互 sudo 验证是三个不同事实；
任一不可判定时必须保留 ``null`` / ``unknown``，不得折算成 ``false``。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from lab_ops_panel.schemas_observability import (
    HomeStorageOut,
    SudoStateOut,
    UserOut,
    UsersOut,
)

NOLOGIN_SHELLS = frozenset(
    {
        "",
        "/usr/sbin/nologin",
        "/sbin/nologin",
        "/usr/bin/nologin",
        "/bin/false",
        "/usr/bin/false",
        "/bin/sync",
        "/usr/sbin/shutdown",
        "/sbin/shutdown",
        "/usr/sbin/halt",
        "/sbin/halt",
    }
)
DEFAULT_THRESHOLD_UID = 1000
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
MAX_USERS = 500
MAX_GROUPS = 64
MAX_TEXT = 256
SUDO_EFFECTIVE_VALUES = {"true", "false", "unknown"}


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    return text[:limit] if text else None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def is_login_shell(shell: str | None) -> bool:
    return (shell or "").strip() not in NOLOGIN_SHELLS


def _normalize_storage(raw: Any, collected_at: datetime) -> HomeStorageOut | None:
    if not isinstance(raw, dict):
        return None
    return HomeStorageOut(
        allocated_bytes=_int(raw.get("allocatedBytes")),
        apparent_bytes=_int(raw.get("apparentBytes")),
        file_count=_int(raw.get("fileCount")),
        directory_count=_int(raw.get("directoryCount")),
        depth=_int(raw.get("depth")),
        truncated=bool(raw.get("truncated", False)),
        incomplete=bool(raw.get("incomplete", False)),
        error=_text(raw.get("error"), 200),
        collected_at=collected_at,
    )


def _normalize_sudo(raw: Any, collected_at: datetime) -> SudoStateOut:
    data = raw if isinstance(raw, dict) else {}
    effective = data.get("effective")
    if effective not in SUDO_EFFECTIVE_VALUES:
        effective = "unknown"
    return SudoStateOut(
        group_member=_bool_or_none(data.get("groupMember")),
        policy_present=_bool_or_none(data.get("policyPresent")),
        effective=effective,  # type: ignore[arg-type]
        checked_at=collected_at if data else None,
        error=_text(data.get("error"), 200),
    )


def normalize_users(
    section: dict[str, Any] | None,
    *,
    server_id: int,
    collected_at: datetime,
    include_system: bool,
) -> UsersOut:
    raw = section if isinstance(section, dict) else {}
    status = (
        raw.get("status")
        if raw.get("status")
        in {
            "ok",
            "permission_denied",
            "unavailable",
            "error",
        }
        else "error"
    )
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    threshold = _int(data.get("thresholdUid"))
    if threshold is None or threshold < 0:
        threshold = DEFAULT_THRESHOLD_UID
    notes = [
        text
        for text in (_text(note, 240) for note in (data.get("notes") or raw.get("notes") or []))
        if text
    ][:8]

    users: list[UserOut] = []
    seen: set[str] = set()
    for item in data.get("users") or []:
        if not isinstance(item, dict):
            continue
        login = _text(item.get("login"), 64)
        uid = _int(item.get("uid"))
        gid = _int(item.get("gid"))
        home = _text(item.get("home"), MAX_TEXT)
        shell = _text(item.get("shell"), MAX_TEXT) or ""
        if not login or not LOGIN_PATTERN.match(login) or uid is None or gid is None:
            continue
        if login in seen:
            continue
        seen.add(login)
        if not included_by_scope(uid, shell, threshold=threshold, include_system=include_system):
            continue
        groups = [
            group
            for group in (_text(value, 64) for value in (item.get("supplementaryGroups") or []))
            if group
        ][:MAX_GROUPS]
        users.append(
            UserOut(
                login=login,
                uid=uid,
                gid=gid,
                primary_group=_text(item.get("primaryGroup"), 64),
                supplementary_groups=groups,
                home=home or "/",
                shell=shell,
                system=bool(item.get("system", uid < threshold)),
                recent_login=_text(item.get("recentLogin"), 64),
                sudo=_normalize_sudo(
                    item.get("sudo")
                    if isinstance(item.get("sudo"), dict)
                    else {
                        "groupMember": item.get("sudoGroupMember"),
                        "policyPresent": item.get("sudoPolicyPresent"),
                        "effective": item.get("sudoEffective"),
                    },
                    collected_at,
                ),
                home_storage=_normalize_storage(item.get("homeStorage"), collected_at),
            )
        )
    users.sort(key=lambda user: (user.uid, user.login))
    return UsersOut(
        server_id=server_id,
        collected_at=collected_at,
        include_system=include_system,
        status=status,  # type: ignore[arg-type]
        error=_text(raw.get("error"), 300),
        requires_sudo=bool(raw.get("requiresSudo", False)),
        users=users[:MAX_USERS],
        notes=notes,
    )


def included_by_scope(
    uid: int,
    shell: str | None,
    *,
    threshold: int,
    include_system: bool,
) -> bool:
    """是否属于“可登录用户”口径。"""

    if not include_system and uid < threshold:
        return False
    return is_login_shell(shell)
