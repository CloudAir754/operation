"""远端探针：一次性 Python 输入，不安装常驻 Agent。

探针脚本以 base64 传输，避免远端 shell 引号差异；输出为单个 JSON 文档，
每个分区都带 status/error，缺少权限或硬件能力时绝不伪造零值。
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import threading
from typing import Any

from lab_ops_panel.remote.diagnosis import diagnose_result
from lab_ops_panel.ssh.transport import CommandResult, SshTarget, SshTransport

PROBE_VERSION = 1
PROBE_MODES = ("snapshot", "services")

PROBE_SOURCE = r"""
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone

VERSION = 1
MAX_UNITS = 200
MAX_MOUNTS = 40
SKIP_FILESYSTEMS = {
    "proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "cgroup", "cgroup2",
    "securityfs", "pstore", "autofs", "debugfs", "tracefs", "hugetlbfs",
    "mqueue", "configfs", "fusectl", "bpf", "rpc_pipefs", "nfsd",
    "selinuxfs", "binfmt_misc", "efivarfs", "fuse.gvfsd-fuse", "snapd",
}


def read_text(path, limit=262144):
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read(limit)
    except Exception:
        return ""


def section(status="ok", error=None, data=None):
    return {"status": status, "error": error, "data": data}


def failed(status, exc):
    return section(status, str(exc)[:300], None)


def to_number(value):
    text = str(value).strip()
    if not text or text.upper() in ("N/A", "[N/A]", "NA"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else round(number, 1)


def to_iso(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_system():
    try:
        release = {}
        for line in read_text("/etc/os-release").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            release[key.strip()] = value.strip().strip('"').strip("'")
        data = {
            "hostname": platform.node() or None,
            "os": release.get("PRETTY_NAME") or release.get("NAME"),
            "kernel": platform.release() or None,
            "arch": platform.machine() or None,
            "uptimeSeconds": None,
            "bootTime": None,
        }
        uptime = read_text("/proc/uptime").split()
        if uptime:
            seconds = int(float(uptime[0]))
            data["uptimeSeconds"] = seconds
            data["bootTime"] = to_iso(time.time() - seconds)
        return section(data=data)
    except Exception as exc:
        return failed("error", exc)


def collect_cpu():
    try:
        model = None
        for line in read_text("/proc/cpuinfo").splitlines():
            if line.lower().startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
        usage = None
        try:
            first = read_text("/proc/stat").splitlines()[0].split()
            values = [int(item) for item in first[1:9]]
            idle_start = values[3] + (values[4] if len(values) > 4 else 0)
            total_start = sum(values)
            time.sleep(0.15)
            second = read_text("/proc/stat").splitlines()[0].split()
            values2 = [int(item) for item in second[1:9]]
            idle_end = values2[3] + (values2[4] if len(values2) > 4 else 0)
            total_end = sum(values2)
            if total_end > total_start:
                percent = (1 - (idle_end - idle_start) / (total_end - total_start)) * 100
                usage = round(max(0.0, min(100.0, percent)), 1)
        except Exception:
            usage = None
        load = (None, None, None)
        if hasattr(os, "getloadavg"):
            try:
                load = os.getloadavg()
            except OSError:
                pass
        data = {
            "model": model,
            "cores": os.cpu_count(),
            "usagePercent": usage,
            "load1": round(load[0], 2) if load[0] is not None else None,
            "load5": round(load[1], 2) if load[1] is not None else None,
            "load15": round(load[2], 2) if load[2] is not None else None,
        }
        return section(data=data)
    except Exception as exc:
        return failed("error", exc)


def collect_memory():
    try:
        info = {}
        for line in read_text("/proc/meminfo").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            parts = value.split()
            if parts:
                try:
                    info[key.strip()] = int(parts[0]) * 1024
                except ValueError:
                    continue
        total = info.get("MemTotal")
        available = info.get("MemAvailable")
        if available is None:
            available = info.get("MemFree")
        used_percent = None
        if total and available is not None:
            used_percent = round((total - available) / total * 100, 1)
        swap_total = info.get("SwapTotal") or 0
        swap_free = info.get("SwapFree") or 0
        data = {
            "totalBytes": total,
            "availableBytes": available,
            "usedPercent": used_percent,
            "swapTotalBytes": swap_total,
            "swapUsedBytes": max(0, swap_total - swap_free),
        }
        return section(data=data)
    except Exception as exc:
        return failed("error", exc)


def collect_disks():
    try:
        mounts = []
        seen = set()
        for line in read_text("/proc/self/mounts").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mountpoint, filesystem = parts[0], parts[1].replace("\\040", " "), parts[2]
            if filesystem in SKIP_FILESYSTEMS:
                continue
            try:
                stats = os.statvfs(mountpoint)
            except OSError:
                continue
            total = stats.f_blocks * stats.f_frsize
            if total <= 0:
                continue
            free = stats.f_bavail * stats.f_frsize
            used = total - stats.f_bfree * stats.f_frsize
            key = (device, mountpoint)
            if key in seen:
                continue
            seen.add(key)
            mounts.append(
                {
                    "mount": mountpoint,
                    "filesystem": device,
                    "totalBytes": total,
                    "usedBytes": used,
                    "freeBytes": free,
                    "usedPercent": round(used / total * 100, 1) if total else None,
                }
            )
            if len(mounts) >= MAX_MOUNTS:
                break
        mounts.sort(key=lambda item: (item["mount"] != "/", item["mount"]))
        return section(data={"mounts": mounts})
    except Exception as exc:
        return failed("error", exc)


def ipv4_addresses(name):
    try:
        import fcntl
    except Exception:
        return []
    addresses = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = struct.pack("256s", name[:15].encode("utf-8"))
        try:
            result = fcntl.ioctl(sock.fileno(), 0x8915, request)
        except OSError:
            return addresses
        address = socket.inet_ntoa(result[20:24])
        try:
            mask = fcntl.ioctl(sock.fileno(), 0x891B, request)[20:24]
            prefix = bin(struct.unpack("!I", mask)[0]).count("1")
            addresses.append("%s/%d" % (address, prefix))
        except OSError:
            addresses.append(address)
    finally:
        sock.close()
    return addresses


def ipv6_addresses(name):
    addresses = []
    for line in read_text("/proc/net/if_inet6").splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[5] != name:
            continue
        raw = parts[0]
        groups = [raw[index:index + 4] for index in range(0, len(raw), 4)]
        prefix = int(parts[2], 16)
        addresses.append("%s/%d" % (":".join(groups), prefix))
    return addresses


def collect_network():
    try:
        counters = {}
        for line in read_text("/proc/net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            name, values = line.split(":", 1)
            fields = values.split()
            if len(fields) < 11:
                continue
            counters[name.strip()] = {
                "rxBytes": int(fields[0]),
                "rxErrors": int(fields[2]),
                "txBytes": int(fields[8]),
                "txErrors": int(fields[10]),
            }
        gateway = None
        default_interface = None
        for line in read_text("/proc/net/route").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 8 or fields[1] != "00000000":
                continue
            flags = int(fields[3], 16)
            if not flags & 0x0002:
                continue
            default_interface = fields[0]
            try:
                gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
            except Exception:
                gateway = None
            break
        interfaces = []
        names = sorted(counters)
        for name in names:
            if name == "lo":
                continue
            state = read_text("/sys/class/net/%s/operstate" % name).strip() or None
            speed = None
            try:
                speed = int(read_text("/sys/class/net/%s/speed" % name).strip())
            except (TypeError, ValueError):
                speed = None
            entry = {
                "name": name,
                "state": state,
                "speedMbps": speed,
                "rxBytes": counters[name]["rxBytes"],
                "txBytes": counters[name]["txBytes"],
                "rxErrors": counters[name]["rxErrors"],
                "txErrors": counters[name]["txErrors"],
                "addresses": ipv4_addresses(name) + ipv6_addresses(name),
            }
            interfaces.append(entry)
        interfaces.sort(key=lambda item: (item["state"] != "up", item["name"]))
        return section(data={
            "defaultInterface": default_interface,
            "gateway": gateway,
            "interfaces": interfaces,
        })
    except Exception as exc:
        return failed("error", exc)


GPU_FIELDS = (
    "index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,driver_version"
)


def collect_gpu():
    binary = shutil.which("nvidia-smi")
    if not binary:
        return section("unavailable", "未检测到 nvidia-smi（可能未安装 NVIDIA 驱动）", None)
    try:
        result = subprocess.run(
            [binary, "--query-gpu=" + GPU_FIELDS, "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return failed("error", exc)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        return section("error", (message or "nvidia-smi 执行失败")[:300], None)
    devices = []
    for line in result.stdout.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 7:
            continue
        index = parts[0]
        devices.append(
            {
                "index": int(index) if index.isdigit() else len(devices),
                "name": parts[1] or None,
                "memoryTotalMb": to_number(parts[2]),
                "memoryUsedMb": to_number(parts[3]),
                "utilizationPercent": to_number(parts[4]),
                "temperatureC": to_number(parts[5]),
                "driverVersion": parts[6] or None,
            }
        )
    return section(data={"devices": devices})


def collect_services():
    binary = shutil.which("systemctl")
    if not binary:
        return section("unavailable", "未检测到 systemctl（可能不是 systemd 系统）", None)
    try:
        result = subprocess.run(
            [binary, "list-units", "--type=service", "--all", "--no-pager", "--plain", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=12,
        )
    except Exception as exc:
        return failed("error", exc)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()[:300]
        lowered = message.lower()
        if "permission" in lowered or "access denied" in lowered or "authentication" in lowered:
            return section("permission_denied", message, None)
        return section("error", message or "systemctl 执行失败", None)
    units = []
    running = 0
    failed_count = 0
    failed_units = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 4)
        if len(parts) < 4:
            continue
        name, load, active, sub = parts[0], parts[1], parts[2], parts[3]
        description = parts[4] if len(parts) > 4 else ""
        if active == "active":
            running += 1
        if active == "failed":
            failed_count += 1
            if len(failed_units) < 50:
                failed_units.append(name)
        if len(units) < MAX_UNITS:
            units.append(
                {
                    "name": name,
                    "load": load,
                    "active": active,
                    "sub": sub,
                    "description": description[:200],
                    "protected": False,
                }
            )
    data = {
        "runningCount": running,
        "failedCount": failed_count,
        "failedUnits": failed_units,
        "units": units,
    }
    return section(data=data)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    collected_at = to_iso(time.time())
    if mode == "services":
        payload = {
            "probeVersion": VERSION,
            "collectedAt": collected_at,
            "services": collect_services(),
        }
    else:
        payload = {
            "probeVersion": VERSION,
            "collectedAt": collected_at,
            "sections": {
                "system": collect_system(),
                "cpu": collect_cpu(),
                "memory": collect_memory(),
                "disks": collect_disks(),
                "network": collect_network(),
                "gpu": collect_gpu(),
                "services": collect_services(),
            },
        }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
"""

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class ProbeError(RuntimeError):
    """探针执行失败或输出不可解析。"""


def build_probe_command(mode: str = "snapshot") -> str:
    if mode not in PROBE_MODES:
        raise ValueError(f"未知探针模式：{mode}")
    encoded = base64.b64encode(PROBE_SOURCE.encode("utf-8")).decode("ascii")
    return (
        "if [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; else PY=python3; fi; "
        f'printf %s {shlex.quote(encoded)} | base64 -d | "$PY" - {mode}'
    )


def parse_probe_payload(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise ProbeError("探针没有输出")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT.search(text)
        if match is None:
            raise ProbeError(f"探针输出不是 JSON：{text[:200]}") from None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ProbeError(f"探针输出不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ProbeError("探针输出格式不正确")
    version = payload.get("probeVersion")
    if version != PROBE_VERSION:
        raise ProbeError(f"探针协议版本不匹配：{version!r}")
    return payload


def _require_ok(result: CommandResult, operation: str) -> None:
    if result.cancelled:
        raise ProbeError(f"{operation}已取消")
    if result.timed_out:
        raise ProbeError(f"{operation}超时")
    if result.exit_code != 0:
        raise ProbeError(diagnose_result(result, operation=operation))
    if result.truncated:
        raise ProbeError(f"{operation}输出超出上限，已拒绝解析")


def run_probe(
    transport: SshTransport,
    target: SshTarget,
    *,
    mode: str = "snapshot",
    timeout: float = 45.0,
    cancel_event: threading.Event | None = None,
    max_output_bytes: int | None = None,
    operation: str = "状态采集",
) -> dict[str, Any]:
    command = build_probe_command(mode)
    result = transport.run(
        target,
        command,
        timeout=timeout,
        cancel_event=cancel_event,
        max_output_bytes=max_output_bytes,
    )
    _require_ok(result, operation)
    return parse_probe_payload(result.stdout)
