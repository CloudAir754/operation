"""只读 API 与启动令牌测试。"""

import base64
import struct
from pathlib import Path

from fastapi.testclient import TestClient

from lab_ops_panel.app import create_app
from lab_ops_panel.application.credentials import export_initial_credentials
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


def test_text_import_and_password_policy_preview(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """预览 API 不执行用户创建，也不返回可用明文密码。"""

    monkeypatch.setattr("lab_ops_panel.api.routes._existing_usernames", lambda: {"existing"})
    client, headers = _client(tmp_path)
    with client:
        imported = client.post(
            "/api/v1/user-imports/preview/text",
            headers=headers,
            json={"text": "alice 张三\nexisting 李四"},
        )
        password = client.get("/api/v1/password-policy/default", headers=headers)

    assert imported.status_code == 200
    assert imported.json()["counts"] == {"will_create": 1, "exists": 1, "invalid": 0}
    assert password.status_code == 200
    assert password.json()["masked_example"] == "•" * 20
    assert "password" not in password.json()


def test_persistent_tasks_are_visible_through_api(tmp_path: Path) -> None:
    """任务页读取持久化 Task/TaskStep，而不是仅依赖内存状态。"""

    client, headers = _client(tmp_path)
    with client:
        task = client.app.state.tasks.create_or_get(
            "test", "idempotency-key-123", {"username": "alice"}, "alice", ["预检"]
        )
        listed = client.get("/api/v1/tasks", headers=headers)
        detail = client.get(f"/api/v1/tasks/{task['id']}", headers=headers)

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == task["id"]
    assert detail.json()["steps"][0]["name"] == "预检"


def test_user_creation_requires_trusted_helper(tmp_path: Path) -> None:
    """未安装 root 所有的可信助手时，实际写操作保持关闭。"""

    client, headers = _client(tmp_path)
    with client:
        response = client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "rows": [{"username": "newaccount", "display_name": "张三"}],
                "idempotency_key": "create-users-key-123",
            },
        )

    assert response.status_code == 503
    assert "特权助手" in response.json()["detail"]


def test_user_creation_revalidates_before_starting(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """提交创建时必须再次以操作系统账号事实拒绝重名。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    monkeypatch.setattr("lab_ops_panel.api.routes._existing_usernames", lambda: {"existing"})
    client, headers = _client(tmp_path)
    with client:
        response = client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "rows": [{"username": "existing", "display_name": "张三"}],
                "idempotency_key": "create-users-key-123",
            },
        )

    assert response.status_code == 409


def test_credentials_download_and_explicit_delete(tmp_path: Path) -> None:
    """一次性凭据只能经关联任务下载，并由管理员显式删除。"""

    client, headers = _client(tmp_path)
    with client:
        task = client.app.state.tasks.create_or_get(
            "create_users",
            "create-users-key-123",
            {"rows": [{"username": "alice", "display_name": "张三"}]},
            "批量用户:1",
            ["导出"],
        )
        export_initial_credentials(tmp_path, task["id"], [("alice", "张三", "temporary-password")])
        downloaded = client.get(f"/api/v1/tasks/{task['id']}/credentials", headers=headers)
        deleted = client.delete(f"/api/v1/tasks/{task['id']}/credentials", headers=headers)
        missing = client.get(f"/api/v1/tasks/{task['id']}/credentials", headers=headers)

    assert downloaded.status_code == 200
    assert "temporary-password" in downloaded.text
    assert downloaded.headers["cache-control"] == "no-store"
    assert deleted.status_code == 204
    assert missing.status_code == 404


def test_ssh_and_sudo_actions_use_revalidated_uid(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SSH 与 sudo API 只把重新读取到的稳定 UID 交给任务管理器。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    monkeypatch.setattr(
        "lab_ops_panel.api.routes.local_users",
        lambda: [{"username": "alice", "uid": 1000, "account_source": "local"}],
    )
    key_type = "ssh-ed25519"
    blob = struct.pack(">I", len(key_type)) + key_type.encode() + b"test-key-material"
    key = f"{key_type} {base64.b64encode(blob).decode()} test@example"
    calls = []
    task = {"id": "task123", "state": "queued", "steps": []}
    client, headers = _client(tmp_path)
    with client:
        manager = client.app.state.account_access
        manager.start_ssh_import = lambda username, uid, keys, idempotency_key: (
            calls.append(("ssh", username, uid, keys[0].fingerprint, idempotency_key)) or task
        )
        manager.start_sudo_change = lambda username, uid, grant, idempotency_key: (
            calls.append(("sudo", username, uid, grant, idempotency_key)) or task
        )
        ssh = client.post(
            "/api/v1/users/alice/ssh-keys",
            headers=headers,
            json={"keys": key, "idempotency_key": "ssh-import-key-123"},
        )
        sudo = client.post(
            "/api/v1/users/alice/sudo/grant",
            headers=headers,
            json={"idempotency_key": "sudo-grant-key-123"},
        )

    assert ssh.status_code == sudo.status_code == 202
    assert calls[0][1:3] == ("alice", 1000)
    assert calls[1][1:4] == ("alice", 1000, True)
