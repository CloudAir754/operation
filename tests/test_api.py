"""REST API 测试：SSH 导入、主机维护、连接测试、凭据、快照、日志与审计。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from lab_ops_panel.app import create_app
from lab_ops_panel.config import Settings
from lab_ops_panel.models import Server, ServerSecret
from lab_ops_panel.ssh.transport import CommandResult, FakeTransport, TransportError


def _result(**overrides) -> CommandResult:
    defaults = {
        "argv": [],
        "command": "",
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "duration_ms": 1,
    }
    defaults.update(overrides)
    return CommandResult(**defaults)


class MissingSshTransport:
    def run(self, *_args, **_kwargs):
        raise TransportError("找不到 SSH 客户端 'ssh'")


def test_health_endpoint(client: TestClient) -> None:
    payload = client.get("/api/health").json()

    assert payload["status"] == "ok"
    assert payload["version"]
    assert payload["time"].endswith("Z")


def test_settings_endpoint(client: TestClient) -> None:
    payload = client.get("/api/settings").json()

    assert payload["bindHost"] == "127.0.0.1"
    assert payload["bindPort"] == 8765
    assert payload["allowArbitraryCommands"] is False
    assert payload["snapshotTtlSeconds"] == 20


def test_actions_endpoint(client: TestClient) -> None:
    payload = client.get("/api/actions").json()

    ids = {action["id"] for action in payload["actions"]}
    assert "service_restart" in ids
    assert "run_command" in ids


def test_ssh_config_endpoint_lists_explicit_hosts(
    client: TestClient, ssh_config_file: Path
) -> None:
    payload = client.get("/api/ssh-config").json()

    aliases = [host["alias"] for host in payload["hosts"]]
    assert aliases == ["lab-gpu-01", "lab-cpu-01"]
    gpu = payload["hosts"][0]
    assert gpu["hostname"] == "10.0.0.11"
    assert gpu["port"] == 2222
    assert gpu["proxyJump"] == "bastion"
    assert gpu["imported"] is False
    assert payload["exists"] is True
    assert any("Port 超出范围" in error["message"] for error in payload["errors"])


def test_import_is_idempotent(client: TestClient, ssh_config_file: Path) -> None:
    first = client.post("/api/servers/import", json={})

    assert first.status_code == 200
    assert sorted(first.json()["imported"]) == ["lab-cpu-01", "lab-gpu-01"]
    assert first.json()["updated"] == []

    second = client.post("/api/servers/import", json={})

    assert second.json()["imported"] == []
    assert sorted(second.json()["updated"]) == ["lab-cpu-01", "lab-gpu-01"]
    listing = client.get("/api/servers").json()
    assert listing["count"] == 2
    assert {server["source"] for server in listing["servers"]} == {"ssh_config"}


def test_import_selected_alias_and_report_missing(
    client: TestClient, ssh_config_file: Path
) -> None:
    payload = client.post("/api/servers/import", json={"aliases": ["lab-gpu-01", "ghost"]}).json()

    assert payload["imported"] == ["lab-gpu-01"]
    assert payload["skipped"] == [{"alias": "ghost", "reason": "SSH 配置中不存在该 Host"}]


def test_import_prune_disables_missing_hosts(client: TestClient, ssh_config_file: Path) -> None:
    client.post("/api/servers/import", json={})
    ssh_config_file.write_text("Host lab-gpu-01\n    HostName 10.0.0.11\n", encoding="utf-8")

    result = client.post("/api/servers/import", json={"pruneMissing": True}).json()

    assert "lab-cpu-01" in result["updated"]
    servers = {server["alias"]: server for server in client.get("/api/servers").json()["servers"]}
    assert servers["lab-cpu-01"]["enabled"] is False


def test_patch_server_updates_fields(client: TestClient, add_server: Callable[..., int]) -> None:
    server_id = add_server()

    response = client.patch(f"/api/servers/{server_id}", json={"enabled": False, "notes": "维护中"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["notes"] == "维护中"
    assert client.get(f"/api/servers/{server_id}").json()["notes"] == "维护中"


def test_patch_rejects_unknown_fields(client: TestClient, add_server: Callable[..., int]) -> None:
    server_id = add_server()

    response = client.patch(f"/api/servers/{server_id}", json={"sudoPassword": "x"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "validation_failed"
    assert detail["fields"]


def test_patch_requires_at_least_one_field(
    client: TestClient, add_server: Callable[..., int]
) -> None:
    server_id = add_server()

    response = client.patch(f"/api/servers/{server_id}", json={})

    assert response.status_code == 422


def test_missing_server_returns_contract_error(client: TestClient) -> None:
    response = client.get("/api/servers/424242")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "server_not_found"


def test_connection_test_success_is_persisted(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = "labops-ok"

    payload = client.post(f"/api/servers/{server_id}/connection-test").json()

    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["error"] is None
    assert payload["testedAt"].endswith("Z")
    servers = client.get("/api/servers").json()["servers"]
    assert servers[0]["lastConnect"]["status"] == "ok"
    assert servers[0]["lastConnect"]["at"] is not None


def test_connection_test_failure_is_explained(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.responses = [_result(exit_code=255, stderr="Permission denied (publickey).")]

    payload = client.post(f"/api/servers/{server_id}/connection-test").json()

    assert payload["ok"] is False
    assert "认证失败" in payload["error"]
    assert client.get(f"/api/servers/{server_id}").json()["lastConnect"]["status"] == "failed"


def test_connection_test_without_ssh_client_returns_503(
    tmp_path: Path, add_server: Callable[..., int]
) -> None:
    settings = Settings(
        app_data_dir=tmp_path / "data2",
        ssh_config_path=tmp_path / "cfg",
        frontend_dist=tmp_path / "dist",
    )
    app = create_app(settings, transport=MissingSshTransport())
    with TestClient(app) as client:
        session = app.state.container.session_factory()
        try:
            server = Server(alias="lab-x", hostname="10.0.0.1", port=22, source="ssh_config")
            session.add(server)
            session.commit()
            server_id = server.id
        finally:
            session.close()
        response = client.post(f"/api/servers/{server_id}/connection-test")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ssh_unavailable"


def test_secret_lifecycle_never_returns_plaintext(
    client: TestClient, add_server: Callable[..., int], container
) -> None:
    server_id = add_server()

    put = client.put(f"/api/servers/{server_id}/secret", json={"password": "p@ssw0rd"})

    assert put.status_code == 200
    assert "p@ssw0rd" not in put.text
    assert put.json()["hasPassword"] is True
    assert client.get(f"/api/servers/{server_id}/secret").json()["hasPassword"] is True

    session = container.session_factory()
    try:
        row = session.get(ServerSecret, server_id)
        assert row is not None
        assert b"p@ssw0rd" not in row.ciphertext
    finally:
        session.close()

    assert client.delete(f"/api/servers/{server_id}/secret").status_code == 204
    assert client.get(f"/api/servers/{server_id}/secret").json()["hasPassword"] is False


def test_secret_for_missing_server_returns_404(client: TestClient) -> None:
    response = client.put("/api/servers/999/secret", json={"password": "x"})

    assert response.status_code == 404


def test_snapshot_collects_then_reuses_cache(
    client: TestClient,
    add_server: Callable[..., int],
    transport: FakeTransport,
    probe_payload,
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(probe_payload())

    first = client.get(f"/api/servers/{server_id}/snapshot?refresh=true").json()
    second = client.get(f"/api/servers/{server_id}/snapshot").json()

    assert first["status"] == "ok"
    assert first["data"]["system"]["data"]["hostname"] == "lab-01"
    assert first["data"]["cpu"]["data"]["cores"] == 32
    assert second["stale"] is False
    assert second["collectedAt"] == first["collectedAt"]
    assert len(transport.calls) == 1


def test_snapshot_refresh_collects_again(
    client: TestClient,
    add_server: Callable[..., int],
    transport: FakeTransport,
    probe_payload,
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(probe_payload())

    client.get(f"/api/servers/{server_id}/snapshot?refresh=true")
    client.get(f"/api/servers/{server_id}/snapshot?refresh=true")

    assert len(transport.calls) == 2


def test_expired_snapshot_is_marked_stale(
    tmp_path: Path, add_server: Callable[..., int], probe_payload
) -> None:
    settings = Settings(
        app_data_dir=tmp_path / "data3",
        ssh_config_path=tmp_path / "cfg",
        frontend_dist=tmp_path / "dist",
        snapshot_ttl_seconds=0,
    )
    transport = FakeTransport(default_stdout=json.dumps(probe_payload()))
    app = create_app(settings, transport=transport)
    with TestClient(app) as client:
        session = app.state.container.session_factory()
        try:
            server = Server(alias="lab-s", hostname="10.0.0.1", port=22, source="ssh_config")
            session.add(server)
            session.commit()
            server_id = server.id
        finally:
            session.close()
        client.get(f"/api/servers/{server_id}/snapshot?refresh=true")
        cached = client.get(f"/api/servers/{server_id}/snapshot").json()

    assert cached["stale"] is True
    assert cached["ageSeconds"] >= 0
    assert len(transport.calls) == 1


def test_snapshot_failure_returns_field_level_errors(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.responses = [
        _result(exit_code=255, stderr="ssh: connect to host 10.0.0.1 port 22: Connection timed out")
    ]

    payload = client.get(f"/api/servers/{server_id}/snapshot?refresh=true").json()

    assert payload["status"] == "failed"
    assert "连接超时" in payload["error"]
    assert payload["data"]["system"]["status"] == "error"
    assert payload["data"]["gpu"]["data"] is None


def test_gpu_unavailable_is_not_zeroed(
    client: TestClient,
    add_server: Callable[..., int],
    transport: FakeTransport,
    probe_payload,
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(
        probe_payload(
            {"gpu": {"status": "unavailable", "error": "未检测到 nvidia-smi", "data": None}}
        )
    )

    payload = client.get(f"/api/servers/{server_id}/snapshot?refresh=true").json()

    assert payload["status"] == "partial"
    assert payload["data"]["gpu"]["status"] == "unavailable"
    assert payload["data"]["gpu"]["data"] is None


def test_services_endpoint_marks_protected_units(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(
        {
            "probeVersion": 1,
            "services": {
                "status": "ok",
                "error": None,
                "data": {
                    "runningCount": 2,
                    "failedCount": 0,
                    "failedUnits": [],
                    "units": [
                        {
                            "name": "ssh.service",
                            "load": "loaded",
                            "active": "active",
                            "sub": "running",
                            "description": "ssh",
                        },
                        {
                            "name": "nginx.service",
                            "load": "loaded",
                            "active": "active",
                            "sub": "running",
                            "description": "web",
                        },
                    ],
                },
            },
        }
    )

    payload = client.get(f"/api/servers/{server_id}/services?refresh=true").json()

    units = {unit["name"]: unit for unit in payload["units"]}
    assert payload["status"] == "ok"
    assert units["ssh.service"]["protected"] is True
    assert units["nginx.service"]["protected"] is False


def test_services_failure_returns_error_status(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.responses = [_result(exit_code=1, stderr="Access denied")]

    payload = client.get(f"/api/servers/{server_id}/services?refresh=true").json()

    assert payload["status"] == "error"
    assert payload["units"] == []


def test_logs_endpoint_returns_lines(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = "line1\nline2\n"

    payload = client.get(f"/api/servers/{server_id}/logs?unit=nginx&lines=50").json()

    assert payload["lines"] == ["line1", "line2"]
    assert "journalctl -u nginx.service -n 50" in payload["command"]
    assert payload["exitCode"] == 0


def test_logs_endpoint_rejects_invalid_unit(
    client: TestClient, add_server: Callable[..., int]
) -> None:
    server_id = add_server()

    response = client.get(f"/api/servers/{server_id}/logs?unit=bad;rm -rf /")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_failed"


def test_logs_failure_reports_error(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.responses = [_result(exit_code=1, stderr="No journal files were found.")]

    payload = client.get(f"/api/servers/{server_id}/logs").json()

    assert payload["lines"] == []
    assert payload["error"]


def test_audit_endpoint_records_actions(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    transport.default_stdout = "labops-ok"
    client.post(f"/api/servers/{server_id}/connection-test")

    payload = client.get("/api/audit?limit=10").json()

    assert payload["entries"]
    actions = {entry["action"] for entry in payload["entries"]}
    assert "connection_test" in actions
    assert all("password" not in entry["detail"].lower() for entry in payload["entries"])


def test_validation_error_uses_contract_shape(client: TestClient) -> None:
    response = client.post("/api/servers/import", json={"unknownField": True})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "validation_failed"
    assert detail["message"]
    assert detail["fields"][0]["field"] == "unknownField"


def test_unknown_route_returns_404(client: TestClient) -> None:
    response = client.get("/api/nope")

    assert response.status_code == 404


def test_snapshot_for_unknown_server_returns_404(client: TestClient) -> None:
    response = client.get("/api/servers/777/snapshot")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "server_not_found"


def test_servers_are_sorted_by_alias(client: TestClient, add_server: Callable[..., int]) -> None:
    add_server("lab-b")
    add_server("lab-a")

    aliases = [server["alias"] for server in client.get("/api/servers").json()["servers"]]

    assert aliases == ["lab-a", "lab-b"]


def test_secret_is_not_included_in_server_listing(
    client: TestClient, add_server: Callable[..., int], container
) -> None:
    server_id = add_server()
    client.put(f"/api/servers/{server_id}/secret", json={"password": "hidden"})

    listing = client.get("/api/servers").json()

    assert listing["servers"][0]["hasSudoPassword"] is True
    assert "hidden" not in json.dumps(listing)


def test_import_audit_entry_is_written(client: TestClient, ssh_config_file: Path) -> None:
    client.post("/api/servers/import", json={})

    entries = client.get("/api/audit").json()["entries"]

    assert any(entry["action"] == "import_ssh_config" for entry in entries)
