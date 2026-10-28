"""构建 journalctl 命令（多处复用）。"""

from __future__ import annotations

import shlex

from lab_ops_panel.remote.services import normalize_unit_name


def build_journalctl_command(unit: str | None, lines: int) -> str:
    arguments = ["journalctl"]
    if unit:
        arguments.extend(["-u", normalize_unit_name(unit)])
    arguments.extend(["-n", str(lines), "--no-pager", "--output=short-iso"])
    return " ".join(shlex.quote(part) for part in arguments)
