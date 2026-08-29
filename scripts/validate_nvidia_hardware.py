#!/usr/bin/env python3
"""在 RTX 3090 或更新测试机上执行不泄露稳定标识的 NVML 验收。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lab_ops_panel.infrastructure.gpu import gpu_snapshot


def validate_snapshot(snapshot: dict[str, Any], require_process: bool = False) -> dict[str, Any]:
    """验证 GPU/进程字段关系并返回不含 UUID、PCI、用户名或命令的摘要。"""

    errors: list[str] = []
    if not snapshot.get("available"):
        errors.append(f"NVML 不可用：{snapshot.get('reason') or '未知原因'}")
    devices = snapshot.get("devices")
    processes = snapshot.get("processes")
    if not isinstance(devices, list) or not devices:
        errors.append("未发现 NVIDIA GPU")
        devices = []
    if not isinstance(processes, list):
        errors.append("GPU 进程列表格式无效")
        processes = []

    identities: set[tuple[int, str]] = set()
    optional_supported = {
        "temperature": 0,
        "fan": 0,
        "power": 0,
        "pcie": 0,
        "dram_bandwidth": 0,
        "process_utilization": 0,
    }
    for device in devices:
        index = device.get("index")
        uuid = device.get("uuid")
        if not isinstance(index, int) or not isinstance(uuid, str) or not uuid:
            errors.append("GPU 缺少稳定 index/UUID 关联")
            continue
        identities.add((index, uuid))
        if not device.get("pci_bus_id") or not device.get("name"):
            errors.append(f"GPU {index} 缺少 PCI Bus ID 或型号")
        total = device.get("memory_total")
        used = device.get("memory_used")
        if (
            not isinstance(total, int)
            or total <= 0
            or not isinstance(used, int)
            or not 0 <= used <= total
        ):
            errors.append(f"GPU {index} 显存事实无效")
        percent = device.get("gpu_percent")
        if not isinstance(percent, int | float) or not 0 <= percent <= 100:
            errors.append(f"GPU {index} 利用率超出范围")
        optional_supported["temperature"] += device.get("temperature_celsius") is not None
        optional_supported["fan"] += device.get("fan_percent") is not None
        optional_supported["power"] += device.get("power_watts") is not None
        optional_supported["pcie"] += (
            device.get("pcie_rx_bytes_per_second") is not None
            and device.get("pcie_tx_bytes_per_second") is not None
        )
        optional_supported["dram_bandwidth"] += device.get("dram_bandwidth_percent") is not None

    for process in processes:
        identity = (process.get("gpu_index"), process.get("gpu_uuid"))
        if identity not in identities:
            errors.append("GPU 进程未关联当前设备 index/UUID")
        if not isinstance(process.get("pid"), int) or process["pid"] <= 0:
            errors.append("GPU 进程 PID 无效")
        if process.get("bandwidth") is not None:
            errors.append("进程级带宽不得由整卡指标伪造")
        memory_percent = process.get("memory_percent")
        if memory_percent is not None and (
            not isinstance(memory_percent, int | float) or not 0 <= memory_percent <= 100
        ):
            errors.append("GPU 进程显存比例超出范围")
        if any(
            process.get(field) is not None
            for field in (
                "sm_percent",
                "memory_activity_percent",
                "encoder_percent",
                "decoder_percent",
            )
        ):
            optional_supported["process_utilization"] += 1
    if require_process and not processes:
        errors.append("未发现 GPU 工作负载；请启动测试计算进程后重试 --require-process")
    return {
        "ok": not errors,
        "device_count": len(devices),
        "process_count": len(processes),
        "optional_supported_counts": optional_supported,
        "errors": errors,
    }


def main() -> None:
    """采集一次实时 NVML 快照并以脱敏 JSON 报告验收结果。"""

    parser = argparse.ArgumentParser(description="LabOpsPanel NVIDIA 实机兼容性验收")
    parser.add_argument(
        "--require-process",
        action="store_true",
        help="要求测试时至少有一个 GPU 进程，用于核对进程级字段",
    )
    arguments = parser.parse_args()
    result = validate_snapshot(gpu_snapshot(), arguments.require_process)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
