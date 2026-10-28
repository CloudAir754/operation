"""远端探针、固定动作与诊断。"""

from lab_ops_panel.remote.actions import (
    ActionRegistry,
    ActionSpec,
    PlanStep,
    build_registry,
)
from lab_ops_panel.remote.diagnosis import diagnose_result
from lab_ops_panel.remote.parsing import (
    error_snapshot_data,
    normalize_services_section,
    normalize_snapshot_sections,
    snapshot_status,
)
from lab_ops_panel.remote.probe import ProbeError, build_probe_command, run_probe
from lab_ops_panel.remote.services import (
    ProtectedServiceError,
    ensure_action_allowed,
    is_protected,
    normalize_unit_name,
)
from lab_ops_panel.remote.targets import target_from_server

__all__ = [
    "ActionRegistry",
    "ActionSpec",
    "PlanStep",
    "ProbeError",
    "ProtectedServiceError",
    "build_probe_command",
    "build_registry",
    "diagnose_result",
    "ensure_action_allowed",
    "error_snapshot_data",
    "is_protected",
    "normalize_services_section",
    "normalize_snapshot_sections",
    "normalize_unit_name",
    "run_probe",
    "snapshot_status",
    "target_from_server",
]
