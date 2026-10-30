"""SSH 传输适配层。

默认调用 Windows OpenSSH 客户端，以完整保留用户的 ``Include``、``ProxyJump``、
``ProxyCommand`` 与 ``IdentityFile`` 行为；对导入自 SSH 配置的主机只传别名，
让 OpenSSH 自己解析配置。传输层统一提供：超时、取消、输出上限与退出码诊断。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024
READ_CHUNK_SIZE = 8192
TERMINATE_GRACE_SECONDS = 3.0

_LONG_COMMAND_LINE_MESSAGE = (
    "SSH 命令行超过 Windows 限制（约 32767 字符）；探针源码应通过 stdin 传入，而不是拼进 argv"
)
_WINDOWS_COMMAND_LINE_TOO_LONG = frozenset({87, 206})


def _command_line_too_long(exc: OSError) -> bool:
    """CreateProcess 在参数过长时返回 WinError 87/206，而不是 ENOENT。"""

    winerror = getattr(exc, "winerror", None)
    if winerror in _WINDOWS_COMMAND_LINE_TOO_LONG:
        return True
    return getattr(exc, "errno", None) == 206


class TransportError(RuntimeError):
    """本机传输层故障（缺少 ssh 客户端等），并非远端执行失败。"""


@dataclass(frozen=True)
class SshTarget:
    alias: str
    hostname: str | None = None
    user: str | None = None
    port: int = 22
    identity_file: str | None = None
    proxy_jump: str | None = None
    proxy_command: str | None = None
    connect_timeout: int = 10
    use_config_alias: bool = False

    @property
    def destination(self) -> str:
        if self.use_config_alias:
            return self.alias
        host = self.hostname or self.alias
        return f"{self.user}@{host}" if self.user else host


@dataclass
class CommandResult:
    argv: list[str]
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    local_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    @property
    def combined_output(self) -> str:
        parts = [part for part in (self.stdout, self.stderr) if part]
        return "\n".join(parts)


class SshTransport(Protocol):
    def run(
        self,
        target: SshTarget,
        command: str,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
        stdin_data: bytes | None = None,
        max_output_bytes: int | None = None,
    ) -> CommandResult: ...


def expand_identity_file(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def build_ssh_argv(
    target: SshTarget,
    command: str,
    *,
    ssh_binary: str = "ssh",
    config_path: Path | None = None,
) -> list[str]:
    """构造 ssh 参数；导入自配置的主机依赖别名解析，其余字段显式传入。"""

    argv: list[str] = [ssh_binary]
    if target.use_config_alias:
        if config_path is not None:
            argv.extend(["-F", str(config_path)])
    else:
        if target.identity_file:
            argv.extend(["-i", expand_identity_file(target.identity_file)])
        if target.proxy_jump:
            argv.extend(["-J", target.proxy_jump])
        if target.proxy_command:
            argv.extend(["-o", f"ProxyCommand={target.proxy_command}"])
        argv.extend(["-p", str(target.port)])

    connect_timeout = target.connect_timeout if target.connect_timeout is not None else 10
    argv.extend(
        [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "NumberOfPasswordPrompts=0",
            "-o",
            f"ConnectTimeout={max(1, int(connect_timeout))}",
            "--",
            target.destination,
            command,
        ]
    )
    return argv


def _decode(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", errors="replace")


class OpenSshTransport:
    """基于子进程的 OpenSSH 传输实现。"""

    def __init__(
        self,
        *,
        ssh_binary: str = "ssh",
        config_path: Path | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        self.ssh_binary = ssh_binary
        self.config_path = config_path
        self.max_output_bytes = max_output_bytes
        self.default_timeout_seconds = default_timeout_seconds

    def run(
        self,
        target: SshTarget,
        command: str,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
        stdin_data: bytes | None = None,
        max_output_bytes: int | None = None,
    ) -> CommandResult:
        argv = build_ssh_argv(
            target, command, ssh_binary=self.ssh_binary, config_path=self.config_path
        )
        limit = max_output_bytes or self.max_output_bytes
        effective_timeout = timeout if timeout is not None else self.default_timeout_seconds
        started = time.monotonic()

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(  # noqa: S603 - 参数为 argv 列表，不经过 shell
                argv,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags,
            )
        except FileNotFoundError as exc:
            if _command_line_too_long(exc):
                raise TransportError(_LONG_COMMAND_LINE_MESSAGE) from exc
            raise TransportError(
                f"找不到 SSH 客户端 {self.ssh_binary!r}；请安装 Windows OpenSSH 客户端"
            ) from exc
        except OSError as exc:  # pragma: no cover - 平台相关
            if _command_line_too_long(exc):
                raise TransportError(_LONG_COMMAND_LINE_MESSAGE) from exc
            raise TransportError(f"启动 SSH 客户端失败：{exc}") from exc

        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        state = {"truncated": False}

        def reader(stream: object, sink: bytearray) -> None:
            assert process.stdout is not None
            while True:
                chunk = stream.read(READ_CHUNK_SIZE)  # type: ignore[attr-defined]
                if not chunk:
                    break
                if len(sink) < limit:
                    sink.extend(chunk[: limit - len(sink)])
                else:
                    state["truncated"] = True

        stdout_thread = threading.Thread(
            target=reader, args=(process.stdout, stdout_buffer), daemon=True
        )
        stderr_thread = threading.Thread(
            target=reader, args=(process.stderr, stderr_buffer), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        stdin_thread: threading.Thread | None = None
        if stdin_data is not None and process.stdin is not None:

            def write_stdin() -> None:
                try:
                    process.stdin.write(stdin_data)  # type: ignore[union-attr]
                    process.stdin.flush()  # type: ignore[union-attr]
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    with contextlib.suppress(OSError):
                        process.stdin.close()  # type: ignore[union-attr]

            # 探针源码可达数十 KB；放到线程里写，避免阻塞进程时无法执行超时/取消。
            stdin_thread = threading.Thread(target=write_stdin, daemon=True)
            stdin_thread.start()

        deadline = time.monotonic() + effective_timeout if effective_timeout else None
        cancelled = False
        timed_out = False
        while True:
            if process.poll() is not None:
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                self._terminate(process)
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                self._terminate(process)
                break
            time.sleep(0.02)

        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况下强制回收
            process.kill()
            process.wait(timeout=TERMINATE_GRACE_SECONDS)

        stdout_thread.join(timeout=TERMINATE_GRACE_SECONDS)
        stderr_thread.join(timeout=TERMINATE_GRACE_SECONDS)
        if stdin_thread is not None:
            stdin_thread.join(timeout=TERMINATE_GRACE_SECONDS)

        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = None if (timed_out or cancelled) else process.returncode
        return CommandResult(
            argv=argv,
            command=command,
            exit_code=exit_code,
            stdout=_decode(stdout_buffer),
            stderr=_decode(stderr_buffer),
            duration_ms=duration_ms,
            truncated=state["truncated"],
            timed_out=timed_out,
            cancelled=cancelled,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            process.terminate()
        except OSError:  # pragma: no cover - 进程已结束
            return
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:  # pragma: no cover - Windows 上 terminate 可能不可靠
            with contextlib.suppress(OSError):
                process.kill()


@dataclass
class RecordedCall:
    target: SshTarget
    command: str
    timeout: float | None
    stdin_data: bytes | None
    max_output_bytes: int | None


@dataclass
class FakeTransport:
    """测试用传输：按命令前缀返回预置结果，并记录调用。"""

    responses: list[CommandResult] = field(default_factory=list)
    default_stdout: str = ""
    calls: list[RecordedCall] = field(default_factory=list)

    def run(
        self,
        target: SshTarget,
        command: str,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
        stdin_data: bytes | None = None,
        max_output_bytes: int | None = None,
    ) -> CommandResult:
        self.calls.append(
            RecordedCall(
                target=target,
                command=command,
                timeout=timeout,
                stdin_data=stdin_data,
                max_output_bytes=max_output_bytes,
            )
        )
        if self.responses:
            return self.responses.pop(0)
        return CommandResult(
            argv=["ssh", target.destination, command],
            command=command,
            exit_code=0,
            stdout=self.default_stdout,
            stderr="",
            duration_ms=1,
        )
