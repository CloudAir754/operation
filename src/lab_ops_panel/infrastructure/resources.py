"""短周期资源采样与计数器速率计算。"""

from __future__ import annotations

import os
import platform
import threading
import time
from dataclasses import dataclass
from typing import Any

import psutil


@dataclass(frozen=True, slots=True)
class _DiskCounters:
    """计算磁盘速率所需的累计计数器。"""

    read_bytes: int
    write_bytes: int
    read_count: int
    write_count: int
    busy_time: int


@dataclass(frozen=True, slots=True)
class _NetworkCounters:
    """计算网卡速率所需的累计计数器。"""

    bytes_sent: int
    bytes_recv: int


def _non_negative_rate(current: int, previous: int, elapsed: float) -> float:
    """计数器复位时返回零，避免产生负速率。"""

    return max(0.0, (current - previous) / elapsed)


def _cpu_model() -> str:
    """优先从 procfs 获取 Linux CPU 型号。"""

    try:
        for line in open("/proc/cpuinfo", encoding="utf-8"):  # noqa: SIM115
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except (OSError, IndexError):
        pass
    return platform.processor() or "N/A"


class ResourceSampler:
    """在进程内保存上一轮累计值，生成不持久化的短周期资源快照。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sampled_at: float | None = None
        self._disks: dict[str, _DiskCounters] = {}
        self._networks: dict[str, _NetworkCounters] = {}

    def sample(self) -> dict[str, Any]:
        """采集 CPU、内存、磁盘 I/O 和网络状态及速率。"""

        with self._lock:
            now = time.monotonic()
            elapsed = now - self._sampled_at if self._sampled_at is not None else None
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()
            disk_counters = psutil.disk_io_counters(perdisk=True) or {}
            network_counters = psutil.net_io_counters(pernic=True) or {}
            network_stats = psutil.net_if_stats()
            network_addresses = psutil.net_if_addrs()

            disks = self._sample_disks(disk_counters, elapsed)
            networks = self._sample_networks(
                network_counters, network_stats, network_addresses, elapsed
            )
            self._sampled_at = now
            return {
                "sample_interval_seconds": elapsed,
                "cpu": {
                    "model": _cpu_model(),
                    "logical_cores": psutil.cpu_count(logical=True),
                    "physical_cores": psutil.cpu_count(logical=False),
                    "load_average": list(os.getloadavg()),
                    "total_percent": psutil.cpu_percent(interval=None),
                    "per_core_percent": psutil.cpu_percent(interval=None, percpu=True),
                },
                "memory": {
                    "total": memory.total,
                    "available": memory.available,
                    "used": memory.used,
                    "cached": getattr(memory, "cached", None),
                    "percent": memory.percent,
                    "swap_total": swap.total,
                    "swap_used": swap.used,
                },
                "disks": disks,
                "networks": networks,
            }

    def _sample_disks(
        self, counters: dict[str, Any], elapsed: float | None
    ) -> list[dict[str, Any]]:
        """根据累计磁盘计数器计算每秒速率。"""

        result = []
        current_counters: dict[str, _DiskCounters] = {}
        for name, item in sorted(counters.items()):
            current = _DiskCounters(
                read_bytes=item.read_bytes,
                write_bytes=item.write_bytes,
                read_count=item.read_count,
                write_count=item.write_count,
                busy_time=getattr(item, "busy_time", 0),
            )
            previous = self._disks.get(name)
            rates_available = previous is not None and elapsed is not None and elapsed > 0
            result.append(
                {
                    "name": name,
                    "read_bytes_per_second": (
                        _non_negative_rate(current.read_bytes, previous.read_bytes, elapsed)
                        if rates_available
                        else None
                    ),
                    "write_bytes_per_second": (
                        _non_negative_rate(current.write_bytes, previous.write_bytes, elapsed)
                        if rates_available
                        else None
                    ),
                    "read_iops": (
                        _non_negative_rate(current.read_count, previous.read_count, elapsed)
                        if rates_available
                        else None
                    ),
                    "write_iops": (
                        _non_negative_rate(current.write_count, previous.write_count, elapsed)
                        if rates_available
                        else None
                    ),
                    "busy_percent": (
                        min(
                            100.0,
                            _non_negative_rate(current.busy_time, previous.busy_time, elapsed) / 10,
                        )
                        if rates_available
                        else None
                    ),
                }
            )
            current_counters[name] = current
        self._disks = current_counters
        return result

    def _sample_networks(
        self,
        counters: dict[str, Any],
        stats: dict[str, Any],
        addresses: dict[str, list[Any]],
        elapsed: float | None,
    ) -> list[dict[str, Any]]:
        """根据累计网卡计数器计算收发速率并附带链路状态。"""

        result = []
        current_counters: dict[str, _NetworkCounters] = {}
        for name, item in sorted(counters.items()):
            current = _NetworkCounters(bytes_sent=item.bytes_sent, bytes_recv=item.bytes_recv)
            previous = self._networks.get(name)
            rates_available = previous is not None and elapsed is not None and elapsed > 0
            interface_stats = stats.get(name)
            result.append(
                {
                    "name": name,
                    "is_up": interface_stats.isup if interface_stats else None,
                    "speed_mbps": interface_stats.speed if interface_stats else None,
                    "addresses": [address.address for address in addresses.get(name, [])],
                    "sent_bytes_per_second": (
                        _non_negative_rate(current.bytes_sent, previous.bytes_sent, elapsed)
                        if rates_available
                        else None
                    ),
                    "received_bytes_per_second": (
                        _non_negative_rate(current.bytes_recv, previous.bytes_recv, elapsed)
                        if rates_available
                        else None
                    ),
                    "errors_in": item.errin,
                    "errors_out": item.errout,
                    "drops_in": item.dropin,
                    "drops_out": item.dropout,
                }
            )
            current_counters[name] = current
        self._networks = current_counters
        return result


resource_sampler = ResourceSampler()


def resource_snapshot() -> dict[str, Any]:
    """使用应用级采样器返回一轮短周期资源快照。"""

    return resource_sampler.sample()
