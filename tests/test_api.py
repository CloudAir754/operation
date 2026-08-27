"""只读 API 与启动令牌测试。"""

from pathlib import Path

from fastapi.testclient import TestClient

from lab_ops_panel.app import create_app
from lab_ops_panel.config import Settings


def _client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    settings = Settings(data_dir=tmp_path, session_token="test-session-token")
    return TestClient(create_app(settings)), {"X-LabOps-Token": settings.session_token}


def test_api_rejects_missing_token(tmp_path: Path) -> None:
    """其他本机网页不能在不知道会话令牌时读取 API。"""

    with _client(tmp_path)[0] as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 403


def test_health_accepts_valid_token(tmp_path: Path) -> None:
    """正确令牌可访问只读健康检查。"""

    client, headers = _client(tmp_path)
    with client:
        response = client.get("/api/v1/health", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_overview_degrades_without_gpu(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """GPU 缺失不能令 CPU、内存和挂载信息接口失败。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.gpu_snapshot",
        lambda: {"available": False, "reason": "NVMLError_LibraryNotFound", "devices": []},
    )
    client, headers = _client(tmp_path)
    with client:
        response = client.get("/api/v1/overview", headers=headers)
    body = response.json()
    assert response.status_code == 200
    assert body["gpu"]["available"] is False
    assert body["system"]["hostname"]
    assert body["resources"]["memory"]["total"] > 0


def test_resources_returns_refreshable_snapshot(tmp_path: Path) -> None:
    """独立资源接口为前端短周期刷新提供 CPU 和内存快照。"""

    client, headers = _client(tmp_path)
    with client:
        response = client.get("/api/v1/resources", headers=headers)
    assert response.status_code == 200
    assert response.json()["cpu"]["logical_cores"] > 0
    assert response.json()["memory"]["total"] > 0


def test_users_have_stable_numeric_identity(tmp_path: Path) -> None:
    """账号响应包含 UID/GID，不能只依赖可变用户名。"""

    client, headers = _client(tmp_path)
    with client:
        response = client.get("/api/v1/users", headers=headers)
    assert response.status_code == 200
    assert response.json()
    assert all(
        isinstance(item["uid"], int) and isinstance(item["gid"], int) for item in response.json()
    )


def test_home_scan_api_requires_structured_validated_parameters(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """扫描 API 只接受受限用户名、幂等键和范围参数。"""

    task = {
        "id": "task-id",
        "username": "alice",
        "root": "/home/alice",
        "max_depth": 5,
        "max_nodes": 100,
        "state": "completed",
        "nodes_scanned": 1,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
    }

    class FakeManager:
        def start(self, username, idempotency_key, max_depth, max_nodes):  # type: ignore[no-untyped-def]
            assert username == "alice"
            assert idempotency_key == "idempotency-key-123"
            return task

        def get(self, task_id):  # type: ignore[no-untyped-def]
            return task

    monkeypatch.setattr("lab_ops_panel.api.routes.home_scan_manager", FakeManager())
    client, headers = _client(tmp_path)
    with client:
        accepted = client.post(
            "/api/v1/home-scans",
            headers=headers,
            json={
                "username": "alice",
                "idempotency_key": "idempotency-key-123",
                "max_depth": 5,
                "max_nodes": 100,
            },
        )
        rejected = client.post(
            "/api/v1/home-scans",
            headers=headers,
            json={"username": "../../etc", "idempotency_key": "idempotency-key-456"},
        )
        events = client.get("/api/v1/home-scans/task-id/events", headers=headers)

    assert accepted.status_code == 202
    assert rejected.status_code == 422
    assert events.headers["content-type"].startswith("text/event-stream")
    assert '"state": "completed"' in events.text
