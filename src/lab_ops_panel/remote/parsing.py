"""探针输出归一化与分区状态处理。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from lab_ops_panel.remote.services import is_protected
from lab_ops_panel.schemas import (
    CpuSectionOut,
    DiskSectionOut,
    GpuSectionOut,
    MemorySectionOut,
    NetworkSectionOut,
    ServiceInfoOut,
    ServiceSectionOut,
    SnapshotDataOut,
    SystemSectionOut,
)

SECTION_MODELS = {
    "system": SystemSectionOut,
    "cpu": CpuSectionOut,
    "memory": MemorySectionOut,
    "disks": DiskSectionOut,
    "network": NetworkSectionOut,
    "gpu": GpuSectionOut,
    "services": ServiceSectionOut,
}


def _first_validation_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {first.get('msg', '格式错误')}" if location else str(first.get("msg"))


def _apply_protection(section: ServiceSectionOut) -> ServiceSectionOut:
    if section.data is None:
        return section
    units = [
        unit.model_copy(update={"protected": is_protected(unit.name)})
        for unit in section.data.units
    ]
    return section.model_copy(update={"data": section.data.model_copy(update={"units": units})})


def normalize_snapshot_sections(raw: Any, *, error: str | None = None) -> SnapshotDataOut:
    """逐分区校验；单分区损坏不影响其他分区。"""

    source = raw if isinstance(raw, dict) else {}
    resolved: dict[str, Any] = {}
    for name, model in SECTION_MODELS.items():
        value = source.get(name)
        if not isinstance(value, dict):
            message = error or f"探针未返回 {name} 分区"
            resolved[name] = model(status="error", error=message[:300], data=None)
            continue
        try:
            resolved[name] = model.model_validate(value)
        except ValidationError as exc:
            resolved[name] = model(
                status="error",
                error=f"{name} 分区解析失败：{_first_validation_message(exc)}"[:300],
                data=None,
            )
    resolved["services"] = _apply_protection(resolved["services"])
    return SnapshotDataOut(**resolved)


def error_snapshot_data(message: str) -> SnapshotDataOut:
    sections = {
        name: model(status="error", error=message[:300], data=None)
        for name, model in SECTION_MODELS.items()
    }
    return SnapshotDataOut(**sections)


def snapshot_status(data: SnapshotDataOut) -> str:
    statuses = [
        data.system.status,
        data.cpu.status,
        data.memory.status,
        data.disks.status,
        data.network.status,
        data.gpu.status,
        data.services.status,
    ]
    if all(status == "ok" for status in statuses):
        return "ok"
    if all(status == "error" for status in statuses):
        return "failed"
    return "partial"


def normalize_services_section(raw: Any) -> ServiceSectionOut:
    if not isinstance(raw, dict):
        return ServiceSectionOut(status="error", error="探针未返回 services 分区", data=None)
    try:
        section = ServiceSectionOut.model_validate(raw)
    except ValidationError as exc:
        return ServiceSectionOut(
            status="error",
            error=f"services 分区解析失败：{_first_validation_message(exc)}"[:300],
            data=None,
        )
    return _apply_protection(section)


def services_section_from_snapshot(data: SnapshotDataOut) -> ServiceSectionOut:
    return data.services


def empty_service_info() -> ServiceInfoOut:
    return ServiceInfoOut(units=[])
