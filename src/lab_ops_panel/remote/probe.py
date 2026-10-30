"""远端探针：一次性 Python 输入，不安装常驻 Agent。

探针脚本以 base64 传输，避免远端 shell 引号差异；输出为单个 JSON 文档，
每个分区都带 status/error，缺少权限或硬件能力时绝不伪造零值。

模式：
- ``snapshot`` 系统/资源/磁盘/网络/GPU/服务总览
- ``services`` 服务列表
- ``metrics`` 指标原始累计计数（由后端做差分与归类）
- ``gpu`` GPU 设备与进程
- ``users`` 可登录用户、sudo 三态与 home 摘要
- ``mounts`` 挂载点清单（扫描 allowlist 来源）
- ``storage`` 有界目录树扫描
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

PROBE_VERSION = 2
PROBE_MODES = ("snapshot", "services", "metrics", "gpu", "users", "mounts", "storage")

PROBE_SOURCE = r"""
import csv
import io
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

VERSION = 2
MAX_UNITS = 200
MAX_MOUNTS = 40
MAX_DEVICES = 200
MAX_INTERFACES = 200
MAX_PROCESSES = 200
MAX_USERS = 500
MAX_SCAN_NODES = 1200
MAX_DELETED_OPEN = 100
MAX_TEXT = 300
SKIP_FILESYSTEMS = set([
    "proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "cgroup", "cgroup2",
    "securityfs", "pstore", "autofs", "debugfs", "tracefs", "hugetlbfs",
    "mqueue", "configfs", "fusectl", "bpf", "rpc_pipefs", "nfsd",
    "selinuxfs", "binfmt_misc", "efivarfs", "fuse.gvfsd-fuse", "snapd",
])


def read_text(path, limit=262144):
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read(limit)
    except Exception:
        return ""


def section(status="ok", error=None, data=None, requires_sudo=False):
    return {
        "status": status,
        "error": error,
        "requiresSudo": requires_sudo,
        "data": data,
    }


def failed(status, exc, requires_sudo=False):
    return section(status, str(exc)[:300], None, requires_sudo)


def to_number(value):
    text = str(value).strip()
    if not text or text.upper() in ("N/A", "[N/A]", "NA"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else round(number, 2)


def to_int(value):
    text = str(value).strip()
    if not text or text.upper() in ("N/A", "[N/A]", "NA"):
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def to_iso(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unescape_mount(value):
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


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
        return section(data={
            "model": model,
            "cores": os.cpu_count(),
            "usagePercent": usage,
            "load1": round(load[0], 2) if load[0] is not None else None,
            "load5": round(load[1], 2) if load[1] is not None else None,
            "load15": round(load[2], 2) if load[2] is not None else None,
        })
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
        return section(data={
            "totalBytes": total,
            "availableBytes": available,
            "usedPercent": used_percent,
            "swapTotalBytes": swap_total,
            "swapUsedBytes": max(0, swap_total - swap_free),
        })
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
            device, mountpoint, filesystem = parts[0], unescape_mount(parts[1]), parts[2]
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
            mounts.append({
                "mount": mountpoint,
                "filesystem": device,
                "totalBytes": total,
                "usedBytes": used,
                "freeBytes": free,
                "usedPercent": round(used / total * 100, 1) if total else None,
            })
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


def interface_kind(name):
    if name == "lo":
        return "loopback"
    base = "/sys/class/net/%s" % name
    if os.path.isdir(os.path.join(base, "bridge")):
        return "bridge"
    if os.path.isdir(os.path.join(base, "bonding")):
        return "bond"
    if "." in name:
        return "vlan"
    if os.path.exists(os.path.join(base, "device")):
        return "physical"
    return "virtual"


def network_meta(name):
    base = "/sys/class/net/%s" % name
    state = read_text(os.path.join(base, "operstate")).strip() or None
    speed = None
    try:
        speed = int(read_text(os.path.join(base, "speed")).strip())
    except (TypeError, ValueError):
        speed = None
    master = None
    try:
        master = os.path.basename(os.readlink(os.path.join(base, "master")))
    except OSError:
        master = None
    return {
        "kind": interface_kind(name),
        "state": state,
        "speedMbps": speed,
        "master": master,
        "addresses": ipv4_addresses(name) + ipv6_addresses(name),
    }


def default_interface():
    gateway = None
    interface = None
    for line in read_text("/proc/net/route").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
        except ValueError:
            continue
        if not flags & 0x0002:
            continue
        interface = fields[0]
        try:
            gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
        except Exception:
            gateway = None
        break
    return interface, gateway


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
        interface, gateway = default_interface()
        interfaces = []
        for name in sorted(counters):
            if name == "lo":
                continue
            meta = network_meta(name)
            interfaces.append({
                "name": name,
                "state": meta["state"],
                "speedMbps": meta["speedMbps"],
                "rxBytes": counters[name]["rxBytes"],
                "txBytes": counters[name]["txBytes"],
                "rxErrors": counters[name]["rxErrors"],
                "txErrors": counters[name]["txErrors"],
                "addresses": meta["addresses"],
            })
        interfaces.sort(key=lambda item: (item["state"] != "up", item["name"]))
        return section(data={
            "defaultInterface": interface,
            "gateway": gateway,
            "interfaces": interfaces,
        })
    except Exception as exc:
        return failed("error", exc)


SNAPSHOT_GPU_FIELDS = "index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,driver_version"


def collect_gpu():
    binary = shutil.which("nvidia-smi")
    if not binary:
        return section("unavailable", "未检测到 nvidia-smi（可能未安装 NVIDIA 驱动）", None)
    try:
        result = subprocess.run(
            [binary, "--query-gpu=" + SNAPSHOT_GPU_FIELDS, "--format=csv,noheader,nounits"],
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
        devices.append({
            "index": int(index) if index.isdigit() else len(devices),
            "name": parts[1] or None,
            "memoryTotalMb": to_number(parts[2]),
            "memoryUsedMb": to_number(parts[3]),
            "utilizationPercent": to_number(parts[4]),
            "temperatureC": to_number(parts[5]),
            "driverVersion": parts[6] or None,
        })
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
            units.append({
                "name": name,
                "load": load,
                "active": active,
                "sub": sub,
                "description": description[:200],
                "protected": False,
            })
    return section(data={
        "runningCount": running,
        "failedCount": failed_count,
        "failedUnits": failed_units,
        "units": units,
    })


def read_meminfo():
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
    return info


def read_vmstat():
    values = {}
    for line in read_text("/proc/vmstat").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return values


def block_device_kind(name):
    if os.path.isdir("/sys/block/%s" % name):
        return "disk", None
    parents = os.listdir("/sys/block") if os.path.isdir("/sys/block") else []
    for parent in parents:
        if os.path.isdir("/sys/block/%s/%s" % (parent, name)):
            return "partition", parent
    if name.startswith(("dm-", "md", "loop", "ram", "sr")):
        return "virtual", None
    return "unknown", None


def disk_meta(name):
    kind, parent = block_device_kind(name)
    base = "/sys/block/%s" % (parent or name)
    model = read_text(os.path.join(base, "device", "model")).strip() or None
    size_bytes = None
    raw_size = read_text(os.path.join(base, "size")).strip()
    if raw_size.isdigit():
        size_bytes = int(raw_size) * 512
    hidden = name.startswith(("loop", "ram", "sr", "dm-", "md"))
    return {
        "kind": kind,
        "parent": parent,
        "model": model,
        "sizeBytes": size_bytes,
        "hidden": hidden,
    }


def collect_metrics():
    try:
        cpu = {}
        for line in read_text("/proc/stat").splitlines():
            parts = line.split()
            if parts and parts[0] == "cpu":
                values = [int(value) for value in parts[1:]]
                cpu = {
                    "user": values[0] if len(values) > 0 else None,
                    "nice": values[1] if len(values) > 1 else None,
                    "system": values[2] if len(values) > 2 else None,
                    "idle": values[3] if len(values) > 3 else None,
                    "iowait": values[4] if len(values) > 4 else None,
                    "irq": values[5] if len(values) > 5 else None,
                    "softirq": values[6] if len(values) > 6 else None,
                    "steal": values[7] if len(values) > 7 else None,
                    "logicalCpus": os.cpu_count(),
                }
                break
        if not cpu:
            return failed("error", "无法读取 /proc/stat")
        try:
            load = os.getloadavg()
        except Exception:
            load = (None, None, None)
        cpu["load1"] = round(load[0], 2) if load[0] is not None else None
        cpu["load5"] = round(load[1], 2) if load[1] is not None else None
        cpu["load15"] = round(load[2], 2) if load[2] is not None else None

        disk = {}
        disk_meta_map = {}
        for line in read_text("/proc/diskstats").splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            try:
                disk[name] = {
                    "readsCompleted": int(parts[3]),
                    "sectorsRead": int(parts[5]),
                    "msReading": int(parts[6]),
                    "writesCompleted": int(parts[7]),
                    "sectorsWritten": int(parts[9]),
                    "msWriting": int(parts[10]),
                    "ioMs": int(parts[12]),
                }
            except ValueError:
                continue
            if len(disk_meta_map) < MAX_DEVICES:
                disk_meta_map[name] = disk_meta(name)

        network = {}
        network_meta_map = {}
        for line in read_text("/proc/net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            name, raw = line.split(":", 1)
            parts = raw.split()
            if len(parts) < 16:
                continue
            name = name.strip()
            try:
                network[name] = {
                    "rxBytes": int(parts[0]),
                    "rxPackets": int(parts[1]),
                    "rxErrors": int(parts[2]),
                    "rxDrops": int(parts[3]),
                    "txBytes": int(parts[8]),
                    "txPackets": int(parts[9]),
                    "txErrors": int(parts[10]),
                    "txDrops": int(parts[11]),
                }
            except ValueError:
                continue
            if len(network_meta_map) < MAX_INTERFACES:
                network_meta_map[name] = network_meta(name)

        mounts = []
        for line in read_text("/proc/self/mounts").splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[2] in SKIP_FILESYSTEMS:
                continue
            device = parts[0]
            device_name = os.path.basename(device) if device.startswith("/dev/") else None
            mounts.append({
                "mount": unescape_mount(parts[1]),
                "device": device,
                "filesystem": parts[2],
                "deviceName": device_name,
            })
            if len(mounts) >= MAX_MOUNTS:
                break

        gpu_section = collect_gpu()
        interface, _gateway = default_interface()
        return section(data={
            "cpu": cpu,
            "memory": read_meminfo(),
            "vmstat": read_vmstat(),
            "disk": disk,
            "diskMeta": disk_meta_map,
            "mounts": mounts,
            "network": network,
            "networkMeta": network_meta_map,
            "defaultInterface": interface,
            "gpu": gpu_section.get("data"),
            "gpuStatus": gpu_section.get("status"),
            "gpuError": gpu_section.get("error"),
        })
    except Exception as exc:
        return failed("error", exc)


GPU_CORE_FIELDS = [
    "index", "uuid", "pci.bus_id", "name", "driver_version",
    "memory.total", "memory.used", "utilization.gpu", "temperature.gpu",
    "mig.mode.current",
]
GPU_EXTRA_FIELDS = [
    "utilization.memory", "utilization.encoder", "utilization.decoder",
    "power.draw", "power.limit", "clocks.current.graphics",
    "clocks.current.memory", "compute_mode",
]


def nvidia_rows(binary, fields):
    result = subprocess.run(
        [binary, "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "nvidia-smi 查询失败").strip()[:300])
    return [row for row in csv.reader(io.StringIO(result.stdout)) if row]


def nvidia_devices(binary):
    fields = GPU_CORE_FIELDS + GPU_EXTRA_FIELDS
    extra = True
    try:
        rows = nvidia_rows(binary, fields)
    except Exception:
        extra = False
        rows = nvidia_rows(binary, GPU_CORE_FIELDS)
    devices = []
    for row in rows:
        if len(row) < len(GPU_CORE_FIELDS):
            continue
        values = [item.strip() for item in row]
        index = to_int(values[0])
        if index is None:
            continue
        device = {
            "index": index,
            "uuid": values[1] or None,
            "pciBusId": values[2] or None,
            "name": values[3] or None,
            "driverVersion": values[4] or None,
            "memoryTotalMb": to_number(values[5]),
            "memoryUsedMb": to_number(values[6]),
            "utilizationGpuPercent": to_number(values[7]),
            "temperatureC": to_number(values[8]),
            "migMode": values[9] or None,
            "utilizationMemoryPercent": None,
            "encoderPercent": None,
            "decoderPercent": None,
            "powerDrawW": None,
            "powerLimitW": None,
            "clockGraphicsMhz": None,
            "clockMemoryMhz": None,
            "computeMode": None,
            "migDevices": [],
        }
        if extra and len(values) >= len(fields):
            extras = values[len(GPU_CORE_FIELDS):]
            device["utilizationMemoryPercent"] = to_number(extras[0])
            device["encoderPercent"] = to_number(extras[1])
            device["decoderPercent"] = to_number(extras[2])
            device["powerDrawW"] = to_number(extras[3])
            device["powerLimitW"] = to_number(extras[4])
            device["clockGraphicsMhz"] = to_number(extras[5])
            device["clockMemoryMhz"] = to_number(extras[6])
            device["computeMode"] = extras[7] or None
        devices.append(device)
    return devices[:32]


def nvidia_mig(binary):
    mapping = {}
    try:
        result = subprocess.run([binary, "-L"], capture_output=True, text=True, timeout=8)
    except Exception:
        return mapping
    if result.returncode != 0:
        return mapping
    current = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("GPU "):
            parts = stripped.split()
            try:
                current = int(parts[1].rstrip(":"))
            except (ValueError, IndexError):
                current = None
            continue
        if "MIG" in stripped and "Device" in stripped and current is not None:
            mapping.setdefault(current, []).append(stripped[:MAX_TEXT])
    return mapping


def proc_start_time(pid, boot_time):
    try:
        stat = read_text("/proc/%d/stat" % pid, 8192)
        if not stat:
            return None
        rest = stat.rsplit(")", 1)[1].split()
        ticks = int(rest[19])
        clock = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        if boot_time is None:
            return None
        return boot_time + ticks / float(clock or 100)
    except Exception:
        return None


def proc_owner(pid):
    try:
        import pwd
        stat = os.stat("/proc/%d" % pid)
        return pwd.getpwuid(stat.st_uid).pw_name
    except Exception:
        return None


def proc_cmdline(pid):
    try:
        value = read_text("/proc/%d/cmdline" % pid, 600)
        cleaned = value.replace("\x00", " ").strip()
        return cleaned[:300] or None
    except Exception:
        return None


def boot_time():
    for line in read_text("/proc/stat").splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                return None
    return None


def gpu_processes(binary, devices):
    uuid_to_index = {}
    for device in devices:
        if device.get("uuid"):
            uuid_to_index[device["uuid"]] = device["index"]
    try:
        result = subprocess.run(
            [binary, "--query-compute-apps=pid,process_name,used_memory,gpu_uuid",
             "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return [], "unavailable", str(exc)[:300]
    if result.returncode != 0:
        return [], "unavailable", (result.stderr or result.stdout or "").strip()[:300]
    processes = []
    partial = False
    started_boot = boot_time()
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 4:
            continue
        values = [item.strip() for item in row]
        pid = to_int(values[0])
        if pid is None or pid <= 0:
            continue
        uuid = values[3] or None
        exists = os.path.exists("/proc/%d" % pid)
        entry = {
            "pid": pid,
            "user": proc_owner(pid),
            "name": values[1] or None,
            "command": proc_cmdline(pid),
            "memoryUsedMb": to_number(values[2]),
            "utilizationPercent": None,
            "gpuUuid": uuid,
            "gpuIndex": uuid_to_index.get(uuid),
            "programPath": None,
            "cwd": None,
            "startedAtEpoch": proc_start_time(pid, started_boot),
            "stale": not exists,
            "error": None if exists else "进程在采样后已退出",
        }
        try:
            entry["programPath"] = os.readlink("/proc/%d/exe" % pid)[:300]
        except OSError:
            partial = True
        try:
            entry["cwd"] = os.readlink("/proc/%d/cwd" % pid)[:300]
        except OSError:
            pass
        if not exists:
            partial = True
        processes.append(entry)
        if len(processes) >= MAX_PROCESSES:
            break
    processes.sort(key=lambda item: (-(item.get("memoryUsedMb") or 0), item["pid"]))
    status = "partial" if partial or not processes else "ok"
    return processes, status, None


def collect_gpu_monitor():
    binary = shutil.which("nvidia-smi")
    if not binary:
        return section(
            "unavailable",
            "未检测到 nvidia-smi（可能未安装 NVIDIA 驱动）",
            {
                "devices": [],
                "processes": [],
                "processesStatus": "unavailable",
                "processesError": "未检测到 nvidia-smi",
                "capabilities": {
                    "nvml": False,
                    "processes": "unavailable",
                    "mig": False,
                    "perProcessUtilization": False,
                },
                "notes": ["NVIDIA 工具不可用；AMD/Intel GPU 适配器尚未实现"],
            },
        )
    try:
        devices = nvidia_devices(binary)
    except Exception as exc:
        return failed("error", exc)
    mig = nvidia_mig(binary)
    for device in devices:
        device["migDevices"] = mig.get(device["index"], [])
    processes, processes_status, processes_error = gpu_processes(binary, devices)
    notes = []
    if any(device.get("migDevices") for device in devices):
        notes.append("MIG 模式下进程可能属于某个 GPU 实例")
    if processes_status == "partial":
        notes.append("部分进程信息不可见（hidepid、容器或进程已退出）")
    return section(data={
        "devices": devices,
        "processes": processes,
        "processesStatus": processes_status,
        "processesError": processes_error,
        "capabilities": {
            "nvml": True,
            "processes": processes_status,
            "mig": any(
                bool(device.get("migMode") and device.get("migMode") != "Disabled")
                for device in devices
            ),
            "perProcessUtilization": False,
        },
        "notes": notes,
    })


def parse_passwd():
    users = []
    for line in read_text("/etc/passwd").splitlines():
        parts = line.split(":")
        if len(parts) < 7:
            continue
        try:
            uid = int(parts[2])
            gid = int(parts[3])
        except ValueError:
            continue
        users.append({
            "login": parts[0],
            "uid": uid,
            "gid": gid,
            "home": parts[5],
            "shell": parts[6],
        })
    return users


def parse_groups():
    groups = {}
    for line in read_text("/etc/group").splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        groups[parts[0]] = {
            "gid": to_int(parts[2]),
            "members": [item for item in parts[3].split(",") if item],
        }
    return groups


def home_storage(home, depth=2, cap=20000, deadline=2.0):
    stats = {
        "allocatedBytes": 0,
        "apparentBytes": 0,
        "fileCount": 0,
        "directoryCount": 0,
        "depth": depth,
        "truncated": False,
        "incomplete": False,
        "error": None,
    }
    started = time.time()
    stack = [(home, 0)]
    visited = 0
    try:
        while stack:
            path, level = stack.pop()
            try:
                with os.scandir(path) as handle:
                    entries = list(handle)
            except OSError as exc:
                stats["incomplete"] = True
                stats["error"] = str(exc)[:200]
                continue
            for entry in entries:
                if visited >= cap:
                    stats["truncated"] = True
                    return stats
                if time.time() - started > deadline:
                    stats["truncated"] = True
                    return stats
                visited += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    stats["incomplete"] = True
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stats["directoryCount"] += 1
                    if level < depth:
                        stack.append((entry.path, level + 1))
                    else:
                        stats["truncated"] = True
                else:
                    stats["fileCount"] += 1
                    stats["apparentBytes"] += int(info.st_size)
                    stats["allocatedBytes"] += int(getattr(info, "st_blocks", 0) * 512)
    except Exception as exc:
        stats["incomplete"] = True
        stats["error"] = str(exc)[:200]
    return stats


def sudo_policy_present(login, groups):
    readable = False
    lines = []
    sudoers = read_text("/etc/sudoers", 131072)
    if sudoers:
        readable = True
        lines.extend(sudoers.splitlines())
    try:
        entries = os.listdir("/etc/sudoers.d")
    except OSError:
        entries = []
    for entry in entries[:200]:
        text = read_text(os.path.join("/etc/sudoers.d", entry), 65536)
        if text:
            readable = True
            lines.extend(text.splitlines())
    if not readable:
        return None
    names = set([login])
    for name, group in groups.items():
        if login in group["members"]:
            names.add("%" + name)
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        first = stripped.split()[0]
        if first in names:
            return True
    return False


def sudo_effective(login, is_self):
    if not is_self:
        return "unknown"
    binary = shutil.which("sudo")
    if not binary:
        return "unknown"
    try:
        result = subprocess.run([binary, "-n", "true"], capture_output=True, timeout=2)
        return "true" if result.returncode == 0 else "false"
    except Exception:
        return "unknown"


def collect_users(include_system=False):
    try:
        threshold = 1000
        groups = parse_groups()
        passwd = parse_passwd()
        self_login = None
        try:
            import pwd
            self_login = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            self_login = None
        users = []
        for entry in passwd:
            uid = entry["uid"]
            shell = entry["shell"]
            if uid < threshold and not include_system:
                continue
            if shell in ("", "/usr/sbin/nologin", "/sbin/nologin", "/usr/bin/nologin",
                         "/bin/false", "/usr/bin/false", "/bin/sync"):
                continue
            if len(users) >= MAX_USERS:
                break
            supplementary = [
                name for name, group in groups.items() if entry["login"] in group["members"]
            ]
            primary = next(
                (name for name, group in groups.items() if group["gid"] == entry["gid"]),
                None,
            )
            group_member = any(
                entry["login"] in groups.get(name, {}).get("members", [])
                for name in ("sudo", "wheel")
            )
            users.append({
                "login": entry["login"],
                "uid": uid,
                "gid": entry["gid"],
                "home": entry["home"],
                "shell": shell,
                "system": uid < threshold,
                "primaryGroup": primary,
                "supplementaryGroups": supplementary[:64],
                "sudoGroupMember": group_member,
                "sudoPolicyPresent": sudo_policy_present(entry["login"], groups),
                "sudoEffective": sudo_effective(entry["login"], entry["login"] == self_login),
                "recentLogin": None,
                "homeStorage": (
                    home_storage(entry["home"]) if os.path.isdir(entry["home"]) else None
                ),
            })
        notes = []
        if self_login is None:
            notes.append("无法确定当前 SSH 用户，sudo 有效性统一标记为 unknown")
        notes.append("sudo 有效权限仅对当前 SSH 用户执行非交互验证，其他用户为 unknown")
        notes.append("最近登录未采集（远端 lastlog 可能不可读）")
        return section(data={"thresholdUid": threshold, "users": users, "notes": notes})
    except Exception as exc:
        return failed("error", exc)


def mount_table():
    entries = []
    for line in read_text("/proc/self/mounts").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        entries.append({
            "device": parts[0],
            "mount": unescape_mount(parts[1]),
            "filesystem": parts[2],
            "options": parts[3],
        })
    return entries


def uuid_map():
    mapping = {}
    try:
        names = os.listdir("/dev/disk/by-uuid")
    except OSError:
        return mapping
    for name in names[:500]:
        try:
            mapping[os.path.realpath(os.path.join("/dev/disk/by-uuid", name))] = name
        except OSError:
            continue
    return mapping


def collect_mounts():
    try:
        uuids = uuid_map()
        mounts = []
        for entry in mount_table():
            if entry["filesystem"] in SKIP_FILESYSTEMS:
                continue
            device = entry["device"]
            major_minor = None
            physical = None
            if device.startswith("/dev/"):
                physical = os.path.basename(device)
                try:
                    info = os.stat(device)
                    major_minor = "%d:%d" % (os.major(info.st_rdev), os.minor(info.st_rdev))
                except OSError:
                    major_minor = None
            mounts.append({
                "mount": entry["mount"],
                "device": device,
                "filesystem": entry["filesystem"],
                "options": entry["options"],
                "uuid": (
                    uuids.get(os.path.realpath(device)) if device.startswith("/dev/") else None
                ),
                "majorMinor": major_minor,
                "physicalDevice": physical,
                "readOnly": "ro" in entry["options"].split(","),
            })
            if len(mounts) >= MAX_MOUNTS:
                break
        mounts.sort(key=lambda item: item["mount"])
        return section(data={"mounts": mounts})
    except Exception as exc:
        return failed("error", exc)


def deleted_open_files(mount, limit):
    results = []
    seen = set()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return results
    prefix = mount.rstrip("/") + "/"
    for pid in pids:
        if not pid.isdigit():
            continue
        fd_dir = "/proc/%s/fd" % pid
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds[:64]:
            if len(results) >= limit:
                return results
            fd_path = os.path.join(fd_dir, fd)
            try:
                target = os.readlink(fd_path)
            except OSError:
                continue
            if not target.endswith(" (deleted)"):
                continue
            path = target[:-10]
            if not path.startswith(prefix):
                continue
            try:
                info = os.stat(fd_path)
            except OSError:
                continue
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "path": path,
                "name": os.path.basename(path) or path,
                "allocatedBytes": int(getattr(info, "st_blocks", 0) * 512),
                "apparentBytes": int(info.st_size),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "device": info.st_dev,
                "inode": int(info.st_ino),
                "pid": int(pid),
            })
    return results


def cross_device(candidate_device, root_device):
    # 某些平台（如 Windows 的 DirEntry.stat）不返回 st_dev，此时按同设备处理。
    if not candidate_device or not root_device:
        return False
    return candidate_device != root_device


def collect_storage(mount, max_depth=3, follow_symlinks=False, one_file_system=True,
                    include_deleted_open=True, node_cap=MAX_SCAN_NODES):
    try:
        entries = mount_table()
        match = None
        for entry in entries:
            if entry["mount"] == mount:
                match = entry
                break
        if match is None:
            return section("permission_denied", "路径不是已发现的挂载点，已拒绝扫描", None)
        root_path = os.path.abspath(mount)
        try:
            root_info = os.stat(root_path)
        except OSError as exc:
            return section("permission_denied", str(exc)[:300], None)
        root_device = root_info.st_dev
        nodes = []
        by_path = {}
        seen_inodes = {}
        visited_dirs = set([(root_info.st_dev, root_info.st_ino)])
        state = {"truncated": False, "cross": 0, "incomplete": False}

        def add_node(path, info, kind, unscanned=False, incomplete=False, error=None,
                     hardlink_duplicate_of=None):
            node = {
                "path": path,
                "name": os.path.basename(path) or path,
                "kind": kind,
                "allocatedBytes": int(getattr(info, "st_blocks", 0) * 512),
                "apparentBytes": int(info.st_size),
                "uid": getattr(info, "st_uid", None),
                "gid": getattr(info, "st_gid", None),
                "device": getattr(info, "st_dev", None),
                "inode": int(getattr(info, "st_ino", 0)),
                "modifiedAtEpoch": float(info.st_mtime),
                "hardlinkDuplicateOf": hardlink_duplicate_of,
                "deletedOpen": False,
                "unscanned": unscanned,
                "incomplete": incomplete,
                "error": error,
            }
            nodes.append(node)
            by_path[path] = node
            return node

        add_node(root_path, root_info, "dir")
        stack = [(root_path, 0)]
        while stack:
            path, depth = stack.pop()
            if len(nodes) >= node_cap:
                state["truncated"] = True
                break
            try:
                with os.scandir(path) as handle:
                    children = list(handle)
            except OSError as exc:
                node = by_path.get(path)
                if node is not None:
                    node["incomplete"] = True
                    node["error"] = str(exc)[:200]
                state["incomplete"] = True
                continue
            if depth >= max_depth and children:
                node = by_path.get(path)
                if node is not None:
                    node["unscanned"] = True
                continue
            for child in children:
                if len(nodes) >= node_cap:
                    state["truncated"] = True
                    break
                try:
                    info = child.stat(follow_symlinks=False)
                    if (
                        not getattr(info, "st_dev", 0)
                        or not getattr(info, "st_ino", 0)
                        or not getattr(info, "st_nlink", 0)
                    ):
                        # 部分平台（Windows）的 DirEntry.stat 不返回设备号/inode/nlink。
                        info = os.lstat(child.path)
                except OSError:
                    by_path[path]["incomplete"] = True
                    state["incomplete"] = True
                    continue
                if one_file_system and cross_device(info.st_dev, root_device):
                    state["cross"] += 1
                    continue
                if child.is_symlink():
                    node = add_node(child.path, info, "symlink")
                    if follow_symlinks:
                        try:
                            target_info = os.stat(child.path)
                        except OSError:
                            node["error"] = "符号链接目标不可读"
                            continue
                        if one_file_system and cross_device(target_info.st_dev, root_device):
                            state["cross"] += 1
                            continue
                        if os.path.isdir(child.path):
                            key = (target_info.st_dev, target_info.st_ino)
                            if key in visited_dirs:
                                node["error"] = "符号链接循环"
                                continue
                            visited_dirs.add(key)
                            stack.append((child.path, depth + 1))
                        else:
                            node["allocatedBytes"] = int(
                                getattr(target_info, "st_blocks", 0) * 512
                            )
                            node["apparentBytes"] = int(target_info.st_size)
                    continue
                if child.is_dir(follow_symlinks=False):
                    add_node(child.path, info, "dir")
                    stack.append((child.path, depth + 1))
                    continue
                duplicate_of = None
                if info.st_nlink and info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    if key in seen_inodes:
                        duplicate_of = seen_inodes[key]
                    else:
                        seen_inodes[key] = child.path
                kind = "file" if child.is_file(follow_symlinks=False) else "other"
                add_node(child.path, info, kind, hardlink_duplicate_of=duplicate_of)

        if include_deleted_open:
            for item in deleted_open_files(root_path, MAX_DELETED_OPEN):
                if len(nodes) >= node_cap:
                    state["truncated"] = True
                    break
                if item["path"] in by_path:
                    by_path[item["path"]]["deletedOpen"] = True
                    continue
                nodes.append({
                    "path": item["path"],
                    "name": item["name"],
                    "kind": "file",
                    "allocatedBytes": item["allocatedBytes"],
                    "apparentBytes": item["apparentBytes"],
                    "uid": item["uid"],
                    "gid": item["gid"],
                    "device": item["device"],
                    "inode": item["inode"],
                    "modifiedAtEpoch": None,
                    "hardlinkDuplicateOf": None,
                    "deletedOpen": True,
                    "unscanned": False,
                    "incomplete": False,
                    "error": None,
                })

        summary = {}
        for node in nodes:
            if node["hardlinkDuplicateOf"] is not None:
                continue
            uid = node["uid"]
            if uid is None:
                continue
            entry = summary.setdefault(str(uid), {
                "allocatedBytes": 0,
                "apparentBytes": 0,
                "fileCount": 0,
                "directoryCount": 0,
                "incomplete": False,
            })
            entry["allocatedBytes"] += node["allocatedBytes"] or 0
            entry["apparentBytes"] += node["apparentBytes"] or 0
            if node["kind"] == "dir":
                entry["directoryCount"] += 1
            else:
                entry["fileCount"] += 1
            if node["incomplete"] or node["unscanned"]:
                entry["incomplete"] = True

        deleted_bytes = sum(node["allocatedBytes"] or 0 for node in nodes if node["deletedOpen"])
        deleted_count = sum(1 for node in nodes if node["deletedOpen"])
        incomplete = state["incomplete"] or state["truncated"] or any(
            node["incomplete"] or node["unscanned"] for node in nodes
        )
        return section(data={
            "mount": root_path,
            "mountInfo": match,
            "oneFileSystem": one_file_system,
            "followSymlinks": follow_symlinks,
            "maxDepth": max_depth,
            "nodes": nodes,
            "nodeCount": len(nodes),
            "truncated": state["truncated"],
            "crossDeviceCount": state["cross"],
            "incomplete": incomplete,
            "deletedOpenBytes": deleted_bytes,
            "deletedOpenCount": deleted_count,
            "summaryByUid": summary,
            "capabilities": {
                "oneFileSystem": True,
                "symlinks": True,
                "hardlinks": True,
                "sparse": True,
                "deletedOpen": include_deleted_open,
            },
            "notes": [
                "allocatedBytes 为 st_blocks*512，apparentBytes 为 st_size；稀疏文件与 reflink 口径见文档",
                "硬链接按 inode 去重，重复项标记 hardlinkDuplicateOf",
            ],
        })
    except Exception as exc:
        return failed("error", exc)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    collected_at = to_iso(time.time())
    payload = {"probeVersion": VERSION, "collectedAt": collected_at, "mode": mode}
    if mode == "services":
        payload["services"] = collect_services()
    elif mode == "metrics":
        payload["metrics"] = collect_metrics()
    elif mode == "gpu":
        payload["gpu"] = collect_gpu_monitor()
    elif mode == "users":
        payload["users"] = collect_users("include-system" in sys.argv)
    elif mode == "mounts":
        payload["mounts"] = collect_mounts()
    elif mode == "storage":
        mount = sys.argv[2] if len(sys.argv) > 2 else "/"
        depth = to_int(sys.argv[3]) if len(sys.argv) > 3 else 3
        if depth is None:
            depth = 3
        depth = max(0, min(8, depth))
        payload["storage"] = collect_storage(
            mount,
            depth,
            follow_symlinks="follow-symlinks" in sys.argv,
            one_file_system="no-one-file-system" not in sys.argv,
            include_deleted_open="no-deleted-open" not in sys.argv,
        )
    else:
        payload["sections"] = {
            "system": collect_system(),
            "cpu": collect_cpu(),
            "memory": collect_memory(),
            "disks": collect_disks(),
            "network": collect_network(),
            "gpu": collect_gpu(),
            "services": collect_services(),
        }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
"""

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_SAFE_ARGUMENT = re.compile(r"^[^\x00\n\r]{1,4096}$")


class ProbeError(RuntimeError):
    """探针执行失败或输出不可解析。"""


def build_probe_command(mode: str = "snapshot", args: list[str] | None = None) -> str:
    if mode not in PROBE_MODES:
        raise ValueError(f"未知探针模式：{mode}")
    normalized_args: list[str] = []
    for argument in args or []:
        text = str(argument)
        if not _SAFE_ARGUMENT.match(text):
            raise ValueError(f"探针参数不合法：{text!r}")
        normalized_args.append(text)
    encoded = base64.b64encode(PROBE_SOURCE.encode("utf-8")).decode("ascii")
    invocation = " ".join([mode, *(shlex.quote(item) for item in normalized_args)]).strip()
    return (
        "if [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; else PY=python3; fi; "
        f'printf %s {shlex.quote(encoded)} | base64 -d | "$PY" - {invocation}'
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
    probe_args: list[str] | None = None,
    timeout: float = 45.0,
    cancel_event: threading.Event | None = None,
    max_output_bytes: int | None = None,
    operation: str = "状态采集",
) -> dict[str, Any]:
    command = build_probe_command(mode, probe_args)
    result = transport.run(
        target,
        command,
        timeout=timeout,
        cancel_event=cancel_event,
        max_output_bytes=max_output_bytes,
    )
    _require_ok(result, operation)
    return parse_probe_payload(result.stdout)
