"""任务事件总线：把工作线程产生的事件安全地投递给异步 SSE 订阅者。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

EVENT_QUEUE_SIZE = 256


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, task_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            queues = self._subscribers.get(task_id)
            if not queues:
                return
            if queue in queues:
                queues.remove(queue)
            if not queues:
                self._subscribers.pop(task_id, None)

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            queues = list(self._subscribers.get(task_id, ()))
        if not queues:
            return
        loop = self._loop
        for queue in queues:
            if loop is None or loop.is_closed():
                self._put(queue, event)
                continue
            try:
                loop.call_soon_threadsafe(self._put, queue, event)
            except RuntimeError:  # pragma: no cover - 事件循环已关闭
                logger.debug("事件循环已关闭，丢弃事件 %s", event.get("type"))

    @staticmethod
    def _put(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - 慢客户端
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("SSE 队列已满，丢弃事件")


def format_sse(event: dict[str, Any]) -> str:
    name = str(event.get("type", "message"))
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n"


KEEPALIVE_FRAME = ": keepalive\n\n"
