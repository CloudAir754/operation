"""指标差分测试：回绕、CPU/内存/Swap、磁盘、网络与分区状态。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lab_ops_panel.remote.metrics import (
    Rate,
    build_point,
    counter_delta,
    derive_metrics,
    empty_sections,
    summarize_status,
)

COLLECTED = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


def _cpu(
    *,
    user: int = 100,
    system: int = 50,
    idle: int = 800,
    iowait: int = 20,
    steal: int = 0,
) -> dict[str, int]:
    return {
        "user": user,
        "nice": 0,
        "system": system,
        "idle": idle,
        "iowait": iowait,
        "irq": 0,
        "softirq": 0,
        "steal": steal,
        "logicalCpus": 16,
        "load1": 0.5,
        "load5": 0.4,
        "load15": 0.3,
    }


def _memory(**overrides: int) -> dict[str, int]:
    base = {
        "MemTotal": 1000,
        "MemAvailable": 400,
        "MemFree": 100,
        "Buffers": 100,
        "Cached": 200,
        "SReclaimable": 50,
        "CommitLimit": 2000,
        "Committed_AS": 500,
        "SwapTotal": 500,
        "SwapFree": 300,
    }
    base.update(overrides)
    return base


def _disk(prefix: str = "sda") -> dict[str, dict[str, int]]:
    return {
        prefix: {
            "readsCompleted": 2,
            "sectorsRead": 100,
            "msReading": 50,
            "writesCompleted": 4,
            "sectorsWritten": 200,
            "msWriting": 100,
            "ioMs": 100,
        }
    }


def _metrics_raw(
    *,
    cpu: dict[str, int] | None = None,
    memory: dict[str, int] | None = None,
    disk: dict[str, dict[str, int]] | None = None,
    network: dict[str, dict[str, int]] | None = None,
) -> dict:
    return {
        "cpu": cpu if cpu is not None else _cpu(),
        "memory": memory if memory is not None else _memory(),
        "vmstat": {"pswpin": 10, "pswpout": 5, "pgpgin": 1, "pgpgout": 2},
        "disk": disk if disk is not None else _disk(),
        "diskMeta": {
            "sda": {
                "kind": "disk",
                "parent": None,
                "model": "TestDisk",
                "sizeBytes": 1000,
                "hidden": False,
            },
            "dm-0": {"kind": "virtual", "parent": "sda", "hidden": True},
        },
        "mounts": [
            {"mount": "/", "device": "/dev/sda1", "filesystem": "ext4", "deviceName": "sda"}
        ],
        "network": network if network is not None else {"eth0": _net_row()},
        "networkMeta": {
            "eth0": {
                "kind": "physical",
                "state": "up",
                "speedMbps": 10000,
                "addresses": ["10.0.0.11/24"],
            },
            "lo": {"kind": "loopback", "state": "unknown"},
        },
        "defaultInterface": "eth0",
        "gpu": {"devices": [{"index": 0, "name": "RTX 4090", "utilizationPercent": 20.0}]},
        "gpuStatus": "ok",
    }


def _net_row() -> dict[str, int]:
    return {
        "rxBytes": 1000,
        "rxPackets": 10,
        "rxErrors": 0,
        "rxDrops": 1,
        "txBytes": 2000,
        "txPackets": 20,
        "txErrors": 2,
        "txDrops": 0,
    }


def test_counter_delta_computes_rate() -> None:
    delta = counter_delta(100, 300, 2.0)

    assert delta.value == 100
    assert delta.reset is False


@pytest.mark.parametrize(
    ("previous", "current", "interval", "reason"),
    [
        (100, 50, 2.0, "计数器回绕"),
        (100, 200, 0.05, "采样间隔过短"),
        (100, 200, 7200.0, "采样间隔过长"),
        (None, 200, 2.0, "缺少上一次采样"),
        (100, None, 2.0, "缺少上一次采样"),
    ],
)
def test_counter_delta_rebuilds_baseline(previous, current, interval, reason) -> None:
    delta = counter_delta(previous, current, interval)

    assert delta.value is None
    assert delta.reset is True
    assert delta.reason == reason


def test_counter_delta_ignores_non_numeric_values() -> None:
    assert counter_delta("abc", 10, 2.0).reset is True


def test_first_sample_has_no_rates_but_keeps_context() -> None:
    sections, reset = derive_metrics(
        previous=None, current=_metrics_raw(), interval_ms=1000, collected_at=COLLECTED
    )

    assert reset == "缺少上一次采样"
    assert sections.cpu.status == "ok"
    assert sections.cpu.data.total_percent is None
    assert sections.cpu.data.logical_cpus == 16
    assert sections.cpu.data.load1 == 0.5
    assert sections.memory.data.used_bytes == 600
    assert sections.memory.data.swap_used_bytes == 200
    assert sections.gpu.status == "ok"


def test_second_sample_computes_cpu_percentages() -> None:
    previous = _metrics_raw()
    current = _metrics_raw(cpu=_cpu(user=120, system=60, idle=880, iowait=25), memory=_memory())

    sections, reset = derive_metrics(
        previous=previous, current=current, interval_ms=2000, collected_at=COLLECTED
    )

    assert reset is None
    cpu = sections.cpu.data
    assert cpu.total_percent == 30.4
    assert cpu.user_percent == 17.4
    assert cpu.system_percent == 8.7
    assert cpu.iowait_percent == 4.3
    assert cpu.idle_percent == 69.6


def test_memory_details_and_swap_rates() -> None:
    previous = _metrics_raw(memory=_memory())
    current = _metrics_raw(memory=_memory(SwapFree=250), cpu=_cpu(user=110))
    current["vmstat"]["pswpin"] = 12
    current["vmstat"]["pswpout"] = 7

    sections, _ = derive_metrics(
        previous=previous, current=current, interval_ms=2000, collected_at=COLLECTED
    )

    memory = sections.memory.data
    assert memory.total_bytes == 1000
    assert memory.used_percent == 60.0
    assert memory.cached_bytes == 250
    assert memory.commit_bytes == 1500
    assert memory.swap_used_bytes == 250
    assert memory.swap_in_per_second == 4096.0
    assert memory.swap_out_per_second == 4096.0


def test_disk_rates_await_util_and_mount_mapping() -> None:
    previous = _metrics_raw()
    current_disk = {
        "sda": {
            "readsCompleted": 6,
            "sectorsRead": 300,
            "msReading": 150,
            "writesCompleted": 8,
            "sectorsWritten": 400,
            "msWriting": 300,
            "ioMs": 300,
        }
    }
    current = _metrics_raw(cpu=_cpu(user=110), disk=current_disk)

    sections, reset = derive_metrics(
        previous=previous, current=current, interval_ms=2000, collected_at=COLLECTED
    )

    assert reset is None
    device = sections.disk.data.devices[0]
    assert device.name == "sda"
    assert device.read_bytes_per_second == 51200
    assert device.write_bytes_per_second == 51200
    assert device.read_iops == 2
    assert device.write_iops == 2
    assert device.await_ms == 37.5
    assert device.util_percent == 10.0
    assert device.model == "TestDisk"
    assert sections.disk.data.mounts[0].read_bytes_per_second == 51200


def test_hidden_devices_are_present_but_flagged() -> None:
    disk = _disk()
    disk["dm-0"] = dict(disk["sda"])

    sections, _ = derive_metrics(
        previous=_metrics_raw(disk=disk),
        current=_metrics_raw(cpu=_cpu(user=110), disk=disk),
        interval_ms=2000,
        collected_at=COLLECTED,
    )

    hidden = [device for device in sections.disk.data.devices if device.hidden]
    assert [device.name for device in hidden] == ["dm-0"]


def test_network_rates_and_counters() -> None:
    previous = _metrics_raw()
    current_net = _net_row()
    current_net.update({"rxBytes": 3000, "txBytes": 4000, "rxPackets": 30, "txPackets": 40})
    current_net["rxErrors"] = 3
    current = _metrics_raw(cpu=_cpu(user=110), network={"eth0": current_net})

    sections, _ = derive_metrics(
        previous=previous, current=current, interval_ms=2000, collected_at=COLLECTED
    )

    interface = sections.network.data.interfaces[0]
    assert interface.name == "eth0"
    assert interface.kind == "physical"
    assert interface.rx_bytes_per_second == 1000
    assert interface.tx_bytes_per_second == 1000
    assert interface.rx_packets_per_second == 10
    assert interface.rx_errors == 3
    assert interface.rx_drops == 1
    assert sections.network.data.default_interface == "eth0"


def test_counter_wrap_marks_reset_and_nulls_rates() -> None:
    previous = _metrics_raw()
    current = _metrics_raw(
        cpu=_cpu(user=110),
        disk={
            "sda": {
                "readsCompleted": 1,
                "sectorsRead": 10,
                "msReading": 5,
                "writesCompleted": 1,
                "sectorsWritten": 10,
                "msWriting": 5,
                "ioMs": 5,
            }
        },
    )

    sections, reset = derive_metrics(
        previous=previous, current=current, interval_ms=2000, collected_at=COLLECTED
    )

    assert reset is not None
    device = sections.disk.data.devices[0]
    assert device.read_bytes_per_second is None
    assert device.reset is True


def test_missing_memory_marks_section_error() -> None:
    raw = _metrics_raw(cpu=_cpu(user=110))
    raw["memory"] = {}
    previous = _metrics_raw()

    sections, _ = derive_metrics(
        previous=previous, current=raw, interval_ms=2000, collected_at=COLLECTED
    )

    assert sections.memory.status == "error"
    assert sections.memory.data is None
    status, error = summarize_status(sections)
    assert status == "error"
    assert error


def test_no_gpu_is_unavailable_not_zero() -> None:
    raw = _metrics_raw(cpu=_cpu(user=110))
    raw["gpu"] = None
    raw["gpuStatus"] = "unavailable"

    sections, _ = derive_metrics(
        previous=_metrics_raw(), current=raw, interval_ms=2000, collected_at=COLLECTED
    )

    assert sections.gpu.status == "unavailable"
    assert sections.gpu.data is None


def test_permission_denied_is_propagated() -> None:
    sections = empty_sections(COLLECTED, status="permission_denied", error="拒绝访问")

    assert sections.cpu.status == "permission_denied"
    assert sections.cpu.data is None
    status, error = summarize_status(sections)
    assert status == "permission_denied"
    assert error == "拒绝访问"


def test_build_point_keeps_reset_and_gpu_samples() -> None:
    sections, _ = derive_metrics(
        previous=_metrics_raw(),
        current=_metrics_raw(cpu=_cpu(user=120)),
        interval_ms=2000,
        collected_at=COLLECTED,
    )

    point = build_point(
        sections,
        collected_at=COLLECTED,
        received_at=COLLECTED,
        interval_ms=2000,
        reset_reason="缺少上一次采样",
    )

    assert point.reset is True
    assert point.reset_reason == "缺少上一次采样"
    assert point.cpu_percent is not None
    assert point.swap_used_percent == 40.0
    assert set(point.disk) == {"sda"}
    assert set(point.network) == {"eth0"}
    assert point.gpu[0].index == 0


def test_rate_dataclass_defaults() -> None:
    rate = Rate(None)

    assert rate.reset is False
    assert rate.reason is None
