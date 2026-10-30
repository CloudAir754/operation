"""共享测试夹具：临时数据目录、内存保护器、假 SSH 传输与 API 客户端。"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lab_ops_panel.app import create_app
from lab_ops_panel.config import Settings
from lab_ops_panel.models import Server
from lab_ops_panel.remote.probe import PROBE_VERSION
from lab_ops_panel.ssh.transport import FakeTransport

TERMINAL_TASK_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")
TERMINAL_SCAN_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

SSH_CONFIG_TEXT = """\
# 测试用 SSH 配置
Host lab-gpu-01
    HostName 10.0.0.11
    User labadmin
    Port 2222
    IdentityFile ~/.ssh/id_ed25519
    ProxyJump bastion
    ConnectTimeout 7

Host lab-cpu-01
    HostName 10.0.0.21
    User labadmin

Host broken
    HostName 10.0.0.99
    Port 70000
"""


class MemoryProtector:
    """仅用于测试的可逆保护器（不是安全实现）。"""

    PREFIX = b"mem:"

    def protect(self, plaintext: bytes) -> bytes:
        return self.PREFIX + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("密文格式不正确")
        return ciphertext[len(self.PREFIX) :][::-1]


BASE_PROBE_PAYLOAD: dict[str, Any] = {
    "probeVersion": PROBE_VERSION,
    "collectedAt": "2026-10-25T12:00:00Z",
    "sections": {
        "system": {
            "status": "ok",
            "error": None,
            "data": {
                "hostname": "lab-01",
                "os": "Ubuntu 24.04.1 LTS",
                "kernel": "6.8.0-45-generic",
                "arch": "x86_64",
                "uptimeSeconds": 864000,
                "bootTime": "2026-10-15T12:00:00Z",
            },
        },
        "cpu": {
            "status": "ok",
            "error": None,
            "data": {
                "model": "AMD EPYC 7543",
                "cores": 32,
                "usagePercent": 12.5,
                "load1": 0.4,
                "load5": 0.5,
                "load15": 0.6,
            },
        },
        "memory": {
            "status": "ok",
            "error": None,
            "data": {
                "totalBytes": 68719476736,
                "availableBytes": 34359738368,
                "usedPercent": 50.0,
                "swapTotalBytes": 0,
                "swapUsedBytes": 0,
            },
        },
        "disks": {
            "status": "ok",
            "error": None,
            "data": {
                "mounts": [
                    {
                        "mount": "/",
                        "filesystem": "/dev/nvme0n1p2",
                        "totalBytes": 1000,
                        "usedBytes": 400,
                        "freeBytes": 600,
                        "usedPercent": 40.0,
                    }
                ]
            },
        },
        "network": {
            "status": "ok",
            "error": None,
            "data": {
                "defaultInterface": "eth0",
                "gateway": "10.0.0.1",
                "interfaces": [
                    {
                        "name": "eth0",
                        "state": "up",
                        "speedMbps": 10000,
                        "rxBytes": 123456,
                        "txBytes": 654321,
                        "rxErrors": 0,
                        "txErrors": 0,
                        "addresses": ["10.0.0.11/24"],
                    }
                ],
            },
        },
        "gpu": {
            "status": "ok",
            "error": None,
            "data": {
                "devices": [
                    {
                        "index": 0,
                        "name": "RTX 4090",
                        "memoryTotalMb": 24576,
                        "memoryUsedMb": 1024,
                        "utilizationPercent": 5.0,
                        "temperatureC": 40.0,
                        "driverVersion": "550.1",
                    }
                ]
            },
        },
        "services": {
            "status": "ok",
            "error": None,
            "data": {
                "runningCount": 2,
                "failedCount": 1,
                "failedUnits": ["broken.service"],
                "units": [
                    {
                        "name": "nginx.service",
                        "load": "loaded",
                        "active": "active",
                        "sub": "running",
                        "description": "web",
                        "protected": False,
                    },
                    {
                        "name": "ssh.service",
                        "load": "loaded",
                        "active": "active",
                        "sub": "running",
                        "description": "ssh",
                        "protected": False,
                    },
                ],
            },
        },
    },
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_data_dir=tmp_path / "data",
        ssh_config_path=tmp_path / "ssh_config",
        frontend_dist=tmp_path / "dist",
        snapshot_ttl_seconds=20,
    )


@pytest.fixture
def protector() -> MemoryProtector:
    return MemoryProtector()


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def app(settings: Settings, transport: FakeTransport, protector: MemoryProtector):
    return create_app(settings, transport=transport, protector=protector)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def container(app):
    return app.state.container


@pytest.fixture
def add_server(container) -> Callable[..., int]:
    def _add(alias: str = "lab-01", **overrides: Any) -> int:
        defaults: dict[str, Any] = {
            "alias": alias,
            "hostname": "10.0.0.1",
            "user": "lab",
            "port": 22,
            "connect_timeout": 10,
            "source": "ssh_config",
            "enabled": True,
            "notes": "",
        }
        defaults.update(overrides)
        session = container.session_factory()
        try:
            server = Server(**defaults)
            session.add(server)
            session.commit()
            return server.id
        finally:
            session.close()

    return _add


@pytest.fixture
def ssh_config_file(tmp_path: Path) -> Path:
    path = tmp_path / "ssh_config"
    path.write_text(SSH_CONFIG_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def probe_payload() -> Callable[..., dict[str, Any]]:
    def build(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = copy.deepcopy(BASE_PROBE_PAYLOAD)
        payload["sections"].update(overrides or {})
        return payload

    return build


@pytest.fixture
def probe_envelope() -> Callable[..., dict[str, Any]]:
    """构造带协议版本与采样时间的探针响应，可同时包含多个模式分区。"""

    def build(**sections: Any) -> dict[str, Any]:
        return {
            "probeVersion": PROBE_VERSION,
            "collectedAt": "2026-10-30T12:00:00Z",
            "mode": "snapshot",
            **sections,
        }

    return build


@pytest.fixture
def wait_scan():
    def _wait(
        client: TestClient,
        scan_id: str,
        *,
        timeout: float = 15.0,
        statuses: tuple[str, ...] = TERMINAL_SCAN_STATUSES,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/storage/scans/{scan_id}")
            assert response.status_code == 200, response.text
            last = response.json()
            if last["status"] in statuses:
                return last
            time.sleep(0.02)
        raise AssertionError(f"扫描 {scan_id} 未在 {timeout}s 内结束：{last}")

    return _wait


def wait_for_task(
    client: TestClient,
    task_id: str,
    *,
    timeout: float = 15.0,
    statuses: tuple[str, ...] = TERMINAL_TASK_STATUSES,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in statuses:
            return last
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内结束：{last}")


@pytest.fixture
def wait_task() -> Callable[..., dict[str, Any]]:
    return wait_for_task
