"""systemd 单元名规范化与服务保护规则。

受保护服务（SSH、网络、systemd 关键组件）禁止通过本应用停止或禁用，
避免管理员一次误操作就失去远端访问能力。
"""

from __future__ import annotations

import re

from lab_ops_panel.errors import ApiError

PROTECTED_UNIT_NAMES = frozenset(
    {
        "ssh",
        "sshd",
        "systemd-logind",
        "systemd-journald",
        "systemd-udevd",
        "systemd-networkd",
        "systemd-resolved",
        "dbus",
        "dbus-broker",
        "auditd",
        "polkit",
        "networking",
        "network-manager",
        "networkmanager",
        "netplan",
        "firewalld",
        "ufw",
        "iscsid",
        "multipathd",
    }
)

PROTECTED_PREFIXES = ("ssh", "systemd-", "dbus", "network", "netplan", "polkit")

# 这些动作会让主机可能失去访问能力，受保护服务一律拒绝。
BLOCKED_PROTECTED_VERBS = frozenset({"stop", "disable", "mask"})

_UNIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\\-]{0,199}$")


class UnitValidationError(ValueError):
    pass


class ProtectedServiceError(ApiError):
    def __init__(self, unit: str, verb: str) -> None:
        super().__init__(
            409,
            "protected_service",
            f"{unit} 属于受保护服务，禁止执行 {verb}；如确需操作，请在远端手动处理",
        )
        self.unit = unit
        self.verb = verb


def normalize_unit_name(unit: str) -> str:
    """校验并规范化 systemd 单元名。"""

    candidate = (unit or "").strip()
    if not candidate:
        raise UnitValidationError("服务名不能为空")
    if not _UNIT_PATTERN.match(candidate):
        raise UnitValidationError(f"服务名 {candidate!r} 含非法字符")
    if "." not in candidate:
        candidate = f"{candidate}.service"
    return candidate


def unit_base_name(unit: str) -> str:
    name = unit.strip()
    for suffix in (".service", ".socket", ".timer", ".target", ".mount"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def is_protected(unit: str) -> bool:
    base = unit_base_name(unit).lower()
    if base in PROTECTED_UNIT_NAMES:
        return True
    return any(base.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def ensure_action_allowed(verb: str, unit: str) -> None:
    if verb in BLOCKED_PROTECTED_VERBS and is_protected(unit):
        raise ProtectedServiceError(unit, verb)


def systemctl_command(verb: str, unit: str) -> str:
    return f"systemctl {verb} {unit}"
