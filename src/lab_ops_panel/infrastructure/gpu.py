"""NVIDIA GPU 只读采集，并在驱动或指标缺失时按字段降级。"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

import psutil


def _text(value: Any) -> str:
    """兼容不同 NVML 绑定返回的字符串与字节串。"""

    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def _optional_call(module: Any, name: str, *args: Any, scale: float = 1.0) -> Any:
    """读取可选 NVML 指标，不支持时返回 None。"""

    try:
        value = getattr(module, name)(*args)
        return value * scale if isinstance(value, int | float) else value
    except Exception:
        return None


def _process_identity(pid: int) -> tuple[str | None, str | None]:
    """从当前 PID 获取系统用户和命令；进程已退出时返回 N/A。"""

    try:
        process = psutil.Process(pid)
        command = " ".join(process.cmdline()) or process.name()
        return process.username(), command
    except (psutil.Error, OSError):
        return None, None


def _process_utilization(module: Any, handle: Any, window_seconds: float = 2.0) -> dict[int, Any]:
    """读取查询窗口内每个 PID 最新的 NVML 利用率样本。"""

    since_microseconds = max(0, time.time_ns() // 1000 - int(window_seconds * 1_000_000))
    samples = (
        _optional_call(module, "nvmlDeviceGetProcessUtilization", handle, since_microseconds) or []
    )
    latest: dict[int, Any] = {}
    for sample in samples:
        previous = latest.get(int(sample.pid))
        if previous is None or getattr(sample, "timeStamp", 0) > getattr(previous, "timeStamp", 0):
            latest[int(sample.pid)] = sample
    return latest


def _running_processes(module: Any, handle: Any, device: dict[str, Any]) -> list[dict[str, Any]]:
    """合并计算和图形进程，绝不把整卡指标伪分配给进程。"""

    by_pid: dict[int, dict[str, Any]] = {}
    getters = (
        ("nvmlDeviceGetComputeRunningProcesses", "计算"),
        ("nvmlDeviceGetGraphicsRunningProcesses", "图形"),
    )
    utilization_by_pid = _process_utilization(module, handle)
    for getter_name, process_type in getters:
        items = _optional_call(module, getter_name, handle) or []
        for item in items:
            pid = int(item.pid)
            used_memory = getattr(item, "usedGpuMemory", None)
            if (
                not isinstance(used_memory, int)
                or used_memory < 0
                or used_memory > device["memory_total"]
            ):
                used_memory = None
            sample = utilization_by_pid.get(pid)
            record = by_pid.setdefault(
                pid,
                {
                    "gpu_index": device["index"],
                    "gpu_uuid": device["uuid"],
                    "pid": pid,
                    "types": [],
                    "memory_used": used_memory,
                    "memory_percent": (
                        used_memory / device["memory_total"] * 100
                        if used_memory is not None and device["memory_total"]
                        else None
                    ),
                    "sm_percent": getattr(sample, "smUtil", None),
                    "memory_activity_percent": getattr(sample, "memUtil", None),
                    "encoder_percent": getattr(sample, "encUtil", None),
                    "decoder_percent": getattr(sample, "decUtil", None),
                    "bandwidth": None,
                    "sample_period_seconds": 2.0 if sample is not None else None,
                },
            )
            record["types"].append(process_type)
    result = []
    for pid, record in by_pid.items():
        username, command = _process_identity(pid)
        record["username"] = username
        record["command"] = command
        result.append(record)
    return sorted(result, key=lambda item: (item["gpu_index"], -int(item["memory_used"] or 0)))


def _device_snapshot(module: Any, handle: Any, index: int, driver: str) -> dict[str, Any]:
    """采集单卡总览，所有可选字段独立降级。"""

    memory = module.nvmlDeviceGetMemoryInfo(handle)
    utilization = module.nvmlDeviceGetUtilizationRates(handle)
    device = {
        "index": index,
        "uuid": _text(module.nvmlDeviceGetUUID(handle)),
        "pci_bus_id": _text(module.nvmlDeviceGetPciInfo(handle).busId),
        "name": _text(module.nvmlDeviceGetName(handle)),
        "driver_version": driver,
        "memory_total": memory.total,
        "memory_used": memory.used,
        "gpu_percent": utilization.gpu,
        "memory_activity_percent": utilization.memory,
        "temperature_celsius": _optional_call(
            module, "nvmlDeviceGetTemperature", handle, module.NVML_TEMPERATURE_GPU
        ),
        "fan_percent": _optional_call(module, "nvmlDeviceGetFanSpeed", handle),
        "power_watts": _optional_call(module, "nvmlDeviceGetPowerUsage", handle, scale=0.001),
        "graphics_clock_mhz": _optional_call(
            module, "nvmlDeviceGetClockInfo", handle, module.NVML_CLOCK_GRAPHICS
        ),
        "sm_clock_mhz": _optional_call(
            module, "nvmlDeviceGetClockInfo", handle, module.NVML_CLOCK_SM
        ),
        "memory_clock_mhz": _optional_call(
            module, "nvmlDeviceGetClockInfo", handle, module.NVML_CLOCK_MEM
        ),
        "pcie_rx_bytes_per_second": _optional_call(
            module,
            "nvmlDeviceGetPcieThroughput",
            handle,
            module.NVML_PCIE_UTIL_RX_BYTES,
            scale=1024,
        ),
        "pcie_tx_bytes_per_second": _optional_call(
            module,
            "nvmlDeviceGetPcieThroughput",
            handle,
            module.NVML_PCIE_UTIL_TX_BYTES,
            scale=1024,
        ),
        # 仅在 GPM 能力经验证后提供真实 DRAM 带宽，不能用 memory utilization 冒充。
        "dram_bandwidth_percent": None,
    }
    device["processes"] = _running_processes(module, handle, device)
    return device


def gpu_snapshot() -> dict[str, Any]:
    """通过 NVML 采集 GPU；初始化失败不影响其他系统功能。"""

    try:
        import pynvml

        pynvml.nvmlInit()
    except Exception as exc:  # NVML 会用多种驱动相关异常表示不可用
        return {"available": False, "reason": type(exc).__name__, "devices": [], "processes": []}

    devices: list[dict[str, Any]] = []
    try:
        driver = _text(pynvml.nvmlSystemGetDriverVersion())
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            devices.append(_device_snapshot(pynvml, handle, index, driver))
        return {
            "available": True,
            "reason": None,
            "devices": devices,
            "processes": [process for device in devices for process in device["processes"]],
        }
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__, "devices": [], "processes": []}
    finally:
        with suppress(Exception):
            pynvml.nvmlShutdown()
