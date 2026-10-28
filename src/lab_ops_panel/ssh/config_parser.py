"""OpenSSH 客户端配置解析器。

只导入显式 ``Host``（不含通配与取反），但会按 OpenSSH 的“首次取值获胜”语义
应用 ``Host *`` 等默认块，从而与 ``ssh <alias>`` 的实际行为保持一致。
解析失败会记录到 ``issues`` 并展示，从不静默降级。
"""

from __future__ import annotations

import fnmatch
import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

WILDCARD_CHARS = ("*", "?")
DEFAULT_PORT = 22
DEFAULT_CONNECT_TIMEOUT = 10
MAX_INCLUDE_DEPTH = 8
MAX_INCLUDE_FILES = 64


@dataclass(frozen=True)
class ParseIssue:
    path: Path
    line: int
    message: str


@dataclass(frozen=True)
class IncludeResult:
    pattern: str
    resolved: list[Path] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class Statement:
    keyword: str
    args: tuple[str, ...]
    path: Path
    line: int


@dataclass(frozen=True)
class HostBlock:
    kind: str  # global | host | match
    patterns: tuple[str, ...]
    statements: tuple[Statement, ...]
    line: int
    path: Path


@dataclass(frozen=True)
class SshHostEntry:
    alias: str
    hostname: str
    user: str | None
    port: int
    identity_file: str | None
    proxy_jump: str | None
    proxy_command: str | None
    connect_timeout: int
    line_number: int
    source_path: Path
    hostname_explicit: bool = True


@dataclass
class ParsedSshConfig:
    path: Path
    exists: bool
    hosts: list[SshHostEntry] = field(default_factory=list)
    includes: list[IncludeResult] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)


class _ParseFailure(ValueError):
    pass


def split_config_line(line: str) -> list[str]:
    """按 OpenSSH 规则切分一行：空白或 ``=`` 分隔，支持引号与 ``#`` 注释。"""

    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if quote is not None:
            if char == "\\" and quote == '"' and index + 1 < length:
                current.append(line[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            else:
                current.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char in (" ", "\t", "="):
            if current:
                tokens.append("".join(current))
                current = []
        elif char == "#" and not current:
            break
        else:
            current.append(char)
        index += 1
    if quote is not None:
        raise _ParseFailure("引号未闭合")
    if current:
        tokens.append("".join(current))
    return tokens


def _expand_path(value: str, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _is_explicit_pattern(pattern: str) -> bool:
    return not pattern.startswith("!") and not any(char in pattern for char in WILDCARD_CHARS)


class SshConfigParser:
    """解析单个 OpenSSH 配置文件（含 ``Include`` 递归）。"""

    def __init__(self, path: Path, *, max_include_depth: int = MAX_INCLUDE_DEPTH) -> None:
        self.path = Path(path)
        self.max_include_depth = max_include_depth
        self._includes: list[IncludeResult] = []
        self._issues: list[ParseIssue] = []
        self._visited: set[Path] = set()
        self._include_files = 0

    def parse(self) -> ParsedSshConfig:
        self._includes = []
        self._issues = []
        self._visited = set()
        self._include_files = 0
        if not self.path.exists():
            return ParsedSshConfig(path=self.path, exists=False)
        statements = self._read_file(self.path, depth=0)
        blocks = self._build_blocks(statements)
        hosts = self._resolve_hosts(blocks)
        return ParsedSshConfig(
            path=self.path,
            exists=True,
            hosts=hosts,
            includes=list(self._includes),
            issues=list(self._issues),
        )

    # -- 读取 ---------------------------------------------------------------

    def _read_file(self, path: Path, *, depth: int) -> list[Statement]:
        resolved = path.resolve()
        if depth > self.max_include_depth:
            self._issues.append(ParseIssue(path, 1, "Include 嵌套层级过深，已停止解析"))
            return []
        if resolved in self._visited:
            self._issues.append(ParseIssue(path, 1, "检测到循环 Include，已跳过该文件"))
            return []
        self._visited.add(resolved)
        statements: list[Statement] = []
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            self._issues.append(ParseIssue(path, 1, f"无法读取配置文件：{exc}"))
            return []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                tokens = split_config_line(raw_line)
            except _ParseFailure as exc:
                self._issues.append(ParseIssue(path, line_number, str(exc)))
                continue
            if not tokens:
                continue
            keyword = tokens[0].lower()
            args = tokens[1:]
            if keyword == "include":
                statements.extend(self._handle_include(args, path, line_number, depth))
                continue
            if not args:
                self._issues.append(ParseIssue(path, line_number, f"{tokens[0]} 缺少参数"))
                continue
            statements.append(Statement(keyword, tuple(args), path, line_number))
        return statements

    def _handle_include(
        self, args: list[str], path: Path, line_number: int, depth: int
    ) -> list[Statement]:
        if not args:
            self._issues.append(ParseIssue(path, line_number, "Include 缺少参数"))
            return []
        collected: list[Statement] = []
        for pattern in args:
            base_dir = path.parent
            expanded = os.path.expanduser(os.path.expandvars(pattern))
            if not os.path.isabs(expanded):
                expanded = str(base_dir / expanded)
            matches = sorted(glob.glob(expanded))
            resolved_paths = [Path(match) for match in matches if Path(match).is_file()]
            if not resolved_paths:
                self._includes.append(
                    IncludeResult(pattern=pattern, resolved=[], error="未匹配任何文件")
                )
                continue
            if self._include_files + len(resolved_paths) > MAX_INCLUDE_FILES:
                self._issues.append(
                    ParseIssue(path, line_number, "Include 文件数量超过上限，后续文件被忽略")
                )
                self._includes.append(
                    IncludeResult(
                        pattern=pattern, resolved=resolved_paths, error="超过 Include 上限"
                    )
                )
                continue
            self._include_files += len(resolved_paths)
            self._includes.append(IncludeResult(pattern=pattern, resolved=resolved_paths))
            for resolved_path in resolved_paths:
                collected.extend(self._read_file(resolved_path, depth=depth + 1))
        return collected

    # -- 分组 ---------------------------------------------------------------

    def _build_blocks(self, statements: list[Statement]) -> list[HostBlock]:
        blocks: list[HostBlock] = []
        current: list[Statement] = []
        kind = "global"
        patterns: tuple[str, ...] = ()
        line = 0
        path = self.path

        def flush() -> None:
            nonlocal current
            if not current and kind == "global":
                return
            blocks.append(
                HostBlock(
                    kind=kind,
                    patterns=patterns,
                    statements=tuple(current),
                    line=line,
                    path=path,
                )
            )
            current = []

        for statement in statements:
            if statement.keyword in {"host", "match"}:
                flush()
                # Match 块只影响其后的指令；首版不评估条件，整块跳过并在文档中说明。
                kind = "host" if statement.keyword == "host" else "match"
                patterns = tuple(statement.args)
                line = statement.line
                path = statement.path
                continue
            current.append(statement)
        flush()
        return blocks

    # -- 解析 ---------------------------------------------------------------

    def _resolve_hosts(self, blocks: list[HostBlock]) -> list[SshHostEntry]:
        explicit: dict[str, tuple[int, Path]] = {}
        seen: set[str] = set()
        for block in blocks:
            if block.kind != "host":
                continue
            for pattern in block.patterns:
                if not _is_explicit_pattern(pattern):
                    continue
                key = pattern.lower()
                if key in seen:
                    # 同名 Host 块按 OpenSSH 语义合并，首个声明位置用于展示。
                    continue
                seen.add(key)
                explicit[pattern] = (block.line, block.path)
        hosts: list[SshHostEntry] = []
        for alias, (line_number, source_path) in explicit.items():
            entry = self._resolve_alias(alias, blocks, line_number, source_path)
            if entry is not None:
                hosts.append(entry)
        return hosts

    def _resolve_alias(
        self,
        alias: str,
        blocks: list[HostBlock],
        line_number: int,
        source_path: Path,
    ) -> SshHostEntry | None:
        values: dict[str, str] = {}
        for block in blocks:
            if not self._block_applies(block, alias):
                continue
            for statement in block.statements:
                keyword = statement.keyword
                if keyword in values:
                    continue
                if keyword in {
                    "hostname",
                    "user",
                    "port",
                    "identityfile",
                    "proxyjump",
                    "proxycommand",
                    "connecttimeout",
                }:
                    values[keyword] = " ".join(statement.args)
        first_line = line_number
        first_path = source_path
        port = DEFAULT_PORT
        if "port" in values:
            try:
                port = int(values["port"])
            except ValueError:
                self._issues.append(
                    ParseIssue(
                        first_path, first_line, f"Host {alias} 的 Port 不是整数，已跳过该主机"
                    )
                )
                return None
            if not 1 <= port <= 65535:
                self._issues.append(
                    ParseIssue(
                        first_path, first_line, f"Host {alias} 的 Port 超出范围，已跳过该主机"
                    )
                )
                return None
        connect_timeout = DEFAULT_CONNECT_TIMEOUT
        if "connecttimeout" in values:
            try:
                connect_timeout = int(values["connecttimeout"])
            except ValueError:
                self._issues.append(
                    ParseIssue(
                        first_path,
                        first_line,
                        f"Host {alias} 的 ConnectTimeout 不是整数，已使用默认 {DEFAULT_CONNECT_TIMEOUT}",
                    )
                )
                connect_timeout = DEFAULT_CONNECT_TIMEOUT
            else:
                if connect_timeout <= 0:
                    self._issues.append(
                        ParseIssue(
                            first_path,
                            first_line,
                            f"Host {alias} 的 ConnectTimeout 非正数，已使用默认 {DEFAULT_CONNECT_TIMEOUT}",
                        )
                    )
                    connect_timeout = DEFAULT_CONNECT_TIMEOUT
        hostname = values.get("hostname")
        return SshHostEntry(
            alias=alias,
            hostname=hostname or alias,
            user=values.get("user"),
            port=port,
            identity_file=values.get("identityfile"),
            proxy_jump=values.get("proxyjump"),
            proxy_command=values.get("proxycommand"),
            connect_timeout=connect_timeout,
            line_number=first_line,
            source_path=first_path,
            hostname_explicit=hostname is not None,
        )

    @staticmethod
    def _block_applies(block: HostBlock, alias: str) -> bool:
        if block.kind == "global":
            return True
        if block.kind != "host" or not block.patterns:
            return False
        if any(pattern.startswith("!") for pattern in block.patterns):
            negatives = [p[1:] for p in block.patterns if p.startswith("!")]
            positives = [p for p in block.patterns if not p.startswith("!")]
            if any(fnmatch.fnmatchcase(alias.lower(), n.lower()) for n in negatives):
                return False
            if not positives:
                return False
            return any(fnmatch.fnmatchcase(alias.lower(), p.lower()) for p in positives)
        if any(
            pattern in ("*",) or any(char in pattern for char in WILDCARD_CHARS)
            for pattern in block.patterns
        ):
            return any(fnmatch.fnmatchcase(alias.lower(), p.lower()) for p in block.patterns)
        return any(alias.lower() == pattern.lower() for pattern in block.patterns)
