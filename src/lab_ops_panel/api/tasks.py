"""维护任务：提交、查询、取消与 SSE 实时事件。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse

from lab_ops_panel.api.deps import ContainerDep
from lab_ops_panel.models import TASK_TERMINAL_STATUSES
from lab_ops_panel.schemas import TaskCancelOut, TaskCreate, TaskListOut, TaskOut
from lab_ops_panel.tasks.events import KEEPALIVE_FRAME, format_sse

router = APIRouter(tags=["tasks"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
KEEPALIVE_SECONDS = 15.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, container: ContainerDep) -> TaskOut:
    return container.tasks.submit(
        server_id=payload.server_id,
        action=payload.action,
        params=payload.params,
        confirm=payload.confirm,
        idempotency_key=payload.idempotency_key,
    )


@router.get("/tasks", response_model=TaskListOut)
def list_tasks(
    container: ContainerDep,
    server_id: int | None = Query(default=None, alias="serverId"),
    task_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> TaskListOut:
    tasks = container.tasks.list_tasks(server_id=server_id, status=task_status, limit=limit)
    return TaskListOut(tasks=tasks, count=len(tasks))


@router.get("/tasks/{task_id}", response_model=TaskOut)
def read_task(task_id: str, container: ContainerDep) -> TaskOut:
    return container.tasks.get(task_id)


@router.post("/tasks/{task_id}/cancel", response_model=TaskCancelOut, status_code=202)
def cancel_task(task_id: str, container: ContainerDep) -> TaskCancelOut:
    return TaskCancelOut(task=container.tasks.cancel(task_id))


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, container: ContainerDep) -> StreamingResponse:
    snapshot = container.tasks.get(task_id)
    queue = container.event_bus.subscribe(task_id)

    async def stream() -> AsyncIterator[str]:
        try:
            yield format_sse(
                {
                    "type": "status",
                    "taskId": snapshot.id,
                    "status": snapshot.status,
                    "progress": snapshot.progress,
                    "at": _now_iso(),
                }
            )
            for step in snapshot.steps:
                yield format_sse(
                    {
                        "type": "step",
                        "taskId": snapshot.id,
                        "seq": step.seq,
                        "name": step.name,
                        "status": step.status,
                    }
                )
            if snapshot.status in TASK_TERMINAL_STATUSES:
                yield format_sse(
                    {
                        "type": "done",
                        "taskId": snapshot.id,
                        "status": snapshot.status,
                        "progress": snapshot.progress,
                        "exitCode": snapshot.exit_code,
                        "error": snapshot.error,
                        "at": _now_iso(),
                    }
                )
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield KEEPALIVE_FRAME
                    continue
                yield format_sse(event)
                if event.get("type") == "done":
                    return
        finally:
            container.event_bus.unsubscribe(task_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
