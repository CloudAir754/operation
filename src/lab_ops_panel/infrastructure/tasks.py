"""持久化任务、步骤、幂等和有限操作日志。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lab_ops_panel.database import OperationLogRecord, TaskRecord, TaskStepRecord

SENSITIVE_KEYS = frozenset(
    {"password", "private_key", "credential", "credentials", "secret", "token", "content"}
)
TASK_TRANSITIONS = {
    "queued": frozenset({"prechecking", "cancelled"}),
    "prechecking": frozenset({"waiting_confirmation", "running", "failed"}),
    "waiting_confirmation": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"succeeded", "failed", "rollback_pending", "interrupted"}),
    "interrupted": frozenset({"running", "rollback_pending", "failed"}),
    "rollback_pending": frozenset({"rolled_back", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "rolled_back": frozenset(),
}


def _assert_no_secrets(value: Any, path: str = "parameters") -> None:
    """递归拒绝可能把凭据写入 SQLite 的字段。"""

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in SENSITIVE_KEYS or "password" in normalized or "secret" in normalized:
                raise ValueError(f"{path}.{key} 不允许持久化敏感内容")
            _assert_no_secrets(nested, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, nested in enumerate(value):
            _assert_no_secrets(nested, f"{path}[{index}]")


def _parameters_hash(parameters: dict[str, Any]) -> str:
    """以规范 JSON 计算幂等参数摘要，不保存原始参数。"""

    _assert_no_secrets(parameters)
    encoded = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class TaskRepository:
    """对任务状态转换提供事务边界和幂等约束。"""

    def __init__(self, database_path: Path) -> None:
        self.engine = create_engine(f"sqlite:///{database_path}")

    def create_or_get(
        self,
        kind: str,
        idempotency_key: str,
        parameters: dict[str, Any],
        target: str,
        step_names: list[str],
    ) -> dict[str, Any]:
        """创建任务，或在参数完全一致时返回同一幂等任务。"""

        digest = _parameters_hash(parameters)
        with Session(self.engine) as session:
            existing = session.scalar(
                select(TaskRecord).where(TaskRecord.idempotency_key == idempotency_key)
            )
            if existing:
                self._assert_same(existing, kind, digest, target)
                return self._public(session, existing)
            task = TaskRecord(
                id=uuid.uuid4().hex,
                kind=kind,
                state="queued",
                idempotency_key=idempotency_key,
                parameters_hash=digest,
                parameters=parameters,
                target=target,
            )
            session.add(task)
            session.add_all(
                TaskStepRecord(
                    task_id=task.id,
                    sequence=index,
                    name=name,
                    state="pending",
                    detail=None,
                )
                for index, name in enumerate(step_names, 1)
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(TaskRecord).where(TaskRecord.idempotency_key == idempotency_key)
                )
                if existing is None:
                    raise
                self._assert_same(existing, kind, digest, target)
                return self._public(session, existing)
            session.refresh(task)
            return self._public(session, task)

    def get(self, task_id: str) -> dict[str, Any]:
        """返回任务及按序步骤。"""

        with Session(self.engine) as session:
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise KeyError(task_id)
            return self._public(session, task)

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """按创建时间倒序返回有限数量任务。"""

        with Session(self.engine) as session:
            tasks = list(
                session.scalars(
                    select(TaskRecord).order_by(TaskRecord.created_at.desc()).limit(limit)
                )
            )
            return [self._public(session, task) for task in tasks]

    def transition(self, task_id: str, state: str, error: str | None = None) -> dict[str, Any]:
        """按显式状态图转换任务，拒绝跳步和终态重入。"""

        with Session(self.engine) as session:
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise KeyError(task_id)
            if state not in TASK_TRANSITIONS.get(task.state, frozenset()):
                raise ValueError(f"任务不能从 {task.state} 转换到 {state}")
            task.state = state
            task.error = error
            task.updated_at = datetime.now(UTC)
            session.commit()
            return self._public(session, task)

    def update_step(
        self, task_id: str, sequence: int, state: str, detail: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """更新步骤摘要；detail 同样不得包含凭据。"""

        if detail is not None:
            _assert_no_secrets(detail, "detail")
        with Session(self.engine) as session:
            step = session.scalar(
                select(TaskStepRecord).where(
                    TaskStepRecord.task_id == task_id,
                    TaskStepRecord.sequence == sequence,
                )
            )
            if step is None:
                raise KeyError((task_id, sequence))
            step.state = state
            step.detail = detail
            step.updated_at = datetime.now(UTC)
            session.commit()
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise KeyError(task_id)
            return self._public(session, task)

    def mark_running_interrupted(self) -> int:
        """启动时把上次异常退出的运行中任务标为 interrupted。"""

        with Session(self.engine) as session:
            tasks = list(session.scalars(select(TaskRecord).where(TaskRecord.state == "running")))
            now = datetime.now(UTC)
            for task in tasks:
                task.state = "interrupted"
                task.error = "上次运行异常中断，需要管理员选择继续或回滚"
                task.updated_at = now
                for step in session.scalars(
                    select(TaskStepRecord).where(
                        TaskStepRecord.task_id == task.id,
                        TaskStepRecord.state == "running",
                    )
                ):
                    step.state = "interrupted"
                    step.updated_at = now
            session.commit()
            return len(tasks)

    def log(self, action: str, target: str, result: str, task_id: str | None = None) -> None:
        """记录有限动作结果，不接受自由文本详情。"""

        with Session(self.engine) as session:
            session.add(
                OperationLogRecord(
                    task_id=task_id,
                    action=action,
                    target=target,
                    result=result,
                )
            )
            session.commit()

    @staticmethod
    def _assert_same(task: TaskRecord, kind: str, digest: str, target: str) -> None:
        if (task.kind, task.parameters_hash, task.target) != (kind, digest, target):
            raise ValueError("幂等键已用于不同任务参数")

    @staticmethod
    def _public(session: Session, task: TaskRecord) -> dict[str, Any]:
        steps = list(
            session.scalars(
                select(TaskStepRecord)
                .where(TaskStepRecord.task_id == task.id)
                .order_by(TaskStepRecord.sequence)
            )
        )
        return {
            "id": task.id,
            "kind": task.kind,
            "state": task.state,
            "target": task.target,
            "parameters": task.parameters,
            "error": task.error,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "steps": [
                {
                    "sequence": step.sequence,
                    "name": step.name,
                    "state": step.state,
                    "detail": step.detail,
                    "updated_at": step.updated_at.isoformat(),
                }
                for step in steps
            ],
        }
