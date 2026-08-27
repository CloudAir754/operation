"""Ubuntu PAM 密码策略的只读能力检测与保守解析。"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

KNOWN_SETTINGS = frozenset(
    {
        "minlen",
        "dcredit",
        "ucredit",
        "lcredit",
        "ocredit",
        "minclass",
        "maxrepeat",
        "maxsequence",
        "maxclassrepeat",
        "usercheck",
        "retry",
        "difok",
    }
)
INTEGER_SETTINGS = KNOWN_SETTINGS - {"usercheck"}


def _parse_assignments(path: Path) -> tuple[dict[str, int | bool], list[str]]:
    """解析 pwquality 键值文件，未知或异常行仅报告。"""

    values: dict[str, int | bool] = {}
    warnings: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values, warnings
    except OSError:
        return values, [f"无法读取 {path}"]
    for number, raw_line in enumerate(lines, 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            warnings.append(f"{path}:{number} 不是可识别的键值配置")
            continue
        key, raw_value = (value.strip() for value in line.split("=", 1))
        key = key.lower()
        if key not in KNOWN_SETTINGS:
            warnings.append(f"{path}:{number} 包含未知设置 {key}")
            continue
        try:
            if key == "usercheck":
                values[key] = raw_value.lower() not in {"0", "false", "no"}
            else:
                values[key] = int(raw_value)
        except ValueError:
            warnings.append(f"{path}:{number} 的 {key} 不是有效值")
    return values, warnings


def _pam_password_entries(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """读取 password 堆栈，不尝试改写未知控制表达式。"""

    entries = []
    warnings = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries, [f"无法读取 {path}"]
    for number, raw_line in enumerate(lines, 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            warnings.append(f"{path}:{number} 无法解析")
            continue
        if not tokens or tokens[0] != "password":
            continue
        module_index = next((index for index, token in enumerate(tokens) if ".so" in token), None)
        if module_index is None or module_index < 2:
            warnings.append(f"{path}:{number} 缺少可识别 PAM 模块")
            continue
        entries.append(
            {
                "line": number,
                "control": " ".join(tokens[1:module_index]),
                "module": tokens[module_index],
                "options": tokens[module_index + 1 :],
            }
        )
    return entries, warnings


def _module_settings(options: list[str]) -> tuple[dict[str, int | bool], list[str]]:
    """解析 pam_pwquality 模块参数，其优先级高于配置文件。"""

    values: dict[str, int | bool] = {}
    warnings = []
    for option in options:
        if "=" not in option:
            if option not in {"debug", "enforce_for_root", "local_users_only"}:
                warnings.append(f"pam_pwquality 包含未知选项 {option}")
            continue
        key, raw_value = option.split("=", 1)
        if key not in KNOWN_SETTINGS:
            warnings.append(f"pam_pwquality 包含未知设置 {key}")
            continue
        try:
            values[key] = int(raw_value) if key in INTEGER_SETTINGS else raw_value != "0"
        except ValueError:
            warnings.append(f"pam_pwquality 的 {key} 不是有效值")
    return values, warnings


def _generation_minimums(settings: dict[str, int | bool]) -> dict[str, int]:
    """将可证明的 pwquality 下限映射为应用随机生成下限。"""

    def required_from_credit(key: str) -> int:
        value = settings.get(key)
        return abs(value) if isinstance(value, int) and value < 0 else 0

    return {
        "length": max(15, int(settings.get("minlen", 0))),
        "min_digits": required_from_credit("dcredit"),
        "min_uppercase": required_from_credit("ucredit"),
        "min_lowercase": required_from_credit("lcredit"),
        "min_symbols": required_from_credit("ocredit"),
        "min_classes": max(0, int(settings.get("minclass", 0))),
    }


def pam_policy_snapshot(
    common_password: Path = Path("/etc/pam.d/common-password"),
    config_path: Path = Path("/etc/security/pwquality.conf"),
    config_directory: Path = Path("/etc/security/pwquality.conf.d"),
    module_glob: str = "/usr/lib/*/security/pam_pwquality.so",
) -> dict[str, Any]:
    """返回实际可见 PAM 堆栈、pwquality 配置和修改能力。"""

    settings: dict[str, int | bool] = {}
    config_sources = []
    warnings: list[str] = []
    try:
        drop_ins = sorted(
            path
            for path in config_directory.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and re.fullmatch(r"[A-Za-z0-9_.-]+\.conf", path.name)
        )
    except OSError:
        drop_ins = []
    for path in drop_ins:
        values, file_warnings = _parse_assignments(path)
        settings.update(values)
        warnings.extend(file_warnings)
        config_sources.append(str(path))
    # libpwquality 先读 ASCII 排序的 drop-in，再读主文件，因此主文件优先级更高。
    main_values, main_warnings = _parse_assignments(config_path)
    settings.update(main_values)
    warnings.extend(main_warnings)
    if main_values:
        config_sources.append(str(config_path))
    entries, pam_warnings = _pam_password_entries(common_password)
    warnings.extend(pam_warnings)
    pwquality_entries = [entry for entry in entries if entry["module"].endswith("pam_pwquality.so")]
    module_settings: dict[str, int | bool] = {}
    for entry in pwquality_entries:
        values, option_warnings = _module_settings(entry["options"])
        module_settings.update(values)
        settings.update(values)
        warnings.extend(option_warnings)
    module_installed = any(Path("/").glob(module_glob.lstrip("/")))
    recognized = len(pwquality_entries) <= 1 and not warnings
    if not module_installed:
        modification_reason = "系统未安装 pam_pwquality 模块"
    elif not pwquality_entries:
        modification_reason = "common-password 尚未启用 pam_pwquality"
    elif not recognized:
        modification_reason = "PAM 或 pwquality 配置包含无法安全理解的内容"
    else:
        modification_reason = None
    return {
        "common_password": str(common_password),
        "password_stack": entries,
        "pwquality_module_installed": module_installed,
        "pwquality_enabled": bool(pwquality_entries),
        "settings": settings,
        "module_settings": module_settings,
        "config_sources": config_sources,
        "generation_minimums": _generation_minimums(settings),
        "warnings": sorted(set(warnings)),
        "modification_supported": modification_reason is None,
        "modification_reason": modification_reason,
        "password_aging_managed": False,
    }
