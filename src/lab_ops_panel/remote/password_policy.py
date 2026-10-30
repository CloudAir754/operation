"""Ubuntu 密码策略：解析实际生效的 PAM/pwquality 配置并生成最小化改动。

设计约束：

- 以**实际生效值**为准：`pam_pwquality.so` 的行内参数优先于
  `/etc/security/pwquality.conf`。
- 只改受管键，未知行、注释与手工配置原样保留；PAM 行只在可识别的规范单行形态下
  才允许改参数，否则该键只读。
- 写入前生成 unified diff，写入前备份原文件，写入后重新读取并比对，失败立即回滚。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Final

from lab_ops_panel.passwords import PolicyFloor

POLICY_PATH = "/etc/security/pwquality.conf"
PAM_PASSWORD_PATH = "/etc/pam.d/common-password"
PAM_AUTH_PATH = "/etc/pam.d/common-auth"
LOGIN_DEFS_PATH = "/etc/login.defs"
BACKUP_DIR = "/var/backups/labopspanel"

MANAGED_KEYS: Final = (
    "minlen",
    "minclass",
    "dcredit",
    "ucredit",
    "lcredit",
    "ocredit",
    "maxrepeat",
    "maxsequence",
    "maxclassrepeat",
    "gecoscheck",
)
PAM_KEYS: Final = ("retry",)
FIELD_KEYS: Final = (*MANAGED_KEYS, *PAM_KEYS)

POLICY_LIMITS: Final[dict[str, tuple[int, int]]] = {
    "minlen": (8, 128),
    "minclass": (0, 4),
    "dcredit": (-128, 128),
    "ucredit": (-128, 128),
    "lcredit": (-128, 128),
    "ocredit": (-128, 128),
    "maxrepeat": (0, 128),
    "maxsequence": (0, 128),
    "maxclassrepeat": (0, 128),
    "gecoscheck": (0, 1),
    "retry": (1, 10),
}
DEFAULTS: Final[dict[str, int]] = {
    "minlen": 8,
    "minclass": 0,
    "dcredit": 0,
    "ucredit": 0,
    "lcredit": 0,
    "ocredit": 0,
    "maxrepeat": 0,
    "maxsequence": 0,
    "maxclassrepeat": 0,
    "gecoscheck": 0,
}
FIELD_LABELS: Final[dict[str, str]] = {
    "minlen": "最小长度",
    "minclass": "最少字符类别数",
    "dcredit": "数字类要求",
    "ucredit": "大写字母要求",
    "lcredit": "小写字母要求",
    "ocredit": "符号类要求",
    "maxrepeat": "最大重复字符数",
    "maxsequence": "最大连续字符数",
    "maxclassrepeat": "同类字符最大连续数",
    "gecoscheck": "禁止包含用户名",
    "retry": "认证重试次数",
}

_PWQUALITY_LINE = re.compile(
    r"^(?P<indent>\s*)password\s+(?P<control>\[[^\]]*\]|\S+)\s+"
    r"(?P<module>[^\s]*pam_(?:pwquality|cracklib)\.so)(?P<args>[^\n]*)$"
)
_UNIX_LINE = re.compile(r"^\s*auth\s+(?:\[[^\]]*\]|\S+)\s+[^\s]*pam_unix\.so(?P<args>[^\n]*)$")
_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


@dataclass
class ParsedConf:
    """pwquality.conf 的受管键值与无法识别的行数。"""

    settings: dict[str, int] = field(default_factory=dict)
    unknown_keys: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


@dataclass
class PamLine:
    present: bool = False
    line_index: int | None = None
    module: str | None = None
    raw: str = ""
    args: dict[str, str] = field(default_factory=dict)
    ambiguous: bool = False
    reason: str | None = None


@dataclass
class PolicySnapshot:
    os_info: dict[str, Any]
    conf_path: str
    conf_content: str
    conf: ParsedConf
    pwquality: PamLine
    unix_auth: PamLine
    login_defs: dict[str, str]
    capabilities: dict[str, Any]
    values: dict[str, int]
    origins: dict[str, str]
    notes: list[str]
    pam_password_content: str = ""
    auth_content: str = ""

    @property
    def writable_keys(self) -> list[str]:
        return list(self.capabilities.get("writableFields") or [])


def parse_conf(content: str) -> ParsedConf:
    parsed = ParsedConf(lines=content.splitlines())
    for line in parsed.lines:
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if "=" not in stripped:
            parsed.unknown_keys.append(stripped[:80])
            continue
        key, _, raw_value = stripped.partition("=")
        name = key.strip().lower()
        try:
            value = int(raw_value.strip())
        except ValueError:
            parsed.unknown_keys.append(stripped[:80])
            continue
        if name not in MANAGED_KEYS:
            parsed.unknown_keys.append(stripped[:80])
            continue
        parsed.settings[name] = value
    return parsed


def _parse_args(raw: str) -> tuple[dict[str, str], bool]:
    args: dict[str, str] = {}
    ambiguous = False
    for token in raw.split():
        if any(quote in token for quote in ('"', "'", "\\")):
            ambiguous = True
            continue
        if "=" in token:
            key, _, value = token.partition("=")
            args[key.strip().lower()] = value.strip()
        else:
            # 裸参数（例如 use_authtok）原样保留，但不是我们管理的键。
            args[token.strip().lower()] = ""
    return args, ambiguous


def find_pwquality_line(content: str) -> PamLine:
    matches: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(content.splitlines()):
        match = _PWQUALITY_LINE.match(line)
        if match is not None:
            matches.append((index, match))
    if not matches:
        return PamLine(reason="common-password 中未找到 pam_pwquality.so / pam_cracklib.so")
    if len(matches) > 1:
        index, match = matches[0]
        args, ambiguous = _parse_args(match.group("args"))
        return PamLine(
            present=True,
            line_index=index,
            module=match.group("module"),
            raw=match.group(0),
            args=args,
            ambiguous=True,
            reason="检测到多行密码质量模块，无法安全判定生效行",
        )
    index, match = matches[0]
    args, ambiguous = _parse_args(match.group("args"))
    reason = None
    if ambiguous:
        reason = "PAM 行包含引号或转义，按无法识别的手工配置处理（只读）"
    return PamLine(
        present=True,
        line_index=index,
        module=match.group("module"),
        raw=match.group(0),
        args=args,
        ambiguous=ambiguous,
        reason=reason,
    )


def find_unix_auth_line(content: str) -> PamLine:
    matches: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(content.splitlines()):
        match = _UNIX_LINE.match(line)
        if match is not None:
            matches.append((index, match))
    if not matches:
        return PamLine(reason="common-auth 中未找到 pam_unix.so")
    index, match = matches[0]
    args, ambiguous = _parse_args(match.group("args"))
    reason = None
    if len(matches) > 1:
        ambiguous = True
        reason = "检测到多行 pam_unix.so，无法安全判定生效行"
    elif ambiguous:
        reason = "PAM 行包含引号或转义，按无法识别的手工配置处理（只读）"
    return PamLine(
        present=True,
        line_index=index,
        module="pam_unix.so",
        raw=match.group(0),
        args=args,
        ambiguous=ambiguous,
        reason=reason,
    )


def parse_login_defs(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) == 2:
            values[parts[0]] = parts[1].strip()
    return values


def _as_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def build_snapshot(payload: dict[str, Any]) -> PolicySnapshot:
    """把探针返回的原始内容转换成结构化快照（不信任任何字段）。"""

    data = payload if isinstance(payload, dict) else {}
    conf_section = data.get("pwquality") if isinstance(data.get("pwquality"), dict) else {}
    pam_section = data.get("pam") if isinstance(data.get("pam"), dict) else {}
    capabilities_raw = (
        data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
    )
    os_info = data.get("os") if isinstance(data.get("os"), dict) else {}

    conf_content = str(conf_section.get("content") or "")
    conf = parse_conf(conf_content)
    pam_password = str(
        (pam_section.get("commonPassword") or {}).get("content")
        if isinstance(pam_section.get("commonPassword"), dict)
        else ""
    )
    pam_auth = str(
        (pam_section.get("commonAuth") or {}).get("content")
        if isinstance(pam_section.get("commonAuth"), dict)
        else ""
    )
    pwquality_line = find_pwquality_line(pam_password)
    unix_line = find_unix_auth_line(pam_auth)
    login_defs_section = data.get("loginDefs")
    login_defs_content = (
        str(login_defs_section.get("content") or "") if isinstance(login_defs_section, dict) else ""
    )
    login_defs = parse_login_defs(login_defs_content)

    values: dict[str, int] = {}
    origins: dict[str, str] = {}
    notes: list[str] = []
    # 无法可靠解析的 PAM 行不能作为“实际生效值”的依据，否则会误报。
    trusted_pam = pwquality_line.present and not pwquality_line.ambiguous
    trusted_auth = unix_line.present and not unix_line.ambiguous
    for key in MANAGED_KEYS:
        pam_value = _as_int(pwquality_line.args.get(key)) if trusted_pam else None
        if pam_value is not None:
            values[key] = pam_value
            origins[key] = "pam"
        elif key in conf.settings:
            values[key] = conf.settings[key]
            origins[key] = "conf"
        else:
            values[key] = DEFAULTS[key]
            origins[key] = "default"
    retry_value = _as_int(unix_line.args.get("retry")) if trusted_auth else None
    values["retry"] = retry_value if retry_value is not None else 3
    origins["retry"] = "pam" if retry_value is not None else "default"

    writable: list[str] = []
    if conf_section.get("exists") and conf_section.get("readable", True):
        for key in MANAGED_KEYS:
            if origins[key] == "pam":
                if trusted_pam:
                    writable.append(key)
            else:
                writable.append(key)
    if trusted_auth:
        writable.append("retry")

    capabilities = {
        "pwqualityConf": bool(conf_section.get("exists")),
        "pwqualityModule": pwquality_line.present,
        "pwqualityModuleEditable": pwquality_line.present and not pwquality_line.ambiguous,
        "pamRetryEditable": unix_line.present and not unix_line.ambiguous,
        "pwscore": bool(capabilities_raw.get("pwscore")),
        "chage": bool(capabilities_raw.get("chage")),
        "useradd": bool(capabilities_raw.get("useradd")),
        "rsync": bool(capabilities_raw.get("rsync")),
        "dropInDir": bool(conf_section.get("dropInDir")),
        "writableFields": writable,
        "readOnly": not writable,
        "reasons": [
            value
            for value in (
                pwquality_line.reason,
                unix_line.reason,
                None if conf_section.get("exists") else f"{POLICY_PATH} 不存在",
            )
            if value
        ],
    }
    if not pwquality_line.present:
        notes.append("PAM 栈中未启用 pam_pwquality，系统未强制密码质量；仅展示读取结果")
    if pwquality_line.present and pwquality_line.ambiguous:
        notes.append("PAM 行无法可靠解析，实际生效值可能与此处展示不同")
    if conf.unknown_keys:
        notes.append(f"pwquality.conf 中有 {len(conf.unknown_keys)} 行无法识别，写入时原样保留")
    if origins.get("minlen") == "pam":
        notes.append("minlen 由 PAM 行内参数决定，修改会改 common-password")

    return PolicySnapshot(
        os_info=os_info,
        conf_path=str(conf_section.get("path") or POLICY_PATH),
        conf_content=conf_content,
        conf=conf,
        pwquality=pwquality_line,
        unix_auth=unix_line,
        login_defs=login_defs,
        capabilities=capabilities,
        values=values,
        origins=origins,
        notes=notes,
        pam_password_content=pam_password,
        auth_content=pam_auth,
    )


def build_floor(snapshot: PolicySnapshot) -> PolicyFloor:
    """从实际生效值推导生成器下限。"""

    values = snapshot.values
    notes: list[str] = []
    if snapshot.origins.get("minlen") == "pam":
        notes.append("最小长度来自 PAM 行内参数")
    elif snapshot.origins.get("minlen") == "conf":
        notes.append("最小长度来自 pwquality.conf")
    else:
        notes.append("未显式配置最小长度，使用内置下限")

    def positive(key: str) -> int:
        value = values.get(key, 0)
        return value if value > 0 else 0

    return PolicyFloor(
        min_length=max(8, values.get("minlen", 8)),
        min_upper=positive("ucredit"),
        min_lower=positive("lcredit"),
        min_digits=positive("dcredit"),
        min_symbols=positive("ocredit"),
        min_classes=max(0, values.get("minclass", 0)),
        max_repeat=max(0, values.get("maxrepeat", 0)),
        max_sequence=max(0, values.get("maxsequence", 0)),
        max_class_repeat=max(0, values.get("maxclassrepeat", 0)),
        reject_username=values.get("gecoscheck", 0) == 1,
        source=snapshot.origins.get("minlen", "default"),
        notes=tuple(notes),
    )


def validate_targets(targets: dict[str, int]) -> dict[str, int]:
    """校验期望值范围；未知键与越界一律拒绝。"""

    cleaned: dict[str, int] = {}
    for key, value in targets.items():
        if key not in FIELD_KEYS:
            raise ValueError(f"未知策略字段：{key}")
        low, high = POLICY_LIMITS[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"策略字段 {key} 必须是整数")
        if value < low or value > high:
            raise ValueError(f"策略字段 {key} 必须在 {low}–{high} 之间")
        cleaned[key] = value
    return cleaned


def render_conf(before: str, desired: dict[str, int], *, header: str = "") -> str:
    """只在必要位置修改受管键，保留注释与未知行。"""

    lines = before.splitlines()
    pending = {key: value for key, value in desired.items() if key in MANAGED_KEYS}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip().lower()
        if key in pending:
            lines[index] = f"{key} = {pending.pop(key)}"
    if pending:
        if header:
            lines.append(header if header.startswith("#") else f"# {header}")
        for key in MANAGED_KEYS:
            if key in pending:
                lines.append(f"{key} = {pending[key]}")
    text = "\n".join(lines)
    return text + "\n" if before.endswith("\n") or not before else text


def render_pam_retry(before: str, retry: int) -> str:
    """只替换可识别 pam_unix 行里的 retry 参数。"""

    line = find_unix_auth_line(before)
    if not line.present or line.ambiguous or line.line_index is None:
        raise ValueError(line.reason or "无法安全修改 common-auth 中的 retry")
    lines = before.splitlines()
    tokens = lines[line.line_index].split()
    replaced = False
    for index, token in enumerate(tokens):
        if token.lower().startswith("retry="):
            tokens[index] = f"retry={retry}"
            replaced = True
            break
    if not replaced:
        tokens.append(f"retry={retry}")
    indent = re.match(r"^\s*", lines[line.line_index])
    lines[line.line_index] = (indent.group(0) if indent else "") + " ".join(tokens)
    text = "\n".join(lines)
    return text + "\n" if before.endswith("\n") else text


def render_pwquality_args(before: str, targets: dict[str, int]) -> str:
    """修改 pam_pwquality 行内参数（仅当键已存在于该行）。"""

    line = find_pwquality_line(before)
    if not line.present or line.ambiguous or line.line_index is None:
        raise ValueError(line.reason or "无法安全修改 pam_pwquality 参数")
    lines = before.splitlines()
    tokens = lines[line.line_index].split()
    for key, value in targets.items():
        replaced = False
        for index, token in enumerate(tokens):
            if token.lower().startswith(f"{key}="):
                tokens[index] = f"{key}={value}"
                replaced = True
                break
        if not replaced:
            tokens.append(f"{key}={value}")
    indent = re.match(r"^\s*", lines[line.line_index])
    lines[line.line_index] = (indent.group(0) if indent else "") + " ".join(tokens)
    text = "\n".join(lines)
    return text + "\n" if before.endswith("\n") else text


def unified_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path}（当前）",
            tofile=f"{path}（修改后）",
        )
    )


def changed_fields(current: dict[str, int], targets: dict[str, int]) -> dict[str, dict[str, int]]:
    return {
        key: {"before": current.get(key), "after": value}
        for key, value in targets.items()
        if current.get(key) != value
    }


__all__ = [
    "BACKUP_DIR",
    "FIELD_KEYS",
    "FIELD_LABELS",
    "MANAGED_KEYS",
    "PAM_AUTH_PATH",
    "PAM_KEYS",
    "PAM_PASSWORD_PATH",
    "POLICY_PATH",
    "PolicySnapshot",
    "build_floor",
    "build_snapshot",
    "changed_fields",
    "find_pwquality_line",
    "find_unix_auth_line",
    "parse_conf",
    "render_conf",
    "render_pam_retry",
    "render_pwquality_args",
    "unified_diff",
    "validate_targets",
]
