"""账户生命周期测试用的“假远端”。

不是简单的队列式假传输：它维护一个最小远端状态（home/备份/目标/链接/空闲空间），
按命令内容返回探针 JSON 或执行结果，因此能覆盖迁移的 9 个阶段与回退路径。
所有断言只看命令字符串，绝不在本机执行任何真实系统命令。
"""

from __future__ import annotations

import json
import re
import shlex
import threading
from dataclasses import dataclass, field
from typing import Any

from lab_ops_panel.remote.probe import PROBE_VERSION
from lab_ops_panel.ssh.transport import CommandResult, SshTarget

_PROBE_CALL = re.compile(r'"\$PY" - (?P<mode>\S+)(?P<args>[^\n]*)')

POLICY_CONF_TEXT = """\
# 手工维护的注释必须保留
minlen = 8
enforcing = 1
"""

PAM_PASSWORD_TEXT = """\
password\trequisite\t\t\tpam_pwquality.so retry=3
password\t[success=1 default=ignore]\tpam_unix.so obscure use_authtok try_first_pass yescrypt
"""

PAM_AUTH_TEXT = """\
auth\t[success=1 default=ignore]\tpam_unix.so nullok retry=3
auth\trequisite\t\t\tpam_deny.so
"""


@dataclass
class FakeRemote:
    """按命令内容模拟远端状态。"""

    home: str = "/home/zhangsan"
    login: str = "zhangsan"
    uid: int = 1001
    gid: int = 1001
    target_root: str = "/data2/users"
    data_root: str = "/data2"
    target: str = ""
    backup: str = ""
    staging: str = ""
    source_exists: bool = True
    source_is_symlink: bool = False
    backup_exists: bool = False
    target_exists: bool = False
    staging_exists: bool = False
    free_bytes: int = 100 * 1024**3
    allocated_bytes: int = 64 * 1024 * 1024
    files: int = 12
    dirs: int = 4
    apparent_bytes: int = 80 * 1024 * 1024
    filesystem: str = "ext4"
    target_filesystem: str = "ext4"
    locked: bool = False
    lock_state: str = "P"
    remaining_processes: int = 0
    rsync_available: bool = True
    pwscore: bool = True
    existing_users: set[str] = field(default_factory=set)
    created_users: dict[str, dict[str, Any]] = field(default_factory=dict)
    datasets: dict[str, str] = field(default_factory=dict)
    policy_conf: str = ""
    pam_password: str = ""
    pam_auth: str = ""
    staged: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_on: dict[str, str] = field(default_factory=dict)
    last_stdin: bytes | None = None
    pwscore_minlen: int = 8
    backups: dict[str, str] = field(default_factory=dict)
    ignore_policy_write: bool = False

    def __post_init__(self) -> None:
        self.policy_conf = POLICY_CONF_TEXT
        self.pam_password = PAM_PASSWORD_TEXT
        self.pam_auth = PAM_AUTH_TEXT
        self.pwscore_minlen = 8

    # -- 传输接口 ------------------------------------------------------

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
            {
                "target": target,
                "command": command,
                "timeout": timeout,
                "stdin": stdin_data,
                "max": max_output_bytes,
            }
        )
        self.last_stdin = stdin_data
        if command.startswith("sudo -S -p '' -- sh -c "):
            command = shlex.split(command)[-1]
        match = _PROBE_CALL.search(command)
        if match is not None:
            return self._probe(match.group("mode"), match.group("args"))
        return self._shell(command)

    # -- 探针 ----------------------------------------------------------

    def _payload(self, mode: str, key: str, data: dict[str, Any]) -> CommandResult:
        return _result(
            command="probe",
            stdout=json.dumps(
                {
                    "probeVersion": PROBE_VERSION,
                    "collectedAt": "2026-10-30T12:00:00Z",
                    "mode": mode,
                    key: {"status": "ok", "error": None, "requiresSudo": False, "data": data},
                }
            ),
        )

    def _path_info(
        self,
        path: str,
        *,
        exists: bool,
        is_dir: bool,
        is_symlink: bool,
        device: int,
        filesystem: str,
        mount: str,
        free_bytes: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> dict[str, Any]:
        return {
            "path": path,
            "exists": exists,
            "isDir": is_dir,
            "isSymlink": is_symlink,
            "isMountPoint": path == mount,
            "device": device,
            "inode": 42,
            "uid": uid,
            "gid": gid,
            "mode": "755",
            "readOnly": False,
            "filesystem": filesystem,
            "mountPoint": mount,
            "local": True,
            "entryCount": 3 if is_dir else None,
            "freeBytes": free_bytes,
            "totalBytes": None,
            "freeInodes": 1_000_000,
        }

    def _probe(self, mode: str, raw_args: str) -> CommandResult:
        args = shlex.split(raw_args.strip()) if raw_args.strip() else []
        if mode == "password_policy":
            return self._payload(
                mode,
                "passwordPolicy",
                {
                    "os": {"id": "ubuntu", "versionId": "24.04", "prettyName": "Ubuntu 24.04 LTS"},
                    "pwquality": {
                        "path": "/etc/security/pwquality.conf",
                        "exists": True,
                        "readable": True,
                        "content": self.policy_conf,
                        "dropInDir": False,
                        "dropIns": [],
                    },
                    "pam": {
                        "commonPassword": {
                            "path": "/etc/pam.d/common-password",
                            "content": self.pam_password,
                        },
                        "commonAuth": {
                            "path": "/etc/pam.d/common-auth",
                            "content": self.pam_auth,
                        },
                    },
                    "loginDefs": {
                        "path": "/etc/login.defs",
                        "content": "PASS_MIN_LEN 8\nENCRYPT_METHOD yescrypt\n",
                    },
                    "capabilities": {
                        "pwscore": self.pwscore,
                        "chage": True,
                        "useradd": True,
                        "rsync": self.rsync_available,
                        "pamAuthUpdate": True,
                    },
                },
            )
        if mode == "account_info":
            login = args[0] if args else self.login
            return self._payload(
                mode,
                "accountInfo",
                {"account": self._account(login), "exists": True},
            )
        if mode == "home_inspect":
            return self._payload(mode, "homeInspect", self._home_inspect_data(args))
        if mode == "tree_stats":
            path = args[0] if args else self.home
            return self._payload(mode, "treeStats", self._tree_stats(path))
        if mode == "dataset_inspect":
            return self._payload(mode, "datasetInspect", self._dataset_data(args))
        raise AssertionError(f"未预期的探针模式：{mode}")

    def _account(self, login: str) -> dict[str, Any]:
        return {
            "login": login,
            "uid": self.uid,
            "gid": self.gid,
            "home": self.home,
            "shell": "/bin/bash",
            "passwordStatus": "L" if self.locked else "P",
        }

    def _home_inspect_data(self, args: list[str]) -> dict[str, Any]:
        source = args[0] if args else self.home
        target_root = args[1] if len(args) > 1 else self.target_root
        target = args[2] if len(args) > 2 else self.target
        staging = args[3] if len(args) > 3 else self.staging
        backup = args[4] if len(args) > 4 else self.backup
        self.target, self.staging, self.backup = target, staging, backup
        return {
            "source": self._path_info(
                source,
                exists=self.source_exists,
                is_dir=self.source_exists,
                is_symlink=self.source_is_symlink,
                device=1,
                filesystem=self.filesystem,
                mount="/home",
                uid=self.uid,
                gid=self.gid,
            ),
            "sourceParent": self._path_info(
                "/home",
                exists=True,
                is_dir=True,
                is_symlink=False,
                device=1,
                filesystem=self.filesystem,
                mount="/home",
            ),
            "targetRoot": self._path_info(
                target_root,
                exists=True,
                is_dir=True,
                is_symlink=False,
                device=2,
                filesystem=self.target_filesystem,
                mount=target_root,
                free_bytes=self.free_bytes,
            ),
            "targetPath": self._path_info(
                target,
                exists=self.target_exists,
                is_dir=self.target_exists,
                is_symlink=False,
                device=2,
                filesystem=self.target_filesystem,
                mount=target_root,
                uid=self.uid,
                gid=self.gid,
            ),
            "stagingPath": self._path_info(
                staging,
                exists=self.staging_exists,
                is_dir=self.staging_exists,
                is_symlink=False,
                device=2,
                filesystem=self.target_filesystem,
                mount=target_root,
            ),
            "backupPath": self._path_info(
                backup,
                exists=self.backup_exists,
                is_dir=self.backup_exists,
                is_symlink=False,
                device=1,
                filesystem=self.filesystem,
                mount="/home",
            ),
            "subMounts": [],
            "counts": {
                "files": self.files,
                "dirs": self.dirs,
                "apparentBytes": self.apparent_bytes,
                "allocatedBytes": self.allocated_bytes,
                "inodes": self.files + self.dirs + 1,
                "truncated": False,
                "incomplete": False,
            },
            "account": self._account(self.login),
            "capabilities": {
                "rsync": self.rsync_available,
                "getfacl": True,
                "setfacl": True,
                "loginctl": True,
                "systemctl": True,
                "pkill": True,
            },
        }

    def _tree_stats(self, path: str) -> dict[str, Any]:
        exists = (
            (path == self.backup and self.backup_exists)
            or (path == self.target and self.target_exists)
            or (path == self.staging and self.staging_exists)
            or (path == self.home and self.source_exists)
        )
        return {
            "path": path,
            "files": self.files if exists else 0,
            "dirs": self.dirs if exists else 0,
            "apparentBytes": self.apparent_bytes if exists else 0,
            "allocatedBytes": self.allocated_bytes if exists else 0,
            "inodes": (self.files + self.dirs + 1) if exists else 0,
            "truncated": False,
            "incomplete": False,
            "error": None,
            "elapsedMs": 5,
        }

    def _dataset_data(self, args: list[str]) -> dict[str, Any]:
        login = args[0] if args else self.login
        data_root = args[1] if len(args) > 1 else self.data_root
        data_path = args[2] if len(args) > 2 else f"{data_root}/{login}"
        link_path = args[3] if len(args) > 3 else f"{self.home}/data2_disk"
        link_target = self.datasets.get(link_path)
        return {
            "account": self._account(login),
            "dataRoot": self._path_info(
                data_root,
                exists=True,
                is_dir=True,
                is_symlink=False,
                device=2,
                filesystem=self.target_filesystem,
                mount=data_root,
                free_bytes=self.free_bytes,
            ),
            "dataPath": self._path_info(
                data_path,
                exists=data_path in self.datasets.values(),
                is_dir=data_path in self.datasets.values(),
                is_symlink=False,
                device=2,
                filesystem=self.target_filesystem,
                mount=data_root,
                uid=self.uid,
                gid=self.gid,
            ),
            "linkPath": self._path_info(
                link_path,
                exists=link_target is not None,
                is_dir=False,
                is_symlink=link_target is not None,
                device=2,
                filesystem=self.target_filesystem,
                mount=data_root,
            ),
            "linkTarget": link_target,
            "capabilities": {"install": True, "mkdir": True, "ln": True},
        }

    # -- 普通命令 ------------------------------------------------------

    def _shell(self, command: str) -> CommandResult:
        # 顺序很重要：先匹配特征字符串，再匹配通用片段。
        if "umask 077; cat >" in command:
            staging = command.split("cat >", 1)[1].split("&&", 1)[0].strip()
            data = (self.last_stdin or b"").decode("utf-8")
            self.staged[staging.strip("'\"")] = data
            return _result(command, "LABOPS_CRED=ok")
        if "install -m 644" in command:
            parts = shlex.split(command)
            staging_index = next(
                (
                    index
                    for index, part in enumerate(parts)
                    if part.startswith("/tmp/labops-policy-")
                ),
                None,
            )
            if staging_index is None or staging_index + 1 >= len(parts):
                return _result(command, "")
            source, destination = parts[staging_index], parts[staging_index + 1]
            content = self.staged.pop(source, "")
            if self.ignore_policy_write:
                return _result(command, "")
            if destination == "/etc/security/pwquality.conf":
                self.policy_conf = content
                match = re.search(r"^minlen\s*=\s*(\d+)$", content, re.MULTILINE)
                if match:
                    self.pwscore_minlen = int(match.group(1))
            elif destination == "/etc/pam.d/common-password":
                self.pam_password = content
            elif destination == "/etc/pam.d/common-auth":
                self.pam_auth = content
            return _result(command, "")
        if "LABOPS_PREFLIGHT" in command:
            lines = [f"LABOPS_EXISTS={login}" for login in sorted(self.existing_users)]
            return _result(command, "\n".join(lines + ["LABOPS_PREFLIGHT=done"]))
        if "useradd" in command:
            return self._create_user(command)
        if "pkill -TERM" in command:
            if self.remaining_processes:
                self.locked = True
            return _result(
                command,
                f"LABOPS_LOCK_BEFORE={self.lock_state}\nLABOPS_REMAINING={self.remaining_processes}",
            )
        if "LABOPS_LOCK_AFTER" in command:
            self.locked = False
            return _result(command, f"LABOPS_LOCK_AFTER={self.lock_state}")
        if "LABOPS_FUNCTIONAL" in command:
            return _result(
                command,
                "LABOPS_FUNCTIONAL=ok\nLABOPS_SSH_DIR=700\nLABOPS_SSH_KEY=600\nLABOPS_SSH_OWNER=1001",
            )
        if "LABOPS_BACKUP_FAILED" in command:
            for source, backup in re.findall(r"cp -a '?([^'\s]+)'? '?([^'\s]+)'?", command):
                current = {
                    "/etc/security/pwquality.conf": self.policy_conf,
                    "/etc/pam.d/common-password": self.pam_password,
                    "/etc/pam.d/common-auth": self.pam_auth,
                }.get(source)
                if current is not None:
                    self.backups[backup] = current
            return _result(command, "LABOPS_BACKUP_FAILED=0\nLABOPS_BACKUP=ok")
        if "LABOPS_POLICY_RESTORE" in command:
            for backup, source in re.findall(r"cp -a '?([^'\s]+)'? '?([^'\s]+)'?", command):
                content = self.backups.get(backup)
                if content is None:
                    continue
                if source == "/etc/security/pwquality.conf":
                    self.policy_conf = content
                    match = re.search(r"^minlen\s*=\s*(\d+)$", content, re.MULTILINE)
                    self.pwscore_minlen = int(match.group(1)) if match else 8
                elif source == "/etc/pam.d/common-password":
                    self.pam_password = content
                elif source == "/etc/pam.d/common-auth":
                    self.pam_auth = content
            return _result(command, "LABOPS_POLICY_RESTORE=ok")
        if "mv -T" in command and "ln -s" in command:
            self.target_exists = True
            self.staging_exists = False
            self.source_is_symlink = True
            return _result(command, f"LABOPS_RESOLVED={self.target}\nLABOPS_RESULT=ok")
        if 'mv -T -- "$backup" "$source"' in command:
            self.backup_exists = False
            self.source_exists = True
            self.source_is_symlink = False
            return _result(command, "LABOPS_RESULT=ok")
        if 'mv -T -- "$source" "$backup"' in command:
            self.source_exists = False
            self.backup_exists = True
            return _result(command, "LABOPS_RESULT=ok")
        if "rsync -aHAX" in command and "--out-format" in command:
            return _result(command, "LABOPS_CHANGED=0\nLABOPS_RESULT=ok")
        if "rsync -aHAX" in command:
            self.staging_exists = True
            self.free_bytes -= self.allocated_bytes
            return _result(command, "LABOPS_RESULT=ok")
        if "rm -rf --one-file-system" in command:
            self.backup_exists = False
            return _result(command, "LABOPS_RESULT=ok")
        if "path=" in command and "link=" in command:
            return self._allocate_dataset(command)
        if 'rm -f -- "$link"' in command:
            link_match = re.search(r"^link=(\S+)$", command, re.MULTILINE)
            link = (link_match.group(1) if link_match else "").strip("'\"")
            self.datasets.pop(link, None)
            return _result(command, "LABOPS_RESULT=ok")
        if "LABOPS_WRITABLE" in command:
            return _result(command, "LABOPS_WRITABLE=ok")
        if "LABOPS_CLEANUP" in command:
            return _result(command, "LABOPS_CLEANUP=ok")
        if "pwscore" in command or "LABOPS_SAMPLE" in command:
            return _result(command, self._pwscore_output(command))
        return _result(command, "")

    def _pwscore_output(self, command: str) -> str:
        # 用当前 pwquality.conf 的 minlen 模拟 pwscore 判定。
        lines = []
        pattern = r'printf (.*?) \| "\$cmd"'
        for index, match in enumerate(re.finditer(pattern, command)):
            raw = match.group(1).replace('"', "").replace("'", "")
            sample = raw.split("\\n", 1)[-1].strip()
            ok = len(sample) >= self.pwscore_minlen
            lines.append(f"LABOPS_SAMPLE_{index}={'ok' if ok else 'fail'}")
        return "\n".join(lines)

    def _create_user(self, command: str) -> CommandResult:
        login_match = re.search(r"^login=(\S+)$", command, re.MULTILINE)
        login = (login_match.group(1) if login_match else "").strip("'\"")
        if login in self.existing_users:
            return _result(command, "LABOPS_RESULT=exists")
        self.created_users[login] = {"uid": self.uid + len(self.created_users) + 1}
        return _result(
            command,
            "LABOPS_RESULT=ok\n"
            f"LABOPS_UID={self.uid + len(self.created_users)}\n"
            f"LABOPS_HOME=/home/{login}\n"
            f"LABOPS_HOME_OWNER={self.uid}:{self.gid}",
        )

    def _allocate_dataset(self, command: str) -> CommandResult:
        path_match = re.search(r"path=(\S+)", command)
        link_match = re.search(r"link=(\S+)", command)
        path = (path_match.group(1) if path_match else "").strip("'\"")
        link = (link_match.group(1) if link_match else "").strip("'\"")
        self.datasets[link] = path
        return _result(command, "LABOPS_LINK=created\nLABOPS_RESULT=ok")


def _result(command: str, stdout: str, *, exit_code: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(
        argv=["ssh", "host", command],
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
    )


def commands(remote: FakeRemote) -> list[str]:
    return [call["command"] for call in remote.calls]
