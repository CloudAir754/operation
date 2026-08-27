"""块设备只读识别测试。"""

import json
import subprocess
from pathlib import Path

from lab_ops_panel.infrastructure.storage import block_device_snapshot


def test_block_devices_prefer_wwn_stable_identity(tmp_path: Path) -> None:
    """磁盘必须以 WWN 等稳定字段标识，设备路径仅用于展示。"""

    payload = {
        "blockdevices": [
            {
                "name": "sda",
                "path": "/dev/sda",
                "type": "disk",
                "size": 1000,
                "fstype": None,
                "mountpoints": [None],
                "model": "Test Disk",
                "serial": "SERIAL-REDACTED",
                "wwn": "WWN-REDACTED",
                "ro": False,
                "rm": False,
                "tran": "sata",
                "uuid": None,
                "children": [],
            }
        ]
    }

    def runner(command, **kwargs):  # type: ignore[no-untyped-def]
        assert command[0] == "lsblk"
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    snapshot = block_device_snapshot(runner=runner, by_id_directory=tmp_path / "missing")
    assert snapshot["available"] is True
    assert snapshot["devices"][0]["stable_identity"] == "wwn:WWN-REDACTED"


def test_block_devices_reject_malformed_output(tmp_path: Path) -> None:
    """无法理解的 lsblk 输出不能被误报为可操作磁盘。"""

    def runner(command, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 0, stdout="not-json", stderr="")

    snapshot = block_device_snapshot(runner=runner, by_id_directory=tmp_path)
    assert snapshot["available"] is False
    assert snapshot["devices"] == []
