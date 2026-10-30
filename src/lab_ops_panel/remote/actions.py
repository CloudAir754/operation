"""固定动作白名单。

每个动作都声明：风险等级、参数模型（拒绝未知字段）、是否需要 sudo、
是否需要幂等键，以及“预检 → 执行 → 校验”的命令计划。
任意命令入口默认关闭，开启后仍需 Bash 语法检查与高风险确认。
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from lab_ops_panel.config import Settings
from lab_ops_panel.errors import ApiError, ValidationFailedError
from lab_ops_panel.remote.logs import build_journalctl_command
from lab_ops_panel.remote.probe import PROBE_SOURCE, build_probe_command
from lab_ops_panel.remote.services import (
    ProtectedServiceError,
    UnitValidationError,
    ensure_action_allowed,
    is_protected,
    normalize_unit_name,
)
from lab_ops_panel.schemas import (
    ActionFieldOut,
    ActionsOut,
    ActionSpecOut,
    CommandParams,
    EmptyParams,
    LogsParams,
    UnitParams,
)

MAX_UNIT_NAME_LENGTH = 200


class UnknownActionError(ApiError):
    def __init__(self, action: str) -> None:
        super().__init__(400, "unknown_action", f"未知动作：{action}")


class ActionDisabledError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(400, "action_disabled", message)


class SudoPasswordMissingError(ApiError):
    def __init__(self, alias: str = "") -> None:
        target = f"主机 {alias} " if alias else "当前主机"
        super().__init__(
            409,
            "sudo_password_missing",
            f"{target}未保存 sudo 密码，无法执行需要提权的动作",
        )


class SudoRequiredButUnavailableError(ApiError):
    def __init__(self) -> None:
        super().__init__(503, "sudo_unavailable", "本机凭据后端不可用，无法使用保存的 sudo 密码")


@dataclass(frozen=True)
class PlanStep:
    name: str
    command: str
    timeout_seconds: float
    use_sudo: bool = False
    allow_failure: bool = False
    # 需要写入远端 stdin 的负载（探针源码）；sudo 步骤已占用 stdin 传密码，二者不能并存。
    stdin_data: bytes | None = None


@dataclass
class ActionContext:
    settings: Settings
    sudo_password: str | None
    action_timeout_seconds: float


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    description: str
    risk: str
    params_model: type[BaseModel]
    requires_sudo: bool
    requires_confirmation: bool
    requires_idempotency_key: bool
    persist_snapshot: bool
    build: Callable[[BaseModel, ActionContext], list[PlanStep]]
    preflight: Callable[[BaseModel, ActionContext], None]

    def describe(self) -> ActionSpecOut:
        return ActionSpecOut(
            id=self.id,
            label=self.label,
            description=self.description,
            risk=self.risk,  # type: ignore[arg-type]
            requires_confirmation=self.requires_confirmation,
            requires_idempotency_key=self.requires_idempotency_key,
            fields=_describe_fields(self.params_model),
        )


def _annotation_kind(annotation: object) -> str:
    text = str(annotation)
    if "int" in text and "str" not in text:
        return "integer"
    if "bool" in text:
        return "boolean"
    return "string"


def _describe_fields(model: type[BaseModel]) -> list[ActionFieldOut]:
    fields: list[ActionFieldOut] = []
    for name, info in model.model_fields.items():
        fields.append(
            ActionFieldOut(
                name=name,
                required=info.is_required(),
                type=_annotation_kind(info.annotation),
            )
        )
    return fields


def _unit_preflight(params: BaseModel, ctx: ActionContext) -> None:
    assert isinstance(params, UnitParams)
    try:
        params.unit = normalize_unit_name(params.unit)
    except UnitValidationError as exc:
        raise ValidationFailedError(
            str(exc), fields=[{"field": "unit", "message": str(exc)}]
        ) from exc


def _require_sudo(params: BaseModel, ctx: ActionContext) -> None:
    if ctx.sudo_password is None:
        raise SudoPasswordMissingError()


def _service_preflight_factory(verb: str) -> Callable[[BaseModel, ActionContext], None]:
    def preflight(params: BaseModel, ctx: ActionContext) -> None:
        _unit_preflight(params, ctx)
        assert isinstance(params, UnitParams)
        unit = params.unit
        if len(unit) > MAX_UNIT_NAME_LENGTH:
            raise ValidationFailedError(
                "服务名过长", fields=[{"field": "unit", "message": "服务名过长"}]
            )
        ensure_action_allowed(verb, unit)
        if ctx.sudo_password is None:
            raise SudoPasswordMissingError("")

    return preflight


def _service_build_factory(
    verb: str, label: str
) -> Callable[[BaseModel, ActionContext], list[PlanStep]]:
    def build(params: BaseModel, ctx: ActionContext) -> list[PlanStep]:
        assert isinstance(params, UnitParams)
        unit = normalize_unit_name(params.unit)
        ensure_action_allowed(verb, unit)
        quoted = shlex.quote(unit)
        verify = "is-enabled" if verb in {"enable", "disable"} else "is-active"
        return [
            PlanStep(
                name="预检",
                command=f"systemctl show {quoted} --property=LoadState --no-pager",
                timeout_seconds=15.0,
            ),
            PlanStep(
                name=label,
                command=f"systemctl {verb} {quoted}",
                timeout_seconds=ctx.action_timeout_seconds,
                use_sudo=True,
            ),
            PlanStep(
                name="校验状态",
                command=f"systemctl {verify} {quoted}",
                timeout_seconds=10.0,
                allow_failure=True,
            ),
        ]

    return build


def _service_spec(
    action_id: str,
    verb: str,
    label: str,
    risk: str,
) -> ActionSpec:
    return ActionSpec(
        id=action_id,
        label=label,
        description=f"systemctl {verb} <unit>（含预检与状态校验）",
        risk=risk,
        params_model=UnitParams,
        requires_sudo=True,
        requires_confirmation=True,
        requires_idempotency_key=True,
        persist_snapshot=False,
        build=_service_build_factory(verb, label),
        preflight=_service_preflight_factory(verb),
    )


def _host_check_preflight(params: BaseModel, ctx: ActionContext) -> None:
    return None


def _host_check_build(params: BaseModel, ctx: ActionContext) -> list[PlanStep]:
    return [
        PlanStep(
            name="连接检查",
            command="printf 'labops-ok\\n'; uname -srm",
            timeout_seconds=min(ctx.action_timeout_seconds, 20.0),
        )
    ]


def _snapshot_preflight(params: BaseModel, ctx: ActionContext) -> None:
    return None


def _snapshot_build(params: BaseModel, ctx: ActionContext) -> list[PlanStep]:
    return [
        PlanStep(
            name="采集状态",
            command=build_probe_command("snapshot"),
            timeout_seconds=ctx.action_timeout_seconds,
            stdin_data=PROBE_SOURCE.encode("utf-8"),
        )
    ]


def _logs_preflight(params: BaseModel, ctx: ActionContext) -> None:
    assert isinstance(params, LogsParams)
    if params.unit is not None:
        if not params.unit.strip():
            params.unit = None
            return
        if len(params.unit) > MAX_UNIT_NAME_LENGTH:
            raise ValidationFailedError(
                "服务名过长", fields=[{"field": "unit", "message": "服务名过长"}]
            )
        try:
            params.unit = normalize_unit_name(params.unit)
        except UnitValidationError as exc:
            raise ValidationFailedError(
                str(exc), fields=[{"field": "unit", "message": str(exc)}]
            ) from exc
    if params.use_sudo and ctx.sudo_password is None:
        raise SudoPasswordMissingError("")


def _logs_build(params: BaseModel, ctx: ActionContext) -> list[PlanStep]:
    assert isinstance(params, LogsParams)
    return [
        PlanStep(
            name="读取日志",
            command=build_journalctl_command(params.unit, params.lines),
            timeout_seconds=min(ctx.action_timeout_seconds, 45.0),
            use_sudo=params.use_sudo,
        )
    ]


def _command_preflight(params: BaseModel, ctx: ActionContext) -> None:
    if not ctx.settings.allow_arbitrary_commands:
        raise ActionDisabledError("任意命令入口默认关闭；请由管理员显式开启后再使用")
    assert isinstance(params, CommandParams)
    if "\x00" in params.command:
        raise ValidationFailedError(
            "命令包含非法字符", fields=[{"field": "command", "message": "包含 NUL 字符"}]
        )
    if params.use_sudo and ctx.sudo_password is None:
        raise SudoPasswordMissingError("")


def _command_build(params: BaseModel, ctx: ActionContext) -> list[PlanStep]:
    assert isinstance(params, CommandParams)
    quoted = shlex.quote(params.command)
    return [
        PlanStep(
            name="Bash 语法检查",
            command=f"bash -n -c {quoted}",
            timeout_seconds=15.0,
        ),
        PlanStep(
            name="执行命令",
            command=f"bash -o pipefail -c {quoted}",
            timeout_seconds=ctx.action_timeout_seconds,
            use_sudo=params.use_sudo,
        ),
    ]


def build_registry(settings: Settings) -> ActionRegistry:
    return ActionRegistry(settings)


class ActionRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._specs: dict[str, ActionSpec] = {
            "host_check": ActionSpec(
                id="host_check",
                label="主机检查",
                description="验证 SSH 可达并读取内核信息",
                risk="low",
                params_model=EmptyParams,
                requires_sudo=False,
                requires_confirmation=False,
                requires_idempotency_key=False,
                persist_snapshot=False,
                build=_host_check_build,
                preflight=_host_check_preflight,
            ),
            "collect_snapshot": ActionSpec(
                id="collect_snapshot",
                label="采集状态",
                description="一次性探针采集系统、资源、磁盘、网络、GPU 与服务状态",
                risk="low",
                params_model=EmptyParams,
                requires_sudo=False,
                requires_confirmation=False,
                requires_idempotency_key=False,
                persist_snapshot=True,
                build=_snapshot_build,
                preflight=_snapshot_preflight,
            ),
            "query_logs": ActionSpec(
                id="query_logs",
                label="导出日志",
                description="journalctl 读取指定服务或全部日志（最多 2000 行）",
                risk="low",
                params_model=LogsParams,
                requires_sudo=False,
                requires_confirmation=False,
                requires_idempotency_key=False,
                persist_snapshot=False,
                build=_logs_build,
                preflight=_logs_preflight,
            ),
            "service_start": _service_spec("service_start", "start", "启动服务", "medium"),
            "service_stop": _service_spec("service_stop", "stop", "停止服务", "high"),
            "service_restart": _service_spec("service_restart", "restart", "重启服务", "medium"),
            "service_enable": _service_spec("service_enable", "enable", "启用开机启动", "medium"),
            "service_disable": _service_spec("service_disable", "disable", "禁用开机启动", "high"),
            "run_command": ActionSpec(
                id="run_command",
                label="受控命令",
                description="默认关闭；开启后执行 Bash 语法检查、风险确认与输出截断",
                risk="high",
                params_model=CommandParams,
                requires_sudo=False,
                requires_confirmation=True,
                requires_idempotency_key=True,
                persist_snapshot=False,
                build=_command_build,
                preflight=_command_preflight,
            ),
        }

    def get(self, action_id: str) -> ActionSpec:
        spec = self._specs.get(action_id)
        if spec is None:
            raise UnknownActionError(action_id)
        return spec

    def all(self) -> list[ActionSpec]:
        return sorted(self._specs.values(), key=lambda spec: spec.id)

    def describe(self) -> ActionsOut:
        return ActionsOut(actions=[spec.describe() for spec in self.all()])

    def validate(
        self,
        action_id: str,
        raw_params: dict[str, object] | None,
        *,
        sudo_password: str | None,
        sudo_available: bool = True,
    ) -> tuple[ActionSpec, BaseModel]:
        spec = self.get(action_id)
        if spec.requires_sudo and not sudo_available:
            raise SudoRequiredButUnavailableError()
        context = ActionContext(
            settings=self.settings,
            sudo_password=sudo_password,
            action_timeout_seconds=self.settings.action_timeout_seconds,
        )
        try:
            params = spec.params_model.model_validate(raw_params or {})
        except ValidationError as exc:
            fields = [
                {
                    "field": ".".join(str(part) for part in error.get("loc", ())),
                    "message": error.get("msg", "参数不合法"),
                }
                for error in exc.errors()
            ]
            raise ValidationFailedError("动作参数校验失败", fields=fields) from exc
        spec.preflight(params, context)
        return spec, params

    def build_plan(
        self,
        spec: ActionSpec,
        params: BaseModel,
        *,
        sudo_password: str | None,
        action_timeout_seconds: float | None = None,
    ) -> list[PlanStep]:
        context = ActionContext(
            settings=self.settings,
            sudo_password=sudo_password,
            action_timeout_seconds=action_timeout_seconds or self.settings.action_timeout_seconds,
        )
        return spec.build(params, context)


def is_unit_protected(unit: str) -> bool:
    return is_protected(unit)


__all__ = [
    "ActionContext",
    "ActionDisabledError",
    "ActionRegistry",
    "ActionSpec",
    "PlanStep",
    "ProtectedServiceError",
    "SudoPasswordMissingError",
    "SudoRequiredButUnavailableError",
    "UnknownActionError",
    "build_registry",
]
