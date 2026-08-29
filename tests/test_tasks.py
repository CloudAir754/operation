"""持久化任务状态机、幂等与日志保留测试。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lab_ops_panel.database import OperationLogRecord, initialize_database
from lab_ops_panel.infrastructure.tasks import TaskRepository


def _repository(tmp_path: Path) -> TaskRepository:
    return TaskRepository(initialize_database(tmp_path))


def test_task_creation_is_idempotent(tmp_path: Path) -> None:
    """重复请求不能创建第二个任务或重复步骤。"""

    repository = _repository(tmp_path)
    first = repository.create_or_get(
        "create_users",
        "idempotency-key-123",
        {"usernames": ["alice"]},
        "alice",
        ["预检", "创建账号", "验证"],
    )
    second = repository.create_or_get(
        "create_users",
        "idempotency-key-123",
        {"usernames": ["alice"]},
        "alice",
        ["预检", "创建账号", "验证"],
    )

    assert first["id"] == second["id"]
    assert len(second["steps"]) == 3
    assert second["parameters"] == {"usernames": ["alice"]}


def test_task_idempotency_collision_is_rejected(tmp_path: Path) -> None:
    """相同幂等键携带不同目标时必须拒绝。"""

    repository = _repository(tmp_path)
    repository.create_or_get("create_users", "idempotency-key-123", {}, "alice", ["预检"])

    with pytest.raises(ValueError, match="幂等键"):
        repository.create_or_get("create_users", "idempotency-key-123", {}, "bob", ["预检"])


def test_task_rejects_sensitive_persistent_parameters(tmp_path: Path) -> None:
    """密码等凭据不能进入任务数据库。"""

    repository = _repository(tmp_path)
    with pytest.raises(ValueError, match="敏感内容"):
        repository.create_or_get(
            "create_users",
            "idempotency-key-123",
            {"password": "must-not-persist"},
            "alice",
            ["预检"],
        )


def test_invalid_task_transition_is_rejected(tmp_path: Path) -> None:
    """任务不能从 queued 直接跳到 succeeded。"""

    repository = _repository(tmp_path)
    task = repository.create_or_get("test", "idempotency-key-123", {}, "target", ["步骤"])

    with pytest.raises(ValueError, match="不能"):
        repository.transition(task["id"], "succeeded")


def test_running_task_is_marked_interrupted_after_restart(tmp_path: Path) -> None:
    """异常退出后的运行中任务不能在重启后误报成功。"""

    repository = _repository(tmp_path)
    task = repository.create_or_get("test", "idempotency-key-123", {}, "target", ["步骤"])
    repository.transition(task["id"], "prechecking")
    repository.transition(task["id"], "running")
    repository.update_step(task["id"], 1, "running")

    assert TaskRepository(initialize_database(tmp_path)).mark_running_interrupted() == 1
    recovered = repository.get(task["id"])
    assert recovered["state"] == "interrupted"
    assert recovered["steps"][0]["state"] == "interrupted"


def test_queued_and_prechecking_tasks_are_also_interrupted_after_restart(
    tmp_path: Path,
) -> None:
    """任务可能在工作线程落库前退出，queued/prechecking 也不能永久悬挂。"""

    repository = _repository(tmp_path)
    queued = repository.create_or_get("test", "queued-key-123", {}, "queued", ["步骤"])
    prechecking = repository.create_or_get(
        "test", "prechecking-key-123", {}, "prechecking", ["步骤"]
    )
    repository.transition(prechecking["id"], "prechecking")

    assert repository.mark_running_interrupted() == 2
    assert repository.get(queued["id"])["state"] == "interrupted"
    assert repository.get(prechecking["id"])["state"] == "interrupted"


def test_operation_log_cleanup_applies_age_and_count_limits(tmp_path: Path) -> None:
    """启动清理应同时移除过期记录，并只保留最新的有限条目。"""

    repository = _repository(tmp_path)
    old_time = datetime.now(UTC) - timedelta(days=91)
    with Session(repository.engine) as session:
        session.add(
            OperationLogRecord(
                action="旧动作", target="uid:1001", result="succeeded", created_at=old_time
            )
        )
        session.add_all(
            OperationLogRecord(action="动作", target=f"uid:{uid}", result="succeeded")
            for uid in range(1002, 1006)
        )
        session.commit()

    assert repository.cleanup_operation_logs(max_age_days=90, max_records=2) == 3
    with Session(repository.engine) as session:
        remaining = list(
            session.scalars(select(OperationLogRecord).order_by(OperationLogRecord.id))
        )
        assert len(remaining) == 2
        assert {record.target for record in remaining} == {"uid:1004", "uid:1005"}
        assert session.scalar(select(func.count()).select_from(OperationLogRecord)) == 2


def test_operation_log_cleanup_rejects_unbounded_configuration(tmp_path: Path) -> None:
    """保留策略不能通过零或负数意外关闭边界。"""

    repository = _repository(tmp_path)
    with pytest.raises(ValueError, match="至少为 1"):
        repository.cleanup_operation_logs(max_age_days=0)
    with pytest.raises(ValueError, match="至少为 1"):
        repository.cleanup_operation_logs(max_records=0)
