"""GPU 监控数据归一化。

只消费结构化字段：绝不把 ``nvidia-smi`` 原始文本透传给前端。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lab_ops_panel.schemas_observability import (
    CapabilityStatus,
    GpuCapabilitiesOut,
    GpuDeviceDetailOut,
    GpuMonitorOut,
    GpuProcessesStatus,
    GpuProcessOut,
    GpuSampleOut,
)

MAX_DEVICES = 32
MAX_PROCESSES = 200
MAX_TEXT = 200
MAX_COMMAND = 512


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 2)


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    if not text:
        return None
    return text[:limit]


def _kind(value: Any, allowed: set[str], default: str) -> str:
    return value if value in allowed else default


def _normalize_device(raw: Any) -> GpuDeviceDetailOut | None:
    if not isinstance(raw, dict):
        return None
    index = _int(raw.get("index"))
    if index is None or index < 0:
        return None
    mig_devices = [
        text for text in (_text(item, MAX_TEXT) for item in (raw.get("migDevices") or [])) if text
    ][:16]
    return GpuDeviceDetailOut(
        index=index,
        uuid=_text(raw.get("uuid")),
        pci_bus_id=_text(raw.get("pciBusId")),
        name=_text(raw.get("name")),
        driver_version=_text(raw.get("driverVersion")),
        memory_total_mb=_number(raw.get("memoryTotalMb")),
        memory_used_mb=_number(raw.get("memoryUsedMb")),
        utilization_gpu_percent=_number(raw.get("utilizationGpuPercent")),
        utilization_memory_percent=_number(raw.get("utilizationMemoryPercent")),
        encoder_percent=_number(raw.get("encoderPercent")),
        decoder_percent=_number(raw.get("decoderPercent")),
        temperature_c=_number(raw.get("temperatureC")),
        power_draw_w=_number(raw.get("powerDrawW")),
        power_limit_w=_number(raw.get("powerLimitW")),
        clock_graphics_mhz=_number(raw.get("clockGraphicsMhz")),
        clock_memory_mhz=_number(raw.get("clockMemoryMhz")),
        mig_mode=_text(raw.get("migMode")),
        compute_mode=_text(raw.get("computeMode")),
        mig_devices=mig_devices,
    )


def _normalize_process(raw: Any) -> GpuProcessOut | None:
    if not isinstance(raw, dict):
        return None
    pid = _int(raw.get("pid"))
    if pid is None or pid <= 0:
        return None
    started_at = raw.get("startedAtEpoch")
    started = None
    if isinstance(started_at, (int, float)) and started_at > 0:
        started = datetime.fromtimestamp(float(started_at), tz=UTC)
    return GpuProcessOut(
        pid=pid,
        user=_text(raw.get("user"), 64),
        name=_text(raw.get("name")),
        command=_text(raw.get("command"), MAX_COMMAND),
        memory_used_mb=_number(raw.get("memoryUsedMb")),
        utilization_percent=_number(raw.get("utilizationPercent")),
        gpu_uuid=_text(raw.get("gpuUuid")),
        gpu_index=_int(raw.get("gpuIndex")),
        program_path=_text(raw.get("programPath"), MAX_COMMAND),
        cwd=_text(raw.get("cwd"), MAX_COMMAND),
        started_at=started,
        stale=bool(raw.get("stale", False)),
        error=_text(raw.get("error"), 160),
    )


def normalize_gpu_monitor(
    section: dict[str, Any] | None,
    *,
    server_id: int,
    collected_at: datetime,
) -> GpuMonitorOut:
    raw = section if isinstance(section, dict) else {}
    status = (
        raw.get("status")
        if raw.get("status")
        in {
            "ok",
            "permission_denied",
            "unavailable",
            "error",
        }
        else "error"
    )
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    capabilities_raw = (
        data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
    )
    process_capability: CapabilityStatus = _kind(
        capabilities_raw.get("processes"), {"ok", "partial", "unavailable"}, "unavailable"
    )
    processes: list[GpuProcessOut] = []
    seen: set[tuple[int, str | None]] = set()
    for item in data.get("processes") or []:
        process = _normalize_process(item)
        if process is None:
            continue
        key = (process.pid, process.gpu_uuid)
        if key in seen:
            continue
        seen.add(key)
        processes.append(process)
    processes.sort(
        key=lambda item: (item.memory_used_mb is None, -(item.memory_used_mb or 0), item.pid)
    )
    processes = processes[:MAX_PROCESSES]
    devices: list[GpuDeviceDetailOut] = []
    seen_indexes: set[int] = set()
    for item in data.get("devices") or []:
        device = _normalize_device(item)
        if device is None or device.index in seen_indexes:
            continue
        seen_indexes.add(device.index)
        devices.append(device)
    devices.sort(key=lambda item: item.index)
    devices = devices[:MAX_DEVICES]

    processes_status_raw = data.get("processesStatus") or process_capability
    processes_status: GpuProcessesStatus = _kind(
        processes_status_raw, {"ok", "partial", "unavailable"}, "unavailable"
    )
    if status != "ok":
        processes_status = "unavailable"
    elif any(process.error or process.stale for process in processes):
        processes_status = "partial"
    elif process_capability == "unavailable":
        processes_status = "unavailable"

    notes = [
        text
        for text in (_text(note, 240) for note in (data.get("notes") or raw.get("notes") or []))
        if text
    ][:8]

    return GpuMonitorOut(
        server_id=server_id,
        collected_at=collected_at,
        status=status,  # type: ignore[arg-type]
        error=_text(raw.get("error"), 300),
        requires_sudo=bool(raw.get("requiresSudo", False)),
        capabilities=GpuCapabilitiesOut(
            nvml=bool(capabilities_raw.get("nvml", status == "ok")),
            processes=process_capability,
            mig=bool(capabilities_raw.get("mig", False)),
            per_process_utilization=bool(capabilities_raw.get("perProcessUtilization", False)),
        ),
        devices=devices,
        processes=processes,
        processes_status=processes_status,
        processes_error=_text(data.get("processesError") or raw.get("error"), 300),
        notes=notes,
    )


def gpu_samples(payload: dict[str, Any] | None) -> list[GpuSampleOut]:
    """把任意一段 GPU 数据映射成曲线点，用于指标接口复用。"""

    data = payload if isinstance(payload, dict) else {}
    samples: list[GpuSampleOut] = []
    for item in data.get("devices") or []:
        if not isinstance(item, dict):
            continue
        index = _int(item.get("index"))
        if index is None:
            continue
        samples.append(
            GpuSampleOut(
                index=index,
                uuid=_text(item.get("uuid")),
                name=_text(item.get("name")),
                utilization_percent=_number(item.get("utilizationPercent")),
                memory_used_mb=_number(item.get("memoryUsedMb")),
                memory_total_mb=_number(item.get("memoryTotalMb")),
                encoder_percent=_number(item.get("encoderPercent")),
                decoder_percent=_number(item.get("decoderPercent")),
                temperature_c=_number(item.get("temperatureC")),
                power_draw_w=_number(item.get("powerDrawW")),
            )
        )
    return samples
