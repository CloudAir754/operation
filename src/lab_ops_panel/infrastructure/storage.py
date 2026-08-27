"""块设备稳定身份与只读拓扑采集。"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

LSBLK_COLUMNS = (
    "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL,WWN,RO,RM,TRAN,UUID,PKNAME"
)


def _by_id_links(directory: Path = Path("/dev/disk/by-id")) -> dict[str, list[str]]:
    """将设备真实路径映射到所有可见的 by-id 稳定链接。"""

    result: dict[str, list[str]] = {}
    try:
        links = list(directory.iterdir())
    except OSError:
        return result
    for link in links:
        if not link.is_symlink():
            continue
        try:
            target = str(link.resolve(strict=True))
        except OSError:
            continue
        result.setdefault(target, []).append(str(link))
    return result


def _stable_identity(device: dict[str, Any], by_id: list[str]) -> str | None:
    """按 WWN、序列号、by-id、文件系统 UUID 的顺序选择展示身份。"""

    if device.get("wwn"):
        return f"wwn:{device['wwn']}"
    if device.get("serial"):
        return f"serial:{device['serial']}"
    if by_id:
        return by_id[0]
    if device.get("uuid"):
        return f"uuid:{device['uuid']}"
    return None


def _flatten_devices(
    devices: list[dict[str, Any]], by_ids: dict[str, list[str]], parent: str | None = None
) -> list[dict[str, Any]]:
    """展开 lsblk 树，同时保留父设备关系。"""

    result = []
    for device in devices:
        path = device.get("path")
        resolved_path = str(Path(path).resolve(strict=False)) if path else ""
        stable_links = sorted(by_ids.get(resolved_path, []))
        item = {
            "name": device.get("name"),
            "path": path,
            "type": device.get("type"),
            "size": device.get("size"),
            "filesystem": device.get("fstype"),
            "mountpoints": [value for value in (device.get("mountpoints") or []) if value],
            "model": (device.get("model") or "").strip() or None,
            "serial": (device.get("serial") or "").strip() or None,
            "wwn": (device.get("wwn") or "").strip() or None,
            "read_only": bool(device.get("ro")),
            "removable": bool(device.get("rm")),
            "transport": device.get("tran"),
            "uuid": device.get("uuid"),
            "parent": parent or device.get("pkname"),
            "by_id": stable_links,
        }
        item["stable_identity"] = _stable_identity(item, stable_links)
        result.append(item)
        result.extend(_flatten_devices(device.get("children") or [], by_ids, device.get("name")))
    return result


def block_device_snapshot(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    by_id_directory: Path = Path("/dev/disk/by-id"),
) -> dict[str, Any]:
    """使用固定参数调用 lsblk，绝不接收 Web 层命令字符串。"""

    command = ["lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS]
    try:
        completed = runner(command, capture_output=True, text=True, check=True, timeout=5)
        payload = json.loads(completed.stdout)
    except FileNotFoundError:
        return {"available": False, "error": "系统缺少 lsblk 命令", "devices": []}
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "lsblk 读取超时", "devices": []}
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError, KeyError) as exc:
        return {
            "available": False,
            "error": f"lsblk 输出无法解析：{type(exc).__name__}",
            "devices": [],
        }
    devices = payload.get("blockdevices")
    if not isinstance(devices, list):
        return {"available": False, "error": "lsblk 输出缺少 blockdevices", "devices": []}
    return {
        "available": True,
        "error": None,
        "devices": _flatten_devices(devices, _by_id_links(by_id_directory)),
    }
