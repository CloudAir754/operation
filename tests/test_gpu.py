"""GPU 能力降级测试。"""

import sys
from types import SimpleNamespace

from lab_ops_panel.infrastructure.gpu import gpu_snapshot


def test_missing_nvml_is_reported_as_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """无 NVIDIA 驱动环境必须返回 N/A 语义而不是抛出异常。"""

    monkeypatch.setitem(sys.modules, "pynvml", None)
    snapshot = gpu_snapshot()
    assert snapshot["available"] is False
    assert snapshot["devices"] == []
    assert snapshot["reason"]


def test_gpu_snapshot_reports_devices_and_processes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """可用 NVML 应返回稳定 GPU 身份、整卡指标和真实进程显存。"""

    fake = SimpleNamespace(
        NVML_TEMPERATURE_GPU=0,
        NVML_CLOCK_GRAPHICS=0,
        NVML_CLOCK_SM=1,
        NVML_CLOCK_MEM=2,
        NVML_PCIE_UTIL_RX_BYTES=0,
        NVML_PCIE_UTIL_TX_BYTES=1,
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlSystemGetDriverVersion=lambda: b"test-driver",
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda index: "handle",
        nvmlDeviceGetMemoryInfo=lambda handle: SimpleNamespace(total=24_000, used=6_000),
        nvmlDeviceGetUtilizationRates=lambda handle: SimpleNamespace(gpu=50, memory=20),
        nvmlDeviceGetUUID=lambda handle: b"GPU-REDACTED",
        nvmlDeviceGetPciInfo=lambda handle: SimpleNamespace(busId=b"0000:01:00.0"),
        nvmlDeviceGetName=lambda handle: b"Test GPU",
        nvmlDeviceGetTemperature=lambda handle, sensor: 65,
        nvmlDeviceGetFanSpeed=lambda handle: 30,
        nvmlDeviceGetPowerUsage=lambda handle: 250_000,
        nvmlDeviceGetClockInfo=lambda handle, clock: 1500,
        nvmlDeviceGetPcieThroughput=lambda handle, counter: 100,
        nvmlDeviceGetComputeRunningProcesses=lambda handle: [
            SimpleNamespace(pid=1234, usedGpuMemory=4_000)
        ],
        nvmlDeviceGetGraphicsRunningProcesses=lambda handle: [],
        nvmlDeviceGetProcessUtilization=lambda handle, since: [
            SimpleNamespace(
                pid=1234,
                smUtil=70,
                memUtil=40,
                encUtil=0,
                decUtil=0,
                timeStamp=since + 1,
            )
        ],
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.gpu._process_identity",
        lambda pid: ("tester", "python train.py"),
    )

    snapshot = gpu_snapshot()

    assert snapshot["available"] is True
    assert snapshot["devices"][0]["uuid"] == "GPU-REDACTED"
    assert snapshot["devices"][0]["power_watts"] == 250.0
    assert snapshot["devices"][0]["dram_bandwidth_percent"] is None
    assert snapshot["processes"][0]["memory_used"] == 4_000
    assert snapshot["processes"][0]["sm_percent"] == 70
    assert snapshot["processes"][0]["sample_period_seconds"] == 2.0
    assert snapshot["processes"][0]["bandwidth"] is None
