"""把 SSH/远端失败翻译成可读诊断。

诊断信息只包含退出码、超时与已脱敏的输出尾部，不包含命令中的凭据。
"""

from __future__ import annotations

import re

from lab_ops_panel.ssh.transport import CommandResult

MAX_DIAGNOSIS_LENGTH = 400

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"permission denied", re.IGNORECASE), "认证失败：权限被拒绝（Permission denied）"),
    (
        re.compile(r"host key verification failed", re.IGNORECASE),
        "主机密钥校验失败：远端密钥与 known_hosts 记录不一致",
    ),
    (
        re.compile(r"remote host identification has changed", re.IGNORECASE),
        "远端主机密钥已变更，请确认后更新 known_hosts",
    ),
    (re.compile(r"could not resolve hostname", re.IGNORECASE), "无法解析主机名"),
    (re.compile(r"name or service not known", re.IGNORECASE), "无法解析主机名"),
    (re.compile(r"connection refused", re.IGNORECASE), "连接被拒绝：目标端口未监听或被防火墙拦截"),
    (re.compile(r"connection timed out|operation timed out", re.IGNORECASE), "连接超时"),
    (re.compile(r"no route to host", re.IGNORECASE), "主机不可达：没有到目标的路由"),
    (re.compile(r"network is unreachable", re.IGNORECASE), "网络不可达"),
    (re.compile(r"too many authentication failures", re.IGNORECASE), "认证失败次数过多"),
    (re.compile(r"kex_exchange_identification", re.IGNORECASE), "SSH 协议握手失败"),
    (re.compile(r"connection closed by", re.IGNORECASE), "远端主动关闭了连接"),
    (re.compile(r"command not found", re.IGNORECASE), "远端缺少所需命令"),
    (
        re.compile(r"sudo: a password is required", re.IGNORECASE),
        "sudo 需要密码，但未提供或密码无效",
    ),
    (re.compile(r"incorrect password|sorry, try again", re.IGNORECASE), "sudo 密码错误"),
    (re.compile(r"not permitted", re.IGNORECASE), "sudo 权限不足"),
    (
        re.compile(r"system has not been booted with systemd", re.IGNORECASE),
        "目标系统未使用 systemd",
    ),
    (re.compile(r"unit .* not found", re.IGNORECASE), "服务不存在"),
)


def _tail(text: str, limit: int = 220) -> str:
    cleaned = " / ".join(part.strip() for part in text.strip().splitlines() if part.strip())
    if len(cleaned) <= limit:
        return cleaned
    return f"…{cleaned[-limit:]}"


def diagnose_result(result: CommandResult, *, operation: str = "远程操作") -> str:
    if result.cancelled:
        return f"{operation}已取消"
    if result.timed_out:
        return f"{operation}超时"
    if result.ok:
        return ""

    haystack = "\n".join(part for part in (result.stderr, result.stdout) if part)
    for pattern, message in _PATTERNS:
        if pattern.search(haystack):
            detail = _tail(haystack)
            return message if not detail else f"{message}（{detail}）"

    exit_code = "未知" if result.exit_code is None else str(result.exit_code)
    detail = _tail(haystack)
    if detail:
        return f"{operation}失败（退出码 {exit_code}）：{detail}"
    return f"{operation}失败（退出码 {exit_code}，无输出）"
