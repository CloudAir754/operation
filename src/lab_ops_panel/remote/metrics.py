"""资源指标：累计计数差分、设备归类与分区状态。

约束：
- 未知值必须是 ``None``，绝不写成 0。
- 计数器回绕、时间倒流或间隔过短时不编造速率，返回 ``reset`` 并重建基线。
- 磁盘速率按 512 字节扇区换算，并按设备/分区区分。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lab_ops_panel.schemas_observability import (
    CpuMetricsOut,
    CpuSectionOut,
    DiskDeviceMetricsOut,
    DiskMetricsOut,
    DiskMountMetricsOut,
    DiskSectionOut,
    GpuMetricsOut,
    GpuMetricsSectionOut,
    GpuSampleOut,
    MemoryMetricsOut,
    MemorySectionOut,
    MetricPointOut,
    MetricsSectionsOut,
    NetworkInterfaceMetricsOut,
    NetworkMetricsOut,
    NetworkSectionOut,
)

SECTOR_SIZE_BYTES = 512
PAGE_SIZE_BYTES = 4096
MIN_INTERVAL_SECONDS = 0.2
MAX_INTERVAL_SECONDS = 3600.0
UNKNOWN_KINDS = {"disk", "partition", "virtual", "unknown"}
NETWORK_KINDS = {
    "physical",
    "bond",
    "bridge",
    "vlan",
    "virtual",
    "loopback",
    "unknown",
}


@dataclass(frozen=True)
class Rate:
    """一次差分结果；``reset`` 表示基线重建而非真实零速率。"""

    value: float | None
    reset: bool = False
    reason: str | None = None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def counter_delta(previous: Any, current: Any, interval_seconds: float) -> Rate:
    """把累计计数差值换算为速率。"""

    previous_value = _number(previous)
    current_value = _number(current)
    if previous_value is None or current_value is None:
        return Rate(None, True, "缺少上一次采样")
    if interval_seconds < MIN_INTERVAL_SECONDS:
        return Rate(None, True, "采样间隔过短")
    if interval_seconds > MAX_INTERVAL_SECONDS:
        return Rate(None, True, "采样间隔过长")
    if current_value < previous_value:
        return Rate(None, True, "计数器回绕")
    return Rate((current_value - previous_value) / interval_seconds)


def counter_rate(previous: Any, current: Any, interval_seconds: float) -> Rate:
    """向后兼容的别名。"""

    return counter_delta(previous, current, interval_seconds)


def _first_reset(*rates: Rate) -> str | None:
    for rate in rates:
        if rate.reset:
            return rate.reason or "基线重建"
    return None


def _percent(part: float | None, total: float | None) -> float | None:
    if part is None or total is None or total <= 0:
        return None
    return round(max(0.0, min(100.0, part / total * 100.0)), 1)


def _section_status(payload: dict[str, Any], *, requires_data: bool) -> tuple[str, str | None]:
    status = payload.get("status")
    if status not in {"ok", "permission_denied", "unavailable", "error"}:
        status = "error"
    error = payload.get("error")
    if status == "ok" and requires_data and not payload.get("data"):
        return "error", error or "探针未返回数据"
    return status, error


def _cpu_metrics(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    interval_seconds: float,
) -> tuple[CpuMetricsOut, str | None]:
    fields = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    old = (previous or {}).get("cpu") or {}
    new = current.get("cpu") or {}
    if not new:
        return CpuMetricsOut(), "缺少 /proc/stat 数据"

    def total(row: dict[str, Any]) -> float | None:
        values = [_number(row.get(field)) for field in fields]
        if any(value is None for value in values):
            return None
        return float(sum(values))  # type: ignore[arg-type]

    total_old = total(old)
    total_new = total(new)
    reset_reason = _first_reset(counter_delta(total_old, total_new, interval_seconds))
    load1 = _number(new.get("load1"))
    load5 = _number(new.get("load5"))
    load15 = _number(new.get("load15"))
    logical = _int(new.get("logicalCpus"))
    if reset_reason is not None or total_old is None or total_new is None:
        return (
            CpuMetricsOut(
                logical_cpus=logical,
                load1=load1,
                load5=load5,
                load15=load15,
            ),
            reset_reason,
        )
    total_delta = total_new - total_old
    cpu_reset = _first_reset(
        counter_delta(old.get("idle"), new.get("idle"), interval_seconds),
        counter_delta(old.get("user"), new.get("user"), interval_seconds),
        counter_delta(old.get("system"), new.get("system"), interval_seconds),
        counter_delta(old.get("iowait"), new.get("iowait"), interval_seconds),
        counter_delta(old.get("steal"), new.get("steal"), interval_seconds),
    )
    if cpu_reset is not None:
        return (
            CpuMetricsOut(logical_cpus=logical, load1=load1, load5=load5, load15=load15),
            cpu_reset,
        )

    def delta(field: str) -> float | None:
        old_value = _number(old.get(field))
        new_value = _number(new.get(field))
        if old_value is None or new_value is None:
            return None
        return new_value - old_value

    idle = delta("idle")
    idle_percent = _percent(idle, total_delta)
    user_percent = _percent((delta("user") or 0) + (delta("nice") or 0), total_delta)
    system_percent = _percent(
        (delta("system") or 0) + (delta("irq") or 0) + (delta("softirq") or 0),
        total_delta,
    )
    return (
        CpuMetricsOut(
            total_percent=None if idle_percent is None else round(100.0 - idle_percent, 1),
            user_percent=user_percent,
            system_percent=system_percent,
            iowait_percent=_percent(delta("iowait"), total_delta),
            steal_percent=_percent(delta("steal"), total_delta),
            idle_percent=idle_percent,
            logical_cpus=logical,
            load1=load1,
            load5=load5,
            load15=load15,
        ),
        None,
    )


def _memory_metrics(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    interval_seconds: float,
) -> tuple[MemoryMetricsOut, str | None]:
    memory = current.get("memory") or {}
    if not memory:
        return MemoryMetricsOut(), "缺少 /proc/meminfo 数据"
    total = _int(memory.get("MemTotal"))
    available = _int(memory.get("MemAvailable"))
    if available is None:
        available = _int(memory.get("MemFree"))
    used = None if total is None or available is None else max(0, total - available)
    percent = _percent(used, total) if used is not None else None
    buffers = _int(memory.get("Buffers"))
    cached = _int(memory.get("Cached"))
    reclaimable = _int(memory.get("SReclaimable")) or 0
    if cached is not None:
        cached += reclaimable
    commit_limit = _int(memory.get("CommitLimit"))
    committed = _int(memory.get("Committed_AS"))
    commit = None
    if committed is not None:
        commit = committed if commit_limit is None else max(0, commit_limit - committed)
    swap_total = _int(memory.get("SwapTotal"))
    swap_free = _int(memory.get("SwapFree"))
    swap_used = None if swap_total is None or swap_free is None else max(0, swap_total - swap_free)
    vm_old = (previous or {}).get("vmstat") or {}
    vm_new = current.get("vmstat") or {}
    swap_in = counter_delta(vm_old.get("pswpin"), vm_new.get("pswpin"), interval_seconds)
    swap_out = counter_delta(vm_old.get("pswpout"), vm_new.get("pswpout"), interval_seconds)
    reset_reason = _first_reset(swap_in, swap_out)
    return (
        MemoryMetricsOut(
            total_bytes=total,
            available_bytes=available,
            used_bytes=used,
            used_percent=percent,
            buffers_bytes=buffers,
            cached_bytes=cached,
            commit_bytes=commit,
            swap_total_bytes=swap_total,
            swap_used_bytes=swap_used,
            swap_free_bytes=swap_free,
            swap_in_per_second=(
                None if swap_in.value is None else round(swap_in.value * PAGE_SIZE_BYTES, 1)
            ),
            swap_out_per_second=(
                None if swap_out.value is None else round(swap_out.value * PAGE_SIZE_BYTES, 1)
            ),
        ),
        reset_reason,
    )


def _disk_metrics(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    interval_seconds: float,
) -> tuple[DiskMetricsOut, str | None]:
    old = (previous or {}).get("disk") or {}
    new = current.get("disk") or {}
    meta = current.get("diskMeta") or {}
    reset_reasons: list[str] = []
    devices: list[DiskDeviceMetricsOut] = []
    for name, row in sorted(new.items()):
        if not isinstance(row, dict):
            continue
        prior = old.get(name) or {}
        sectors_read = counter_delta(
            prior.get("sectorsRead"), row.get("sectorsRead"), interval_seconds
        )
        sectors_written = counter_delta(
            prior.get("sectorsWritten"), row.get("sectorsWritten"), interval_seconds
        )
        reads = counter_delta(
            prior.get("readsCompleted"), row.get("readsCompleted"), interval_seconds
        )
        writes = counter_delta(
            prior.get("writesCompleted"), row.get("writesCompleted"), interval_seconds
        )
        io_ms = counter_delta(prior.get("ioMs"), row.get("ioMs"), interval_seconds)
        read_ms = counter_delta(prior.get("msReading"), row.get("msReading"), interval_seconds)
        write_ms = counter_delta(prior.get("msWriting"), row.get("msWriting"), interval_seconds)
        reset_reason = _first_reset(
            sectors_read, sectors_written, reads, writes, io_ms, read_ms, write_ms
        )
        if reset_reason:
            reset_reasons.append(f"{name}: {reset_reason}")
        device_meta = meta.get(name) if isinstance(meta.get(name), dict) else {}
        kind = device_meta.get("kind")
        if kind not in UNKNOWN_KINDS:
            kind = "unknown"
        await_ms = None
        if reads.value is not None and writes.value is not None and read_ms.value is not None:
            ops = reads.value + writes.value
            if ops > 0:
                await_ms = round((read_ms.value + (write_ms.value or 0)) / ops, 2)
        util = None
        if io_ms.value is not None:
            util = round(min(100.0, io_ms.value / 1000.0 * 100.0), 1)
        devices.append(
            DiskDeviceMetricsOut(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                parent=device_meta.get("parent"),
                model=device_meta.get("model"),
                size_bytes=_int(device_meta.get("sizeBytes")),
                hidden=bool(device_meta.get("hidden", False)),
                read_bytes_per_second=(
                    None
                    if sectors_read.value is None
                    else round(sectors_read.value * SECTOR_SIZE_BYTES, 1)
                ),
                write_bytes_per_second=(
                    None
                    if sectors_written.value is None
                    else round(sectors_written.value * SECTOR_SIZE_BYTES, 1)
                ),
                read_iops=reads.value,
                write_iops=writes.value,
                await_ms=await_ms,
                util_percent=util,
                reset=reset_reason is not None,
            )
        )
    rate_by_name = {
        device.name: (device.read_bytes_per_second, device.write_bytes_per_second)
        for device in devices
    }
    mounts: list[DiskMountMetricsOut] = []
    for mount in current.get("mounts") or []:
        if not isinstance(mount, dict):
            continue
        mountpoint = str(mount.get("mount") or "")
        if not mountpoint:
            continue
        device_name = mount.get("deviceName")
        read_rate, write_rate = rate_by_name.get(device_name, (None, None))
        mounts.append(
            DiskMountMetricsOut(
                mount=mountpoint,
                device=mount.get("device"),
                filesystem=mount.get("filesystem"),
                device_name=device_name,
                read_bytes_per_second=read_rate,
                write_bytes_per_second=write_rate,
            )
        )
    reason = "; ".join(reset_reasons[:3]) if reset_reasons else None
    return DiskMetricsOut(devices=devices, mounts=mounts), reason


def _network_metrics(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    interval_seconds: float,
) -> tuple[NetworkMetricsOut, str | None]:
    old = (previous or {}).get("network") or {}
    new = current.get("network") or {}
    meta = current.get("networkMeta") or {}
    reset_reasons: list[str] = []
    interfaces: list[NetworkInterfaceMetricsOut] = []
    for name, row in sorted(new.items()):
        if not isinstance(row, dict):
            continue
        prior = old.get(name) or {}
        rx = counter_delta(prior.get("rxBytes"), row.get("rxBytes"), interval_seconds)
        tx = counter_delta(prior.get("txBytes"), row.get("txBytes"), interval_seconds)
        rx_packets = counter_delta(prior.get("rxPackets"), row.get("rxPackets"), interval_seconds)
        tx_packets = counter_delta(prior.get("txPackets"), row.get("txPackets"), interval_seconds)
        reset_reason = _first_reset(rx, tx, rx_packets, tx_packets)
        if reset_reason:
            reset_reasons.append(f"{name}: {reset_reason}")
        interface_meta = meta.get(name) if isinstance(meta.get(name), dict) else {}
        kind = interface_meta.get("kind")
        if kind not in NETWORK_KINDS:
            kind = "unknown"
        interfaces.append(
            NetworkInterfaceMetricsOut(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                state=interface_meta.get("state"),
                speed_mbps=_int(interface_meta.get("speedMbps")),
                addresses=list(interface_meta.get("addresses") or []),
                rx_bytes_per_second=rx.value,
                tx_bytes_per_second=tx.value,
                rx_packets_per_second=rx_packets.value,
                tx_packets_per_second=tx_packets.value,
                rx_errors=_int(row.get("rxErrors")),
                tx_errors=_int(row.get("txErrors")),
                rx_drops=_int(row.get("rxDrops")),
                tx_drops=_int(row.get("txDrops")),
                reset=reset_reason is not None,
            )
        )
    reason = "; ".join(reset_reasons[:3]) if reset_reasons else None
    return (
        NetworkMetricsOut(
            default_interface=current.get("defaultInterface"),
            interfaces=interfaces,
        ),
        reason,
    )


def _gpu_sample(raw: dict[str, Any] | None) -> GpuMetricsOut:
    gpu = raw or {}
    devices = []
    for device in gpu.get("devices") or []:
        if not isinstance(device, dict):
            continue
        index = _int(device.get("index"))
        if index is None:
            continue
        devices.append(
            GpuSampleOut(
                index=index,
                uuid=device.get("uuid"),
                name=device.get("name"),
                utilization_percent=_number(device.get("utilizationPercent")),
                memory_used_mb=_number(device.get("memoryUsedMb")),
                memory_total_mb=_number(device.get("memoryTotalMb")),
                encoder_percent=_number(device.get("encoderPercent")),
                decoder_percent=_number(device.get("decoderPercent")),
                temperature_c=_number(device.get("temperatureC")),
                power_draw_w=_number(device.get("powerDrawW")),
            )
        )
    return GpuMetricsOut(devices=devices)


def derive_metrics(
    *,
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    interval_ms: int,
    collected_at: datetime,
    requires_sudo: bool = False,
) -> tuple[MetricsSectionsOut, str | None]:
    """由两次累计计数快照推导当前指标与重置原因。"""

    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=UTC)
    interval_seconds = max(0.0, interval_ms / 1000.0)
    cpu, cpu_reset = _cpu_metrics(previous, current, interval_seconds)
    memory, memory_reset = _memory_metrics(previous, current, interval_seconds)
    disk, disk_reset = _disk_metrics(previous, current, interval_seconds)
    network, network_reset = _network_metrics(previous, current, interval_seconds)
    gpu_status = current.get("gpuStatus") or "unavailable"
    gpu = _gpu_sample(current.get("gpu"))
    sections = MetricsSectionsOut(
        cpu=CpuSectionOut(
            status="ok"
            if cpu.logical_cpus is not None or cpu.total_percent is not None
            else "error",
            error=(
                None
                if cpu.logical_cpus is not None or cpu.total_percent is not None
                else "缺少 /proc/stat 数据"
            ),
            requires_sudo=requires_sudo,
            collected_at=collected_at,
            data=cpu,
        ),
        memory=MemorySectionOut(
            status="ok" if memory.total_bytes is not None else "error",
            error=None if memory.total_bytes is not None else "缺少 /proc/meminfo 数据",
            requires_sudo=requires_sudo,
            collected_at=collected_at,
            data=memory,
        ),
        disk=DiskSectionOut(
            status="ok" if disk.devices else "unavailable",
            error=None,
            requires_sudo=requires_sudo,
            collected_at=collected_at,
            data=disk if disk.devices else None,
        ),
        network=NetworkSectionOut(
            status="ok" if network.interfaces else "unavailable",
            error=None,
            requires_sudo=requires_sudo,
            collected_at=collected_at,
            data=network if network.interfaces else None,
        ),
        gpu=GpuMetricsSectionOut(
            status=gpu_status
            if gpu_status in {"ok", "unavailable", "permission_denied", "error"}
            else "error",
            error=current.get("gpuError"),
            requires_sudo=requires_sudo,
            collected_at=collected_at,
            data=gpu if gpu.devices else None,
        ),
    )
    reset_reason = _first_reset(
        Rate(None, cpu_reset is not None, cpu_reset),
        Rate(None, memory_reset is not None, memory_reset),
        Rate(None, disk_reset is not None, disk_reset),
        Rate(None, network_reset is not None, network_reset),
    )
    return sections, reset_reason


def build_point(
    sections: MetricsSectionsOut,
    *,
    collected_at: datetime,
    received_at: datetime,
    interval_ms: int,
    reset_reason: str | None,
) -> MetricPointOut:
    cpu = sections.cpu.data
    memory = sections.memory.data
    disk = {
        device.name: {
            "readBytesPerSecond": device.read_bytes_per_second,
            "writeBytesPerSecond": device.write_bytes_per_second,
            "readIops": device.read_iops,
            "writeIops": device.write_iops,
            "reset": device.reset,
        }
        for device in (sections.disk.data.devices if sections.disk.data else [])
        if not device.hidden
    }
    network = {
        interface.name: {
            "rxBytesPerSecond": interface.rx_bytes_per_second,
            "txBytesPerSecond": interface.tx_bytes_per_second,
            "rxPacketsPerSecond": interface.rx_packets_per_second,
            "txPacketsPerSecond": interface.tx_packets_per_second,
            "reset": interface.reset,
        }
        for interface in (sections.network.data.interfaces if sections.network.data else [])
    }
    swap_percent = None
    if memory and memory.swap_total_bytes and memory.swap_used_bytes is not None:
        swap_percent = round(memory.swap_used_bytes / memory.swap_total_bytes * 100.0, 1)
    return MetricPointOut(
        collected_at=collected_at,
        received_at=received_at,
        sample_interval_ms=interval_ms,
        reset=reset_reason is not None,
        reset_reason=reset_reason,
        cpu_percent=cpu.total_percent if cpu else None,
        user_percent=cpu.user_percent if cpu else None,
        system_percent=cpu.system_percent if cpu else None,
        iowait_percent=cpu.iowait_percent if cpu else None,
        memory_used_percent=memory.used_percent if memory else None,
        swap_used_percent=swap_percent,
        disk=disk,
        network=network,
        gpu=sections.gpu.data.devices if sections.gpu.data else [],
    )


def summarize_status(sections: MetricsSectionsOut) -> tuple[str, str | None]:
    """整体状态：CPU/内存决定主状态，其余分区只影响自身。"""

    core = (sections.cpu, sections.memory)
    for status in ("permission_denied", "error"):
        for section in core:
            if section.status == status:
                return status, section.error or "核心指标不可用"
    if all(section.status != "ok" for section in core):
        return ("unavailable", sections.cpu.error or sections.memory.error or "核心指标不可用")
    return "ok", None


def empty_sections(collected_at: datetime, *, status: str, error: str | None) -> MetricsSectionsOut:
    return MetricsSectionsOut(
        cpu=CpuSectionOut(status=status, error=error, collected_at=collected_at),  # type: ignore[arg-type]
        memory=MemorySectionOut(status=status, error=error, collected_at=collected_at),  # type: ignore[arg-type]
        disk=DiskSectionOut(status=status, error=error, collected_at=collected_at),  # type: ignore[arg-type]
        network=NetworkSectionOut(status=status, error=error, collected_at=collected_at),  # type: ignore[arg-type]
        gpu=GpuMetricsSectionOut(status=status, error=error, collected_at=collected_at),  # type: ignore[arg-type]
    )
