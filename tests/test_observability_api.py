"""阶段三 API 测试：指标、GPU、用户、挂载点与空间扫描。"""

from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from lab_ops_panel.app import create_app
from lab_ops_panel.config import Settings
from lab_ops_panel.models import Server, StorageScan
from lab_ops_panel.remote.probe import PROBE_VERSION
from lab_ops_panel.ssh.transport import CommandResult, FakeTransport


def metrics_data(**overrides) -> dict:
    data = {
        "cpu": {
            "user": 100,
            "nice": 0,
            "system": 50,
            "idle": 800,
            "iowait": 20,
            "irq": 0,
            "softirq": 0,
            "steal": 0,
            "logicalCpus": 16,
            "load1": 0.5,
            "load5": 0.4,
            "load15": 0.3,
        },
        "memory": {
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
        },
        "vmstat": {"pswpin": 10, "pswpout": 5, "pgpgin": 1, "pgpgout": 2},
        "disk": {
            "sda": {
                "readsCompleted": 2,
                "sectorsRead": 100,
                "msReading": 50,
                "writesCompleted": 4,
                "sectorsWritten": 200,
                "msWriting": 100,
                "ioMs": 100,
            }
        },
        "diskMeta": {
            "sda": {
                "kind": "disk",
                "parent": None,
                "model": "TestDisk",
                "sizeBytes": 1000,
                "hidden": False,
            }
        },
        "mounts": [
            {"mount": "/", "device": "/dev/sda1", "filesystem": "ext4", "deviceName": "sda"}
        ],
        "network": {
            "eth0": {
                "rxBytes": 1000,
                "rxPackets": 10,
                "rxErrors": 0,
                "rxDrops": 0,
                "txBytes": 2000,
                "txPackets": 20,
                "txErrors": 0,
                "txDrops": 0,
            }
        },
        "networkMeta": {
            "eth0": {
                "kind": "physical",
                "state": "up",
                "speedMbps": 10000,
                "addresses": ["10.0.0.11/24"],
            }
        },
        "defaultInterface": "eth0",
        "gpu": {
            "devices": [
                {
                    "index": 0,
                    "name": "NVIDIA GeForce RTX 4090",
                    "utilizationPercent": 20.0,
                    "memoryUsedMb": 8192,
                    "memoryTotalMb": 24564,
                    "temperatureC": 50.0,
                }
            ]
        },
        "gpuStatus": "ok",
    }
    data.update(overrides)
    return data


def metrics_section(**overrides) -> dict:
    return {
        "status": "ok",
        "error": None,
        "requiresSudo": False,
        "data": metrics_data(**overrides),
    }


def gpu_section() -> dict:
    return {
        "status": "ok",
        "error": None,
        "requiresSudo": False,
        "data": {
            "devices": [
                {
                    "index": 0,
                    "uuid": "GPU-0",
                    "pciBusId": "0000:01:00.0",
                    "name": "NVIDIA GeForce RTX 4090",
                    "driverVersion": "550.107.02",
                    "memoryTotalMb": 24564,
                    "memoryUsedMb": 8192,
                    "utilizationGpuPercent": 37.0,
                    "temperatureC": 52.0,
                    "powerDrawW": 210.5,
                    "migMode": "Disabled",
                    "migDevices": [],
                }
            ],
            "processes": [
                {
                    "pid": 4242,
                    "user": "labuser",
                    "name": "python",
                    "command": "python train.py",
                    "memoryUsedMb": 8192,
                    "gpuUuid": "GPU-0",
                    "gpuIndex": 0,
                    "programPath": "/usr/bin/python3.12",
                    "stale": False,
                }
            ],
            "processesStatus": "ok",
            "capabilities": {
                "nvml": True,
                "processes": "ok",
                "mig": False,
                "perProcessUtilization": False,
            },
            "notes": [],
        },
    }


def users_section() -> dict:
    def user(login: str, uid: int, **overrides) -> dict:
        base = {
            "login": login,
            "uid": uid,
            "gid": uid,
            "home": f"/home/{login}",
            "shell": "/bin/bash",
            "system": uid < 1000,
            "primaryGroup": login,
            "supplementaryGroups": ["sudo"],
            "sudoGroupMember": True,
            "sudoPolicyPresent": True,
            "sudoEffective": "unknown",
            "recentLogin": None,
            "homeStorage": {
                "allocatedBytes": 1024,
                "apparentBytes": 2048,
                "fileCount": 2,
                "directoryCount": 1,
                "depth": 2,
                "truncated": False,
                "incomplete": False,
            },
        }
        base.update(overrides)
        return base

    return {
        "status": "ok",
        "error": None,
        "requiresSudo": False,
        "data": {
            "thresholdUid": 1000,
            "users": [user("labuser", 1001), user("root", 0, shell="/bin/bash")],
            "notes": ["sudo 有效权限仅对当前 SSH 用户验证"],
        },
    }


def mounts_section() -> dict:
    return {
        "status": "ok",
        "error": None,
        "requiresSudo": False,
        "data": {
            "mounts": [
                {
                    "mount": "/",
                    "device": "/dev/sda1",
                    "filesystem": "ext4",
                    "options": "rw,relatime",
                    "uuid": None,
                    "majorMinor": None,
                    "physicalDevice": "sda1",
                    "readOnly": False,
                },
                {
                    "mount": "/data",
                    "device": "/dev/sdb1",
                    "filesystem": "xfs",
                    "options": "rw,noatime",
                    "uuid": None,
                    "majorMinor": None,
                    "physicalDevice": "sdb1",
                    "readOnly": False,
                },
            ]
        },
    }


def storage_section() -> dict:
    return {
        "status": "ok",
        "error": None,
        "requiresSudo": False,
        "data": {
            "mount": "/data",
            "mountInfo": {"device": "/dev/sdb1", "mount": "/data", "filesystem": "xfs"},
            "oneFileSystem": True,
            "followSymlinks": False,
            "maxDepth": 3,
            "nodes": [
                {
                    "path": "/data",
                    "name": "data",
                    "kind": "dir",
                    "allocatedBytes": 0,
                    "apparentBytes": 0,
                    "uid": 0,
                    "gid": 0,
                    "device": 1,
                    "inode": 1,
                    "modifiedAtEpoch": 1760000000,
                    "hardlinkDuplicateOf": None,
                    "deletedOpen": False,
                    "unscanned": False,
                    "incomplete": False,
                    "error": None,
                },
                {
                    "path": "/data/logs",
                    "name": "logs",
                    "kind": "dir",
                    "allocatedBytes": 0,
                    "apparentBytes": 0,
                    "uid": 0,
                    "gid": 0,
                    "device": 1,
                    "inode": 2,
                    "modifiedAtEpoch": 1760000000,
                    "hardlinkDuplicateOf": None,
                    "deletedOpen": False,
                    "unscanned": False,
                    "incomplete": False,
                    "error": None,
                },
                {
                    "path": "/data/logs/app.log",
                    "name": "app.log",
                    "kind": "file",
                    "allocatedBytes": 300,
                    "apparentBytes": 300,
                    "uid": 1001,
                    "gid": 1001,
                    "device": 1,
                    "inode": 3,
                    "modifiedAtEpoch": 1760000000,
                    "hardlinkDuplicateOf": None,
                    "deletedOpen": False,
                    "unscanned": False,
                    "incomplete": False,
                    "error": None,
                },
                {
                    "path": "/data/big.bin",
                    "name": "big.bin",
                    "kind": "file",
                    "allocatedBytes": 1000,
                    "apparentBytes": 1200,
                    "uid": 1002,
                    "gid": 1002,
                    "device": 1,
                    "inode": 4,
                    "modifiedAtEpoch": 1760000000,
                    "hardlinkDuplicateOf": None,
                    "deletedOpen": False,
                    "unscanned": False,
                    "incomplete": False,
                    "error": None,
                },
                {
                    "path": "/data/hard1.bin",
                    "name": "hard1.bin",
                    "kind": "file",
                    "allocatedBytes": 500,
                    "apparentBytes": 500,
                    "uid": 1001,
                    "gid": 1001,
                    "device": 1,
                    "inode": 9,
                    "modifiedAtEpoch": 1760000000,
                    "hardlinkDuplicateOf": None,
                    "deletedOpen": False,
                    "unscanned": False,
                    "incomplete": False,
                    "error": None,
                },
                {
                    "path": "/data/hard2.bin",
                    "name": "hard2.bin",
                    "kind": "file",
                    "allocatedBytes": 500,
                    "apparentBytes": 500,
                    "uid": 1001,
                    "gid": 1001,
                    "device": 1,
                    "inode": 9,
                    "modifiedAtEpoch": 1760000000,
                    "hardlinkDuplicateOf": "/data/hard1.bin",
                    "deletedOpen": False,
                    "unscanned": False,
                    "incomplete": False,
                    "error": None,
                },
            ],
            "nodeCount": 6,
            "truncated": False,
            "crossDeviceCount": 0,
            "incomplete": False,
            "deletedOpenBytes": 0,
            "deletedOpenCount": 0,
            "summaryByUid": {},
            "capabilities": {"oneFileSystem": True},
            "notes": [],
        },
    }


def payload(**overrides) -> dict:
    base = {
        "probeVersion": PROBE_VERSION,
        "collectedAt": "2026-10-30T12:00:00Z",
        "metrics": metrics_section(),
        "gpu": gpu_section(),
        "users": users_section(),
        "mounts": mounts_section(),
        "storage": storage_section(),
    }
    base.update(overrides)
    return base


def _with_transport(settings: Settings, transport) -> TestClient:
    app = create_app(settings, transport=transport)
    return TestClient(app)


def _add_server(app, alias: str = "lab-01", *, enabled: bool = True) -> int:
    session = app.state.container.session_factory()
    try:
        server = Server(
            alias=alias,
            hostname="10.0.0.11",
            user="labadmin",
            port=22,
            source="ssh_config",
            enabled=enabled,
        )
        session.add(server)
        session.commit()
        return server.id
    finally:
        session.close()


def _settings(tmp_path) -> Settings:
    return Settings(
        app_data_dir=tmp_path / "data",
        ssh_config_path=tmp_path / "cfg",
        frontend_dist=tmp_path / "dist",
    )


class SlowStorageTransport:
    """storage 模式阻塞直到取消，用于验证取消与并发限制。"""

    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body)
        self.started = threading.Event()
        self.calls: list[str] = []

    def run(
        self,
        target,
        command,
        *,
        timeout=None,
        cancel_event=None,
        stdin_data=None,
        max_output_bytes=None,
    ):
        self.calls.append(command)
        if command.rstrip().endswith("storage") or " storage " in command:
            self.started.set()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.wait(0.02):
                    return CommandResult(
                        argv=[],
                        command=command,
                        exit_code=None,
                        stdout="",
                        stderr="",
                        duration_ms=1,
                        cancelled=True,
                    )
            return CommandResult(
                argv=[], command=command, exit_code=0, stdout=self.body, stderr="", duration_ms=1
            )
        return CommandResult(
            argv=[], command=command, exit_code=0, stdout=self.body, stderr="", duration_ms=1
        )


def test_metrics_endpoint_accumulates_points(
    client: TestClient, add_server, transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())

    first = client.get(f"/api/servers/{server_id}/metrics?window=5m")

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "ok"
    assert body["sampleIntervalMs"] == 1000
    assert len(body["points"]) == 1
    assert body["points"][0]["cpuPercent"] is None
    assert body["current"]["cpu"]["data"]["logicalCpus"] == 16
    assert body["current"]["memory"]["data"]["usedPercent"] == 60.0
    assert body["stale"] is False

    transport.default_stdout = json.dumps(
        payload(
            metrics=metrics_section(
                cpu={**metrics_data()["cpu"], "user": 120, "system": 60, "idle": 880, "iowait": 25}
            )
        )
    )
    time.sleep(0.25)
    second = client.get(f"/api/servers/{server_id}/metrics?window=5m").json()

    assert len(second["points"]) == 2
    latest = second["points"][-1]
    assert latest["cpuPercent"] == 30.4
    assert latest["memoryUsedPercent"] == 60.0
    assert latest["sampleIntervalMs"] >= 1
    assert second["current"]["disk"]["data"]["devices"][0]["name"] == "sda"


def test_metrics_window_validation(client: TestClient, add_server) -> None:
    server_id = add_server()

    assert client.get(f"/api/servers/{server_id}/metrics?window=7m").status_code == 422


def test_metrics_failure_returns_status(
    client: TestClient, add_server, transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.responses = [
        CommandResult(
            argv=[],
            command="",
            exit_code=255,
            stdout="",
            stderr="Permission denied (publickey).",
            duration_ms=1,
        )
    ]

    body = client.get(f"/api/servers/{server_id}/metrics").json()

    assert body["status"] == "error"
    assert body["current"]["cpu"]["status"] == "error"
    assert body["points"] == []


def test_gpus_endpoint_uses_cache(client: TestClient, add_server, transport: FakeTransport) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())
    calls_before = len(transport.calls)

    first = client.get(f"/api/servers/{server_id}/gpus").json()
    second = client.get(f"/api/servers/{server_id}/gpus").json()

    assert first["devices"][0]["index"] == 0
    assert first["processes"][0]["pid"] == 4242
    assert first["processesStatus"] == "ok"
    assert len(transport.calls) == calls_before + 1
    assert second["devices"][0]["name"] == "NVIDIA GeForce RTX 4090"

    client.get(f"/api/servers/{server_id}/gpus?refresh=true")
    assert len(transport.calls) == calls_before + 2


def test_gpus_unavailable(client: TestClient, add_server, transport: FakeTransport) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(
        payload(
            gpu={
                "status": "unavailable",
                "error": "未检测到 nvidia-smi",
                "data": {"devices": [], "processes": [], "processesStatus": "unavailable"},
            }
        )
    )

    body = client.get(f"/api/servers/{server_id}/gpus").json()

    assert body["status"] == "unavailable"
    assert body["devices"] == []
    assert body["processesStatus"] == "unavailable"


def test_users_endpoint_filters_system_accounts(
    client: TestClient, add_server, transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())

    default = client.get(f"/api/servers/{server_id}/users").json()
    with_system = client.get(f"/api/servers/{server_id}/users?includeSystem=true").json()

    assert [user["login"] for user in default["users"]] == ["labuser"]
    assert [user["login"] for user in with_system["users"]] == ["root", "labuser"]
    assert default["users"][0]["sudo"]["groupMember"] is True
    assert default["users"][0]["sudo"]["effective"] == "unknown"
    assert "include-system" in transport.calls[-1].command


def test_mounts_endpoint(client: TestClient, add_server, transport: FakeTransport) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())

    body = client.get(f"/api/servers/{server_id}/mounts").json()

    assert [mount["mount"] for mount in body["mounts"]] == ["/", "/data"]
    assert body["mounts"][1]["filesystem"] == "xfs"


def test_storage_scan_lifecycle(
    client: TestClient, add_server, transport: FakeTransport, wait_scan
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())

    created = client.post(
        f"/api/servers/{server_id}/storage/scans",
        json={"mount": "/data", "maxDepth": 3, "includeDeletedOpen": False},
    )

    assert created.status_code == 201, created.text
    scan = wait_scan(client, created.json()["scanId"])
    assert scan["status"] == "succeeded"
    assert scan["serverAlias"] == "lab-01"
    assert scan["mount"] == "/data"
    assert scan["nodeCount"] >= 6
    assert scan["truncated"] is False
    by_path = {node["path"]: node for node in scan["nodes"]}
    assert by_path["/data"]["allocatedBytes"] == 1800
    assert by_path["/data"]["fileCount"] == 3
    assert by_path["/data/hard2.bin"]["hardlinkDuplicateOf"] == "/data/hard1.bin"
    assert scan["largestFiles"][0]["path"] == "/data/big.bin"
    assert {item["uid"] for item in scan["summaryByUid"]} == {1001, 1002}

    events = client.get(f"/api/storage/scans/{scan['scanId']}/events")
    assert events.status_code == 200
    assert "event: progress" in events.text
    assert "event: nodes" in events.text
    assert "event: done" in events.text

    audit = client.get("/api/audit").json()["entries"]
    assert any(entry["action"] == "storage_scan" for entry in audit)

    listing = client.get(f"/api/servers/{server_id}/storage/scans").json()
    assert listing["count"] == 1
    assert listing["scans"][0]["nodes"] == []
    assert listing["scans"][0]["nodeCount"] == scan["nodeCount"]


def test_storage_scan_rejects_unknown_mount(
    client: TestClient, add_server, transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())

    response = client.post(f"/api/servers/{server_id}/storage/scans", json={"mount": "/etc"})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "mount_not_allowed"


def test_storage_scan_rejects_unknown_fields(
    client: TestClient, add_server, transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())

    response = client.post(
        f"/api/servers/{server_id}/storage/scans",
        json={"mount": "/data", "extra": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_failed"


def test_storage_scan_requires_mounts(
    client: TestClient, add_server, transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(
        payload(mounts={"status": "error", "error": "remote failed", "data": None})
    )

    response = client.post(f"/api/servers/{server_id}/storage/scans", json={"mount": "/data"})

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "mounts_unavailable"


def test_storage_scan_children_and_path_guard(
    client: TestClient, add_server, transport: FakeTransport, wait_scan
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())
    scan = wait_scan(
        client,
        client.post(f"/api/servers/{server_id}/storage/scans", json={"mount": "/data"}).json()[
            "scanId"
        ],
    )

    children = client.get(f"/api/storage/scans/{scan['scanId']}/children", params={"path": "/data"})
    assert children.status_code == 200
    assert {node["path"] for node in children.json()} == {
        "/data/logs",
        "/data/big.bin",
        "/data/hard1.bin",
        "/data/hard2.bin",
    }

    escaped = client.get(f"/api/storage/scans/{scan['scanId']}/children", params={"path": "/etc"})
    assert escaped.status_code == 422
    assert escaped.json()["detail"]["code"] == "path_not_allowed"

    missing = client.get(
        f"/api/storage/scans/{scan['scanId']}/children",
        params={"path": "/data/not-there"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "scan_path_not_found"


def test_storage_scan_can_be_cancelled(tmp_path, wait_scan) -> None:
    transport = SlowStorageTransport(payload())
    app = create_app(_settings(tmp_path), transport=transport)
    with TestClient(app) as client:
        server_id = _add_server(app)
        created = client.post(f"/api/servers/{server_id}/storage/scans", json={"mount": "/data"})
        assert created.status_code == 201
        scan_id = created.json()["scanId"]
        assert transport.started.wait(10.0) is True

        cancelled = client.post(f"/api/storage/scans/{scan_id}/cancel")

        assert cancelled.status_code == 200
        scan = wait_scan(client, scan_id)
        assert scan["status"] == "cancelled"
        assert scan["error"]


def test_second_scan_is_rejected_while_running(tmp_path, wait_scan) -> None:
    transport = SlowStorageTransport(payload())
    app = create_app(_settings(tmp_path), transport=transport)
    with TestClient(app) as client:
        server_id = _add_server(app)
        first = client.post(f"/api/servers/{server_id}/storage/scans", json={"mount": "/data"})
        assert first.status_code == 201
        assert transport.started.wait(10.0) is True

        second = client.post(f"/api/servers/{server_id}/storage/scans", json={"mount": "/data"})

        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "scan_limit_reached"
        client.post(f"/api/storage/scans/{first.json()['scanId']}/cancel")
        wait_scan(client, first.json()["scanId"])


def test_scan_recovery_marks_interrupted(container, add_server) -> None:
    server_id = add_server()
    with container.session_factory() as session:
        session.add(
            StorageScan(
                id="stale-scan",
                server_id=server_id,
                mount="/data",
                request={"mount": "/data", "max_depth": 3},
                status="running",
                stage="scanning",
                progress=35,
            )
        )
        session.commit()

    count = container.observability.recover_scans()

    assert count >= 1
    with container.session_factory() as session:
        row = session.get(StorageScan, "stale-scan")
        assert row.status == "interrupted"
        assert row.error
        assert row.finished_at is not None


def test_user_storage_scan(
    client: TestClient, add_server, transport: FakeTransport, wait_scan
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())

    created = client.post(
        f"/api/servers/{server_id}/users/storage-scan",
        json={"uid": 1001, "mount": "/", "maxDepth": 8},
    )
    assert created.status_code == 201, created.text
    scan = wait_scan(client, created.json()["scanId"])
    assert scan["status"] == "succeeded"
    assert scan["userFilter"] == 1001


def test_user_storage_scan_unknown_uid(
    client: TestClient, add_server, transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())

    response = client.post(
        f"/api/servers/{server_id}/users/storage-scan", json={"uid": 4242, "mount": "/"}
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "user_not_found"


def test_scan_cancel_finished_conflicts(
    client: TestClient, add_server, transport: FakeTransport, wait_scan
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(payload())
    scan = wait_scan(
        client,
        client.post(f"/api/servers/{server_id}/storage/scans", json={"mount": "/data"}).json()[
            "scanId"
        ],
    )

    response = client.post(f"/api/storage/scans/{scan['scanId']}/cancel")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "scan_finished"


def test_unknown_scan_returns_404(client: TestClient) -> None:
    assert client.get("/api/storage/scans/nope").status_code == 404
