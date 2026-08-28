#!/usr/bin/python3
"""安装 root 控制的特权助手和最小 sudo 白名单。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import pwd
import re
import stat
import subprocess
import tempfile
from pathlib import Path

USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,30}[$]?$")
ACTIONS = (
    "create-user",
    "ssh-import",
    "sudo-grant",
    "sudo-revoke",
    "pam-update",
    "pam-restore",
    "port-lookup",
    "process-terminate",
    "uid-processes-terminate",
    "user-delete-preflight",
    "user-delete",
)
HELPER = Path("/usr/local/libexec/labopspanel-privileged")
TOOL_DIRECTORY = Path("/usr/local/lib/labopspanel-tools")
TOOL_BINARY = TOOL_DIRECTORY / "lab-ops-panel/bin/labopspanel-privileged"
SUDOERS_DIRECTORY = Path("/etc/sudoers.d")
MARKER = "# 由 LabOpsPanel 安装器管理；请勿手工修改"


def sudoers_content(username: str, uid: int) -> str:
    """生成只允许已登记固定动作且绑定启动用户的 sudo 规则。"""

    if not USERNAME.fullmatch(username) or uid <= 0:
        raise ValueError("启动用户名或 UID 无效")
    commands = ", ".join(f"{HELPER} {action}" for action in ACTIONS)
    return f"{MARKER}\n# 启动用户 UID: {uid}\n{username} ALL=(root) NOPASSWD: {commands}\n"


def _run(command: list[str], env: dict[str, str] | None = None) -> None:
    """执行安装所需固定命令，并保留可诊断但不含凭据的输出。"""

    subprocess.run(command, check=True, timeout=300, env=env)


def _atomic_regular_file(path: Path, content: bytes, mode: int) -> None:
    """在目标目录创建 root 所有的普通临时文件后原子替换。"""

    path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_existing_sudoers(path: Path) -> bytes | None:
    """只允许更新安装器自己创建且仍为安全普通文件的规则。"""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise ValueError("已有 sudo 规则类型、所有者或权限不安全")
    content = path.read_bytes()
    if not content.decode("utf-8").startswith(MARKER):
        raise ValueError("拒绝覆盖非本安装器创建的 sudo 规则")
    return content


def install(username: str, uv_path: Path, project: Path) -> None:
    """安装隔离工具环境、非符号链接入口和经 visudo 验证的规则。"""

    if os.geteuid() != 0:
        raise PermissionError("安装必须由 root 执行")
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("当前版本只支持 amd64")
    account = pwd.getpwnam(username)
    if not USERNAME.fullmatch(username) or account.pw_uid <= 0:
        raise ValueError("必须指定现有普通本机启动用户")
    uv_path = uv_path.resolve(strict=True)
    if not uv_path.is_file() or not os.access(uv_path, os.X_OK):
        raise ValueError("uv 路径不可执行")
    if not (project / "pyproject.toml").is_file():
        raise ValueError("项目目录无效")

    visudo = Path("/usr/sbin/visudo")
    if not visudo.is_file():
        raise RuntimeError("系统缺少 /usr/sbin/visudo")
    _run([str(visudo), "-c"])
    TOOL_DIRECTORY.mkdir(parents=True, mode=0o755, exist_ok=True)
    os.chown(TOOL_DIRECTORY, 0, 0)
    os.chmod(TOOL_DIRECTORY, 0o755)
    environment = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "UV_TOOL_DIR": str(TOOL_DIRECTORY),
        "UV_TOOL_BIN_DIR": str(TOOL_DIRECTORY / ".bin"),
        "UV_CACHE_DIR": "/var/cache/labopspanel-uv",
        "UV_LINK_MODE": "copy",
    }
    _run(
        [
            str(uv_path),
            "tool",
            "install",
            "--force",
            "--python",
            "3.12",
            str(project.resolve()),
        ],
        environment,
    )
    source = TOOL_BINARY.resolve(strict=True)
    _atomic_regular_file(HELPER, source.read_bytes(), 0o755)

    rule = SUDOERS_DIRECTORY / f"labopspanel-web-uid-{account.pw_uid}"
    previous = _safe_existing_sudoers(rule)
    content = sudoers_content(username, account.pw_uid).encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{rule.name}.", dir=rule.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o440)
            os.fsync(stream.fileno())
        _run([str(visudo), "-c", "-f", str(temporary)])
        os.replace(temporary, rule)
        try:
            _run([str(visudo), "-c"])
        except Exception:
            if previous is None:
                rule.unlink(missing_ok=True)
            else:
                _atomic_regular_file(rule, previous, 0o440)
            _run([str(visudo), "-c"])
            raise
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    """解析显式启动用户，并支持无写入的安装计划预览。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="未来启动 LabOpsPanel 的普通本机用户名")
    parser.add_argument("--uv", type=Path, required=True, help="uv 可执行文件的绝对路径")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true", help="只输出计划，不进行写入")
    arguments = parser.parse_args()
    account = pwd.getpwnam(arguments.username)
    if arguments.dry_run:
        print(
            json.dumps(
                {
                    "architecture": platform.machine(),
                    "username": arguments.username,
                    "uid": account.pw_uid,
                    "helper": str(HELPER),
                    "tool_directory": str(TOOL_DIRECTORY),
                    "sudoers": str(SUDOERS_DIRECTORY / f"labopspanel-web-uid-{account.pw_uid}"),
                    "actions": ACTIONS,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    install(arguments.username, arguments.uv, arguments.project)
    print("特权助手与最小 sudo 白名单安装完成；请重新启动 LabOpsPanel。")


if __name__ == "__main__":
    main()
