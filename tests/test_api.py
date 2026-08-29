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


def test_pam_change_requires_preview_capability_and_exact_confirmation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """PAM 修改只有能力检测通过且确认文本完全匹配时才启动。"""

    snapshot = {
        "modification_supported": True,
        "modification_reason": None,
        "settings": {"minlen": 15, "usercheck": True},
        "module_settings": {},
    }
    monkeypatch.setattr("lab_ops_panel.api.routes.pam_policy_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    task = {"id": "pamtask", "state": "queued", "steps": []}
    calls = []
    client, headers = _client(tmp_path)
    with client:
        client.app.state.pam_policy.start_update = lambda changes, idempotency_key: (
            calls.append((changes, idempotency_key)) or task
        )
        preview = client.post(
            "/api/v1/pam-policy/change-preview",
            headers=headers,
            json={"changes": {"minlen": 20, "maxrepeat": 3}},
        )
        rejected = client.post(
            "/api/v1/pam-policy/apply",
            headers=headers,
            json={
                "changes": {"minlen": 20},
                "confirmation": "错误文本",
                "idempotency_key": "pam-update-key-123",
            },
        )
        accepted = client.post(
            "/api/v1/pam-policy/apply",
            headers=headers,
            json={
                "changes": {"minlen": 20},
                "confirmation": "修改全机PAM策略",
                "idempotency_key": "pam-update-key-123",
            },
        )

    assert preview.status_code == 200
    assert preview.json()["effective_after"]["minlen"] == 20
    assert rejected.status_code == 400
    assert accepted.status_code == 202
    assert calls == [({"minlen": 20}, "pam-update-key-123")]


def test_pam_preview_reports_module_option_conflict(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """模块行参数覆盖主配置时，预览必须明确阻断修改。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.pam_policy_snapshot",
        lambda: {
            "modification_supported": True,
            "modification_reason": None,
            "settings": {"minlen": 24},
            "module_settings": {"minlen": 24},
        },
    )
    client, headers = _client(tmp_path)
    with client:
        response = client.post(
            "/api/v1/pam-policy/change-preview",
            headers=headers,
            json={"changes": {"minlen": 20}},
        )

    assert response.status_code == 200
    assert response.json()["supported"] is False
    assert response.json()["module_conflicts"] == ["minlen"]


def test_port_lookup_degrades_without_privileged_helper(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """助手未安装时仍返回普通用户可见结果，并明确标记不完整。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": False, "reason": "未安装", "path": str(path)},
    )
    monkeypatch.setattr(
        "lab_ops_panel.api.routes.port_usage",
        lambda port, protocol: {
            "port": port,
            "protocol": protocol,
            "complete": False,
            "reason": "权限受限",
            "connections": [],
        },
    )
    client, headers = _client(tmp_path)
    with client:
        response = client.get("/api/v1/ports/8080?protocol=tcp", headers=headers)

    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert response.json()["port"] == 8080


def test_process_termination_revalidates_pid_uid_and_confirmation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """单进程终止提交前再次核对 PID、UID、启动时间和确认文本。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    monkeypatch.setattr("lab_ops_panel.api.routes.minimum_regular_uid", lambda: 1000)
    monkeypatch.setattr(
        "lab_ops_panel.api.routes.process_identity",
        lambda pid: {"pid": pid, "uid": 2001, "create_time": 1234.5},
    )
    task = {"id": "processtask", "state": "queued", "steps": []}
    calls = []
    client, headers = _client(tmp_path)
    with client:
        client.app.state.process_control.start_process_termination = (
            lambda pid, uid, created, grace, key: (
                calls.append((pid, uid, created, grace, key)) or task
            )
        )
        rejected = client.post(
            "/api/v1/processes/4321/terminate",
            headers=headers,
            json={
                "expected_uid": 2001,
                "expected_create_time": 1234.5,
                "confirmation": "错误",
                "idempotency_key": "process-stop-key-123",
            },
        )
        accepted = client.post(
            "/api/v1/processes/4321/terminate",
            headers=headers,
            json={
                "expected_uid": 2001,
                "expected_create_time": 1234.5,
                "confirmation": "结束进程:4321",
                "idempotency_key": "process-stop-key-123",
            },
        )

    assert rejected.status_code == 400
    assert accepted.status_code == 202
    assert calls == [(4321, 2001, 1234.5, 5, "process-stop-key-123")]


def test_terminate_all_user_processes_uses_stable_uid(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """按用户结束进程只能针对普通本机账号，并向任务传稳定 UID。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    monkeypatch.setattr("lab_ops_panel.api.routes.minimum_regular_uid", lambda: 1000)
    monkeypatch.setattr(
        "lab_ops_panel.api.routes.local_users",
        lambda: [{"username": "alice", "uid": 2001, "account_source": "local"}],
    )
    task = {"id": "uidtask", "state": "queued", "steps": []}
    calls = []
    client, headers = _client(tmp_path)
    with client:
        client.app.state.process_control.start_uid_termination = lambda username, uid, grace, key: (
            calls.append((username, uid, grace, key)) or task
        )
        response = client.post(
            "/api/v1/users/alice/processes/terminate",
            headers=headers,
            json={
                "confirmation": "结束全部进程:alice",
                "idempotency_key": "uid-process-stop-key-123",
            },
        )

    assert response.status_code == 202
    assert calls == [("alice", 2001, 5, "uid-process-stop-key-123")]


def test_user_delete_requires_successful_matching_preflight_and_username_confirmation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """用户删除必须引用近期成功预检，并完整输入用户名二次确认。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    monkeypatch.setattr("lab_ops_panel.api.routes.minimum_regular_uid", lambda: 1000)
    monkeypatch.setattr(
        "lab_ops_panel.api.routes.local_users",
        lambda: [
            {
                "username": "alice",
                "uid": 2001,
                "account_source": "local",
                "home": "/home/alice",
            }
        ],
    )
    response_task = {"id": "deletetask", "state": "queued", "steps": []}
    calls = []
    client, headers = _client(tmp_path)
    with client:
        preflight = client.app.state.tasks.create_or_get(
            "user_delete_preflight",
            "delete-preflight-key-123",
            {"username": "alice", "uid": 2001, "home": "/home/alice"},
            "UID:2001",
            ["账号", "依赖", "home"],
        )
        client.app.state.tasks.transition(preflight["id"], "prechecking")
        client.app.state.tasks.transition(preflight["id"], "running")
        client.app.state.tasks.update_step(
            preflight["id"],
            3,
            "succeeded",
            {"blockers": [], "delete_home_supported": True},
        )
        client.app.state.tasks.transition(preflight["id"], "succeeded")
        client.app.state.user_deletion.start_delete = (
            lambda username, uid, home, delete_home, preflight_id, key: (
                calls.append((username, uid, home, delete_home, preflight_id, key)) or response_task
            )
        )
        rejected = client.post(
            "/api/v1/users/alice/delete",
            headers=headers,
            json={
                "preflight_task_id": preflight["id"],
                "delete_home": True,
                "confirmation": "Alice",
                "idempotency_key": "delete-user-key-123",
            },
        )
        accepted = client.post(
            "/api/v1/users/alice/delete",
            headers=headers,
            json={
                "preflight_task_id": preflight["id"],
                "delete_home": True,
                "confirmation": "alice",
                "idempotency_key": "delete-user-key-123",
            },
        )

    assert rejected.status_code == 400
    assert accepted.status_code == 202
    assert calls == [
        (
            "alice",
            2001,
            "/home/alice",
            True,
            preflight["id"],
            "delete-user-key-123",
        )
    ]


def test_file_transfer_copy_and_cleanup_bind_successful_tasks(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """文件复制绑定近期预检，源清理绑定成功复制结果和完整路径确认。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    response_task = {"id": "response", "state": "queued", "steps": []}
    calls = []
    client, headers = _client(tmp_path)
    with client:
        repository = client.app.state.tasks
        preflight = repository.create_or_get(
            "file_transfer_preflight",
            "file-preview-key-123",
            {"source": "/data/source", "target_directory": "/mnt/archive"},
            "/data/source",
            ["边界", "容量"],
        )
        repository.transition(preflight["id"], "prechecking")
        repository.transition(preflight["id"], "running")
        repository.update_step(
            preflight["id"],
            2,
            "succeeded",
            {"source_summary": {"device": 10, "inode": 20}},
        )
        repository.transition(preflight["id"], "succeeded")
        copy_record = repository.create_or_get(
            "file_transfer_copy",
            "file-copy-record-123",
            {"source": "/data/source"},
            "/data/source",
            ["预检", "复制", "发布"],
        )
        repository.transition(copy_record["id"], "prechecking")
        repository.transition(copy_record["id"], "running")
        repository.update_step(
            copy_record["id"],
            3,
            "succeeded",
            {
                "source": "/data/source",
                "destination": "/mnt/archive/source",
                "source_device": 10,
                "source_inode": 20,
                "source_retained": True,
            },
        )
        repository.transition(copy_record["id"], "succeeded")
        client.app.state.file_transfer.start_copy = lambda *args: (
            calls.append(("copy", args)) or response_task
        )
        client.app.state.file_transfer.start_cleanup = lambda *args: (
            calls.append(("cleanup", args)) or response_task
        )
        copied = client.post(
            "/api/v1/file-transfers/copy",
            headers=headers,
            json={
                "preflight_task_id": preflight["id"],
                "confirmation": "复制并保留源文件",
                "idempotency_key": "file-copy-key-123",
            },
        )
        rejected = client.post(
            "/api/v1/file-transfers/cleanup",
            headers=headers,
            json={
                "copy_task_id": copy_record["id"],
                "confirmation": "/data/other",
                "idempotency_key": "file-clean-key-123",
            },
        )
        cleaned = client.post(
            "/api/v1/file-transfers/cleanup",
            headers=headers,
            json={
                "copy_task_id": copy_record["id"],
                "confirmation": "/data/source",
                "idempotency_key": "file-clean-key-123",
            },
        )

    assert copied.status_code == cleaned.status_code == 202
    assert rejected.status_code == 400
    assert calls[0][0] == "copy" and calls[0][1][:4] == (
        "/data/source",
        "/mnt/archive",
        10,
        20,
    )
    assert calls[1][0] == "cleanup" and calls[1][1][:4] == (
        "/data/source",
        "/mnt/archive/source",
        10,
        20,
    )


def test_home_migration_requires_bound_preflight_and_confirmations(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """home 迁移、恢复、回退和清理均绑定持久化任务及明确确认文本。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    monkeypatch.setattr("lab_ops_panel.api.routes.minimum_regular_uid", lambda: 1000)
    monkeypatch.setattr(
        "lab_ops_panel.api.routes.local_users",
        lambda: [
            {
                "username": "alice",
                "uid": 2001,
                "account_source": "local",
                "home": "/home/alice",
            }
        ],
    )
    response_task = {"id": "response", "state": "queued", "steps": []}
    calls = []
    client, headers = _client(tmp_path)
    with client:
        repository = client.app.state.tasks
        preflight = repository.create_or_get(
            "home_migration_preflight",
            "home-preview-record-123",
            {
                "username": "alice",
                "uid": 2001,
                "source": "/home/alice",
                "target_parent": "/data/users",
                "safety_margin_percent": 10,
                "verify_contents": False,
            },
            "UID:2001",
            ["账号", "容量", "日志"],
        )
        repository.transition(preflight["id"], "prechecking")
        repository.transition(preflight["id"], "running")
        repository.transition(preflight["id"], "succeeded")
        execution = repository.create_or_get(
            "home_migration_execute",
            "home-execute-record-123",
            {"migration_id": preflight["id"], "preflight_task_id": preflight["id"]},
            preflight["id"],
            ["执行"],
        )
        repository.transition(execution["id"], "prechecking")
        repository.transition(execution["id"], "running")
        repository.transition(execution["id"], "interrupted", "构造中断")
        manager = client.app.state.home_migration
        manager.start_execute = lambda *args: calls.append(("execute", args)) or response_task
        manager.resume_execute = lambda *args: calls.append(("resume", args)) or response_task
        manager.start_rollback = lambda *args: calls.append(("rollback", args)) or response_task
        manager.start_cleanup = lambda *args: calls.append(("cleanup", args)) or response_task
        client.app.state.privileged.execute = lambda action, payload: {
            "migration_id": payload["migration_id"],
            "phase": "copying",
            "username": "alice",
        }

        rejected = client.post(
            "/api/v1/home-migrations/execute",
            headers=headers,
            json={
                "preflight_task_id": preflight["id"],
                "confirmation": "错误",
                "idempotency_key": "home-execute-key-123",
            },
        )
        accepted = client.post(
            "/api/v1/home-migrations/execute",
            headers=headers,
            json={
                "preflight_task_id": preflight["id"],
                "confirmation": "迁移home:alice",
                "idempotency_key": "home-execute-key-123",
            },
        )
        resumed = client.post(
            f"/api/v1/tasks/{execution['id']}/home-migration-resume",
            headers=headers,
            json={"confirmation": f"继续home迁移:{preflight['id']}"},
        )
        status = client.get(f"/api/v1/home-migrations/{preflight['id']}", headers=headers)
        rolled = client.post(
            f"/api/v1/home-migrations/{preflight['id']}/rollback",
            headers=headers,
            json={
                "confirmation": "回退home:alice",
                "idempotency_key": "home-rollback-key-123",
            },
        )
        cleaned = client.post(
            f"/api/v1/home-migrations/{preflight['id']}/cleanup",
            headers=headers,
            json={
                "confirmation": "清理原home备份:alice",
                "idempotency_key": "home-cleanup-key-123",
            },
        )

    assert rejected.status_code == 400
    assert accepted.status_code == resumed.status_code == rolled.status_code == 202
    assert cleaned.status_code == 202
    assert status.status_code == 200 and status.json()["phase"] == "copying"
    assert [name for name, _ in calls] == ["execute", "resume", "rollback", "cleanup"]


def test_disk_initialization_requires_plan_and_exact_serial_confirmation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """磁盘命令计划可预览，执行和恢复均要求序列号且不交给任务参数。"""

    monkeypatch.setattr(
        "lab_ops_panel.api.routes.helper_capability",
        lambda path: {"available": True, "reason": None, "path": str(path)},
    )
    response_task = {"id": "response", "state": "queued", "steps": []}
    calls = []
    client, headers = _client(tmp_path)
    with client:
        plan = {
            "stable_identity": "serial:SERIAL-REDACTED",
            "serial": "SERIAL-REDACTED",
            "model": "构造磁盘",
            "size": 1000,
            "label": "research",
            "mountpoint": "/mnt/research",
            "mount_options": "defaults,nofail",
            "commands": [["/usr/sbin/parted", "--script"]],
            "fstab_preview": "UUID=<new> /mnt/research ext4 defaults,nofail 0 2",
            "destructive": True,
        }

        def privileged_execute(action, payload):  # type: ignore[no-untyped-def]
            if action == "disk-initialization-plan":
                return plan
            return {"operation_id": payload["operation_id"], "phase": "partitioning"}

        client.app.state.privileged.execute = privileged_execute
        manager = client.app.state.disk_initialization
        manager.start = lambda *args: calls.append(("start", args)) or response_task
        manager.resume = lambda *args: calls.append(("resume", args)) or response_task
        planned = client.post(
            "/api/v1/disk-initializations/plan",
            headers=headers,
            json={
                "stable_identity": "serial:SERIAL-REDACTED",
                "label": "research",
                "mountpoint": "/mnt/research",
                "mount_options": ["defaults", "nofail"],
            },
        )
        rejected = client.post(
            "/api/v1/disk-initializations/execute",
            headers=headers,
            json={
                "stable_identity": "serial:SERIAL-REDACTED",
                "confirmed_serial": "WRONG",
                "label": "research",
                "mountpoint": "/mnt/research",
                "mount_options": ["defaults", "nofail"],
                "idempotency_key": "disk-execute-key-123",
            },
        )
        accepted = client.post(
            "/api/v1/disk-initializations/execute",
            headers=headers,
            json={
                "stable_identity": "serial:SERIAL-REDACTED",
                "confirmed_serial": "SERIAL-REDACTED",
                "label": "research",
                "mountpoint": "/mnt/research",
                "mount_options": ["defaults", "nofail"],
                "idempotency_key": "disk-execute-key-123",
            },
        )
        task = client.app.state.tasks.create_or_get(
            "disk_initialization",
            "disk-record-key-123",
            {
                "stable_identity": "serial:SERIAL-REDACTED",
                "label": "research",
                "mountpoint": "/mnt/research",
                "mount_options": ["defaults", "nofail"],
            },
            "serial:SERIAL-REDACTED",
            ["分区"],
        )
        resumed = client.post(
            f"/api/v1/tasks/{task['id']}/disk-initialization-resume",
            headers=headers,
            json={"confirmed_serial": "SERIAL-REDACTED"},
        )
        status = client.get(f"/api/v1/disk-initializations/{task['id']}", headers=headers)

    assert planned.status_code == 200 and planned.json()["destructive"] is True
    assert rejected.status_code == 400
    assert accepted.status_code == resumed.status_code == 202
    assert status.status_code == 200 and status.json()["phase"] == "partitioning"
    assert [name for name, _ in calls] == ["start", "resume"]
    assert calls[0][1][1] == "SERIAL-REDACTED"
