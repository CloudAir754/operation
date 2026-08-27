"""短周期资源采样测试。"""

from types import SimpleNamespace

from lab_ops_panel.infrastructure.resources import ResourceSampler, _non_negative_rate


def _disk(read_bytes: int, write_bytes: int, busy_time: int) -> SimpleNamespace:
    return SimpleNamespace(
        read_bytes=read_bytes,
        write_bytes=write_bytes,
        read_count=10,
        write_count=20,
        busy_time=busy_time,
    )


def _network(sent: int, received: int) -> SimpleNamespace:
    return SimpleNamespace(
        bytes_sent=sent,
        bytes_recv=received,
        errin=1,
        errout=2,
        dropin=3,
        dropout=4,
    )


def test_resource_sampler_calculates_counter_rates(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """第二轮采样应按真实时间差计算吞吐而不是展示累计值。"""

    times = iter([10.0, 12.0])
    disks = iter([{"sda": _disk(1000, 2000, 100)}, {"sda": _disk(1200, 2600, 200)}])
    networks = iter([{"eth0": _network(1000, 2000)}, {"eth0": _network(1400, 4000)}])
    memory = SimpleNamespace(total=100, available=40, used=60, cached=10, percent=60.0)
    swap = SimpleNamespace(total=20, used=5)

    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.resources.time.monotonic", lambda: next(times)
    )
    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.resources.psutil.disk_io_counters",
        lambda perdisk: next(disks),
    )
    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.resources.psutil.net_io_counters",
        lambda pernic: next(networks),
    )
    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.resources.psutil.net_if_stats",
        lambda: {"eth0": SimpleNamespace(isup=True, speed=1000)},
    )
    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.resources.psutil.net_if_addrs",
        lambda: {"eth0": [SimpleNamespace(address="192.0.2.10")]},
    )
    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.resources.psutil.virtual_memory", lambda: memory
    )
    monkeypatch.setattr("lab_ops_panel.infrastructure.resources.psutil.swap_memory", lambda: swap)
    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.resources.psutil.cpu_count",
        lambda logical: 4 if logical else 2,
    )
    monkeypatch.setattr(
        "lab_ops_panel.infrastructure.resources.psutil.cpu_percent",
        lambda interval, percpu=False: [10.0, 20.0] if percpu else 15.0,
    )
    monkeypatch.setattr("lab_ops_panel.infrastructure.resources.os.getloadavg", lambda: (1, 2, 3))
    monkeypatch.setattr("lab_ops_panel.infrastructure.resources._cpu_model", lambda: "Test CPU")

    sampler = ResourceSampler()
    first = sampler.sample()
    second = sampler.sample()

    assert first["disks"][0]["read_bytes_per_second"] is None
    assert second["sample_interval_seconds"] == 2.0
    assert second["disks"][0]["read_bytes_per_second"] == 100.0
    assert second["disks"][0]["write_bytes_per_second"] == 300.0
    assert second["disks"][0]["busy_percent"] == 5.0
    assert second["networks"][0]["received_bytes_per_second"] == 1000.0


def test_counter_reset_never_produces_negative_rate() -> None:
    """设备重置或热插拔造成计数回退时速率归零。"""

    assert _non_negative_rate(5, 10, 2.0) == 0.0
