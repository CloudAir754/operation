"""systemd 服务保护与固定动作白名单测试。"""

from __future__ import annotations

import pytest

from lab_ops_panel.config import Settings
from lab_ops_panel.errors import ApiError, ValidationFailedError
from lab_ops_panel.remote.actions import (
    ActionDisabledError,
    ActionRegistry,
    SudoPasswordMissingError,
    UnknownActionError,
    build_registry,
)
from lab_ops_panel.remote.services import (
    ProtectedServiceError,
    UnitValidationError,
    ensure_action_allowed,
    is_protected,
    normalize_unit_name,
    unit_base_name,
)


def test_normalize_unit_name_adds_service_suffix() -> None:
    assert normalize_unit_name("nginx") == "nginx.service"
    assert normalize_unit_name(" nginx.service ") == "nginx.service"
    assert normalize_unit_name("nginx@blue.service") == "nginx@blue.service"


@pytest.mark.parametrize("value", ["", "   ", "-bad.service", "bad name", "bad;rm -rf", "a" * 250])
def test_invalid_unit_names_are_rejected(value: str) -> None:
    with pytest.raises(UnitValidationError):
        normalize_unit_name(value)


def test_unit_base_name_strips_known_suffixes() -> None:
    assert unit_base_name("ssh.service") == "ssh"
    assert unit_base_name("apt-daily.timer") == "apt-daily"
    assert unit_base_name("custom") == "custom"


@pytest.mark.parametrize(
    "unit",
    [
        "ssh.service",
        "sshd.service",
        "systemd-logind.service",
        "dbus.service",
        "NetworkManager.service",
        "networking.service",
        "ufw.service",
    ],
)
def test_protected_services_are_detected(unit: str) -> None:
    assert is_protected(unit) is True


@pytest.mark.parametrize("unit", ["nginx.service", "docker.service", "jupyter.service"])
def test_regular_services_are_not_protected(unit: str) -> None:
    assert is_protected(unit) is False


@pytest.mark.parametrize("verb", ["stop", "disable", "mask"])
def test_protected_service_blocks_dangerous_verbs(verb: str) -> None:
    with pytest.raises(ProtectedServiceError) as excinfo:
        ensure_action_allowed(verb, "ssh.service")

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "protected_service"


@pytest.mark.parametrize("verb", ["start", "restart", "enable"])
def test_protected_service_allows_recovery_verbs(verb: str) -> None:
    ensure_action_allowed(verb, "ssh.service")


@pytest.fixture
def registry() -> ActionRegistry:
    return build_registry(Settings(app_data_dir=".", ssh_config_path="."))


def test_registry_lists_all_fixed_actions(registry: ActionRegistry) -> None:
    ids = [spec.id for spec in registry.all()]

    assert set(ids) == {
        "host_check",
        "collect_snapshot",
        "query_logs",
        "service_start",
        "service_stop",
        "service_restart",
        "service_enable",
        "service_disable",
        "run_command",
    }
    described = registry.describe().actions
    assert all(action.fields is not None for action in described)
    stop = next(action for action in described if action.id == "service_stop")
    assert stop.risk == "high"
    assert stop.requires_idempotency_key is True


def test_unknown_action_is_rejected(registry: ActionRegistry) -> None:
    with pytest.raises(UnknownActionError) as excinfo:
        registry.validate("rm_rf", {}, sudo_password="pw")

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "unknown_action"


def test_unknown_params_are_rejected(registry: ActionRegistry) -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        registry.validate("service_restart", {"unit": "nginx", "extra": True}, sudo_password="pw")

    assert excinfo.value.status_code == 422
    assert excinfo.value.fields


def test_service_action_requires_sudo_password(registry: ActionRegistry) -> None:
    with pytest.raises(SudoPasswordMissingError) as excinfo:
        registry.validate("service_restart", {"unit": "nginx"}, sudo_password=None)

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "sudo_password_missing"


def test_protected_service_is_rejected_during_validation(registry: ActionRegistry) -> None:
    with pytest.raises(ProtectedServiceError):
        registry.validate("service_stop", {"unit": "ssh"}, sudo_password="pw")


def test_service_unit_is_normalized_to_service_suffix(registry: ActionRegistry) -> None:
    spec, params = registry.validate("service_start", {"unit": "nginx"}, sudo_password="pw")

    assert params.unit == "nginx.service"  # type: ignore[attr-defined]
    steps = registry.build_plan(spec, params, sudo_password="pw")
    assert steps[1].command == "systemctl start nginx.service"
    assert steps[1].use_sudo is True
    assert steps[0].command.startswith("systemctl show nginx.service")
    assert steps[2].allow_failure is True


def test_run_command_is_disabled_by_default(registry: ActionRegistry) -> None:
    with pytest.raises(ActionDisabledError) as excinfo:
        registry.validate("run_command", {"command": "uptime"}, sudo_password=None)

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "action_disabled"


def test_run_command_builds_syntax_check_when_enabled(tmp_path) -> None:
    settings = Settings(
        app_data_dir=tmp_path / "data",
        ssh_config_path=tmp_path / "ssh_config",
        allow_arbitrary_commands=True,
    )
    registry = build_registry(settings)

    spec, params = registry.validate(
        "run_command", {"command": "uptime && df -h"}, sudo_password=None
    )
    steps = registry.build_plan(spec, params, sudo_password=None)

    assert [step.name for step in steps] == ["Bash 语法检查", "执行命令"]
    assert steps[0].command.startswith("bash -n -c")
    assert "uptime && df -h" in steps[1].command
    assert spec.risk == "high"
    assert spec.requires_idempotency_key is True


def test_logs_params_bounds_are_enforced(registry: ActionRegistry) -> None:
    with pytest.raises(ValidationFailedError):
        registry.validate("query_logs", {"lines": 5000}, sudo_password=None)

    spec, params = registry.validate(
        "query_logs", {"unit": "nginx", "lines": 50}, sudo_password=None
    )
    steps = registry.build_plan(spec, params, sudo_password=None)
    assert "journalctl -u nginx.service -n 50" in steps[0].command


def test_collect_snapshot_plan_uses_probe(registry: ActionRegistry) -> None:
    spec, params = registry.validate("collect_snapshot", {}, sudo_password=None)
    steps = registry.build_plan(spec, params, sudo_password=None)

    assert spec.persist_snapshot is True
    assert "base64" not in steps[0].command
    assert steps[0].command.endswith("snapshot")
    assert '"$PY" -' in steps[0].command


def test_action_errors_expose_api_payload(registry: ActionRegistry) -> None:
    error = ApiError(418, "teapot", "测试错误")

    payload = error.to_payload()

    assert payload == {"detail": {"code": "teapot", "message": "测试错误"}}
