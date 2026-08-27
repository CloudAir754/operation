"""NVIDIA GPU 只读采集，并在驱动或硬件缺失时明确降级。"""

from __future__ import annotations

from contextlib import suppress
from typing import Any


def gpu_snapshot() -> dict[str, Any]:
    """通过 NVML 采集 GPU；任何初始化失败都不影响其他系统功能。"""

    try:
        import pynvml

        pynvml.nvmlInit()
    except Exception as exc:  # NVML 会用多种驱动相关异常表示不可用
        return {"available": False, "reason": type(exc).__name__, "devices": []}

    devices: list[dict[str, Any]] = []
    try:
        driver = pynvml.nvmlSystemGetDriverVersion()
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
            devices.append(
                {
                    "index": index,
                    "uuid": pynvml.nvmlDeviceGetUUID(handle),
                    "pci_bus_id": pynvml.nvmlDeviceGetPciInfo(handle).busId,
                    "name": pynvml.nvmlDeviceGetName(handle),
                    "driver_version": driver,
                    "memory_total": memory.total,
                    "memory_used": memory.used,
                    "gpu_percent": utilization.gpu,
                    "memory_percent": utilization.memory,
                }
            )
        return {"available": True, "reason": None, "devices": devices}
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__, "devices": []}
    finally:
        with suppress(Exception):
            pynvml.nvmlShutdown()
