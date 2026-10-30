"""账户生命周期 API：密码规则、系统策略、批量建用户、目录迁移与新空间分配。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from accounts_support import FakeRemote, commands
from lab_ops_panel.app import create_app
from lab_ops_panel.config import Settings

SUDO_PASSWORD = "s3cret-pass"
TERMINAL_MIGRATION = ("succeeded", "failed", "cancelled", "interrupted", "rolled_back")
TERMINAL_BATCH = ("succeeded", "failed", "cancelled", "interrupted")


@pytest.fixture
def remote() -> FakeRemote:
    return FakeRemote()


@pytest.fixture
def app(settings: Settings, remote: FakeRemote, protector: Any):
    return create_app(settings, transport=remote, protector=protector)


@pytest.fixture
def server_id(add_server: Callable[..., int]) -> int:
    return add_server("lab-01")


@pytest.fixture
def sudo_server(server_id: int, container: Any) -> int:
    container.credentials.set_password(server_id, SUDO_PASSWORD)
    return server_id


def _wait(
    client: TestClient, path: str, statuses: tuple[str, ...], *, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in statuses:
            return last
        time.sleep(0.02)
    raise AssertionError(f"{path} 未在 {timeout}s 内结束：{last}")


# ---------------------------------------------------------------------------
# 密码生成规则
# ---------------------------------------------------------------------------


def test_generator_reports_unconfigured_defaults(client: TestClient) -> None:
    response = client.get("/api/accounts/generator")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["rules"]["length"] == 20
    assert body["rules"]["minUpper"] == 1
    # 示例必须脱敏：只保留前两位。
    assert body["example"].startswith(body["example"][:2])
    assert "•" in body["example"]
    assert all(check["ok"] for check in body["checks"])


def test_generator_can_be_saved_and_reloaded(client: TestClient) -> None:
    payload = {
        "rules": {
            "length": 24,
            "minUpper": 2,
            "minLower": 2,
            "minDigits": 2,
            "minSymbols": 2,
            "symbols": "!#%+",
            "excludeAmbiguous": True,
            "excludeChars": "",
        }
    }

    saved = client.put("/api/accounts/generator", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert saved.json()["rules"]["length"] == 24

    reloaded = client.get("/api/accounts/generator").json()
    assert reloaded["configured"] is True
    assert reloaded["rules"]["symbols"] == "!#%+"


def test_generator_rejects_inconsistent_rules(client: TestClient) -> None:
    response = client.put(
        "/api/accounts/generator",
        json={
            "rules": {
                "length": 8,
                "minUpper": 4,
                "minLower": 4,
                "minDigits": 4,
                "minSymbols": 4,
                "symbols": "!#",
            }
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_failed"


def test_generator_preview_applies_server_pam_floor(client: TestClient, sudo_server: int) -> None:
    response = client.post(
        "/api/accounts/generator/preview",
        json={
            "serverId": sudo_server,
            "rules": {
                "length": 10,
                "minUpper": 1,
                "minLower": 1,
                "minDigits": 1,
                "minSymbols": 1,
                "symbols": "!#",
                "excludeAmbiguous": True,
            },
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["floor"] is not None
    assert body["effectiveLength"] >= body["rules"]["length"]


# ---------------------------------------------------------------------------
# 系统密码策略
# ---------------------------------------------------------------------------


def test_policy_reports_effective_values_and_capabilities(
    client: TestClient, server_id: int
) -> None:
    response = client.get(f"/api/servers/{server_id}/accounts/policy")

    assert response.status_code == 200, response.text
    body = response.json()
    fields = {field["key"]: field for field in body["fields"]}
    assert fields["minlen"]["value"] == 8
    assert fields["minlen"]["origin"] == "conf"
    assert fields["minlen"]["writable"] is True
    assert fields["retry"]["origin"] == "pam"
    assert fields["retry"]["writable"] is True
    assert body["capabilities"]["pwqualityModule"] is True
    assert body["capabilities"]["readOnly"] is False
    assert any("enforcing" in key for key in body["unknownKeys"])
    assert body["generator"]["configured"] is False


def test_policy_preview_shows_diff_without_writing(
    client: TestClient, server_id: int, remote: FakeRemote
) -> None:
    before = len(commands(remote))

    response = client.post(
        f"/api/servers/{server_id}/accounts/policy/preview",
        json={"targets": {"minlen": 14}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applicable"] is True
    assert body["fields"]["minlen"] == {"before": 8, "after": 14}
    assert "-minlen = 8" in body["diff"]
    assert "+minlen = 14" in body["diff"]
    assert not any("install -m 644" in command for command in commands(remote)[before:])


def test_policy_apply_backs_up_writes_and_validates(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    response = client.put(
        f"/api/servers/{sudo_server}/accounts/policy",
        json={"targets": {"minlen": 14, "retry": 4}, "confirm": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    assert body["backupId"]
    assert body["validation"]["ok"] is True
    issued = commands(remote)
    assert any("LABOPS_BACKUP_FAILED" in command for command in issued)
    assert any("install -m 644" in command for command in issued)
    assert any("pwscore" in command for command in issued)
    assert "minlen = 14" in remote.policy_conf
    assert "retry=4" in remote.pam_auth
    # 手工注释与未知键必须保留
    assert "enforcing = 1" in remote.policy_conf
    assert "# 手工维护的注释必须保留" in remote.policy_conf


def test_policy_apply_restores_backup_when_validation_fails(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    original = remote.policy_conf
    # 远端“写入不生效”，用于验证失败后的自动恢复路径。
    remote.ignore_policy_write = True

    response = client.put(
        f"/api/servers/{sudo_server}/accounts/policy",
        json={"targets": {"minlen": 20}, "confirm": True},
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "policy_validation_failed"
    assert remote.policy_conf == original
    assert any("LABOPS_POLICY_RESTORE" in command for command in commands(remote))
    history = client.get(f"/api/servers/{sudo_server}/accounts/policy/changes").json()
    assert history["changes"][0]["status"] == "failed"
    assert history["changes"][0]["validation"]["restored"] is True


def test_policy_apply_requires_confirmation(client: TestClient, sudo_server: int) -> None:
    response = client.put(
        f"/api/servers/{sudo_server}/accounts/policy",
        json={"targets": {"minlen": 14}, "confirm": False},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "confirmation_required"


def test_policy_apply_requires_sudo_password(client: TestClient, server_id: int) -> None:
    response = client.put(
        f"/api/servers/{server_id}/accounts/policy",
        json={"targets": {"minlen": 14}, "confirm": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "sudo_password_missing"


def test_policy_change_can_be_restored(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    applied = client.put(
        f"/api/servers/{sudo_server}/accounts/policy",
        json={"targets": {"minlen": 16}, "confirm": True},
    ).json()
    assert "minlen = 16" in remote.policy_conf

    restored = client.post(
        f"/api/servers/{sudo_server}/accounts/policy/changes/{applied['id']}/restore"
    )

    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "restored"
    assert "minlen = 8" in remote.policy_conf
    changes = client.get(f"/api/servers/{sudo_server}/accounts/policy/changes").json()
    assert changes["count"] == 1
    assert changes["changes"][0]["status"] == "restored"


def test_policy_read_only_fields_are_blocked(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    remote.pam_auth = "auth [success=1 default=ignore] pam_unix.so nullok\n"

    response = client.put(
        f"/api/servers/{sudo_server}/accounts/policy",
        json={"targets": {"retry": 5}, "confirm": True},
    )

    # retry 不再来自 PAM 行，行内参数会被追加。
    assert response.status_code == 200, response.text
    assert "retry=5" in remote.pam_auth

    blocked = client.put(
        f"/api/servers/{sudo_server}/accounts/policy",
        json={"targets": {"minclass": 9}, "confirm": True},
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# 批量新建用户
# ---------------------------------------------------------------------------


def test_batch_creates_users_and_reveals_passwords_once(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    response = client.post(
        f"/api/servers/{sudo_server}/account-batches",
        json={
            "users": [
                {"login": "alice", "comment": "Alice", "groups": []},
                {"login": "bob", "password": "Str0ng!Password#2026"},
            ],
            "confirm": True,
        },
    )

    assert response.status_code == 201, response.text
    batch_id = response.json()["id"]
    final = _wait(client, f"/api/account-batches/{batch_id}", TERMINAL_BATCH)

    assert final["status"] == "succeeded", final
    assert [item["login"] for item in final["results"]] == ["alice", "bob"]
    assert all(item["status"] == "succeeded" for item in final["results"])
    assert set(remote.created_users) == {"alice", "bob"}

    revealed = client.post(f"/api/account-batches/{batch_id}/reveal")
    assert revealed.status_code == 200, revealed.text
    passwords = revealed.json()["passwords"]
    assert set(passwords) == {"alice", "bob"}
    assert passwords["bob"] == "Str0ng!Password#2026"
    assert len(passwords["alice"]) >= 20

    again = client.post(f"/api/account-batches/{batch_id}/reveal")
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "passwords_unavailable"


def test_batch_password_never_enters_argv(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    client.post(
        f"/api/servers/{sudo_server}/account-batches",
        json={"users": [{"login": "carol"}], "confirm": True},
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "carol" not in remote.created_users:
        time.sleep(0.02)
    batches = client.get(f"/api/servers/{sudo_server}/account-batches").json()
    batch_id = batches["batches"][0]["id"]
    passwords = client.post(f"/api/account-batches/{batch_id}/reveal").json()["passwords"]

    for command in commands(remote):
        assert passwords["carol"] not in command
    # 密码通过 stdin 写入远端 0600 临时文件。
    staged = [call for call in remote.calls if "cat >" in call["command"]]
    assert staged
    assert any(call["stdin"] for call in staged)


def test_batch_requires_sudo_password(client: TestClient, server_id: int) -> None:
    response = client.post(
        f"/api/servers/{server_id}/account-batches",
        json={"users": [{"login": "dave"}], "confirm": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "sudo_password_missing"


def test_batch_reports_existing_user_failure(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    remote.existing_users.add("erin")

    response = client.post(
        f"/api/servers/{sudo_server}/account-batches",
        json={"users": [{"login": "erin"}, {"login": "frank"}], "confirm": True},
    )
    final = _wait(client, f"/api/account-batches/{response.json()['id']}", TERMINAL_BATCH)

    statuses = {item["login"]: item["status"] for item in final["results"]}
    assert statuses == {"erin": "failed", "frank": "succeeded"}
    assert final["status"] == "failed"
    assert any("已存在" in (item["error"] or "") for item in final["results"])


def test_batch_rejects_manual_password_below_policy(client: TestClient, sudo_server: int) -> None:
    response = client.post(
        f"/api/servers/{sudo_server}/account-batches",
        json={"users": [{"login": "grace", "password": "short"}], "confirm": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "password_policy_violation"


def test_batch_validates_login_format(client: TestClient, sudo_server: int) -> None:
    response = client.post(
        f"/api/servers/{sudo_server}/account-batches",
        json={"users": [{"login": "Bad Name"}], "confirm": True},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 用户目录迁移
# ---------------------------------------------------------------------------


def test_migration_preflight_blocks_non_ext4(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    remote.filesystem = "xfs"

    response = client.post(
        f"/api/servers/{sudo_server}/home-migrations/preflight",
        json={"login": "zhangsan", "targetRoot": "/data2/users"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is False
    assert any("ext4" in blocker for blocker in body["blockers"])
    checks = {check["code"]: check for check in body["checks"]}
    assert checks["source_filesystem"]["ok"] is False


def test_migration_preflight_reports_space_plan(client: TestClient, sudo_server: int) -> None:
    response = client.post(
        f"/api/servers/{sudo_server}/home-migrations/preflight",
        json={"login": "zhangsan"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is True
    assert body["paths"]["target"] == "/data2/users/zhangsan"
    assert body["paths"]["backup"].startswith("/home/zhangsan.labops-backup-")
    assert body["space"]["requiredBytes"] > body["space"]["allocatedBytes"]
    assert body["counts"]["files"] == 12


def test_migration_runs_all_stages_and_keeps_backup(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    created = client.post(
        f"/api/servers/{sudo_server}/home-migrations",
        json={"login": "zhangsan", "confirm": True, "fullChecksum": True},
    )

    assert created.status_code == 201, created.text
    migration_id = created.json()["id"]
    final = _wait(client, f"/api/home-migrations/{migration_id}", TERMINAL_MIGRATION)

    assert final["status"] == "succeeded", final
    assert final["stage"] == "observe"
    assert final["rollbackAvailable"] is True
    assert final["cleanupAvailable"] is True
    assert final["result"]["restore"]["functional"] == "ok"
    assert remote.source_is_symlink is True
    assert remote.backup_exists is True
    assert remote.target_exists is True

    issued = commands(remote)
    for fragment in (
        "pkill -TERM",
        'mv -T -- "$source" "$backup"',
        "rsync -aHAX",
        "--out-format",
        'mv -T -- "$staging" "$target"',
        "LABOPS_FUNCTIONAL",
    ):
        assert any(fragment in command for command in issued), fragment


def test_migration_fails_when_processes_remain(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    remote.remaining_processes = 2

    response = client.post(
        f"/api/servers/{sudo_server}/home-migrations",
        json={"login": "zhangsan", "confirm": True},
    )
    final = _wait(client, f"/api/home-migrations/{response.json()['id']}", TERMINAL_MIGRATION)

    assert final["status"] == "failed"
    assert final["stage"] == "freeze"
    assert "进程" in (final["error"] or "")
    assert any("LABOPS_LOCK_AFTER" in command for command in commands(remote))
    assert remote.backup_exists is False


def test_migration_cleanup_requires_matching_path(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    created = client.post(
        f"/api/servers/{sudo_server}/home-migrations",
        json={"login": "zhangsan", "confirm": True},
    )
    migration_id = created.json()["id"]
    final = _wait(client, f"/api/home-migrations/{migration_id}", TERMINAL_MIGRATION)

    wrong = client.post(
        f"/api/home-migrations/{migration_id}/cleanup",
        json={"confirmPath": "/home/zhangsan"},
    )
    assert wrong.status_code == 422
    assert wrong.json()["detail"]["code"] == "cleanup_confirmation_mismatch"

    cleaned = client.post(
        f"/api/home-migrations/{migration_id}/cleanup",
        json={"confirmPath": final["paths"]["backup"]},
    )
    assert cleaned.status_code == 200, cleaned.text
    assert cleaned.json()["cleanedAt"]
    assert remote.backup_exists is False


def test_migration_rollback_restores_original_home(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    created = client.post(
        f"/api/servers/{sudo_server}/home-migrations",
        json={"login": "zhangsan", "confirm": True},
    )
    migration_id = created.json()["id"]
    _wait(client, f"/api/home-migrations/{migration_id}", TERMINAL_MIGRATION)

    rolled = client.post(
        f"/api/home-migrations/{migration_id}/rollback",
        json={"discardNewData": True},
    )

    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["status"] == "rolled_back"
    assert remote.source_exists is True
    assert remote.source_is_symlink is False
    assert remote.backup_exists is False


def test_migration_cancel_marks_row(client: TestClient, sudo_server: int, container: Any) -> None:
    response = client.post(
        f"/api/servers/{sudo_server}/home-migrations",
        json={"login": "zhangsan", "confirm": True},
    )
    migration_id = response.json()["id"]
    _wait(client, f"/api/home-migrations/{migration_id}", TERMINAL_MIGRATION)

    cancelled = client.post(f"/api/home-migrations/{migration_id}/cancel")
    assert cancelled.status_code == 409
    assert cancelled.json()["detail"]["code"] == "migration_finished"


def test_recover_on_startup_marks_running_migrations(
    client: TestClient, sudo_server: int, container: Any
) -> None:
    created = client.post(
        f"/api/servers/{sudo_server}/home-migrations",
        json={"login": "zhangsan", "confirm": True},
    )
    migration_id = created.json()["id"]
    _wait(client, f"/api/home-migrations/{migration_id}", TERMINAL_MIGRATION)

    with container.session_factory() as session:
        from lab_ops_panel.models import HomeMigration

        row = session.get(HomeMigration, migration_id)
        assert row is not None
        row.status = "running"
        row.stage = "copy"
        session.commit()

    counts = container.provisioning.recover_on_startup()
    assert counts["migrations"] == 1
    detail = client.get(f"/api/home-migrations/{migration_id}").json()
    assert detail["status"] == "interrupted"
    assert detail["stage"] == "copy"


def _insert_migration(container: Any, server_id: int, migration_id: str) -> None:
    from lab_ops_panel.models import HomeMigration

    with container.session_factory() as session:
        session.add(
            HomeMigration(
                id=migration_id,
                server_id=server_id,
                login="zhangsan",
                uid=1001,
                gid=1001,
                source_home="/home/zhangsan",
                target_root="/data2/users",
                target_path="/data2/users/zhangsan",
                staging_path=f"/data2/users/zhangsan.labops-staging-{migration_id}",
                backup_path=f"/home/zhangsan.labops-backup-20261030-120000-{migration_id}",
                status="interrupted",
                stage="rename",
                progress=40,
                request={},
                preflight={},
                backup_retention_days=7,
            )
        )
        session.commit()


def test_resume_reconciles_stage_from_remote_state(
    client: TestClient,
    sudo_server: int,
    container: Any,
    remote: FakeRemote,
) -> None:
    _insert_migration(container, sudo_server, "mig0000000000000000000000000001")
    migration_id = "mig0000000000000000000000000001"
    # 远端已经改名成功：源消失、备份存在、目标尚未建立。
    remote.source_exists = False
    remote.backup_exists = True
    remote.backup = f"/home/zhangsan.labops-backup-20261030-120000-{migration_id}"
    remote.target = "/data2/users/zhangsan"
    remote.staging = f"/data2/users/zhangsan.labops-staging-{migration_id}"

    resumed = client.post(f"/api/home-migrations/{migration_id}/resume")

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["stage"] == "copy"
    final = _wait(client, f"/api/home-migrations/{migration_id}", TERMINAL_MIGRATION)
    assert final["status"] == "succeeded", final


def test_resume_rejects_ambiguous_remote_state(
    client: TestClient,
    sudo_server: int,
    container: Any,
    remote: FakeRemote,
) -> None:
    _insert_migration(container, sudo_server, "mig0000000000000000000000000002")
    migration_id = "mig0000000000000000000000000002"
    remote.source_exists = True
    remote.backup_exists = True
    remote.backup = f"/home/zhangsan.labops-backup-20261030-120000-{migration_id}"
    remote.target = "/data2/users/zhangsan"
    remote.staging = f"/data2/users/zhangsan.labops-staging-{migration_id}"

    response = client.post(f"/api/home-migrations/{migration_id}/resume")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "migration_state_ambiguous"


# ---------------------------------------------------------------------------
# 新空间分配
# ---------------------------------------------------------------------------


def test_storage_settings_roundtrip(client: TestClient, server_id: int) -> None:
    initial = client.get(f"/api/servers/{server_id}/storage-settings").json()
    assert initial["defaultDataRoot"] == "/data2"
    assert initial["dataRoot"] is None

    saved = client.put(
        f"/api/servers/{server_id}/storage-settings",
        json={"dataRoot": "/data2/", "migrationRoot": "/data2/users"},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["dataRoot"] == "/data2"
    assert saved.json()["migrationRoot"] == "/data2/users"


def test_dataset_allocation_creates_directory_and_link(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    response = client.post(
        f"/api/servers/{sudo_server}/datasets",
        json={"login": "zhangsan", "linkName": "data2_disk", "confirm": True},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dataPath"] == "/data2/zhangsan"
    assert body["linkPath"] == "/home/zhangsan/data2_disk"
    assert body["writable"] is True
    assert remote.datasets == {"/home/zhangsan/data2_disk": "/data2/zhangsan"}

    listed = client.get(f"/api/servers/{sudo_server}/datasets").json()
    assert listed["count"] == 1

    unlinked = client.post(f"/api/datasets/{body['id']}/unlink")
    assert unlinked.status_code == 200
    assert unlinked.json()["status"] == "unlinked"


def test_dataset_rejects_non_ext4_data_root(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    remote.target_filesystem = "nfs"

    response = client.post(
        f"/api/servers/{sudo_server}/datasets",
        json={"login": "zhangsan", "confirm": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "filesystem_not_supported"


def test_dataset_requires_confirmation(client: TestClient, sudo_server: int) -> None:
    response = client.post(
        f"/api/servers/{sudo_server}/datasets",
        json={"login": "zhangsan", "confirm": False},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "confirmation_required"


def test_dataset_rejects_system_account(
    client: TestClient, sudo_server: int, remote: FakeRemote
) -> None:
    remote.uid = 0

    response = client.post(
        f"/api/servers/{sudo_server}/datasets",
        json={"login": "root", "confirm": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] in {"system_account", "validation_failed"}
