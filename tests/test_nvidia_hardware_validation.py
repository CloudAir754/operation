"""NVIDIA 实机验收脚本的脱敏字段关系测试。"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_nvidia_hardware.py"
SPEC = importlib.util.spec_from_file_location("validate_nvidia_hardware", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _snapshot() -> dict[str, object]:
    return {
        "available": True,
        "reason": None,
        "devices": [
            {
                "index": 0,
                "uuid": "GPU-REDACTED",
                "pci_bus_id": "0000:01:00.0",
                "name": "Synthetic NVIDIA GPU",
                "memory_total": 24 * 1024**3,
                "memory_used": 1024**3,
                "gpu_percent": 25,
                "temperature_celsius": 50,
                "fan_percent": None,
                "power_watts": 100,
                "pcie_rx_bytes_per_second": 10,
                "pcie_tx_bytes_per_second": 20,
                "dram_bandwidth_percent": None,
            }
        ],
        "processes": [
            {
                "gpu_index": 0,
                "gpu_uuid": "GPU-REDACTED",
                "pid": 1234,
                "memory_percent": 4.2,
                "bandwidth": None,
                "sm_percent": 20,
                "memory_activity_percent": None,
                "encoder_percent": None,
                "decoder_percent": None,
            }
        ],
    }


def test_nvidia_validation_accepts_consistent_device_and_process() -> None:
    """设备与进程稳定身份一致且不伪造带宽时应通过。"""

    result = MODULE.validate_snapshot(_snapshot(), require_process=True)
    assert result["ok"] is True
    assert result["device_count"] == result["process_count"] == 1


def test_nvidia_validation_rejects_missing_hardware_and_fake_bandwidth() -> None:
    """无硬件或出现伪造进程带宽都必须令实机验收失败。"""

    missing = MODULE.validate_snapshot(
        {"available": False, "reason": "NVMLError_LibraryNotFound", "devices": [], "processes": []}
    )
    snapshot = _snapshot()
    snapshot["processes"][0]["bandwidth"] = 100  # type: ignore[index]
    fake_bandwidth = MODULE.validate_snapshot(snapshot)

    assert missing["ok"] is False
    assert fake_bandwidth["ok"] is False
    assert any("不得" in error for error in fake_bandwidth["errors"])
