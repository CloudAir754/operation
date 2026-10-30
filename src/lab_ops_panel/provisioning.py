"""账户生命周期服务。

覆盖三件事，共用同一套 sudo 传输、审计与阶段持久化：

1. 目标账户密码规则与 Ubuntu 系统密码策略（读取 → 差异 → 备份 → 写入 → 验证 → 恢复）。
2. 批量新建用户（密码一次性下发，不落库）。
3. 用户目录迁移与新空间分配（阶段状态机，可中断、可回退、人工清理）。

安全约束：所有写操作都通过 ``sudo -S`` 传入密码（密码只走 stdin，不进 argv）；
需要 stdin 数据的步骤一律拆成“非 sudo 暂存文件 → sudo 执行 → 非 sudo 清理”。
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from lab_ops_panel.audit import record_audit
from lab_ops_panel.db import session_scope
from lab_ops_panel.errors import ApiError, ConflictError, NotFoundError, ValidationFailedError
from lab_ops_panel.models import (
    AccountBatch,
    AppSetting,
    DatasetAllocation,
    HomeMigration,
    PasswordPolicyChange,
    Server,
)
from lab_ops_panel.passwords import (
    BUILTIN_FLOOR,
    EffectiveRules,
    PasswordGenerationError,
    PasswordRuleError,
    PasswordRules,
    PolicyFloor,
    check_password,
    default_rules,
    generate_password,
    mask_password,
    solve_rules,
)
from lab_ops_panel.remote.accounts import (
    BATCH_TERMINAL_STATUSES,
    MIGRATION_FILESYSTEMS,
    MIGRATION_STAGES,
    MIGRATION_TERMINAL_STATUSES,
    SAFETY_RESERVE_BYTES,
    MigrationPaths,
    UserSpec,
    build_checksum_script,
    build_cleanup_script,
    build_copy_script,
    build_create_user_script,
    build_credential_cleanup_command,
    build_credential_write_command,
    build_dataset_script,
    build_dataset_verify_script,
    build_freeze_script,
    build_functional_test_script,
    build_install_file_command,
    build_migration_paths,
    build_policy_backup_command,
    build_policy_restore_command,
    build_preflight_users_command,
    build_pwscore_validation_script,
    build_rename_script,
    build_rollback_script,
    build_switch_script,
    build_thaw_script,
    build_unlink_script,
    normalize_path,
    quote,
    validate_uid,
)
from lab_ops_panel.remote.diagnosis import diagnose_result
from lab_ops_panel.remote.password_policy import (
    FIELD_KEYS,
    FIELD_LABELS,
    MANAGED_KEYS,
    PAM_AUTH_PATH,
    PAM_PASSWORD_PATH,
    POLICY_LIMITS,
    POLICY_PATH,
    build_floor,
    build_snapshot,
    changed_fields,
    render_conf,
    render_pam_retry,
    render_pwquality_args,
    unified_diff,
    validate_targets,
)
from lab_ops_panel.remote.probe import run_probe
from lab_ops_panel.remote.targets import target_from_server
from lab_ops_panel.schemas_accounts import (
    AccountBatchOut,
    AccountBatchResultOut,
    AdjustmentOut,
    CheckOut,
    DatasetOut,
    GeneratorOut,
    MigrationOut,
    MigrationPathsOut,
    MigrationPreflightOut,
    PasswordPolicyOut,
    PolicyCapabilitiesOut,
    PolicyChangeOut,
    PolicyDiffOut,
    PolicyFieldOut,
    PolicyFloorOut,
    RevealPasswordsOut,
)
from lab_ops_panel.ssh.transport import CommandResult, TransportError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from lab_ops_panel.config import Settings
    from lab_ops_panel.secrets_store import CredentialStore
    from lab_ops_panel.ssh.transport import SshTransport
    from lab_ops_panel.tasks.events import EventBus

logger = logging.getLogger(__name__)

GENERATOR_KEY = "password_generator_rules"
DEFAULT_DATA_ROOT = "/data2"
DEFAULT_MIGRATION_ROOT = "/data2/users"
DEFAULT_LINK_NAME = "data2_disk"
POLICY_TTL_SECONDS = 20.0
COMMAND_TIMEOUT_SECONDS = 60.0
COPY_TIMEOUT_SECONDS = 1800.0
CLEANUP_TIMEOUT_SECONDS = 1800.0
PROBE_TIMEOUT_SECONDS = 180.0
_MARKER = re.compile(r"^LABOPS_([A-Z0-9_]+)=(.*)$", re.MULTILINE)
_SPACE_TOLERANCE_BYTES = 64 * 1024 * 1024
_SPACE_TOLERANCE_RATIO = 0.05


def _now() -> datetime:
    return datetime.now(UTC)


def _age_seconds(stamp: datetime | None) -> float:
    if stamp is None:
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (_now() - stamp).total_seconds())


def _check(
    code: str, label: str, ok: bool, message: str = "", *, blocking: bool = True
) -> CheckOut:
    return CheckOut(code=code, label=label, ok=ok, message=message, blocking=blocking)


def _markers(output: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip() for match in _MARKER.finditer(output or "")}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class _PolicyState:
    snapshot: Any
    stamp: datetime


@dataclass
class _MigrationContext:
    server: Server
    password: str
    row: HomeMigration
    paths: MigrationPaths
    request: dict[str, Any]
    preflight: dict[str, Any] = field(default_factory=dict)


class ProvisioningService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        transport: SshTransport,
        settings: Settings,
        credentials: CredentialStore,
        bus: EventBus,
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport
        self._settings = settings
        self._credentials = credentials
        self._bus = bus
        self._lock = threading.RLock()
        self._policy_cache: dict[int, _PolicyState] = {}
        self._batch_passwords: dict[str, dict[str, str]] = {}
        self._batch_cancel: dict[str, threading.Event] = {}
        self._migration_cancel: dict[str, threading.Event] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="labops-provision")
        self._closed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            events = [*self._batch_cancel.values(), *self._migration_cancel.values()]
            self._batch_passwords.clear()
        for event in events:
            event.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def recover_on_startup(self) -> dict[str, int]:
        """把上次进程遗留的作业标记 interrupted，保留阶段以便恢复或回退。"""

        now = _now()
        counts = {"batches": 0, "migrations": 0}
        with session_scope(self._session_factory) as session:
            for batch in session.scalars(
                select(AccountBatch).where(AccountBatch.status.in_(["queued", "running"]))
            ).all():
                batch.status = "interrupted"
                batch.stage = "interrupted"
                batch.error = "应用重启，批次已中断；可重新提交未完成的账户"
                batch.finished_at = now
                batch.cancel_requested = False
                counts["batches"] += 1
            for migration in session.scalars(
                select(HomeMigration).where(HomeMigration.status.in_(["queued", "running"]))
            ).all():
                migration.status = "interrupted"
                migration.error = "应用重启，迁移已中断；请核对阶段后「继续」或「回退」"
                migration.finished_at = now
                migration.cancel_requested = False
                counts["migrations"] += 1
        if counts["batches"] or counts["migrations"]:
            logger.warning("账户生命周期作业恢复：%s", counts)
        return counts

    # ------------------------------------------------------------------
    # 远端调用
    # ------------------------------------------------------------------

    def _password(self, server_id: int) -> str | None:
        try:
            return self._credentials.get_password(server_id)
        except Exception as exc:  # noqa: BLE001 - 凭据后端不可用时按缺失处理
            logger.warning("读取 sudo 密码失败：%s", exc)
            return None

    def _require_password(self, server: Server) -> str:
        password = self._password(server.id)
        if password is None:
            raise ApiError(
                409,
                "sudo_password_missing",
                f"主机 {server.alias} 未保存运维 sudo 密码；请先在「运维 sudo 密码」中录入",
            )
        return password

    def _run(
        self,
        server: Server,
        command: str,
        *,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        sudo: bool = False,
        password: str | None = None,
        allow_failure: bool = False,
        stdin_data: bytes | None = None,
        operation: str = "远端操作",
    ) -> CommandResult:
        target = target_from_server(server)
        payload = stdin_data
        if sudo:
            if password is None:
                raise ApiError(409, "sudo_password_missing", "未保存 sudo 密码，无法提权执行")
            if stdin_data is not None:
                raise ApiError(500, "step_stdin_conflict", "sudo 步骤不能同时携带标准输入负载")
            command = f"sudo -S -p '' {command}"
            payload = (password + "\n").encode("utf-8")
        try:
            result = self._transport.run(
                target,
                command,
                timeout=timeout,
                stdin_data=payload,
                max_output_bytes=self._settings.max_output_bytes,
            )
        except TransportError as exc:
            raise ApiError(503, "ssh_unavailable", str(exc)) from exc
        if result.cancelled:
            raise ApiError(409, "operation_cancelled", f"{operation}已取消")
        if result.timed_out:
            raise ApiError(504, "operation_timeout", f"{operation}超时（{timeout:.0f} 秒）")
        if result.exit_code != 0 and not allow_failure:
            raise ApiError(
                502, "remote_command_failed", diagnose_result(result, operation=operation)
            )
        return result

    def _probe(
        self,
        server: Server,
        mode: str,
        key: str,
        args: list[str],
        *,
        operation: str,
        timeout: float,
    ) -> dict[str, Any]:
        try:
            payload = run_probe(
                self._transport,
                target_from_server(server),
                mode=mode,
                probe_args=args,
                timeout=timeout,
                max_output_bytes=self._settings.max_output_bytes,
                operation=operation,
            )
        except Exception as exc:  # noqa: BLE001 - 探针失败统一转成业务错误
            raise ApiError(502, "probe_failed", f"{operation}失败：{exc}") from exc
        section = payload.get(key)
        if not isinstance(section, dict):
            raise ApiError(502, "probe_failed", f"{operation}返回的数据结构不正确")
        if section.get("status") != "ok":
            raise ApiError(
                502,
                "probe_failed",
                f"{operation}失败：{section.get('error') or '未知原因'}",
            )
        data = section.get("data")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # 密码生成规则
    # ------------------------------------------------------------------

    def _load_rules(self) -> tuple[PasswordRules, bool, datetime | None]:
        with self._session_factory() as session:
            row = session.get(AppSetting, GENERATOR_KEY)
            if row is None:
                return default_rules(), False, None
            raw = (row.value or {}).get("rules")
            try:
                return PasswordRules.model_validate(raw or {}), True, row.updated_at
            except Exception:  # noqa: BLE001 - 规则损坏时回退默认值并提示未配置
                return default_rules(), False, row.updated_at

    def generator(self, server: Server | None = None) -> GeneratorOut:
        rules, configured, updated_at = self._load_rules()
        floor = self._floor(server) if server is not None else None
        return self._generator_out(
            rules,
            configured=configured,
            updated_at=updated_at,
            floor=floor,
            server_id=server.id if server is not None else None,
        )

    def preview_generator(self, rules: PasswordRules, server: Server | None) -> GeneratorOut:
        return self._generator_out(
            rules,
            configured=True,
            updated_at=None,
            floor=self._floor(server) if server is not None else None,
            server_id=server.id if server is not None else None,
        )

    def save_generator(self, rules: PasswordRules) -> GeneratorOut:
        with session_scope(self._session_factory) as session:
            row = session.get(AppSetting, GENERATOR_KEY)
            if row is None:
                row = AppSetting(key=GENERATOR_KEY, value={})
                session.add(row)
            row.value = {"rules": rules.model_dump(mode="json", by_alias=True)}
        record_audit(
            self._session_factory,
            action="update_password_rules",
            target="local",
            outcome="succeeded",
            detail=f"length={rules.length} classes={rules.model_dump(mode='json')}",
        )
        return self.generator()

    def _generator_out(
        self,
        rules: PasswordRules,
        *,
        configured: bool,
        updated_at: datetime | None,
        floor: PolicyFloor | None,
        server_id: int | None,
    ) -> GeneratorOut:
        effective = solve_rules(rules, floor)
        example = ""
        generation_error: str | None = None
        try:
            example = generate_password(effective)
        except PasswordRuleError as exc:
            generation_error = str(exc)
        checks = self._rule_checks(rules, effective, generation_error)
        return GeneratorOut(
            configured=configured,
            rules=rules,
            effective_length=effective.length,
            adjustments=[
                AdjustmentOut(
                    field=item["field"],
                    label=item["label"],
                    requested=int(item["requested"]),
                    applied=int(item["applied"]),
                    reason=str(item["reason"]),
                )
                for item in effective.adjustments
            ],
            example=mask_password(example) if example else "",
            checks=checks,
            floor=self._floor_out(floor) if floor is not None else None,
            server_id=server_id,
            updated_at=updated_at,
        )

    def _rule_checks(
        self, rules: PasswordRules, effective: EffectiveRules, generation_error: str | None
    ) -> list[CheckOut]:
        total = rules.min_upper + rules.min_lower + rules.min_digits + rules.min_symbols
        checks = [
            _check(
                "length_sum",
                "各类最少字符数之和不超过总长度",
                total <= rules.length,
                f"{total} / {rules.length}",
            ),
            _check(
                "symbols_present",
                "启用符号要求时符号集合非空",
                rules.min_symbols == 0 or bool(rules.symbols),
                "符号集合为空" if rules.min_symbols > 0 and not rules.symbols else "",
            ),
            _check(
                "generation",
                "按当前规则可生成合规密码",
                generation_error is None,
                generation_error or "",
            ),
        ]
        if effective.adjustments:
            checks.append(
                _check(
                    "floor",
                    "已按系统策略提高生成规则",
                    True,
                    "、".join(
                        f"{item['label']}→{item['applied']}" for item in effective.adjustments
                    ),
                    blocking=False,
                )
            )
        return checks

    def _floor(self, server: Server) -> PolicyFloor:
        try:
            snapshot = self._policy_snapshot(server)
        except ApiError:
            return BUILTIN_FLOOR
        return build_floor(snapshot)

    @staticmethod
    def _floor_out(floor: PolicyFloor) -> PolicyFloorOut:
        return PolicyFloorOut(
            min_length=floor.min_length,
            min_upper=floor.min_upper,
            min_lower=floor.min_lower,
            min_digits=floor.min_digits,
            min_symbols=floor.min_symbols,
            min_classes=floor.min_classes,
            max_repeat=floor.max_repeat,
            max_sequence=floor.max_sequence,
            max_class_repeat=floor.max_class_repeat,
            reject_username=floor.reject_username,
            source=floor.source,
            notes=list(floor.notes),
        )

    # ------------------------------------------------------------------
    # 系统密码策略
    # ------------------------------------------------------------------

    def _policy_snapshot(self, server: Server, *, force: bool = False) -> Any:
        with self._lock:
            cached = self._policy_cache.get(server.id)
            if (
                not force
                and cached is not None
                and _age_seconds(cached.stamp) <= POLICY_TTL_SECONDS
            ):
                return cached.snapshot
        data = self._probe(
            server,
            "password_policy",
            "passwordPolicy",
            [],
            operation="密码策略读取",
            timeout=min(45.0, max(20.0, self._settings.command_timeout_seconds)),
        )
        snapshot = build_snapshot(data)
        with self._lock:
            self._policy_cache[server.id] = _PolicyState(snapshot=snapshot, stamp=_now())
        return snapshot

    def policy(self, server: Server, *, refresh: bool = False) -> PasswordPolicyOut:
        snapshot = self._policy_snapshot(server, force=refresh)
        floor = build_floor(snapshot)
        writable = set(snapshot.writable_keys)
        fields = [
            PolicyFieldOut(
                key=key,
                label=FIELD_LABELS.get(key, key),
                value=snapshot.values.get(key, 0),
                origin=snapshot.origins.get(key, "default"),
                writable=key in writable,
                min=POLICY_LIMITS[key][0],
                max=POLICY_LIMITS[key][1],
                reason=None if key in writable else self._read_only_reason(snapshot, key),
            )
            for key in FIELD_KEYS
        ]
        return PasswordPolicyOut(
            server_id=server.id,
            collected_at=_now(),
            status="ok",
            fields=fields,
            capabilities=PolicyCapabilitiesOut(**snapshot.capabilities),
            managed_lines=[
                line for line in (snapshot.pwquality.raw, snapshot.unix_auth.raw) if line
            ],
            unknown_keys=list(snapshot.conf.unknown_keys)[:20],
            login_defs={
                key: value
                for key, value in snapshot.login_defs.items()
                if key in {"PASS_MIN_LEN", "ENCRYPT_METHOD", "PASS_MAX_DAYS"}
            },
            notes=list(snapshot.notes),
            floor=self._floor_out(floor),
            os=dict(snapshot.os_info),
            generator=self.generator(server),
        )

    @staticmethod
    def _read_only_reason(snapshot: Any, key: str) -> str:
        if key == "retry":
            return snapshot.unix_auth.reason or "common-auth 中的 pam_unix 行不可安全修改"
        if snapshot.origins.get(key) == "pam":
            return snapshot.pwquality.reason or "该字段由 PAM 行内参数决定且不可安全修改"
        if not snapshot.capabilities.get("pwqualityConf"):
            return f"{POLICY_PATH} 不存在或不完整，未启用写入"
        return "该字段当前只读"

    def policy_preview(self, server: Server, targets: dict[str, int]) -> PolicyDiffOut:
        snapshot = self._policy_snapshot(server, force=True)
        try:
            diff, _texts = self._build_policy_diff(snapshot, targets, server_id=server.id)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc
        return diff

    def _build_policy_diff(
        self, snapshot: Any, targets: dict[str, int], *, server_id: int = 0
    ) -> tuple[PolicyDiffOut, dict[str, str]]:
        cleaned = validate_targets(targets)
        blockers: list[str] = []
        warnings: list[str] = []
        writable = set(snapshot.writable_keys)
        for key in cleaned:
            if key not in writable:
                blockers.append(
                    f"{FIELD_LABELS.get(key, key)} 当前只读：{self._read_only_reason(snapshot, key)}"
                )

        conf_targets = {
            key: value
            for key, value in cleaned.items()
            if key in MANAGED_KEYS and snapshot.origins.get(key) != "pam"
        }
        pam_targets = {
            key: value
            for key, value in cleaned.items()
            if key in MANAGED_KEYS and snapshot.origins.get(key) == "pam"
        }
        retry_target = cleaned.get("retry")

        texts: dict[str, str] = {}
        diffs: list[str] = []
        if conf_targets and not blockers:
            texts["conf"] = render_conf(snapshot.conf_content, conf_targets, header="labops-panel")
            if texts["conf"] != snapshot.conf_content:
                diffs.append(unified_diff(snapshot.conf_content, texts["conf"], POLICY_PATH))
        if pam_targets and not blockers:
            try:
                texts["password"] = render_pwquality_args(
                    snapshot.pam_password_content, pam_targets
                )
            except ValueError as exc:
                blockers.append(str(exc))
            else:
                if texts["password"] != snapshot.pam_password_content:
                    diffs.append(
                        unified_diff(
                            snapshot.pam_password_content, texts["password"], PAM_PASSWORD_PATH
                        )
                    )
        if retry_target is not None and not blockers:
            try:
                texts["auth"] = render_pam_retry(snapshot.auth_content, retry_target)
            except ValueError as exc:
                blockers.append(str(exc))
            else:
                if texts["auth"] != snapshot.auth_content:
                    diffs.append(unified_diff(snapshot.auth_content, texts["auth"], PAM_AUTH_PATH))

        current_minlen = snapshot.values.get("minlen")
        if (
            current_minlen is not None
            and cleaned.get("minlen") is not None
            and cleaned["minlen"] < current_minlen
        ):
            warnings.append("新值低于当前生效的最小长度，会放宽系统要求，请再次确认")
        if cleaned.get("minclass", 0) == 0 and snapshot.values.get("minclass", 0) > 0:
            warnings.append("清空字符类别要求会放宽系统要求")

        diff_out = PolicyDiffOut(
            server_id=server_id,
            fields=changed_fields(snapshot.values, cleaned),
            diff="\n".join(diffs),
            blockers=blockers,
            warnings=warnings,
            applicable=not blockers and bool(diffs),
        )
        return diff_out, texts

    def apply_policy(self, server: Server, targets: dict[str, int]) -> PolicyChangeOut:
        password = self._require_password(server)
        snapshot = self._policy_snapshot(server, force=True)
        try:
            diff, texts = self._build_policy_diff(snapshot, targets, server_id=server.id)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc
        if diff.blockers:
            raise ApiError(422, "policy_not_applicable", "；".join(diff.blockers))
        if not texts:
            raise ApiError(422, "policy_unchanged", "目标值与当前生效值相同，无需修改")

        change_id = uuid.uuid4().hex
        backup_id = f"{change_id[:16]}-{int(_now().timestamp())}"
        backup_result = self._run(
            server,
            build_policy_backup_command(backup_id),
            sudo=True,
            password=password,
            operation="备份密码策略文件",
        )
        markers = _markers(backup_result.stdout)
        if markers.get("BACKUP_FAILED", "0") != "0":
            raise ApiError(500, "policy_backup_failed", "备份密码策略文件失败，已中止修改")

        validation: dict[str, Any] = {"restored": False}
        try:
            self._write_policy_files(server, password, change_id, texts)
            verify = self._verify_policy(server, targets, password)
            validation.update(verify)
            if not verify.get("ok", False):
                raise ApiError(
                    500,
                    "policy_validation_failed",
                    f"写入后验证失败，已恢复备份：{verify.get('error') or '配置不一致'}",
                )
        except ApiError as exc:
            restored, error = self._restore_policy_files(server, password, backup_id)
            self._store_policy_change(
                change_id=change_id,
                server_id=server.id,
                status="failed",
                targets=targets,
                diff=diff.diff,
                backup_id=backup_id,
                snapshot=snapshot,
                texts=texts,
                validation={**validation, "restored": restored, "restoreError": error},
                error=exc.message,
            )
            raise ApiError(
                exc.status_code,
                exc.code,
                f"{exc.message}（{'已自动恢复备份' if restored else '自动恢复失败，请手工检查'}）",
            ) from exc

        self._store_policy_change(
            change_id=change_id,
            server_id=server.id,
            status="applied",
            targets=targets,
            diff=diff.diff,
            backup_id=backup_id,
            snapshot=snapshot,
            texts=texts,
            validation=validation,
            error=None,
        )
        with self._lock:
            self._policy_cache.pop(server.id, None)
        record_audit(
            self._session_factory,
            action="update_password_policy",
            target=server.alias,
            outcome="succeeded",
            detail=", ".join(f"{key}={value}" for key, value in targets.items()),
        )
        return self._get_policy_change(change_id, server.alias)

    def _write_policy_files(
        self, server: Server, password: str, change_id: str, texts: dict[str, str]
    ) -> None:
        destinations = {
            "conf": POLICY_PATH,
            "password": PAM_PASSWORD_PATH,
            "auth": PAM_AUTH_PATH,
        }
        for index, (kind, content) in enumerate(texts.items()):
            staging = f"/tmp/labops-policy-{change_id}-{index}"
            self._run(
                server,
                f"umask 077; cat > {staging} && echo LABOPS_STAGE=ok",
                stdin_data=content.encode("utf-8"),
                operation="暂存策略文件",
            )
            self._run(
                server,
                build_install_file_command(staging, destinations[kind]),
                sudo=True,
                password=password,
                operation="写入策略文件",
            )

    def _verify_policy(
        self, server: Server, targets: dict[str, int], password: str
    ) -> dict[str, Any]:
        snapshot = self._policy_snapshot(server, force=True)
        mismatched = {
            key: {"expected": value, "actual": snapshot.values.get(key)}
            for key, value in targets.items()
            if snapshot.values.get(key) != value
        }
        result: dict[str, Any] = {
            "ok": not mismatched,
            "mismatched": mismatched,
            "error": None if not mismatched else f"字段未生效：{sorted(mismatched)}",
            "behavior": "skipped",
        }
        if snapshot.capabilities.get("pwscore"):
            behavior = self._verify_policy_behavior(server, targets, password)
            result["behavior"] = behavior.get("status", "skipped")
            result["behaviorError"] = behavior.get("error")
            if behavior.get("status") == "failed":
                result["ok"] = False
                result["error"] = behavior.get("error") or "行为验证未通过"
        return result

    def _verify_policy_behavior(
        self, server: Server, targets: dict[str, int], password: str
    ) -> dict[str, Any]:
        snapshot = self._policy_snapshot(server)
        floor = build_floor(snapshot)
        try:
            rules = PasswordRules(
                length=max(floor.min_length, 20),
                min_upper=max(1, floor.min_upper),
                min_lower=max(1, floor.min_lower),
                min_digits=max(1, floor.min_digits),
                min_symbols=max(1, floor.min_symbols),
            )
            effective = solve_rules(rules, floor)
            samples = [(generate_password(effective), True) for _ in range(2)]
        except PasswordRuleError as exc:
            return {"status": "skipped", "error": str(exc)}
        min_length = max(targets.get("minlen", snapshot.values.get("minlen", 8)), 8)
        weak = "Aa1!" if min_length > 4 else None
        if weak is not None:
            samples.append((weak, False))
        script = build_pwscore_validation_script(samples)
        result = self._run(
            server,
            f"bash -c {quote(script)}",
            sudo=True,
            password=password,
            allow_failure=True,
            operation="密码策略行为验证",
        )
        if result.exit_code == 3:
            return {"status": "skipped", "error": "pwscore 不可用"}
        markers = _markers(result.stdout)
        mismatched = [
            index
            for index, (_sample, expected) in enumerate(samples)
            if (markers.get(f"SAMPLE_{index}") == "ok") != expected
        ]
        if mismatched:
            return {"status": "failed", "error": f"样例 {mismatched} 的结果与预期不符"}
        return {"status": "ok", "samples": len(samples)}

    def _restore_policy_files(
        self, server: Server, password: str, backup_id: str
    ) -> tuple[bool, str | None]:
        try:
            result = self._run(
                server,
                build_policy_restore_command(backup_id),
                sudo=True,
                password=password,
                allow_failure=True,
                operation="恢复密码策略备份",
            )
        except ApiError as exc:
            return False, exc.message
        markers = _markers(result.stdout)
        if markers.get("POLICY_RESTORE") != "ok":
            return False, markers.get("POLICY_RESTORE") or "恢复命令执行失败"
        with self._lock:
            self._policy_cache.pop(server.id, None)
        return True, None

    def _store_policy_change(
        self,
        *,
        change_id: str,
        server_id: int,
        status: str,
        targets: dict[str, int],
        diff: str,
        backup_id: str,
        snapshot: Any,
        texts: dict[str, str],
        validation: dict[str, Any],
        error: str | None,
    ) -> None:
        with session_scope(self._session_factory) as session:
            row = PasswordPolicyChange(
                id=change_id,
                server_id=server_id,
                status=status,
                targets={key: int(value) for key, value in targets.items()},
                diff=diff,
                backup_id=backup_id,
                conf_before=snapshot.conf_content,
                conf_after=texts.get("conf", snapshot.conf_content),
                password_before=snapshot.pam_password_content,
                password_after=texts.get("password", snapshot.pam_password_content),
                auth_before=snapshot.auth_content,
                auth_after=texts.get("auth", snapshot.auth_content),
                validation=validation,
                error=error,
            )
            session.add(row)

    def _get_policy_change(self, change_id: str, alias: str) -> PolicyChangeOut:
        with self._session_factory() as session:
            row = session.get(PasswordPolicyChange, change_id)
            if row is None:
                raise NotFoundError("policy_change_not_found", "策略修改记录不存在")
            return _policy_change_out(row)

    def policy_changes(self, server_id: int, *, limit: int = 20) -> list[PolicyChangeOut]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(PasswordPolicyChange)
                .where(PasswordPolicyChange.server_id == server_id)
                .order_by(PasswordPolicyChange.created_at.desc())
                .limit(limit)
            ).all()
            return [_policy_change_out(row) for row in rows]

    def restore_policy(self, server: Server, change_id: str) -> PolicyChangeOut:
        password = self._require_password(server)
        with self._session_factory() as session:
            row = session.get(PasswordPolicyChange, change_id)
            if row is None or row.server_id != server.id:
                raise NotFoundError("policy_change_not_found", "策略修改记录不存在")
            if row.status == "restored":
                raise ConflictError("policy_already_restored", "该记录已恢复过")
            if not row.backup_id:
                raise ConflictError("policy_backup_missing", "该记录没有可用的备份")
            alias = self._alias(session, server.id)
        restored, error = self._restore_policy_files(server, password, row.backup_id)
        if not restored:
            raise ApiError(500, "policy_restore_failed", f"恢复备份失败：{error or '未知原因'}")
        with session_scope(self._session_factory) as session:
            stored = session.get(PasswordPolicyChange, change_id)
            if stored is not None:
                stored.status = "restored"
                stored.restored_at = _now()
        with self._lock:
            self._policy_cache.pop(server.id, None)
        record_audit(
            self._session_factory,
            action="restore_password_policy",
            target=server.alias,
            outcome="succeeded",
            detail=f"恢复 {change_id} 修改前的配置",
        )
        return self._get_policy_change(change_id, alias)

    # ------------------------------------------------------------------
    # 批量新建用户
    # ------------------------------------------------------------------

    def create_batch(self, server: Server, request: Any) -> AccountBatchOut:
        if self._closed:
            raise ApiError(503, "service_stopping", "服务正在关闭，无法提交新作业")
        password = self._require_password(server)
        rules = request.rules or self._load_rules()[0]
        floor = self._floor(server)
        effective = solve_rules(rules, floor)
        generated: dict[str, str] = {}
        entries: list[dict[str, Any]] = []
        for entry in request.users:
            spec = UserSpec(
                login=entry.login,
                comment=entry.comment,
                shell=entry.shell,
                groups=list(entry.groups),
                uid=entry.uid,
            )
            if entry.password:
                violations = check_password(entry.password, effective, username=entry.login)
                if violations:
                    raise ApiError(
                        422,
                        "password_policy_violation",
                        f"{entry.login} 的密码不满足规则：{'；'.join(violations)}",
                    )
                generated[entry.login] = entry.password
                managed = False
            else:
                try:
                    candidate = generate_password(effective, username=entry.login)
                except PasswordGenerationError as exc:
                    raise ApiError(422, "password_generation_failed", str(exc)) from exc
                generated[entry.login] = candidate
                managed = True
            entries.append(
                {
                    "login": spec.login,
                    "comment": spec.comment,
                    "shell": spec.shell,
                    "groups": spec.groups,
                    "uid": spec.uid,
                    "generated": managed,
                }
            )

        batch_id = uuid.uuid4().hex
        request_payload = {
            "users": entries,
            "rules": rules.model_dump(mode="json", by_alias=True),
            "floor": {
                "minLength": floor.min_length,
                "source": floor.source,
            },
        }
        with session_scope(self._session_factory) as session:
            row = AccountBatch(
                id=batch_id,
                server_id=server.id,
                status="queued",
                stage="queued",
                progress=0,
                request=request_payload,
                results=[],
            )
            session.add(row)
            session.flush()
            out = self._batch_out(row, server.alias)
            self._batch_cancel[batch_id] = threading.Event()
        with self._lock:
            self._batch_passwords[batch_id] = generated
        record_audit(
            self._session_factory,
            action="create_accounts",
            target=server.alias,
            outcome="queued",
            detail=", ".join(entry["login"] for entry in entries),
        )
        self._pool.submit(self._run_batch, batch_id, password)
        return out

    def _run_batch(self, batch_id: str, password: str) -> None:
        cancel = self._batch_cancel.get(batch_id) or threading.Event()
        try:
            with session_scope(self._session_factory) as session:
                row = session.get(AccountBatch, batch_id)
                if row is None or row.status in BATCH_TERMINAL_STATUSES:
                    return
                if row.cancel_requested or cancel.is_set():
                    row.status = "cancelled"
                    row.stage = "cancelled"
                    row.finished_at = _now()
                    return
                row.status = "running"
                row.stage = "preflight"
                row.started_at = _now()
                row.progress = 5
                server = session.get(Server, row.server_id)
                if server is None:
                    row.status = "failed"
                    row.error = "主机记录已删除"
                    row.finished_at = _now()
                    return
                entries = list((row.request or {}).get("users") or [])
                alias = server.alias
                server_obj = server
            self._publish(
                batch_id,
                {"type": "status", "batchId": batch_id, "status": "running", "progress": 5},
            )

            logins = [entry["login"] for entry in entries]
            existing: set[str] = set()
            if logins:
                preflight = self._run(
                    server_obj,
                    build_preflight_users_command(logins),
                    operation="账户预检",
                )
                existing.update(
                    match.group(1)
                    for match in re.finditer(r"LABOPS_EXISTS=(\S+)", preflight.stdout)
                )

            results: list[dict[str, Any]] = []
            staged: list[str] = []
            with self._lock:
                passwords = dict(self._batch_passwords.get(batch_id) or {})
            total = max(1, len(entries))
            for index, entry in enumerate(entries, start=1):
                login = entry["login"]
                if cancel.is_set():
                    results.append({"login": login, "status": "cancelled", "error": "已取消"})
                    continue
                if login in existing:
                    results.append({"login": login, "status": "failed", "error": "用户名已存在"})
                    continue
                credential = f"/tmp/labops-{batch_id}-{index}.cred"
                try:
                    self._run(
                        server_obj,
                        build_credential_write_command(credential),
                        stdin_data=passwords.get(login, "").encode("utf-8"),
                        operation="暂存账户密码",
                    )
                except ApiError as exc:
                    results.append({"login": login, "status": "failed", "error": exc.message})
                    continue
                staged.append(credential)
                spec = UserSpec(
                    login=login,
                    comment=entry.get("comment") or "",
                    shell=entry.get("shell") or "/bin/bash",
                    groups=list(entry.get("groups") or []),
                    uid=entry.get("uid"),
                )
                try:
                    result = self._run(
                        server_obj,
                        build_create_user_script(spec, credential),
                        sudo=True,
                        password=password,
                        allow_failure=True,
                        operation=f"创建账户 {login}",
                    )
                except ApiError as exc:
                    results.append({"login": login, "status": "failed", "error": exc.message})
                    continue
                markers = _markers(result.stdout)
                status = markers.get("RESULT", "unknown")
                if status == "ok":
                    results.append(
                        {
                            "login": login,
                            "status": "succeeded",
                            "error": None,
                            "uid": _int_or_none(markers.get("UID")),
                            "home": markers.get("HOME"),
                            "passwordGenerated": bool(entry.get("generated")),
                        }
                    )
                else:
                    detail = result.combined_output.strip().splitlines()
                    results.append(
                        {
                            "login": login,
                            "status": "failed",
                            "error": _batch_error(status, detail),
                        }
                    )
                self._update_batch_progress(batch_id, results, int(100 * index / total))

            if staged:
                try:
                    self._run(
                        server_obj,
                        build_credential_cleanup_command(staged),
                        allow_failure=True,
                        operation="清理临时凭据文件",
                    )
                except ApiError:
                    logger.warning("清理批次 %s 的临时凭据文件失败", batch_id)

            failures = [item for item in results if item["status"] == "failed"]
            cancelled_any = any(item["status"] == "cancelled" for item in results)
            if results and not failures and not cancelled_any:
                final = "succeeded"
            elif cancelled_any and not failures:
                final = "cancelled"
            else:
                final = "failed" if results else "cancelled"
            with session_scope(self._session_factory) as session:
                row = session.get(AccountBatch, batch_id)
                if row is None:
                    return
                row.results = results
                row.status = final
                row.stage = "done" if final != "cancelled" else "cancelled"
                row.progress = 100
                row.finished_at = _now()
                row.error = None if final == "succeeded" else f"{len(failures)} 个账户失败"
                out = self._batch_out(row, alias)
            self._publish_done(batch_id, out)
            record_audit(
                self._session_factory,
                action="create_accounts",
                target=alias,
                outcome=final,
                detail=f"{len(results)} 个账户，失败 {len(failures)}",
            )
        except ApiError as exc:
            self._fail_batch(batch_id, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("批次执行异常：%s", batch_id)
            self._fail_batch(batch_id, f"批次执行异常：{exc}")
        finally:
            with self._lock:
                self._batch_cancel.pop(batch_id, None)

    def _update_batch_progress(
        self, batch_id: str, results: list[dict[str, Any]], progress: int
    ) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(AccountBatch, batch_id)
            if row is None or row.status in BATCH_TERMINAL_STATUSES:
                return
            row.results = list(results)
            row.progress = min(95, progress)
            row.stage = "creating"
        self._publish(
            batch_id,
            {
                "type": "progress",
                "batchId": batch_id,
                "status": "running",
                "progress": min(95, progress),
            },
        )

    def _fail_batch(self, batch_id: str, message: str) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(AccountBatch, batch_id)
            if row is None or row.status in BATCH_TERMINAL_STATUSES:
                return
            row.status = "failed"
            row.error = message
            row.finished_at = _now()
            row.progress = 100
            alias = self._alias(session, row.server_id)
            out = self._batch_out(row, alias)
        self._publish_done(batch_id, out)

    def cancel_batch(self, batch_id: str) -> AccountBatchOut:
        with session_scope(self._session_factory) as session:
            row = session.get(AccountBatch, batch_id)
            if row is None:
                raise NotFoundError("account_batch_not_found", "批次不存在")
            if row.status in BATCH_TERMINAL_STATUSES:
                raise ConflictError("batch_finished", "批次已结束，无法取消")
            alias = self._alias(session, row.server_id)
            if row.status == "queued":
                row.status = "cancelled"
                row.stage = "cancelled"
                row.finished_at = _now()
                row.progress = 100
            else:
                row.cancel_requested = True
            out = self._batch_out(row, alias)
        with self._lock:
            event = self._batch_cancel.get(batch_id)
        if event is not None:
            event.set()
        return out

    def get_batch(self, batch_id: str) -> AccountBatchOut:
        with self._session_factory() as session:
            row = session.get(AccountBatch, batch_id)
            if row is None:
                raise NotFoundError("account_batch_not_found", "批次不存在")
            return self._batch_out(row, self._alias(session, row.server_id))

    def list_batches(self, server_id: int, *, limit: int = 20) -> list[AccountBatchOut]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(AccountBatch)
                .where(AccountBatch.server_id == server_id)
                .order_by(AccountBatch.created_at.desc())
                .limit(limit)
            ).all()
            alias = self._alias(session, server_id)
            return [self._batch_out(row, alias) for row in rows]

    def reveal_passwords(self, batch_id: str) -> RevealPasswordsOut:
        with self._lock:
            passwords = self._batch_passwords.pop(batch_id, None)
        if not passwords:
            raise ConflictError(
                "passwords_unavailable",
                "生成密码已读取或已过期；如需新的密码请重置该账户的密码",
            )
        record_audit(
            self._session_factory,
            action="reveal_generated_passwords",
            target=batch_id,
            outcome="succeeded",
            detail=f"{len(passwords)} 个账户（内容不记录）",
        )
        return RevealPasswordsOut(batch_id=batch_id, passwords=passwords)

    def _batch_out(self, row: AccountBatch, alias: str) -> AccountBatchOut:
        results = [
            AccountBatchResultOut(
                login=item.get("login", ""),
                status=item.get("status", "unknown"),
                error=item.get("error"),
                uid=item.get("uid"),
                home=item.get("home"),
                password_generated=bool(item.get("passwordGenerated", False)),
            )
            for item in (row.results or [])
            if isinstance(item, dict)
        ]
        with self._lock:
            has_passwords = row.id in self._batch_passwords
        return AccountBatchOut(
            id=row.id,
            server_id=row.server_id,
            server_alias=alias,
            status=row.status,  # type: ignore[arg-type]
            stage=row.stage,
            progress=row.progress,
            results=results,
            error=row.error,
            has_generated_passwords=has_passwords,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    # ------------------------------------------------------------------
    # 用户目录迁移
    # ------------------------------------------------------------------

    def _account(self, server: Server, login: str) -> dict[str, Any]:
        data = self._probe(
            server, "account_info", "accountInfo", [login], operation="账户信息读取", timeout=30.0
        )
        account = data.get("account")
        if not isinstance(account, dict):
            raise NotFoundError("user_not_found", f"主机上不存在账户 {login}")
        return account

    def preflight_migration(
        self,
        server: Server,
        login: str,
        *,
        target_root: str | None = None,
        margin_percent: int = 10,
        migration_id: str | None = None,
    ) -> MigrationPreflightOut:
        account = self._account(server, login)
        validate_uid(account.get("uid"))
        home = normalize_path(str(account.get("home") or ""), label="源 home")
        root = self._resolve_migration_root(server, target_root)
        paths = build_migration_paths(
            home=home,
            target_root=root,
            migration_id=migration_id or uuid.uuid4().hex,
            timestamp=_now().strftime("%Y%m%d-%H%M%S"),
        )
        data = self._probe(
            server,
            "home_inspect",
            "homeInspect",
            [paths.source, paths.target_root, paths.target, paths.staging, paths.backup],
            operation="迁移预检",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        checks, space = self._evaluate_preflight(
            paths, data, account=account, margin=margin_percent
        )
        blockers = [
            check.message or check.label for check in checks if check.blocking and not check.ok
        ]
        return MigrationPreflightOut(
            server_id=server.id,
            login=login,
            paths=_paths_out(paths),
            source_info=dict(data.get("source") or {}),
            target_info=dict(data.get("targetRoot") or {}),
            counts=dict(data.get("counts") or {}),
            space=space,
            checks=checks,
            allowed=not blockers,
            blockers=blockers,
            collected_at=_now(),
        )

    def _resolve_migration_root(self, server: Server, target_root: str | None) -> str:
        if target_root:
            return normalize_path(target_root, label="迁移根目录")
        if server.migration_root:
            return normalize_path(server.migration_root, label="迁移根目录")
        return DEFAULT_MIGRATION_ROOT

    def _evaluate_preflight(
        self,
        paths: MigrationPaths,
        data: dict[str, Any],
        *,
        account: dict[str, Any],
        margin: int,
    ) -> tuple[list[CheckOut], dict[str, Any]]:
        source = data.get("source") or {}
        root = data.get("targetRoot") or {}
        target = data.get("targetPath") or {}
        staging = data.get("stagingPath") or {}
        backup = data.get("backupPath") or {}
        counts = data.get("counts") or {}
        caps = data.get("capabilities") or {}
        sub_mounts = data.get("subMounts") or []

        checks = [
            _check(
                "source_exists",
                "源 home 存在且是普通目录",
                bool(source.get("exists") and source.get("isDir")),
                "源目录不存在",
            ),
            _check(
                "source_not_symlink",
                "源 home 不是符号链接",
                not bool(source.get("isSymlink")),
                "源 home 已是符号链接，可能已迁移",
            ),
            _check(
                "source_is_home",
                "源路径与账户 home 一致",
                str(account.get("home") or "") == paths.source,
                f"账户 home 为 {account.get('home')}",
            ),
            _check(
                "source_uid",
                "只迁移普通账户（UID ≥ 1000）",
                int(account.get("uid") or 0) >= 1000,
                "系统账户不允许迁移",
            ),
            _check(
                "source_filesystem",
                "源文件系统为本地 ext4",
                str(source.get("filesystem")) in MIGRATION_FILESYSTEMS,
                f"源文件系统为 {source.get('filesystem') or '未知'}，首版只允许 ext4",
            ),
            _check(
                "source_local",
                "源设备为本地块设备",
                source.get("local") is True,
                f"源设备 {source.get('mountPoint')} 不是本地块设备",
            ),
            _check(
                "source_rw", "源文件系统可写", not bool(source.get("readOnly")), "源文件系统只读"
            ),
            _check(
                "source_not_mountpoint",
                "源 home 不是挂载点",
                not bool(source.get("isMountPoint")),
                "源 home 是挂载点，rename 会跨文件系统",
            ),
            _check(
                "no_submounts",
                "源目录下没有子挂载",
                not sub_mounts,
                f"存在子挂载：{', '.join(str(item) for item in sub_mounts[:5])}",
            ),
            _check(
                "target_root_exists",
                "目标根目录存在且是普通目录",
                bool(root.get("exists") and root.get("isDir") and not root.get("isSymlink")),
                "目标根目录不存在或不是目录",
            ),
            _check(
                "target_filesystem",
                "目标文件系统为本地 ext4",
                str(root.get("filesystem")) in MIGRATION_FILESYSTEMS,
                f"目标文件系统为 {root.get('filesystem') or '未知'}，首版只允许 ext4",
            ),
            _check(
                "target_local",
                "目标设备为本地块设备",
                root.get("local") is True,
                "目标不是本地块设备（网络或未知布局不允许迁移）",
            ),
            _check(
                "target_rw", "目标文件系统可写", not bool(root.get("readOnly")), "目标文件系统只读"
            ),
            _check(
                "target_distinct",
                "目标与源不在同一文件系统",
                root.get("device") != source.get("device"),
                "目标与源在同一文件系统，迁移没有意义",
            ),
            _check(
                "target_absent",
                "目标目录尚未存在",
                not bool(target.get("exists")),
                f"{paths.target} 已存在",
            ),
            _check(
                "staging_absent",
                "临时目录尚未存在",
                not bool(staging.get("exists")),
                f"{paths.staging} 已存在",
            ),
            _check(
                "backup_absent",
                "备份目录尚未存在",
                not bool(backup.get("exists")),
                f"{paths.backup} 已存在",
            ),
            _check(
                "counts_complete",
                "空间统计完整（未截断）",
                bool(counts) and not counts.get("truncated") and not counts.get("incomplete"),
                "统计被截断或不完整，按保守口径拒绝迁移",
            ),
            _check(
                "rsync_available",
                "远端提供 rsync",
                bool(caps.get("rsync")),
                "目标主机没有 rsync，无法保留 ACL/扩展属性",
            ),
        ]
        space = self._space_plan(counts, root, margin)
        checks.append(
            _check(
                "space",
                "目标剩余空间与 inode 充足",
                space["ok"],
                space["message"],
            )
        )
        shell = str(account.get("shell") or "")
        if shell.endswith("nologin") or shell.endswith("/false"):
            checks.append(
                _check(
                    "shell",
                    "账户 shell 为 nologin，迁移后无法用该用户做功能验证",
                    False,
                    shell,
                    blocking=False,
                )
            )
        return checks, space

    @staticmethod
    def _space_plan(counts: dict[str, Any], root: dict[str, Any], margin: int) -> dict[str, Any]:
        allocated = int(counts.get("allocatedBytes") or 0)
        required = int(allocated * (1 + margin / 100.0)) + SAFETY_RESERVE_BYTES
        free = int(root.get("freeBytes") or 0)
        required_inodes = int(counts.get("inodes") or 0) + 1024
        free_inodes = int(root.get("freeInodes") or 0)
        inode_ok = free_inodes <= 0 or free_inodes >= required_inodes
        ok = allocated > 0 and free >= required and inode_ok
        message = (
            f"需要 {required} 字节（含 {margin}% 余量与 {SAFETY_RESERVE_BYTES} 字节保留），"
            f"可用 {free} 字节；需要 inode {required_inodes}，可用 {free_inodes}"
        )
        return {
            "allocatedBytes": allocated,
            "apparentBytes": int(counts.get("apparentBytes") or 0),
            "files": int(counts.get("files") or 0),
            "dirs": int(counts.get("dirs") or 0),
            "inodes": int(counts.get("inodes") or 0),
            "requiredBytes": required,
            "freeBytes": free,
            "requiredInodes": required_inodes,
            "freeInodes": free_inodes,
            "marginPercent": margin,
            "ok": ok,
            "message": message,
        }

    def create_migration(self, server: Server, request: Any) -> MigrationOut:
        if self._closed:
            raise ApiError(503, "service_stopping", "服务正在关闭，无法提交新作业")
        password = self._require_password(server)
        migration_id = uuid.uuid4().hex
        preflight = self.preflight_migration(
            server,
            request.login,
            target_root=request.target_root,
            margin_percent=request.safety_margin_percent,
            migration_id=migration_id,
        )
        if not preflight.allowed:
            raise ApiError(
                422, "preflight_blocked", "迁移预检未通过：" + "；".join(preflight.blockers)
            )
        paths = preflight.paths
        account = self._account(server, request.login)
        with session_scope(self._session_factory) as session:
            row = HomeMigration(
                id=migration_id,
                server_id=server.id,
                login=request.login,
                uid=int(account.get("uid") or 0),
                gid=int(account.get("gid") or 0),
                source_home=paths.source,
                target_root=paths.target_root,
                target_path=paths.target,
                staging_path=paths.staging,
                backup_path=paths.backup,
                status="queued",
                stage="preflight",
                progress=0,
                request={
                    "safetyMarginPercent": request.safety_margin_percent,
                    "fullChecksum": bool(request.full_checksum),
                    "retentionDays": request.retention_days,
                },
                preflight={
                    "counts": preflight.counts,
                    "space": preflight.space,
                    "checks": [
                        check.model_dump(mode="json", by_alias=True) for check in preflight.checks
                    ],
                },
                backup_retention_days=request.retention_days,
            )
            session.add(row)
            session.flush()
            out = self._migration_out(row, server.alias)
            self._migration_cancel[migration_id] = threading.Event()
        record_audit(
            self._session_factory,
            action="migrate_home",
            target=f"{server.alias}:{paths.source}",
            outcome="queued",
            detail=f"→ {paths.target}",
        )
        self._pool.submit(self._run_migration, migration_id, password)
        return out

    def _run_migration(self, migration_id: str, password: str) -> None:
        cancel = self._migration_cancel.get(migration_id) or threading.Event()
        try:
            with session_scope(self._session_factory) as session:
                row = session.get(HomeMigration, migration_id)
                if row is None or row.status in MIGRATION_TERMINAL_STATUSES:
                    return
                if row.cancel_requested or cancel.is_set():
                    row.status = "cancelled"
                    row.finished_at = _now()
                    return
                row.status = "running"
                row.started_at = _now()
                row.progress = 2
                server = session.get(Server, row.server_id)
                if server is None:
                    row.status = "failed"
                    row.error = "主机记录已删除"
                    row.finished_at = _now()
                    return
                paths = MigrationPaths(
                    source=row.source_home,
                    target_root=row.target_root,
                    target=row.target_path,
                    staging=row.staging_path,
                    backup=row.backup_path,
                )
                context = _MigrationContext(
                    server=server,
                    password=password,
                    row=row,
                    paths=paths,
                    request=dict(row.request or {}),
                    preflight=dict(row.preflight or {}),
                )
                start_index = (
                    MIGRATION_STAGES.index(row.stage) if row.stage in MIGRATION_STAGES else 0
                )
            self._publish_migration(migration_id, "running", 2)
            for index in range(start_index, len(MIGRATION_STAGES)):
                stage = MIGRATION_STAGES[index]
                if cancel.is_set() or self._migration_cancelled(migration_id):
                    self._finish_migration(
                        migration_id,
                        status="cancelled",
                        stage=stage,
                        error="迁移已取消；请核对远端状态后回退或清理",
                    )
                    return
                progress = min(95, int(100 * index / len(MIGRATION_STAGES)) + 3)
                self._set_migration_stage(migration_id, stage, progress)
                getattr(self, f"_stage_{stage}")(context)
            self._finish_migration(migration_id, status="succeeded", stage="observe", error=None)
        except ApiError as exc:
            self._handle_migration_failure(migration_id, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("迁移执行异常：%s", migration_id)
            self._handle_migration_failure(migration_id, f"迁移执行异常：{exc}")
        finally:
            with self._lock:
                self._migration_cancel.pop(migration_id, None)

    def _migration_cancelled(self, migration_id: str) -> bool:
        with self._session_factory() as session:
            row = session.get(HomeMigration, migration_id)
            return bool(row and row.cancel_requested)

    def _set_migration_stage(self, migration_id: str, stage: str, progress: int) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, migration_id)
            if row is None or row.status in MIGRATION_TERMINAL_STATUSES:
                return
            row.stage = stage
            row.progress = progress
        self._publish_migration(migration_id, "running", progress, stage=stage)

    def _handle_migration_failure(self, migration_id: str, message: str) -> None:
        with self._session_factory() as session:
            row = session.get(HomeMigration, migration_id)
            stage = row.stage if row is not None else "unknown"
            alias = self._alias(session, row.server_id) if row is not None else ""
        self._finish_migration(migration_id, status="failed", stage=stage, error=message)
        record_audit(
            self._session_factory,
            action="migrate_home",
            target=alias,
            outcome="failed",
            detail=f"{stage}: {message}"[:200],
        )

    def _finish_migration(
        self, migration_id: str, *, status: str, stage: str, error: str | None
    ) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, migration_id)
            if row is None:
                return
            row.status = status
            row.stage = stage
            row.error = error
            row.progress = 100 if status == "succeeded" else row.progress
            row.finished_at = _now()
            alias = self._alias(session, row.server_id)
            out = self._migration_out(row, alias)
        self._publish_migration(migration_id, status, out.progress, error=error, stage=stage)

    # -- 阶段实现 -------------------------------------------------------

    def _stage_preflight(self, ctx: _MigrationContext) -> None:
        data = self._probe(
            ctx.server,
            "home_inspect",
            "homeInspect",
            [
                ctx.paths.source,
                ctx.paths.target_root,
                ctx.paths.target,
                ctx.paths.staging,
                ctx.paths.backup,
            ],
            operation="迁移预检",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        account = dict(data.get("account") or {})
        checks, space = self._evaluate_preflight(
            ctx.paths,
            data,
            account=account,
            margin=int(ctx.request.get("safetyMarginPercent", 10)),
        )
        blockers = [
            check.message or check.label for check in checks if check.blocking and not check.ok
        ]
        preflight = {
            "counts": data.get("counts") or {},
            "space": space,
            "checks": [check.model_dump(mode="json", by_alias=True) for check in checks],
            "sourceUid": account.get("uid"),
            "sourceGid": account.get("gid"),
            "sourceMode": (data.get("source") or {}).get("mode"),
            "sourceEntries": (data.get("source") or {}).get("entryCount"),
        }
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, ctx.row.id)
            if row is not None:
                row.preflight = preflight
                if account.get("uid") is not None:
                    row.uid = int(account.get("uid") or 0)
                if account.get("gid") is not None:
                    row.gid = int(account.get("gid") or 0)
        ctx.preflight = preflight
        if blockers:
            raise ApiError(422, "preflight_blocked", "迁移预检未通过：" + "；".join(blockers))

    def _stage_space_check(self, ctx: _MigrationContext) -> None:
        counts = dict(ctx.preflight.get("counts") or {})
        if not counts:
            raise ApiError(422, "preflight_missing", "缺少空间统计结果，无法计算需求")
        data = self._probe(
            ctx.server,
            "home_inspect",
            "homeInspect",
            [
                ctx.paths.source,
                ctx.paths.target_root,
                ctx.paths.target,
                ctx.paths.staging,
                ctx.paths.backup,
            ],
            operation="空间复核",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        space = self._space_plan(
            counts,
            dict(data.get("targetRoot") or {}),
            int(ctx.request.get("safetyMarginPercent", 10)),
        )
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, ctx.row.id)
            if row is not None:
                preflight = dict(row.preflight or {})
                preflight["space"] = space
                row.preflight = preflight
        ctx.preflight["space"] = space
        if not space["ok"]:
            raise ApiError(422, "insufficient_space", f"目标空间不足：{space['message']}")

    def _stage_freeze(self, ctx: _MigrationContext) -> None:
        result = self._run(
            ctx.server,
            build_freeze_script(ctx.row.login, int(ctx.row.uid)),
            sudo=True,
            password=ctx.password,
            operation="冻结账户",
        )
        markers = _markers(result.stdout)
        before = markers.get("LOCK_BEFORE", "unknown")
        remaining = _int_or_none(markers.get("REMAINING")) or 0
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, ctx.row.id)
            if row is not None:
                row.account_status_before = before[:16]
        ctx.row.account_status_before = before[:16]
        if remaining > 0:
            self._run(
                ctx.server,
                build_thaw_script(ctx.row.login, before),
                sudo=True,
                password=ctx.password,
                allow_failure=True,
                operation="恢复账户",
            )
            raise ApiError(
                409,
                "processes_remaining",
                f"仍有 {remaining} 个进程属于 UID {ctx.row.uid}，已恢复账户并中止迁移",
            )

    def _stage_rename(self, ctx: _MigrationContext) -> None:
        result = self._run(
            ctx.server,
            build_rename_script(ctx.paths),
            sudo=True,
            password=ctx.password,
            allow_failure=True,
            operation="原地改名",
        )
        markers = _markers(result.stdout)
        if markers.get("RESULT") != "ok":
            self._restore_account(ctx)
            raise ApiError(
                409, "rename_failed", f"原地改名失败：{markers.get('RESULT') or '未知原因'}"
            )

    def _stage_copy(self, ctx: _MigrationContext) -> None:
        result = self._run(
            ctx.server,
            build_copy_script(ctx.paths),
            sudo=True,
            password=ctx.password,
            allow_failure=True,
            timeout=COPY_TIMEOUT_SECONDS,
            operation="复制到新盘",
        )
        markers = _markers(result.stdout)
        if markers.get("RESULT") != "ok":
            detail = " ".join(result.combined_output.strip().splitlines()[-2:])
            raise ApiError(
                502,
                "copy_failed",
                f"rsync 复制失败：{markers.get('RESULT') or '未知原因'} {detail}".strip(),
            )

    def _stage_verify(self, ctx: _MigrationContext) -> None:
        source_stats = self._probe(
            ctx.server,
            "tree_stats",
            "treeStats",
            [ctx.paths.backup],
            operation="源目录统计",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        target_stats = self._probe(
            ctx.server,
            "tree_stats",
            "treeStats",
            [ctx.paths.staging],
            operation="目标目录统计",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        for stats, label in ((source_stats, "源"), (target_stats, "目标")):
            if stats.get("truncated") or stats.get("incomplete"):
                raise ApiError(422, "verify_incomplete", f"{label}统计被截断或不完整，已停止迁移")

        mismatches: list[str] = []
        if int(source_stats.get("files", 0)) != int(target_stats.get("files", 0)):
            mismatches.append(f"文件数 {source_stats.get('files')} → {target_stats.get('files')}")
        if int(source_stats.get("dirs", 0)) != int(target_stats.get("dirs", 0)):
            mismatches.append(f"目录数 {source_stats.get('dirs')} → {target_stats.get('dirs')}")
        if int(source_stats.get("apparentBytes", 0)) != int(target_stats.get("apparentBytes", 0)):
            mismatches.append("逻辑大小不一致")
        allocated_source = int(source_stats.get("allocatedBytes", 0))
        allocated_target = int(target_stats.get("allocatedBytes", 0))
        tolerance = max(_SPACE_TOLERANCE_BYTES, int(allocated_source * _SPACE_TOLERANCE_RATIO))
        if abs(allocated_target - allocated_source) > tolerance:
            mismatches.append(f"实际占用差异过大（{allocated_source} → {allocated_target}）")
        if mismatches:
            raise ApiError(409, "verify_failed", "复制前后校验失败：" + "；".join(mismatches))

        if bool(ctx.request.get("fullChecksum")):
            checksum = self._run(
                ctx.server,
                build_checksum_script(ctx.paths),
                sudo=True,
                password=ctx.password,
                allow_failure=True,
                timeout=COPY_TIMEOUT_SECONDS,
                operation="全量内容校验",
            )
            markers = _markers(checksum.stdout)
            changed = _int_or_none(markers.get("CHANGED")) or 0
            if markers.get("RESULT") != "ok" or changed > 0:
                raise ApiError(
                    409,
                    "checksum_failed",
                    f"全量内容校验发现 {changed} 个差异文件",
                )

        fresh = self._probe(
            ctx.server,
            "home_inspect",
            "homeInspect",
            [
                ctx.paths.source,
                ctx.paths.target_root,
                ctx.paths.target,
                ctx.paths.staging,
                ctx.paths.backup,
            ],
            operation="目标盘空间复核",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        space_before = dict((ctx.preflight or {}).get("space") or {})
        free_before = int(space_before.get("freeBytes") or 0)
        free_after = int((dict(fresh.get("targetRoot") or {})).get("freeBytes") or 0)
        consumed = max(0, free_before - free_after)
        if (
            free_before
            and allocated_target
            and abs(consumed - allocated_target)
            > max(_SPACE_TOLERANCE_BYTES, int(allocated_target * _SPACE_TOLERANCE_RATIO))
        ):
            raise ApiError(
                409,
                "space_delta_anomaly",
                f"目标盘可用空间变化异常（占用 {allocated_target}，实际减少 {consumed}）",
            )
        source_info = dict(fresh.get("source") or {})
        expected_uid = int(ctx.preflight.get("sourceUid") or ctx.row.uid)
        target_root_info = dict(fresh.get("stagingPath") or {})
        if target_root_info.get("exists") and target_root_info.get("uid") not in (
            None,
            expected_uid,
        ):
            raise ApiError(
                409,
                "owner_mismatch",
                f"目标目录属主为 {target_root_info.get('uid')}，预期 {expected_uid}",
            )
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, ctx.row.id)
            if row is not None:
                row.result = {
                    "sourceStats": source_stats,
                    "targetStats": target_stats,
                    "spaceDelta": {"freeBefore": free_before, "freeAfter": free_after},
                    "sourceInfo": source_info,
                }

    def _stage_switch(self, ctx: _MigrationContext) -> None:
        result = self._run(
            ctx.server,
            build_switch_script(ctx.paths),
            sudo=True,
            password=ctx.password,
            allow_failure=True,
            operation="切换链接",
        )
        markers = _markers(result.stdout)
        if markers.get("RESULT") != "ok":
            raise ApiError(
                409,
                "switch_failed",
                f"切换符号链接失败：{markers.get('RESULT') or '未知原因'}",
            )
        if markers.get("RESOLVED") not in (None, "", ctx.paths.target):
            raise ApiError(
                409,
                "link_mismatch",
                f"符号链接解析到 {markers.get('RESOLVED')}，预期 {ctx.paths.target}",
            )

    def _stage_restore(self, ctx: _MigrationContext) -> None:
        self._run(
            ctx.server,
            build_thaw_script(ctx.row.login, ctx.row.account_status_before),
            sudo=True,
            password=ctx.password,
            allow_failure=True,
            operation="恢复账户状态",
        )
        functional = self._run(
            ctx.server,
            build_functional_test_script(ctx.row.login),
            sudo=True,
            password=ctx.password,
            allow_failure=True,
            operation="功能验证",
        )
        markers = _markers(functional.stdout)
        outcome = {
            "functional": markers.get("FUNCTIONAL", "unknown"),
            "sshDirMode": markers.get("SSH_DIR"),
            "sshKeyMode": markers.get("SSH_KEY"),
            "sshOwner": _int_or_none(markers.get("SSH_OWNER")),
        }
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, ctx.row.id)
            if row is not None:
                result = dict(row.result or {})
                result["restore"] = outcome
                row.result = result
        if outcome["functional"] != "ok":
            self._auto_rollback(ctx, "功能验证未通过，已自动回退")
            raise ApiError(
                409,
                "functional_test_failed",
                "迁移后功能验证未通过，已自动回退到原目录；请检查备份与日志",
            )
        if outcome["sshKeyMode"] not in (None, "600") and outcome["sshKeyMode"] != "600":
            logger.warning("authorized_keys 权限异常：%s", outcome["sshKeyMode"])

    def _stage_observe(self, ctx: _MigrationContext) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, ctx.row.id)
            if row is not None:
                result = dict(row.result or {})
                result["backup"] = {
                    "path": ctx.paths.backup,
                    "retentionDays": row.backup_retention_days,
                    "counts": (ctx.preflight or {}).get("counts") or {},
                }
                result["target"] = ctx.paths.target
                row.result = result

    def _restore_account(self, ctx: _MigrationContext) -> None:
        try:
            self._run(
                ctx.server,
                build_thaw_script(ctx.row.login, ctx.row.account_status_before),
                sudo=True,
                password=ctx.password,
                allow_failure=True,
                operation="恢复账户状态",
            )
        except ApiError:
            logger.warning("恢复账户 %s 失败", ctx.row.login)

    def _auto_rollback(self, ctx: _MigrationContext, reason: str) -> None:
        """迁移后验证失败时自动回退：重新冻结 → 删除链接 → 备份改名回原路径 → 恢复登录。"""

        try:
            self._run(
                ctx.server,
                build_freeze_script(ctx.row.login, int(ctx.row.uid)),
                sudo=True,
                password=ctx.password,
                allow_failure=True,
                operation="回退前冻结账户",
            )
            result = self._run(
                ctx.server,
                build_rollback_script(ctx.paths, discard_new_data=True),
                sudo=True,
                password=ctx.password,
                allow_failure=True,
                operation="自动回退",
            )
            markers = _markers(result.stdout)
            rolled_back = markers.get("RESULT") == "ok"
            self._run(
                ctx.server,
                build_thaw_script(ctx.row.login, ctx.row.account_status_before),
                sudo=True,
                password=ctx.password,
                allow_failure=True,
                operation="回退后恢复账户",
            )
            with session_scope(self._session_factory) as session:
                row = session.get(HomeMigration, ctx.row.id)
                if row is not None:
                    result_data = dict(row.result or {})
                    result_data["autoRollback"] = {"ok": rolled_back, "reason": reason}
                    row.result = result_data
            logger.warning("迁移 %s 自动回退：%s（%s）", ctx.row.id, reason, rolled_back)
        except ApiError as exc:
            logger.error("迁移 %s 自动回退失败：%s", ctx.row.id, exc.message)

    # -- 迁移查询与操作 -------------------------------------------------

    def _migration_out(self, row: HomeMigration, alias: str) -> MigrationOut:
        preflight = row.preflight if isinstance(row.preflight, dict) else {}
        result = row.result if isinstance(row.result, dict) else {}
        checks = []
        for item in preflight.get("checks") or []:
            try:
                checks.append(CheckOut.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
        stage = row.stage
        stage_index = MIGRATION_STAGES.index(stage) if stage in MIGRATION_STAGES else 0
        rollback_available = (
            stage in MIGRATION_STAGES
            and MIGRATION_STAGES.index(stage) >= MIGRATION_STAGES.index("rename")
            and row.cleaned_at is None
            and row.status in {"succeeded", "failed", "interrupted"}
        )
        cleanup_available = (
            row.status == "succeeded" and stage == "observe" and row.cleaned_at is None
        )
        return MigrationOut(
            id=row.id,
            server_id=row.server_id,
            server_alias=alias,
            login=row.login,
            uid=row.uid,
            gid=row.gid,
            status=row.status,  # type: ignore[arg-type]
            stage=stage,
            stage_index=stage_index,
            stages=list(MIGRATION_STAGES),
            progress=row.progress,
            paths=MigrationPathsOut(
                source=row.source_home,
                target_root=row.target_root,
                target=row.target_path,
                staging=row.staging_path,
                backup=row.backup_path,
            ),
            counts=dict(preflight.get("counts") or {}),
            space=dict(preflight.get("space") or {}),
            checks=checks,
            result=result,
            error=row.error,
            account_status_before=row.account_status_before,
            backup_retention_days=row.backup_retention_days,
            rollback_available=rollback_available,
            cleanup_available=cleanup_available,
            cancel_requested=row.cancel_requested,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            cleaned_at=row.cleaned_at,
        )

    def get_migration(self, migration_id: str) -> MigrationOut:
        with self._session_factory() as session:
            row = session.get(HomeMigration, migration_id)
            if row is None:
                raise NotFoundError("migration_not_found", "迁移作业不存在")
            return self._migration_out(row, self._alias(session, row.server_id))

    def list_migrations(self, server_id: int, *, limit: int = 20) -> list[MigrationOut]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(HomeMigration)
                .where(HomeMigration.server_id == server_id)
                .order_by(HomeMigration.created_at.desc())
                .limit(limit)
            ).all()
            alias = self._alias(session, server_id)
            return [self._migration_out(row, alias) for row in rows]

    def cancel_migration(self, migration_id: str) -> MigrationOut:
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, migration_id)
            if row is None:
                raise NotFoundError("migration_not_found", "迁移作业不存在")
            if row.status in MIGRATION_TERMINAL_STATUSES:
                raise ConflictError("migration_finished", "迁移已结束，无法取消")
            alias = self._alias(session, row.server_id)
            row.cancel_requested = True
            out = self._migration_out(row, alias)
        with self._lock:
            event = self._migration_cancel.get(migration_id)
        if event is not None:
            event.set()
        return out

    def resume_migration(self, migration_id: str) -> MigrationOut:
        password = self._require_password_for(migration_id)
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, migration_id)
            if row is None:
                raise NotFoundError("migration_not_found", "迁移作业不存在")
            if row.status not in {"failed", "interrupted", "cancelled"}:
                raise ConflictError("migration_not_resumable", "只有失败/中断/取消的迁移才能继续")
            server = session.get(Server, row.server_id)
            if server is None:
                raise NotFoundError("server_not_found", "主机不存在")
            stage, reconciled = self._reconcile_stage(server, row)
            row.status = "queued"
            row.stage = stage
            row.error = None
            row.cancel_requested = False
            row.finished_at = None
            alias = server.alias
            out = self._migration_out(row, alias)
            self._migration_cancel[migration_id] = threading.Event()
        record_audit(
            self._session_factory,
            action="resume_home_migration",
            target=alias,
            outcome="queued",
            detail=f"阶段 {stage}（{reconciled}）",
        )
        self._pool.submit(self._run_migration, migration_id, password)
        return out

    def _reconcile_stage(self, server: Server, row: HomeMigration) -> tuple[str, str]:
        """核对远端实际状态，决定从哪个阶段继续。"""

        data = self._probe(
            server,
            "home_inspect",
            "homeInspect",
            [row.source_home, row.target_root, row.target_path, row.staging_path, row.backup_path],
            operation="迁移状态核对",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        source = dict(data.get("source") or {})
        backup = dict(data.get("backupPath") or {})
        target = dict(data.get("targetPath") or {})
        if source.get("isSymlink") and target.get("exists"):
            return "restore", "源路径已是符号链接且目标存在"
        if backup.get("exists") and not source.get("exists"):
            if target.get("exists"):
                return "verify", "备份存在、目标已就绪，从校验继续"
            return "copy", "备份存在、源路径已移走，从复制继续"
        if source.get("exists") and not backup.get("exists"):
            return "preflight", "源目录完好，从预检重新开始"
        raise ConflictError(
            "migration_state_ambiguous",
            "远端状态无法安全判定（源、备份、目标同时存在或同时缺失），请人工检查后再回退",
        )

    def rollback_migration(self, migration_id: str, *, discard_new_data: bool) -> MigrationOut:
        password = self._require_password_for(migration_id)
        with self._session_factory() as session:
            row = session.get(HomeMigration, migration_id)
            if row is None:
                raise NotFoundError("migration_not_found", "迁移作业不存在")
            if row.cleaned_at is not None:
                raise ConflictError("backup_cleaned", "备份已被清理，无法回退")
            if row.stage not in MIGRATION_STAGES or (
                MIGRATION_STAGES.index(row.stage) < MIGRATION_STAGES.index("rename")
            ):
                raise ConflictError("nothing_to_rollback", "尚未执行改名，无需回退")
            server = session.get(Server, row.server_id)
            if server is None:
                raise NotFoundError("server_not_found", "主机不存在")
            alias = server.alias
            paths = MigrationPaths(
                source=row.source_home,
                target_root=row.target_root,
                target=row.target_path,
                staging=row.staging_path,
                backup=row.backup_path,
            )
            login = row.login
            uid = row.uid
            status_before = row.account_status_before
            server_obj = server
        self._run(
            server_obj,
            build_freeze_script(login, uid),
            sudo=True,
            password=password,
            allow_failure=True,
            operation="回退前冻结账户",
        )
        try:
            result = self._run(
                server_obj,
                build_rollback_script(paths, discard_new_data=discard_new_data),
                sudo=True,
                password=password,
                allow_failure=True,
                operation="回退目录",
            )
        finally:
            self._run(
                server_obj,
                build_thaw_script(login, status_before),
                sudo=True,
                password=password,
                allow_failure=True,
                operation="回退后恢复账户",
            )
        markers = _markers(result.stdout)
        if markers.get("RESULT") != "ok":
            raise ApiError(
                409,
                "rollback_failed",
                f"回退失败：{markers.get('RESULT') or '未知原因'}",
            )
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, migration_id)
            if row is not None:
                row.status = "rolled_back"
                row.stage = "rolled_back"
                row.error = None
                row.finished_at = _now()
                row.progress = 100
                out = self._migration_out(row, alias)
            else:  # pragma: no cover - 记录被并发删除
                raise NotFoundError("migration_not_found", "迁移作业不存在")
        self._publish_migration(migration_id, "rolled_back", 100, stage="rolled_back")
        record_audit(
            self._session_factory,
            action="rollback_home_migration",
            target=alias,
            outcome="succeeded",
            detail=f"{paths.source} 已回退",
        )
        return out

    def cleanup_migration(self, migration_id: str, confirm_path: str) -> MigrationOut:
        password = self._require_password_for(migration_id)
        with self._session_factory() as session:
            row = session.get(HomeMigration, migration_id)
            if row is None:
                raise NotFoundError("migration_not_found", "迁移作业不存在")
            if row.status != "succeeded" or row.stage != "observe":
                raise ConflictError(
                    "cleanup_not_available", "只有成功完成且处于观察阶段的迁移才能清理备份"
                )
            if row.cleaned_at is not None:
                raise ConflictError("already_cleaned", "备份已清理")
            if confirm_path.strip() != row.backup_path:
                raise ApiError(
                    422,
                    "cleanup_confirmation_mismatch",
                    "确认路径与备份路径不一致，已拒绝删除",
                )
            server = session.get(Server, row.server_id)
            if server is None:
                raise NotFoundError("server_not_found", "主机不存在")
            alias = server.alias
            paths = MigrationPaths(
                source=row.source_home,
                target_root=row.target_root,
                target=row.target_path,
                staging=row.staging_path,
                backup=row.backup_path,
            )
            server_obj = server
        data = self._probe(
            server_obj,
            "home_inspect",
            "homeInspect",
            [paths.source, paths.target_root, paths.target, paths.staging, paths.backup],
            operation="清理前复核",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        source = dict(data.get("source") or {})
        backup = dict(data.get("backupPath") or {})
        if not source.get("isSymlink") or not backup.get("exists"):
            raise ConflictError(
                "cleanup_precheck_failed",
                "清理前复核失败：原路径不是符号链接或备份目录不存在",
            )
        result = self._run(
            server_obj,
            build_cleanup_script(paths),
            sudo=True,
            password=password,
            allow_failure=True,
            timeout=CLEANUP_TIMEOUT_SECONDS,
            operation="清理备份目录",
        )
        markers = _markers(result.stdout)
        if markers.get("RESULT") != "ok":
            raise ApiError(
                409, "cleanup_failed", f"清理失败：{markers.get('RESULT') or '未知原因'}"
            )
        with session_scope(self._session_factory) as session:
            row = session.get(HomeMigration, migration_id)
            if row is not None:
                row.cleaned_at = _now()
                out = self._migration_out(row, alias)
            else:  # pragma: no cover
                raise NotFoundError("migration_not_found", "迁移作业不存在")
        record_audit(
            self._session_factory,
            action="cleanup_home_migration",
            target=alias,
            outcome="succeeded",
            detail=f"删除备份 {paths.backup}",
        )
        return out

    def _require_password_for(self, migration_id: str) -> str:
        with self._session_factory() as session:
            row = session.get(HomeMigration, migration_id)
            if row is None:
                raise NotFoundError("migration_not_found", "迁移作业不存在")
            server = session.get(Server, row.server_id)
            if server is None:
                raise NotFoundError("server_not_found", "主机不存在")
            return self._require_password(server)

    def _publish_migration(
        self,
        migration_id: str,
        status: str,
        progress: int,
        *,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": "done" if status in MIGRATION_TERMINAL_STATUSES else "progress",
            "migrationId": migration_id,
            "status": status,
            "progress": progress,
        }
        if stage:
            event["stage"] = stage
            event["stageIndex"] = (
                MIGRATION_STAGES.index(stage) if stage in MIGRATION_STAGES else None
            )
        if error:
            event["error"] = error
        self._publish(migration_id, event)

    # ------------------------------------------------------------------
    # 新空间分配
    # ------------------------------------------------------------------

    def storage_settings(self, server: Server) -> dict[str, Any]:
        return {
            "server_id": server.id,
            "data_root": server.data_root,
            "migration_root": server.migration_root,
            "default_data_root": DEFAULT_DATA_ROOT,
            "default_migration_root": DEFAULT_MIGRATION_ROOT,
        }

    def update_storage_settings(
        self, server: Server, *, data_root: str | None, migration_root: str | None
    ) -> dict[str, Any]:
        with session_scope(self._session_factory) as session:
            row = session.get(Server, server.id)
            if row is None:
                raise NotFoundError("server_not_found", "主机不存在")
            if data_root is not None:
                row.data_root = normalize_path(data_root, label="数据盘路径")
            if migration_root is not None:
                row.migration_root = normalize_path(migration_root, label="迁移根目录")
            result = {
                "server_id": row.id,
                "data_root": row.data_root,
                "migration_root": row.migration_root,
                "default_data_root": DEFAULT_DATA_ROOT,
                "default_migration_root": DEFAULT_MIGRATION_ROOT,
            }
        record_audit(
            self._session_factory,
            action="update_storage_settings",
            target=server.alias,
            outcome="succeeded",
            detail=f"dataRoot={result['data_root']} migrationRoot={result['migration_root']}",
        )
        return result

    def create_dataset(self, server: Server, request: Any) -> DatasetOut:
        password = self._require_password(server)
        if not request.confirm:
            raise ApiError(422, "confirmation_required", "分配空间需要确认后才能执行")
        data_root = request.data_root or server.data_root or DEFAULT_DATA_ROOT
        data_root = normalize_path(data_root, label="数据盘路径")
        account = self._account(server, request.login)
        uid = int(account.get("uid") or 0)
        gid = int(account.get("gid") or 0)
        if uid < 1000:
            raise ApiError(422, "system_account", "只允许给普通账户（UID ≥ 1000）分配空间")
        home = normalize_path(str(account.get("home") or ""), label="home 路径")
        data_path = normalize_path(f"{data_root}/{request.login}", label="数据目录")
        link_path = normalize_path(f"{home}/{request.link_name}", label="链接路径")
        if not link_path.startswith(home.rstrip("/") + "/"):
            raise ApiError(422, "invalid_link_path", "链接必须位于该用户的 home 目录内")

        probe = self._probe(
            server,
            "dataset_inspect",
            "datasetInspect",
            [request.login, data_root, data_path, link_path],
            operation="空间分配预检",
            timeout=60.0,
        )
        root_info = dict(probe.get("dataRoot") or {})
        if not root_info.get("exists"):
            raise ApiError(422, "data_root_missing", f"数据盘 {data_root} 不存在")
        if not root_info.get("isDir") or root_info.get("isSymlink"):
            raise ApiError(422, "data_root_invalid", f"{data_root} 不是普通目录")
        if str(root_info.get("filesystem")) not in MIGRATION_FILESYSTEMS:
            raise ApiError(
                422,
                "filesystem_not_supported",
                f"{data_root} 的文件系统为 {root_info.get('filesystem') or '未知'}，首版只支持本地 ext4",
            )
        if root_info.get("local") is not True:
            raise ApiError(422, "filesystem_not_supported", f"{data_root} 不是本地块设备")
        if root_info.get("readOnly"):
            raise ApiError(422, "read_only", f"{data_root} 所在文件系统只读")

        result = self._run(
            server,
            build_dataset_script(
                data_path=data_path,
                link_path=link_path,
                uid=uid,
                gid=gid,
                reuse_existing=request.reuse_existing,
            ),
            sudo=True,
            password=password,
            allow_failure=True,
            operation="分配新空间",
        )
        markers = _markers(result.stdout)
        if markers.get("RESULT") != "ok":
            raise ApiError(
                409,
                "dataset_failed",
                f"分配空间失败：{markers.get('RESULT') or '未知原因'}",
            )
        verify = self._run(
            server,
            build_dataset_verify_script(request.login, link_path),
            sudo=True,
            password=password,
            allow_failure=True,
            operation="空间可写性验证",
        )
        writable = _markers(verify.stdout).get("WRITABLE") == "ok"
        with session_scope(self._session_factory) as session:
            row = DatasetAllocation(
                id=uuid.uuid4().hex,
                server_id=server.id,
                login=request.login,
                uid=uid,
                gid=gid,
                data_root=data_root,
                data_path=data_path,
                link_path=link_path,
                status="succeeded",
                error=None if writable else "目录已创建，但账户写入验证未通过",
            )
            session.add(row)
            session.flush()
            out = _dataset_out(row, writable)
        record_audit(
            self._session_factory,
            action="allocate_dataset",
            target=server.alias,
            outcome="succeeded",
            detail=f"{request.login} → {data_path}（链接 {link_path}）",
        )
        return out

    def list_datasets(self, server_id: int, *, limit: int = 50) -> list[DatasetOut]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(DatasetAllocation)
                .where(DatasetAllocation.server_id == server_id)
                .order_by(DatasetAllocation.created_at.desc())
                .limit(limit)
            ).all()
            return [_dataset_out(row) for row in rows]

    def unlink_dataset(self, dataset_id: str) -> DatasetOut:
        with self._session_factory() as session:
            row = session.get(DatasetAllocation, dataset_id)
            if row is None:
                raise NotFoundError("dataset_not_found", "空间分配记录不存在")
            if row.status == "unlinked":
                raise ConflictError("dataset_already_unlinked", "该链接已解除")
            server = session.get(Server, row.server_id)
            if server is None:
                raise NotFoundError("server_not_found", "主机不存在")
            server_obj = server
            link_path = row.link_path
            data_path = row.data_path
            alias = server.alias
        password = self._require_password(server_obj)
        result = self._run(
            server_obj,
            build_unlink_script(link_path, data_path),
            sudo=True,
            password=password,
            allow_failure=True,
            operation="解除空间链接",
        )
        markers = _markers(result.stdout)
        if markers.get("RESULT") != "ok":
            raise ApiError(
                409, "unlink_failed", f"解除链接失败：{markers.get('RESULT') or '未知原因'}"
            )
        with session_scope(self._session_factory) as session:
            row = session.get(DatasetAllocation, dataset_id)
            if row is not None:
                row.status = "unlinked"
                out = _dataset_out(row)
            else:  # pragma: no cover
                raise NotFoundError("dataset_not_found", "空间分配记录不存在")
        record_audit(
            self._session_factory,
            action="unlink_dataset",
            target=alias,
            outcome="succeeded",
            detail=f"解除 {link_path}（数据目录保留）",
        )
        return out

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def _publish(self, job_id: str, event: dict[str, Any]) -> None:
        self._bus.publish(job_id, event)

    def _publish_done(self, batch_id: str, out: AccountBatchOut) -> None:
        self._publish(
            batch_id,
            {
                "type": "done",
                "batchId": batch_id,
                "status": out.status,
                "progress": out.progress,
                "error": out.error,
            },
        )

    @staticmethod
    def _alias(session: Session, server_id: int) -> str:
        server = session.get(Server, server_id)
        return server.alias if server is not None else ""


def _batch_error(status: str, detail: list[str]) -> str:
    mapping = {
        "exists": "用户名已存在",
        "useradd_failed": "useradd 执行失败",
        "credential_missing": "临时密码文件缺失",
        "password_failed": "chpasswd 设置密码失败",
        "password_not_set": "密码未生效（passwd -S 状态不是 P）",
    }
    message = mapping.get(status, f"远端返回未知状态：{status}")
    extra = " ".join(item.strip() for item in detail if item.strip())[:200]
    return f"{message}（{extra}）" if extra else message


def _paths_out(paths: MigrationPaths) -> MigrationPathsOut:
    return MigrationPathsOut(
        source=paths.source,
        target_root=paths.target_root,
        target=paths.target,
        staging=paths.staging,
        backup=paths.backup,
    )


def _policy_change_out(row: PasswordPolicyChange) -> PolicyChangeOut:
    return PolicyChangeOut(
        id=row.id,
        server_id=row.server_id,
        status=row.status,  # type: ignore[arg-type]
        targets=dict(row.targets or {}),
        diff=row.diff,
        backup_id=row.backup_id,
        validation=dict(row.validation or {}),
        error=row.error,
        created_at=row.created_at,
        restored_at=row.restored_at,
    )


def _dataset_out(row: DatasetAllocation, writable: bool | None = None) -> DatasetOut:
    resolved = (
        bool(writable)
        if writable is not None
        else (row.status == "succeeded" and row.error is None)
    )
    return DatasetOut(
        id=row.id,
        server_id=row.server_id,
        login=row.login,
        uid=row.uid,
        gid=row.gid,
        data_root=row.data_root,
        data_path=row.data_path,
        link_path=row.link_path,
        status=row.status,  # type: ignore[arg-type]
        writable=resolved,
        error=row.error,
        created_at=row.created_at,
    )


__all__ = ["ProvisioningService", "GENERATOR_KEY", "DEFAULT_DATA_ROOT", "DEFAULT_MIGRATION_ROOT"]
