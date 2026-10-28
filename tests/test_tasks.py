"""后台任务测试：提交校验、成功/失败路径、取消、重启恢复与 SSE。"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from lab_ops_panel.app import create_app
from lab_ops_panel.config import Settings
from lab_ops_panel.models import Server, Task, TaskStep
from lab_ops_panel.ssh.transport import CommandResult, FakeTransport, TransportError


def _secret(client: TestClient, server_id: int, password: str = "pw") -> None:
    response = client.put(f"/api/servers/{server_id}/secret", json={"password": password})
    assert response.status_code == 200, response.text


def _body(
    server_id: int,
    *,
    action: str = "service_restart",
    params: dict | None = None,
    confirm: bool = True,
    key: str | None = "idem-1",
) -> dict:
    return {
        "serverId": server_id,
        "action": action,
        "params": {"unit": "nginx"} if params is None else params,
        "confirm": confirm,
        "idempotencyKey": key,
    }


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=[], command="", exit_code=0, stdout=stdout, stderr="", duration_ms=1)


def _fail(stderr: str = "", exit_code: int = 1) -> CommandResult:
    return CommandResult(
        argv=[], command="", exit_code=exit_code, stdout="", stderr=stderr, duration_ms=1
    )


class BlockingTransport:
    """阻塞直到被取消，用于验证运行中任务可取消。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.calls: list[dict] = []

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
        self.calls.append({"target": target, "command": command})
        self.started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return CommandResult(
                    argv=[],
                    command=command,
                    exit_code=None,
                    stdout="",
                    stderr="",
                    duration_ms=1,
                    cancelled=True,
                )
            time.sleep(0.01)
        return _ok()


class RaisingTransport:
    def run(self, *_args, **_kwargs):
        raise TransportError("找不到 SSH 客户端 'ssh'")


def test_service_restart_runs_preflight_sudo_and_verify(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport, wait_task
) -> None:
    server_id = add_server()
    _secret(client, server_id, "pw")
    transport.responses = [_ok("LoadState=loaded\n"), _ok(""), _ok("active\n")]

    response = client.post("/api/tasks", json=_body(server_id))

    assert response.status_code == 201, response.text
    task = wait_task(client, response.json()["id"])
    assert task["status"] == "succeeded"
    assert [step["name"] for step in task["steps"]] == ["预检", "重启服务", "校验状态"]
    assert [step["status"] for step in task["steps"]] == ["ok", "ok", "ok"]
    assert task["progress"] == 100
    assert task["params"]["unit"] == "nginx.service"
    assert len(transport.calls) == 3
    assert transport.calls[1].command.startswith("sudo -S -p '' systemctl restart nginx.service")
    assert transport.calls[1].stdin_data == b"pw\n"
    assert transport.calls[0].stdin_data is None


def test_missing_idempotency_key_is_rejected(
    client: TestClient, add_server: Callable[..., int]
) -> None:
    server_id = add_server()
    _secret(client, server_id)

    response = client.post("/api/tasks", json=_body(server_id, key=None))

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_missing_confirmation_is_rejected(
    client: TestClient, add_server: Callable[..., int]
) -> None:
    server_id = add_server()
    _secret(client, server_id)

    response = client.post("/api/tasks", json=_body(server_id, confirm=False))

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "confirmation_required"


def test_duplicate_idempotency_key_conflicts(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport
) -> None:
    server_id = add_server()
    _secret(client, server_id)
    transport.responses = [_ok(), _ok(), _ok()]
    first = client.post("/api/tasks", json=_body(server_id, key="dup-key"))
    assert first.status_code == 201

    second = client.post("/api/tasks", json=_body(server_id, key="dup-key"))

    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == "duplicate_idempotency_key"
    assert detail["existingTaskId"] == first.json()["id"]


def test_unknown_action_is_rejected(client: TestClient, add_server: Callable[..., int]) -> None:
    server_id = add_server()

    response = client.post("/api/tasks", json=_body(server_id, action="rm_rf", params={}))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_action"


def test_protected_service_is_rejected(client: TestClient, add_server: Callable[..., int]) -> None:
    server_id = add_server()
    _secret(client, server_id)

    response = client.post(
        "/api/tasks", json=_body(server_id, action="service_stop", params={"unit": "ssh"})
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "protected_service"


def test_sudo_password_missing_is_rejected(
    client: TestClient, add_server: Callable[..., int]
) -> None:
    server_id = add_server()

    response = client.post("/api/tasks", json=_body(server_id))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "sudo_password_missing"


def test_disabled_server_cannot_submit(client: TestClient, add_server: Callable[..., int]) -> None:
    server_id = add_server(enabled=False)

    response = client.post("/api/tasks", json=_body(server_id))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "server_disabled"


def test_missing_server_returns_404(client: TestClient) -> None:
    response = client.post("/api/tasks", json=_body(9999))

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "server_not_found"


def test_sudo_failure_marks_task_failed(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport, wait_task
) -> None:
    server_id = add_server()
    _secret(client, server_id)
    transport.responses = [
        _ok("LoadState=loaded\n"),
        _fail("sudo: a password is required", exit_code=1),
        _ok("active\n"),
    ]

    response = client.post("/api/tasks", json=_body(server_id))
    task = wait_task(client, response.json()["id"])

    assert task["status"] == "failed"
    assert "sudo 需要密码" in task["error"]
    assert task["steps"][1]["status"] == "failed"
    assert task["steps"][1]["exitCode"] == 1


def test_timeout_marks_task_failed(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport, wait_task
) -> None:
    server_id = add_server()
    transport.responses = [
        CommandResult(
            argv=[], command="", exit_code=None, stdout="", stderr="", duration_ms=1, timed_out=True
        )
    ]

    response = client.post(
        "/api/tasks", json=_body(server_id, action="host_check", params={}, key=None)
    )
    task = wait_task(client, response.json()["id"])

    assert task["status"] == "failed"
    assert "超时" in task["error"]


def test_transport_error_marks_task_failed(
    tmp_path, add_server: Callable[..., int], wait_task
) -> None:
    settings = Settings(
        app_data_dir=tmp_path / "data2",
        ssh_config_path=tmp_path / "cfg",
        frontend_dist=tmp_path / "dist",
    )
    app = create_app(settings, transport=RaisingTransport())
    with TestClient(app) as client:
        session = app.state.container.session_factory()
        try:
            server = Server(alias="lab-x", hostname="10.0.0.1", port=22, source="ssh_config")
            session.add(server)
            session.commit()
            server_id = server.id
        finally:
            session.close()
        response = client.post(
            "/api/tasks", json=_body(server_id, action="host_check", params={}, key=None)
        )
        task = wait_task(client, response.json()["id"])

    assert task["status"] == "failed"
    assert "SSH 客户端" in task["error"]


def test_running_task_can_be_cancelled(tmp_path, wait_task) -> None:
    blocking = BlockingTransport()
    settings = Settings(
        app_data_dir=tmp_path / "data3",
        ssh_config_path=tmp_path / "cfg",
        frontend_dist=tmp_path / "dist",
    )
    app = create_app(settings, transport=blocking)
    with TestClient(app) as client:
        session = app.state.container.session_factory()
        try:
            server = Server(alias="lab-b", hostname="10.0.0.1", port=22, source="ssh_config")
            session.add(server)
            session.commit()
            server_id = server.id
        finally:
            session.close()
        created = client.post(
            "/api/tasks", json=_body(server_id, action="host_check", params={}, key=None)
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert blocking.started.wait(5.0) is True

        cancelled = client.post(f"/api/tasks/{task_id}/cancel")

        assert cancelled.status_code == 202
        task = wait_task(client, task_id)
        assert task["status"] == "cancelled"
        assert task["error"] == "任务已取消"


def test_cannot_cancel_finished_task(
    client: TestClient, add_server: Callable[..., int], transport: FakeTransport, wait_task
) -> None:
    server_id = add_server()
    transport.responses = [_ok("LoadState=loaded\n"), _ok(""), _ok("active\n")]
    _secret(client, server_id)
    created = client.post("/api/tasks", json=_body(server_id))
    wait_task(client, created.json()["id"])

    response = client.post(f"/api/tasks/{created.json()['id']}/cancel")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "task_finished"


def test_queued_task_can_be_cancelled(container, add_server: Callable[..., int]) -> None:
    server_id = add_server()
    session = container.session_factory()
    try:
        task = Task(
            id="queued-task",
            server_id=server_id,
            action="host_check",
            action_label="主机检查",
            risk="low",
            params={},
            status="queued",
            progress=0,
            output_tail="",
        )
        session.add(task)
        session.commit()
    finally:
        session.close()

    result = container.tasks.cancel("queued-task")

    assert result.status == "cancelled"
    assert result.error is not None
    assert result.finished_at is not None


def test_recovery_marks_unfinished_tasks_interrupted(
    container, add_server: Callable[..., int]
) -> None:
    server_id = add_server()
    session = container.session_factory()
    try:
        task = Task(
            id="stale-task",
            server_id=server_id,
            action="service_restart",
            action_label="重启服务",
            risk="medium",
            params={"unit": "nginx.service"},
            status="running",
            progress=40,
            output_tail="",
            started_at=datetime.now(UTC),
        )
        session.add(task)
        session.add(TaskStep(task_id="stale-task", seq=1, name="预检", status="running"))
        session.commit()
    finally:
        session.close()

    count = container.tasks.recover_on_startup()

    session = container.session_factory()
    try:
        task = session.get(Task, "stale-task")
        assert task.status == "interrupted"
        assert "重启" in (task.error or "")
        assert task.finished_at is not None
        step = session.query(TaskStep).filter_by(task_id="stale-task").one()
        assert step.status == "failed"
    finally:
        session.close()
    assert count >= 1


def test_collect_snapshot_persists_cache(
    client: TestClient,
    add_server: Callable[..., int],
    transport: FakeTransport,
    probe_payload,
    wait_task,
) -> None:
    server_id = add_server()
    transport.default_stdout = json.dumps(probe_payload())

    created = client.post(
        "/api/tasks",
        json=_body(server_id, action="collect_snapshot", params={}, key=None),
    )
    task = wait_task(client, created.json()["id"])

    assert task["status"] == "succeeded"
    snapshot = client.get(f"/api/servers/{server_id}/snapshot").json()
    assert snapshot["status"] == "ok"
    assert snapshot["data"]["system"]["data"]["hostname"] == "lab-01"
    assert len(transport.calls) == 1


def test_output_tail_is_bounded(
    client: TestClient,
    add_server: Callable[..., int],
    transport: FakeTransport,
    wait_task,
) -> None:
    server_id = add_server()
    transport.responses = [_ok("x" * 20000)]

    created = client.post(
        "/api/tasks", json=_body(server_id, action="host_check", params={}, key=None)
    )
    task = wait_task(client, created.json()["id"])

    assert task["status"] == "succeeded"
    assert len(task["outputTail"]) <= 4001
    assert task["steps"][0]["outputTail"]


def test_query_logs_runs_journalctl(
    client: TestClient,
    add_server: Callable[..., int],
    transport: FakeTransport,
    wait_task,
) -> None:
    server_id = add_server()
    transport.responses = [_ok("line1\nline2\n")]

    created = client.post(
        "/api/tasks",
        json=_body(
            server_id,
            action="query_logs",
            params={"unit": "nginx", "lines": 20},
            key=None,
        ),
    )
    task = wait_task(client, created.json()["id"])

    assert task["status"] == "succeeded"
    assert "journalctl -u nginx.service -n 20" in transport.calls[0].command


def test_run_command_is_disabled_via_api(
    client: TestClient, add_server: Callable[..., int]
) -> None:
    server_id = add_server()

    response = client.post(
        "/api/tasks",
        json=_body(server_id, action="run_command", params={"command": "id"}),
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "action_disabled"


def test_task_list_filters_by_server_and_status(
    client: TestClient,
    add_server: Callable[..., int],
    transport: FakeTransport,
    wait_task,
) -> None:
    server_id = add_server()
    transport.responses = [_ok("LoadState=loaded\n"), _ok(""), _ok("active\n")]
    _secret(client, server_id)
    created = client.post("/api/tasks", json=_body(server_id))
    wait_task(client, created.json()["id"])

    listing = client.get(f"/api/tasks?serverId={server_id}&status=succeeded").json()

    assert listing["count"] == 1
    assert listing["tasks"][0]["serverAlias"] == "lab-01"
    empty = client.get(f"/api/tasks?serverId={server_id}&status=failed").json()
    assert empty["count"] == 0


def test_sse_stream_returns_snapshot_and_done(
    client: TestClient,
    add_server: Callable[..., int],
    transport: FakeTransport,
    wait_task,
) -> None:
    server_id = add_server()
    transport.responses = [_ok("Linux lab-01\n")]
    created = client.post(
        "/api/tasks", json=_body(server_id, action="host_check", params={}, key=None)
    )
    task_id = created.json()["id"]
    wait_task(client, task_id)

    response = client.get(f"/api/tasks/{task_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: status" in body
    assert "event: step" in body
    assert "event: done" in body
    assert '"status":"succeeded"' in body


def test_events_unknown_task_returns_404(client: TestClient) -> None:
    response = client.get("/api/tasks/does-not-exist/events")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "task_not_found"
