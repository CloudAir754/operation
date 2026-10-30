"""账户生命周期相关的参数校验与远端命令构造。

所有命令都是固定模板 + 严格校验过的参数：用户名、路径、UID/GID 与链接名在进入
命令前先经过本模块的校验，任何可疑输入直接抛 ``ValueError``，不进入远端。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Final

from lab_ops_panel.remote.password_policy import (
    BACKUP_DIR,
    PAM_AUTH_PATH,
    PAM_PASSWORD_PATH,
    POLICY_PATH,
)

LOGIN_PATTERN: Final = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
LINK_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
ALLOWED_SHELLS: Final = frozenset(
    {
        "/bin/bash",
        "/bin/sh",
        "/bin/dash",
        "/usr/bin/bash",
        "/usr/bin/sh",
        "/usr/bin/zsh",
        "/bin/zsh",
        "/usr/bin/fish",
    }
)
MAX_BATCH_USERS: Final = 50
MAX_PATH_LENGTH: Final = 1024
MAX_COMMENT_LENGTH: Final = 128
MAX_GROUPS: Final = 16
MIN_MIGRATABLE_UID: Final = 1000
MIGRATION_FILESYSTEMS: Final = frozenset({"ext4"})
SAFETY_RESERVE_BYTES: Final = 256 * 1024 * 1024

MIGRATION_STAGES: Final = (
    "preflight",
    "space_check",
    "freeze",
    "rename",
    "copy",
    "verify",
    "switch",
    "restore",
    "observe",
)
MIGRATION_TERMINAL_STATUSES: Final = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted", "rolled_back"}
)
BATCH_TERMINAL_STATUSES: Final = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class AccountValidationError(ValueError):
    """参数不合法；消息直接展示给管理员。"""


def validate_login(login: str) -> str:
    normalized = (login or "").strip()
    if not LOGIN_PATTERN.match(normalized):
        raise AccountValidationError(
            "用户名必须匹配 ^[a-z_][a-z0-9_-]{0,31}$（小写字母或下划线开头）"
        )
    return normalized


def validate_comment(comment: str | None) -> str:
    normalized = (comment or "").strip()
    if "\n" in normalized or "\r" in normalized or "\x00" in normalized:
        raise AccountValidationError("备注不能包含换行或控制字符")
    if len(normalized) > MAX_COMMENT_LENGTH:
        raise AccountValidationError(f"备注不能超过 {MAX_COMMENT_LENGTH} 个字符")
    return normalized


def validate_shell(shell: str | None) -> str:
    normalized = (shell or "/bin/bash").strip()
    if normalized not in ALLOWED_SHELLS:
        raise AccountValidationError(f"不允许的登录 shell：{normalized}")
    return normalized


def validate_groups(groups: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for name in groups or []:
        normalized = (name or "").strip()
        if not LOGIN_PATTERN.match(normalized):
            raise AccountValidationError(f"组名不合法：{name!r}")
        if normalized not in cleaned:
            cleaned.append(normalized)
    if len(cleaned) > MAX_GROUPS:
        raise AccountValidationError(f"附加组最多 {MAX_GROUPS} 个")
    return cleaned


def normalize_path(value: str, *, label: str = "路径") -> str:
    """绝对路径规范化：拒绝相对路径、``..``、控制字符与过长输入。"""

    text = (value or "").strip()
    if not text:
        raise AccountValidationError(f"{label}不能为空")
    if len(text) > MAX_PATH_LENGTH:
        raise AccountValidationError(f"{label}超过 {MAX_PATH_LENGTH} 个字符")
    if not text.startswith("/"):
        raise AccountValidationError(f"{label}必须是绝对路径")
    if any(char in text for char in ("\x00", "\n", "\r", "\t")):
        raise AccountValidationError(f"{label}包含控制字符")
    parts = [part for part in text.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise AccountValidationError(f"{label}不允许包含 . 或 .. 路径段")
    return "/" + "/".join(parts) if parts else "/"


def is_within(path: str, parent: str) -> bool:
    if parent == "/":
        return path.startswith("/")
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def validate_link_name(value: str) -> str:
    normalized = (value or "").strip()
    if not LINK_NAME_PATTERN.match(normalized):
        raise AccountValidationError(
            "链接名必须以字母或数字开头，只允许字母、数字、点、下划线与连字符"
        )
    return normalized


def validate_uid(uid: Any) -> int:
    try:
        value = int(uid)
    except (TypeError, ValueError) as exc:
        raise AccountValidationError("UID 必须是整数") from exc
    if value < MIN_MIGRATABLE_UID or value > 4_000_000_000:
        raise AccountValidationError("只允许迁移 UID ≥ 1000 的普通账户")
    return value


def quote(value: str) -> str:
    return shlex.quote(value)


def validate_password_text(password: str) -> str:
    if not password:
        raise AccountValidationError("密码不能为空")
    if "\n" in password or "\r" in password or "\x00" in password:
        raise AccountValidationError("密码不能包含换行或 NUL 字符")
    if len(password) > 256:
        raise AccountValidationError("密码长度不能超过 256")
    return password


# ---------------------------------------------------------------------------
# 新建用户
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserSpec:
    login: str
    comment: str = ""
    shell: str = "/bin/bash"
    groups: list[str] = field(default_factory=list)
    uid: int | None = None


def build_preflight_users_command(logins: list[str]) -> str:
    quoted = " ".join(quote(login) for login in logins)
    return (
        "for login in " + quoted + "; do "
        'if getent passwd "$login" >/dev/null 2>&1; then echo "LABOPS_EXISTS=$login"; fi; '
        'if getent group "$login" >/dev/null 2>&1; then echo "LABOPS_GROUP_EXISTS=$login"; fi; '
        "done; echo LABOPS_PREFLIGHT=done"
    )


def build_credential_write_command(path: str) -> str:
    """把密码写入远端 0600 临时文件；密码走 stdin，不进入 argv。"""

    return f"umask 077; cat > {quote(path)} && chmod 600 {quote(path)} && echo LABOPS_CRED=ok"


def build_create_user_script(spec: UserSpec, credential_path: str) -> str:
    args = ["--create-home", "--shell", quote(spec.shell)]
    if spec.comment:
        args.extend(["--comment", quote(spec.comment)])
    if spec.groups:
        args.extend(["--groups", quote(",".join(spec.groups))])
    if spec.uid is not None:
        args.extend(["--uid", str(spec.uid)])
    credential = quote(credential_path)
    login = quote(spec.login)
    return f"""set -u
login={login}
cred={credential}
if getent passwd "$login" >/dev/null 2>&1; then echo LABOPS_RESULT=exists; exit 0; fi
if ! useradd {" ".join(args)} "$login" 2>/tmp/labops-useradd.err; then
  echo LABOPS_RESULT=useradd_failed
  sed -n '1p' /tmp/labops-useradd.err 2>/dev/null
  exit 0
fi
password=$(cat "$cred" 2>/dev/null || true)
if [ -z "$password" ]; then
  userdel -r "$login" >/dev/null 2>&1 || true
  echo LABOPS_RESULT=credential_missing
  exit 0
fi
if ! printf '%s:%s\\n' "$login" "$password" | chpasswd; then
  userdel -r "$login" >/dev/null 2>&1 || true
  echo LABOPS_RESULT=password_failed
  exit 0
fi
status=$(passwd -S "$login" 2>/dev/null | awk '{{print $2}}')
if [ "$status" != "P" ]; then
  userdel -r "$login" >/dev/null 2>&1 || true
  echo LABOPS_RESULT=password_not_set
  exit 0
fi
uid=$(id -u "$login" 2>/dev/null || true)
home=$(getent passwd "$login" | cut -d: -f6)
owner=$(stat -c '%u:%g' "$home" 2>/dev/null || true)
echo LABOPS_RESULT=ok
echo "LABOPS_UID=$uid"
echo "LABOPS_HOME=$home"
echo "LABOPS_HOME_OWNER=$owner"
"""


def build_credential_cleanup_command(paths: list[str]) -> str:
    if not paths:
        return "true"
    joins = " ".join(quote(path) for path in paths)
    return f"rm -f {joins}; echo LABOPS_CLEANUP=ok"


# ---------------------------------------------------------------------------
# 密码策略
# ---------------------------------------------------------------------------


def policy_backup_paths(backup_id: str) -> dict[str, tuple[str, str]]:
    """返回 ``{种类: (远端原文件, 备份文件)}``；三类文件分别备份。"""

    return {
        "conf": (POLICY_PATH, f"{BACKUP_DIR}/{backup_id}.pwquality.conf"),
        "password": (PAM_PASSWORD_PATH, f"{BACKUP_DIR}/{backup_id}.common-password"),
        "auth": (PAM_AUTH_PATH, f"{BACKUP_DIR}/{backup_id}.common-auth"),
    }


def build_policy_backup_command(backup_id: str) -> str:
    pairs = policy_backup_paths(backup_id)
    lines = ["set -u", f"install -d -m 700 {quote(BACKUP_DIR)}", "failed=0"]
    for source, backup in pairs.values():
        lines.extend(
            [
                f"if [ -f {quote(source)} ]; then",
                f"  cp -a {quote(source)} {quote(backup)} || failed=1",
                "fi",
            ]
        )
    lines.extend(['echo "LABOPS_BACKUP_FAILED=$failed"', "echo LABOPS_BACKUP=ok"])
    return "\n".join(lines) + "\n"


def build_policy_restore_command(backup_id: str) -> str:
    pairs = policy_backup_paths(backup_id)
    lines = ["set -u", "restored=0"]
    for source, backup in pairs.values():
        lines.extend(
            [
                f"if [ -f {quote(backup)} ]; then",
                f"  cp -a {quote(backup)} {quote(source)} || ",
                "    { echo LABOPS_POLICY_RESTORE=failed; exit 0; }",
                "  restored=$((restored + 1))",
                "fi",
            ]
        )
    lines.extend(
        [
            'if [ "$restored" -eq 0 ]; then echo LABOPS_POLICY_RESTORE=no_backup; exit 0; fi',
            "echo LABOPS_POLICY_RESTORE=ok",
        ]
    )
    return "\n".join(lines) + "\n"


def build_install_file_command(source: str, destination: str, mode: str = "644") -> str:
    return (
        f"install -m {mode} -o root -g root {quote(source)} {quote(destination)} && "
        f"rm -f {quote(source)}"
    )


def build_pwscore_validation_script(samples: list[tuple[str, bool]], *, login: str = "root") -> str:
    """生成行为验证脚本：样本密码只出现在脚本文本中，不进入独立 argv。"""

    lines = ["set -u", "cmd=$(command -v pwscore || true)", 'if [ -z "$cmd" ]; then exit 3; fi']
    for index, (sample, _expected) in enumerate(samples):
        lines.extend(
            [
                f"if printf '%s\\n' {quote(sample)} | \"$cmd\" {quote(login)} >/dev/null 2>&1; then",
                f"  echo LABOPS_SAMPLE_{index}=ok",
                "else",
                f"  echo LABOPS_SAMPLE_{index}=fail",
                "fi",
            ]
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 用户目录迁移
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationPaths:
    source: str
    target_root: str
    target: str
    staging: str
    backup: str


def build_migration_paths(
    *, home: str, target_root: str, migration_id: str, timestamp: str
) -> MigrationPaths:
    source = normalize_path(home, label="源 home")
    root = normalize_path(target_root, label="迁移根目录")
    if source == "/":
        raise AccountValidationError("不能迁移根目录")
    login = source.rstrip("/").rsplit("/", 1)[-1]
    target = normalize_path(f"{root}/{login}", label="目标目录")
    if is_within(target, source) or is_within(source, root):
        raise AccountValidationError("目标目录不能位于源目录之内，也不能是源目录的父级")
    short_id = migration_id.replace("-", "")[:8]
    return MigrationPaths(
        source=source,
        target_root=root,
        target=target,
        staging=f"{target}.labops-staging-{short_id}",
        backup=f"{source}.labops-backup-{timestamp}-{short_id}",
    )


def build_freeze_script(login: str, uid: int) -> str:
    return f"""set -u
login={quote(login)}
uid={int(uid)}
before=$(passwd -S "$login" 2>/dev/null | awk '{{print $2}}')
echo "LABOPS_LOCK_BEFORE=${{before:-unknown}}"
if command -v loginctl >/dev/null 2>&1; then loginctl terminate-user "$login" >/dev/null 2>&1 || true; fi
if command -v systemctl >/dev/null 2>&1; then systemctl stop "user@$uid.service" >/dev/null 2>&1 || true; fi
usermod -L "$login" >/dev/null 2>&1 || true
pkill -TERM -u "$uid" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
  sleep 1
  left=$(ps -u "$uid" -o pid= 2>/dev/null | wc -l)
  if [ "$left" -eq 0 ]; then break; fi
done
pkill -KILL -u "$uid" >/dev/null 2>&1 || true
sleep 1
remaining=$(ps -u "$uid" -o pid= 2>/dev/null | wc -l)
echo "LABOPS_REMAINING=$remaining"
"""


def build_thaw_script(login: str, status_before: str | None) -> str:
    if (status_before or "").upper().startswith("P"):
        action = f"usermod -U {quote(login)} >/dev/null 2>&1 || true"
    else:
        action = "true"
    return f"""set -u
{action}
status=$(passwd -S {quote(login)} 2>/dev/null | awk '{{print $2}}')
echo "LABOPS_LOCK_AFTER=${{status:-unknown}}"
"""


def build_rename_script(paths: MigrationPaths) -> str:
    return f"""set -u
source={quote(paths.source)}
backup={quote(paths.backup)}
if [ -L "$source" ]; then echo LABOPS_RESULT=symlink_source; exit 0; fi
if [ -e "$backup" ]; then echo LABOPS_RESULT=backup_exists; exit 0; fi
if ! mv -T -- "$source" "$backup"; then echo LABOPS_RESULT=rename_failed; exit 0; fi
if [ -e "$source" ]; then echo LABOPS_RESULT=source_still_present; exit 0; fi
if [ ! -d "$backup" ]; then echo LABOPS_RESULT=backup_missing; exit 0; fi
echo LABOPS_RESULT=ok
"""


def build_copy_script(paths: MigrationPaths) -> str:
    return f"""set -u
backup={quote(paths.backup)}
staging={quote(paths.staging)}
if [ ! -d "$backup" ]; then echo LABOPS_RESULT=backup_missing; exit 0; fi
if [ -e "$staging" ]; then echo LABOPS_RESULT=staging_exists; exit 0; fi
if rsync -aHAX --numeric-ids --one-file-system "$backup"/ "$staging"/ 2>/tmp/labops-rsync.err; then
  echo LABOPS_RESULT=ok
else
  echo LABOPS_RESULT=rsync_failed
  tail -n 2 /tmp/labops-rsync.err 2>/dev/null
fi
"""


def build_checksum_script(paths: MigrationPaths) -> str:
    return f"""set -u
backup={quote(paths.backup)}
staging={quote(paths.staging)}
if [ ! -d "$backup" ] || [ ! -d "$staging" ]; then echo LABOPS_RESULT=missing; exit 0; fi
out=$(rsync -aHAXnc --numeric-ids --one-file-system --out-format='%i %n' "$backup"/ "$staging"/ 2>/dev/null | grep -c '^>f\\|^c' || true)
echo "LABOPS_CHANGED=$out"
echo LABOPS_RESULT=ok
"""


def build_switch_script(paths: MigrationPaths) -> str:
    return f"""set -u
source={quote(paths.source)}
target={quote(paths.target)}
staging={quote(paths.staging)}
if [ -e "$source" ] || [ -L "$source" ]; then echo LABOPS_RESULT=source_present; exit 0; fi
if [ -e "$target" ] || [ -L "$target" ]; then echo LABOPS_RESULT=target_conflict; exit 0; fi
if [ ! -d "$staging" ]; then echo LABOPS_RESULT=staging_missing; exit 0; fi
if ! mv -T -- "$staging" "$target"; then echo LABOPS_RESULT=promote_failed; exit 0; fi
if ! ln -s -- "$target" "$source"; then echo LABOPS_RESULT=link_failed; exit 0; fi
resolved=$(readlink -f "$source" 2>/dev/null || true)
echo "LABOPS_RESOLVED=$resolved"
if [ "$resolved" = "$target" ]; then echo LABOPS_RESULT=ok; else echo LABOPS_RESULT=link_mismatch; fi
"""


def build_functional_test_script(login: str) -> str:
    return f"""set -u
login={quote(login)}
if su -s /bin/sh "$login" -c 'f="$HOME/.labops-migration-test"; test -r "$HOME" && test -w "$HOME" && echo ok > "$f" && cat "$f" >/dev/null && rm -f "$f"' >/dev/null 2>&1; then
  echo LABOPS_FUNCTIONAL=ok
else
  echo LABOPS_FUNCTIONAL=failed
fi
home=$(getent passwd "$login" | cut -d: -f6)
if [ -d "$home/.ssh" ]; then
  echo "LABOPS_SSH_DIR=$(stat -c '%a' "$home/.ssh" 2>/dev/null)"
  if [ -f "$home/.ssh/authorized_keys" ]; then
    echo "LABOPS_SSH_KEY=$(stat -c '%a' "$home/.ssh/authorized_keys" 2>/dev/null)"
    echo "LABOPS_SSH_OWNER=$(stat -c '%u' "$home/.ssh/authorized_keys" 2>/dev/null)"
  fi
fi
"""


def build_rollback_script(paths: MigrationPaths, *, discard_new_data: bool) -> str:
    guard = (
        ""
        if discard_new_data
        else """if [ -n "$(ls -A "$target" 2>/dev/null | head -n 1)" ]; then
  echo LABOPS_RESULT=new_data_present
  exit 0
fi"""
    )
    return f"""set -u
source={quote(paths.source)}
target={quote(paths.target)}
backup={quote(paths.backup)}
if [ ! -d "$backup" ]; then echo LABOPS_RESULT=backup_missing; exit 0; fi
if [ -L "$source" ]; then
  current=$(readlink -f "$source" 2>/dev/null || true)
  if [ "$current" != "$target" ]; then echo LABOPS_RESULT=link_mismatch; exit 0; fi
  {guard}
  rm -f -- "$source"
fi
if [ -e "$source" ]; then echo LABOPS_RESULT=source_occupied; exit 0; fi
if ! mv -T -- "$backup" "$source"; then echo LABOPS_RESULT=restore_failed; exit 0; fi
echo LABOPS_RESULT=ok
"""


def build_cleanup_script(paths: MigrationPaths) -> str:
    return f"""set -u
source={quote(paths.source)}
target={quote(paths.target)}
backup={quote(paths.backup)}
if [ ! -L "$source" ]; then echo LABOPS_RESULT=source_not_link; exit 0; fi
resolved=$(readlink -f "$source" 2>/dev/null || true)
if [ "$resolved" != "$target" ]; then echo LABOPS_RESULT=link_mismatch; exit 0; fi
if [ ! -d "$backup" ]; then echo LABOPS_RESULT=backup_missing; exit 0; fi
if ! rm -rf --one-file-system -- "$backup"; then echo LABOPS_RESULT=cleanup_failed; exit 0; fi
echo LABOPS_RESULT=ok
"""


# ---------------------------------------------------------------------------
# 新空间分配
# ---------------------------------------------------------------------------


def build_dataset_script(
    *, data_path: str, link_path: str, uid: int, gid: int, reuse_existing: bool
) -> str:
    guard = (
        ""
        if reuse_existing
        else """  if [ -n "$(ls -A "$path" 2>/dev/null | head -n 1)" ]; then
    echo LABOPS_RESULT=target_not_empty
    exit 0
  fi"""
    )
    return f"""set -u
path={quote(data_path)}
link={quote(link_path)}
uid={int(uid)}
gid={int(gid)}
if [ -e "$path" ] && [ ! -d "$path" ]; then echo LABOPS_RESULT=target_not_dir; exit 0; fi
if [ -d "$path" ]; then
{guard}
  chown "$uid:$gid" "$path" 2>/dev/null || true
  chmod 0750 "$path" 2>/dev/null || true
else
  if ! install -d -o "$uid" -g "$gid" -m 0750 "$path"; then echo LABOPS_RESULT=mkdir_failed; exit 0; fi
fi
if [ -L "$link" ]; then
  current=$(readlink -f "$link" 2>/dev/null || true)
  if [ "$current" = "$path" ]; then echo LABOPS_RESULT=ok; echo LABOPS_LINK=existing; exit 0; fi
  echo LABOPS_RESULT=link_conflict
  exit 0
fi
if [ -e "$link" ]; then echo LABOPS_RESULT=link_occupied; exit 0; fi
if ! ln -s -- "$path" "$link"; then echo LABOPS_RESULT=link_failed; exit 0; fi
echo LABOPS_LINK=created
echo LABOPS_RESULT=ok
"""


def build_dataset_verify_script(login: str, link_path: str) -> str:
    return f"""set -u
if su -s /bin/sh {quote(login)} -c 'test -w {quote(link_path)}' >/dev/null 2>&1; then
  echo LABOPS_WRITABLE=ok
else
  echo LABOPS_WRITABLE=no
fi
"""


def build_unlink_script(link_path: str, expected_target: str) -> str:
    return f"""set -u
link={quote(link_path)}
expected={quote(expected_target)}
if [ ! -L "$link" ]; then echo LABOPS_RESULT=not_a_link; exit 0; fi
current=$(readlink -f "$link" 2>/dev/null || true)
if [ "$current" != "$expected" ]; then echo LABOPS_RESULT=link_mismatch; exit 0; fi
rm -f -- "$link"
echo LABOPS_RESULT=ok
"""


__all__ = [
    "ALLOWED_SHELLS",
    "BATCH_TERMINAL_STATUSES",
    "MIGRATION_STAGES",
    "MIGRATION_TERMINAL_STATUSES",
    "MIGRATION_FILESYSTEMS",
    "MAX_BATCH_USERS",
    "MIN_MIGRATABLE_UID",
    "AccountValidationError",
    "MigrationPaths",
    "SAFETY_RESERVE_BYTES",
    "UserSpec",
    "build_checksum_script",
    "build_cleanup_script",
    "build_copy_script",
    "build_create_user_script",
    "build_credential_cleanup_command",
    "build_credential_write_command",
    "build_dataset_script",
    "build_dataset_verify_script",
    "build_freeze_script",
    "build_functional_test_script",
    "build_install_file_command",
    "build_migration_paths",
    "build_policy_backup_command",
    "build_policy_restore_command",
    "build_preflight_users_command",
    "build_pwscore_validation_script",
    "build_rename_script",
    "build_rollback_script",
    "build_switch_script",
    "build_thaw_script",
    "build_unlink_script",
    "is_within",
    "normalize_path",
    "policy_backup_paths",
    "quote",
    "validate_comment",
    "validate_groups",
    "validate_link_name",
    "validate_login",
    "validate_password_text",
    "validate_shell",
    "validate_uid",
]
