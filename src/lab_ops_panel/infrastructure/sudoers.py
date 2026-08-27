"""sudoers 文件的保守只读解析。"""

from __future__ import annotations

import grp
import pwd
import re
from pathlib import Path
from typing import Any

SAFE_DROP_IN_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
USER_ALIAS = re.compile(r"^User_Alias\s+([A-Z][A-Z0-9_]*)\s*=\s*(.+)$")
RULE_PRINCIPALS = re.compile(r"^([^\s]+)\s+")
SIMPLE_USERNAME = re.compile(r"^[a-z_][a-z0-9_.-]*[$]?$", re.IGNORECASE)


def _candidate_files(sudoers_path: Path, sudoers_directory: Path) -> list[Path]:
    """只纳入主文件及符合 sudo 命名规则的非符号链接 drop-in。"""

    result = [sudoers_path]
    try:
        entries = sorted(sudoers_directory.iterdir())
    except OSError:
        return result
    result.extend(
        entry
        for entry in entries
        if entry.is_file() and not entry.is_symlink() and SAFE_DROP_IN_NAME.fullmatch(entry.name)
    )
    return result


def _group_members(group_name: str, usernames: set[str]) -> set[str]:
    """同时考虑组成员列表与用户主 GID。"""

    try:
        group = grp.getgrnam(group_name)
    except KeyError:
        return set()
    members = set(group.gr_mem)
    members.update(entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == group.gr_gid)
    return members & usernames


def scan_sudoers_rules(
    usernames: set[str],
    sudoers_path: Path = Path("/etc/sudoers"),
    sudoers_directory: Path = Path("/etc/sudoers.d"),
) -> dict[str, Any]:
    """识别直接用户和 User_Alias 规则；复杂规则仅报告而不猜测。"""

    sources: dict[str, list[str]] = {username: [] for username in usernames}
    warnings: list[str] = []
    for path in _candidate_files(sudoers_path, sudoers_directory):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            warnings.append(f"无法读取 {path}")
            continue
        aliases: dict[str, set[str]] = {}
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            alias_match = USER_ALIAS.match(line)
            if alias_match:
                members = {value.strip() for value in alias_match.group(2).split(",")}
                aliases[alias_match.group(1)] = members & usernames
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith(
                ("#", "Defaults", "User_Alias", "Runas_Alias", "Host_Alias", "Cmnd_Alias")
            ):
                continue
            principal_match = RULE_PRINCIPALS.match(line)
            if not principal_match:
                warnings.append(f"无法识别 {path} 中的一条规则")
                continue
            principals = {value.strip() for value in principal_match.group(1).split(",")}
            matched: dict[str, str] = {}
            for principal in principals:
                if principal in usernames:
                    matched[principal] = ""
                elif principal in aliases:
                    matched.update({username: f" ({principal})" for username in aliases[principal]})
                elif principal.startswith("%"):
                    matched.update(
                        {
                            username: f" ({principal})"
                            for username in _group_members(principal[1:], usernames)
                        }
                    )
                elif SIMPLE_USERNAME.fullmatch(principal):
                    # 不存在的简单用户无需告警。
                    continue
                else:
                    warnings.append(f"{path} 包含复杂主体 {principal}，仅展示不自动解析")
            label = "/etc/sudoers" if path == sudoers_path else f"/etc/sudoers.d/{path.name}"
            for username, suffix in matched.items():
                sources[username].append(f"{label}{suffix}")
    return {
        "sources": sources,
        "complete": not warnings,
        "warnings": sorted(set(warnings)),
    }
