"""后台任务管理器。

约束：
- 一个任务只执行一次；相同幂等键重复提交返回 409。
- 取消通过 ``threading.Event`` 传递到传输层，远端进程会被终止。
- 应用重启后，未完成的任务统一标记为 ``interrupted``，绝不伪造成功。
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from lab_ops_panel.audit import record_audit
from lab_ops_panel.db import session_scope
from lab_ops_panel.errors import ApiError, ConflictError, InvalidRequestError, NotFoundError
from lab_ops_panel.models import (
    STEP_STATUS_FAILED,
    STEP_STATUS_OK,
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCEEDED,
    TASK_TERMINAL_STATUSES,
    Server,
    Task,
    TaskStep,
)
from lab_ops_panel.remote.diagnosis import diagnose_result
from lab_ops_panel.remote.parsing import normalize_snapshot_sections, snapshot_status
from lab_ops_panel.remote.probe import parse_probe_payload
from lab_ops_panel.remote.targets import target_from_server
from lab_ops_panel.schemas import TaskOut, TaskStepOut
from lab_ops_panel.ssh.transport import TransportError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from lab_ops_panel.config import Settings
    from lab_ops_panel.remote.actions import ActionRegistry, PlanStep
    from lab_ops_panel.secrets_store import CredentialStore
    from lab_ops_panel.snapshots import SnapshotService
    from lab_ops_panel.ssh.transport import SshTransport
    from lab_ops_panel.tasks.events import EventBus

logger = logging.getLogger(__name__)

OUTPUT_TAIL_LIMIT = 4000
STEP_OUTPUT_TAIL_LIMIT = 2000
EVENT_CHUNK_LIMIT = 2000


def _now() -> datetime:
    return datetime.now(UTC)


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"…{text[-limit:]}"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def task_to_out(task: Task, alias: str) -> TaskOut:
    return TaskOut(
        id=task.id,
        server_id=task.server_id,
        server_alias=alias,
        action=task.action,
        action_label=task.action_label,
        risk=task.risk,  # type: ignore[arg-type]
        params=dict(task.params or {}),
        status=task.status,  # type: ignore[arg-type]
        progress=task.progress,
        exit_code=task.exit_code,
        error=task.error,
        output_tail=task.output_tail,
        idempotency_key=task.idempotency_key,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        steps=[
            TaskStepOut(
                seq=step.seq,
                name=step.name,
                status=step.status,  # type: ignore[arg-type]
                exit_code=step.exit_code,
                output_tail=step.output_tail,
                started_at=step.started_at,
                finished_at=step.finished_at,
            )
            for step in task.steps
        ],
    )


class TaskManager:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        transport: SshTransport,
        settings: Settings,
        registry: ActionRegistry,
        credentials: CredentialStore,
        bus: EventBus,
        snapshots: SnapshotService,
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport
        self._settings = settings
        self._registry = registry
        self._credentials = credentials
        self._bus = bus
        self._snapshots = snapshots
        self._pool = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_tasks, thread_name_prefix="labops-task"
        )
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._closed = False

    # -- 提交与查询 ---------------------------------------------------------

    def submit(
        self,
        *,
        server_id: int,
        action: str,
        params: dict[str, Any] | None,
        confirm: bool,
        idempotency_key: str | None,
    ) -> TaskOut:
        if self._closed:
            raise InvalidRequestError("service_stopping", "服务正在关闭，无法提交新任务")
        with session_scope(self._session_factory) as session:
            server = session.get(Server, server_id)
            if server is None:
                raise NotFoundError("server_not_found", "主机不存在")
            if not server.enabled:
                raise InvalidRequestError("server_disabled", "主机已停用，无法提交维护任务")
            has_password = self._credentials.has_password(server.id)
            sudo_sentinel = "<stored>" if has_password else None
            spec, parsed = self._registry.validate(
                action,
                params or {},
                sudo_password=sudo_sentinel,
                sudo_available=self._credentials.available,
            )
            if spec.requires_confirmation and not confirm:
                raise ApiError(
                    422,
                    "confirmation_required",
                    "该动作需要目标确认后才能执行",
                )
            if spec.requires_idempotency_key and not idempotency_key:
                raise ApiError(
                    422,
                    "idempotency_key_required",
                    "该动作需要幂等键（idempotencyKey）以避免重复执行",
                )
            if idempotency_key:
                existing = session.scalar(
                    select(Task).where(Task.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    raise ConflictError(
                        "duplicate_idempotency_key",
                        "相同的幂等键已提交过任务",
                        extra={"existingTaskId": existing.id},
                    )
            task = Task(
                id=str(uuid.uuid4()),
                server_id=server.id,
                action=spec.id,
                action_label=spec.label,
                risk=spec.risk,
                params=parsed.model_dump(mode="json", by_alias=True, exclude_none=True),
                status=TASK_STATUS_QUEUED,
                progress=0,
                idempotency_key=idempotency_key,
                output_tail="",
            )
            session.add(task)
            session.flush()
            result = task_to_out(task, server.alias)
        self._pool.submit(self._execute, task.id)
        return result

    def get(self, task_id: str) -> TaskOut:
        with self._session_factory() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise NotFoundError("task_not_found", "任务不存在")
            return task_to_out(task, self._alias(session, task.server_id))

    def list_tasks(
        self,
        *,
        server_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[TaskOut]:
        with self._session_factory() as session:
            statement = select(Task).order_by(Task.created_at.desc(), Task.id.desc()).limit(limit)
            if server_id is not None:
                statement = statement.where(Task.server_id == server_id)
            if status is not None:
                statement = statement.where(Task.status == status)
            tasks = session.scalars(statement).all()
            aliases = {
                server.id: server.alias
                for server in session.scalars(
                    select(Server).where(Server.id.in_({task.server_id for task in tasks}))
                )
            }
            return [task_to_out(task, aliases.get(task.server_id, "")) for task in tasks]

    def cancel(self, task_id: str) -> TaskOut:
        with session_scope(self._session_factory) as session:
            task = session.get(Task, task_id)
            if task is None:
                raise NotFoundError("task_not_found", "任务不存在")
            alias = self._alias(session, task.server_id)
            if task.status in TASK_TERMINAL_STATUSES:
                raise ConflictError("task_finished", "任务已结束，无法取消")
            if task.status == TASK_STATUS_QUEUED:
                task.status = TASK_STATUS_CANCELLED
                task.error = "已取消（尚未开始执行）"
                task.finished_at = _now()
                task.progress = 100
                out = task_to_out(task, alias)
                cancelled_now = True
            else:
                task.cancel_requested = True
                out = task_to_out(task, alias)
                cancelled_now = False
        with self._lock:
            event = self._cancel_events.get(task_id)
        if event is not None:
            event.set()
        if cancelled_now:
            self._publish_done(out)
            record_audit(
                self._session_factory,
                action=out.action,
                target=out.server_alias,
                outcome=TASK_STATUS_CANCELLED,
                detail="取消排队中的任务",
            )
        return out

    def recover_on_startup(self) -> int:
        """把上次进程遗留的未完成任务标记为 interrupted。"""

        with session_scope(self._session_factory) as session:
            interrupted = session.scalars(
                select(Task).where(Task.status.in_([TASK_STATUS_QUEUED, TASK_STATUS_RUNNING]))
            ).all()
            for task in interrupted:
                task.status = TASK_STATUS_INTERRUPTED
                task.error = "应用重启，任务已中断；请确认远端状态后重新提交"
                task.finished_at = _now()
                task.cancel_requested = False
                for step in task.steps:
                    if step.status == STEP_STATUS_RUNNING:
                        step.status = STEP_STATUS_FAILED
                        step.finished_at = _now()
                        step.output_tail = _tail(
                            (step.output_tail or "") + "\n应用重启，执行中断",
                            STEP_OUTPUT_TAIL_LIMIT,
                        )
            count = len(interrupted)
        if count:
            logger.warning("已将 %d 个未完成任务标记为 interrupted", count)
        return count

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            events = list(self._cancel_events.values())
        for event in events:
            event.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- 执行 ---------------------------------------------------------------

    def _execute(self, task_id: str) -> None:
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[task_id] = cancel_event
        try:
            self._run_task(task_id, cancel_event)
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须落库为失败
            logger.exception("任务执行异常：%s", task_id)
            self._finish(task_id, TASK_STATUS_FAILED, f"任务执行异常：{exc}")
        finally:
            with self._lock:
                self._cancel_events.pop(task_id, None)

    def _run_task(self, task_id: str, cancel_event: threading.Event) -> None:
        with session_scope(self._session_factory) as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            if task.status in TASK_TERMINAL_STATUSES:
                return
            if task.cancel_requested or cancel_event.is_set():
                task.status = TASK_STATUS_CANCELLED
                task.error = "任务已取消"
                task.finished_at = _now()
                task.progress = 100
                return
            started = session.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == TASK_STATUS_QUEUED)
                .values(status=TASK_STATUS_RUNNING, started_at=_now(), progress=5)
            )
            if started.rowcount == 0:
                return
            server_id = task.server_id
            action = task.action
            raw_params = dict(task.params or {})
            self._alias(session, server_id)
            server = session.get(Server, server_id)
            if server is None:
                raise ApiError(404, "server_not_found", "主机记录已被删除")
            target = target_from_server(server)
            server_obj = server

        self._publish_status(task_id, TASK_STATUS_RUNNING, 5)

        sudo_password: str | None = None
        try:
            spec = self._registry.get(action)
            if spec.requires_sudo or bool(raw_params.get("useSudo")):
                sudo_password = self._load_password(server_id)
            spec, parsed = self._registry.validate(
                action,
                raw_params,
                sudo_password=sudo_password,
                sudo_available=self._credentials.available,
            )
            steps = self._registry.build_plan(
                spec,
                parsed,
                sudo_password=sudo_password,
                action_timeout_seconds=self._settings.action_timeout_seconds,
            )
        except ApiError as exc:
            self._finish(task_id, TASK_STATUS_FAILED, exc.message)
            return
        except Exception as exc:  # noqa: BLE001
            self._finish(task_id, TASK_STATUS_FAILED, f"构建执行计划失败：{exc}")
            return

        step_ids = self._create_steps(task_id, steps)
        if step_ids is None:
            return

        outputs: list[str] = []
        last_result = None
        last_stdout = ""
        total = len(steps)
        for index, step in enumerate(steps, start=1):
            if cancel_event.is_set():
                self._finish(task_id, TASK_STATUS_CANCELLED, "任务已取消")
                return
            step_id = step_ids[index]
            self._patch_step(step_id, status=STEP_STATUS_RUNNING, started_at=_now())
            self._publish(
                {
                    "type": "step",
                    "taskId": task_id,
                    "seq": index,
                    "name": step.name,
                    "status": STEP_STATUS_RUNNING,
                }
            )
            try:
                command, stdin_data = self._prepare_command(step, sudo_password)
            except ApiError as exc:
                self._patch_step(
                    step_id,
                    status=STEP_STATUS_FAILED,
                    finished_at=_now(),
                    output_tail=exc.message[:STEP_OUTPUT_TAIL_LIMIT],
                )
                self._finish(task_id, TASK_STATUS_FAILED, exc.message)
                return
            try:
                result = self._transport.run(
                    target,
                    command,
                    timeout=step.timeout_seconds,
                    cancel_event=cancel_event,
                    stdin_data=stdin_data,
                    max_output_bytes=self._settings.max_output_bytes,
                )
            except TransportError as exc:
                self._patch_step(
                    step_id,
                    status=STEP_STATUS_FAILED,
                    finished_at=_now(),
                    output_tail=str(exc)[:STEP_OUTPUT_TAIL_LIMIT],
                )
                self._finish(task_id, TASK_STATUS_FAILED, str(exc))
                return
            last_result = result
            last_stdout = result.stdout
            step_output = result.combined_output
            outputs.append(step_output)
            self._publish(
                {
                    "type": "log",
                    "taskId": task_id,
                    "channel": "stdout",
                    "chunk": _tail(step_output, EVENT_CHUNK_LIMIT),
                    "at": _iso(_now()),
                }
            )
            if result.cancelled:
                self._patch_step(
                    step_id,
                    status=STEP_STATUS_FAILED,
                    exit_code=result.exit_code,
                    finished_at=_now(),
                    output_tail=_tail(step_output, STEP_OUTPUT_TAIL_LIMIT),
                )
                self._finish(task_id, TASK_STATUS_CANCELLED, "任务已取消")
                return
            if result.timed_out:
                self._patch_step(
                    step_id,
                    status=STEP_STATUS_FAILED,
                    exit_code=result.exit_code,
                    finished_at=_now(),
                    output_tail=_tail(step_output, STEP_OUTPUT_TAIL_LIMIT),
                )
                self._finish(
                    task_id,
                    TASK_STATUS_FAILED,
                    f"步骤「{step.name}」超时（{step.timeout_seconds:.0f} 秒）",
                )
                return
            if result.exit_code != 0:
                message = diagnose_result(result, operation=f"步骤「{step.name}」")
                self._patch_step(
                    step_id,
                    status=STEP_STATUS_FAILED,
                    exit_code=result.exit_code,
                    finished_at=_now(),
                    output_tail=_tail(step_output, STEP_OUTPUT_TAIL_LIMIT),
                )
                if not step.allow_failure:
                    self._finish(task_id, TASK_STATUS_FAILED, message, exit_code=result.exit_code)
                    return
                logger.info("容忍校验步骤失败：%s（%s）", step.name, message)
            else:
                self._patch_step(
                    step_id,
                    status=STEP_STATUS_OK,
                    exit_code=result.exit_code,
                    finished_at=_now(),
                    output_tail=_tail(step_output, STEP_OUTPUT_TAIL_LIMIT),
                )
            progress = 5 + int(90 * index / max(total, 1))
            self._patch_task(task_id, progress=min(progress, 95))
            self._publish_status(task_id, TASK_STATUS_RUNNING, min(progress, 95))

        if spec.persist_snapshot:
            try:
                payload = parse_probe_payload(last_stdout)
                data = normalize_snapshot_sections(payload.get("sections"))
                self._snapshots.store(server_obj, data, status=snapshot_status(data), error=None)
            except Exception as exc:  # noqa: BLE001
                self._finish(task_id, TASK_STATUS_FAILED, f"快照解析失败：{exc}")
                return

        exit_code = last_result.exit_code if last_result is not None else None
        self._finish(
            task_id,
            TASK_STATUS_SUCCEEDED,
            None,
            exit_code=exit_code,
            output_tail=_tail("\n".join(part for part in outputs if part), OUTPUT_TAIL_LIMIT),
        )

    # -- 内部工具 -----------------------------------------------------------

    def _load_password(self, server_id: int) -> str | None:
        try:
            return self._credentials.get_password(server_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 sudo 密码失败：%s", exc)
            return None

    @staticmethod
    def _prepare_command(step: PlanStep, password: str | None) -> tuple[str, bytes | None]:
        if not step.use_sudo:
            return step.command, step.stdin_data
        if password is None:
            raise ApiError(409, "sudo_password_missing", "未保存 sudo 密码，无法提权执行")
        if step.stdin_data is not None:
            # sudo -S 会先读走 stdin 的首行密码，负载无法可靠地交给被执行的命令。
            raise ApiError(
                500, "step_stdin_conflict", "该步骤同时需要 sudo 密码与标准输入，无法执行"
            )
        return f"sudo -S -p '' {step.command}", (password + "\n").encode("utf-8")

    def _create_steps(self, task_id: str, steps: list[PlanStep]) -> dict[int, int] | None:
        with session_scope(self._session_factory) as session:
            task = session.get(Task, task_id)
            if task is None:
                return None
            if task.status in TASK_TERMINAL_STATUSES:
                return None
            existing = {
                step.seq: step
                for step in session.scalars(
                    select(TaskStep).where(TaskStep.task_id == task_id)
                ).all()
            }
            for index, step in enumerate(steps, start=1):
                row = existing.get(index)
                if row is None:
                    row = TaskStep(task_id=task_id, seq=index, name=step.name)
                    session.add(row)
                else:
                    row.name = step.name
                row.status = STEP_STATUS_PENDING
                row.exit_code = None
                row.output_tail = ""
                row.command = step.command
                row.started_at = None
                row.finished_at = None
            session.flush()
            return {
                row.seq: row.id
                for row in session.scalars(
                    select(TaskStep).where(TaskStep.task_id == task_id)
                ).all()
            }

    def _patch_task(self, task_id: str, **fields: Any) -> None:
        with session_scope(self._session_factory) as session:
            task = session.get(Task, task_id)
            if task is None or task.status in TASK_TERMINAL_STATUSES:
                return
            for key, value in fields.items():
                setattr(task, key, value)

    def _patch_step(self, step_id: int, **fields: Any) -> None:
        with session_scope(self._session_factory) as session:
            step = session.get(TaskStep, step_id)
            if step is None:
                return
            for key, value in fields.items():
                setattr(step, key, value)

    def _finish(
        self,
        task_id: str,
        status: str,
        error: str | None = None,
        *,
        exit_code: int | None = None,
        output_tail: str | None = None,
    ) -> None:
        with session_scope(self._session_factory) as session:
            task = session.get(Task, task_id)
            if task is None or task.status in TASK_TERMINAL_STATUSES:
                return
            task.status = status
            task.error = error
            task.finished_at = _now()
            task.exit_code = exit_code if exit_code is not None else task.exit_code
            if status in (TASK_STATUS_SUCCEEDED, TASK_STATUS_CANCELLED):
                task.progress = 100
            if output_tail is not None:
                task.output_tail = output_tail
            out = task_to_out(task, self._alias(session, task.server_id))
        self._publish_done(out)
        detail = error or (f"退出码 {out.exit_code}" if out.exit_code is not None else "")
        record_audit(
            self._session_factory,
            action=out.action,
            target=out.server_alias,
            outcome=status,
            detail=detail,
        )

    def _publish(self, event: dict[str, Any]) -> None:
        task_id = event.get("taskId")
        if isinstance(task_id, str):
            self._bus.publish(task_id, event)

    def _publish_status(self, task_id: str, status: str, progress: int) -> None:
        self._publish(
            {
                "type": "status",
                "taskId": task_id,
                "status": status,
                "progress": progress,
                "at": _iso(_now()),
            }
        )

    def _publish_done(self, task: TaskOut) -> None:
        self._bus.publish(
            task.id,
            {
                "type": "done",
                "taskId": task.id,
                "status": task.status,
                "progress": task.progress,
                "exitCode": task.exit_code,
                "error": task.error,
                "at": _iso(task.finished_at),
            },
        )

    @staticmethod
    def _alias(session: Session, server_id: int) -> str:
        server = session.get(Server, server_id)
        return server.alias if server is not None else ""
