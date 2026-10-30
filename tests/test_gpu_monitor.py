"""GPU 监控归一化测试：多卡、MIG、进程可见性、超时与降级。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lab_ops_panel.remote.gpu import normalize_gpu_monitor

COLLECTED = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


def device(index: int = 0, **overrides) -> dict:
    data = {
        "index": index,
        "uuid": f"GPU-{index}",
        "pciBusId": f"0000:0{index + 1}:00.0",
        "name": "NVIDIA GeForce RTX 4090",
        "driverVersion": "550.107.02",
        "memoryTotalMb": 24564,
        "memoryUsedMb": 8192,
        "utilizationGpuPercent": 37.0,
        "utilizationMemoryPercent": 33.0,
        "encoderPercent": None,
        "decoderPercent": None,
        "temperatureC": 52.0,
        "powerDrawW": 210.5,
        "powerLimitW": 450.0,
        "clockGraphicsMhz": 2520,
        "clockMemoryMhz": 10501,
        "migMode": "Disabled",
        "computeMode": "Default",
        "migDevices": [],
    }
    data.update(overrides)
    return data


def process(pid: int = 4242, **overrides) -> dict:
    data = {
        "pid": pid,
        "user": "labuser",
        "name": "python",
        "command": "python train.py",
        "memoryUsedMb": 8192,
        "utilizationPercent": None,
        "gpuUuid": "GPU-0",
        "gpuIndex": 0,
        "programPath": "/usr/bin/python3.12",
        "cwd": "/home/labuser/work",
        "startedAtEpoch": 1760000000,
        "stale": False,
        "error": None,
    }
    data.update(overrides)
    return data


def section(devices: list[dict], processes: list[dict], **overrides) -> dict:
    data = {
        "devices": devices,
        "processes": processes,
        "processesStatus": "ok",
        "processesError": None,
        "capabilities": {
            "nvml": True,
            "processes": "ok",
            "mig": False,
            "perProcessUtilization": False,
        },
        "notes": [],
    }
    data.update(overrides)
    return {"status": "ok", "error": None, "requiresSudo": False, "data": data}


def test_missing_driver_is_unavailable_not_empty_success() -> None:
    result = normalize_gpu_monitor(
        {
            "status": "unavailable",
            "error": "未检测到 nvidia-smi",
            "data": {"devices": [], "processes": [], "processesStatus": "unavailable"},
        },
        server_id=1,
        collected_at=COLLECTED,
    )

    assert result.status == "unavailable"
    assert result.error == "未检测到 nvidia-smi"
    assert result.devices == []
    assert result.processes_status == "unavailable"
    assert result.capabilities.nvml is False


def test_multi_gpu_is_sorted_and_deduplicated() -> None:
    result = normalize_gpu_monitor(
        section(
            [device(1), device(0), device(0, name="duplicate")],
            [],
        ),
        server_id=1,
        collected_at=COLLECTED,
    )

    assert [item.index for item in result.devices] == [0, 1]
    assert result.devices[0].name == "NVIDIA GeForce RTX 4090"


def test_mig_devices_are_reported() -> None:
    result = normalize_gpu_monitor(
        section(
            [
                device(
                    0,
                    migMode="Enabled",
                    migDevices=["MIG 1g.5gb Device 0: (UUID: MIG-abc)"],
                )
            ],
            [],
        ),
        server_id=1,
        collected_at=COLLECTED,
    )

    assert result.devices[0].mig_mode == "Enabled"
    assert result.devices[0].mig_devices == ["MIG 1g.5gb Device 0: (UUID: MIG-abc)"]


def test_processes_sorted_by_memory_and_deduplicated() -> None:
    result = normalize_gpu_monitor(
        section(
            [device()],
            [
                process(100, memoryUsedMb=1024),
                process(200, memoryUsedMb=8192),
                process(200, memoryUsedMb=8192),
                {"pid": "not-a-number", "memoryUsedMb": 1},
            ],
        ),
        server_id=1,
        collected_at=COLLECTED,
    )

    assert [item.pid for item in result.processes] == [200, 100]
    assert result.processes[0].memory_used_mb == 8192
    assert result.processes_status == "ok"


def test_stale_or_invisible_process_marks_partial() -> None:
    result = normalize_gpu_monitor(
        section(
            [device()],
            [process(1, stale=True, error="进程已退出"), process(2)],
            processesStatus="ok",
        ),
        server_id=1,
        collected_at=COLLECTED,
    )

    assert result.processes_status == "partial"
    assert result.processes[0].pid == 1
    assert result.processes[0].stale is True
    assert result.processes[0].error == "进程已退出"


def test_started_at_is_converted_to_datetime() -> None:
    result = normalize_gpu_monitor(
        section([device()], [process(startedAtEpoch=1760000000)]),
        server_id=1,
        collected_at=COLLECTED,
    )

    started = result.processes[0].started_at
    assert started is not None
    assert started.tzinfo is not None


def test_invalid_processes_are_skipped() -> None:
    result = normalize_gpu_monitor(
        section(
            [device()],
            [
                process(0),
                process(-5),
                {"pid": None},
                "junk",
                process(7),
            ],
        ),
        server_id=1,
        collected_at=COLLECTED,
    )

    assert [item.pid for item in result.processes] == [7]


def test_process_list_is_capped() -> None:
    many = [process(pid) for pid in range(1, 260)]

    result = normalize_gpu_monitor(section([device()], many), server_id=1, collected_at=COLLECTED)

    assert len(result.processes) == 200


def test_per_process_utilization_stays_null() -> None:
    result = normalize_gpu_monitor(
        section([device()], [process()]),
        server_id=1,
        collected_at=COLLECTED,
    )

    assert result.capabilities.per_process_utilization is False
    assert result.processes[0].utilization_percent is None


def test_raw_tool_text_is_not_exposed() -> None:
    payload = section([device()], [])
    payload["data"]["rawText"] = "GPU 00000000:01:00.0 ..."
    payload["data"]["devices"][0]["raw"] = "secret raw text"

    result = normalize_gpu_monitor(payload, server_id=1, collected_at=COLLECTED)

    dumped = result.model_dump_json()
    assert "secret raw text" not in dumped
    assert "rawText" not in dumped


def test_partial_capability_without_processes() -> None:
    payload = section(
        [device()],
        [],
        processesStatus="unavailable",
        processesError="hidepid 限制",
        capabilities={
            "nvml": True,
            "processes": "unavailable",
            "mig": False,
            "perProcessUtilization": False,
        },
    )

    result = normalize_gpu_monitor(payload, server_id=1, collected_at=COLLECTED)

    assert result.processes_status == "unavailable"
    assert result.processes_error == "hidepid 限制"
    assert result.capabilities.processes == "unavailable"


@pytest.mark.parametrize("status", ["error", "permission_denied"])
def test_failed_status_forces_no_processes(status: str) -> None:
    payload = section([device()], [process()])
    payload["status"] = status
    payload["error"] = "驱动错误"

    result = normalize_gpu_monitor(payload, server_id=1, collected_at=COLLECTED)

    assert result.status == status
    assert result.error == "驱动错误"
    assert result.processes_status == "unavailable"
