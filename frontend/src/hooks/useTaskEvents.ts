import { useEffect, useRef, useState } from "react";
import { taskEventsUrl } from "../api/client";
import type {
  TaskDoneEvent,
  TaskEvent,
  TaskLogEvent,
  TaskStatus,
  TaskStatusEvent,
  TaskStepEvent,
  TaskStepStatus,
} from "../api/types";

export interface UseTaskEventsOptions {
  enabled?: boolean;
  onEvent: (event: TaskEvent) => void;
}

export interface UseTaskEventsResult {
  connected: boolean;
  error: string | null;
}

const TASK_STATUSES: readonly TaskStatus[] = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
];

const STEP_STATUSES: readonly TaskStepStatus[] = [
  "pending",
  "running",
  "ok",
  "failed",
  "skipped",
];

function isTaskStatus(value: unknown): value is TaskStatus {
  return typeof value === "string" && (TASK_STATUSES as readonly string[]).includes(value);
}

function isStepStatus(value: unknown): value is TaskStepStatus {
  return typeof value === "string" && (STEP_STATUSES as readonly string[]).includes(value);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

/** Parse one SSE `data:` payload, returning null for unknown/malformed frames. */
export function parseTaskEvent(raw: string): TaskEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  const record = asRecord(parsed);
  if (!record) return null;
  const taskId = record.taskId;
  if (typeof taskId !== "string") return null;

  switch (record.type) {
    case "status": {
      if (!isTaskStatus(record.status) || typeof record.progress !== "number") return null;
      const event: TaskStatusEvent = {
        type: "status",
        taskId,
        status: record.status,
        progress: record.progress,
        at: typeof record.at === "string" ? record.at : new Date().toISOString(),
      };
      return event;
    }
    case "step": {
      if (typeof record.seq !== "number" || !isStepStatus(record.status)) return null;
      const event: TaskStepEvent = {
        type: "step",
        taskId,
        seq: record.seq,
        name: typeof record.name === "string" ? record.name : `步骤 ${record.seq}`,
        status: record.status,
      };
      return event;
    }
    case "log": {
      if (typeof record.chunk !== "string") return null;
      const event: TaskLogEvent = {
        type: "log",
        taskId,
        channel: typeof record.channel === "string" ? record.channel : "stdout",
        chunk: record.chunk,
        at: typeof record.at === "string" ? record.at : new Date().toISOString(),
      };
      return event;
    }
    case "done": {
      if (!isTaskStatus(record.status)) return null;
      const event: TaskDoneEvent = {
        type: "done",
        taskId,
        status: record.status,
        progress: typeof record.progress === "number" ? record.progress : 100,
        exitCode: typeof record.exitCode === "number" ? record.exitCode : null,
        error: typeof record.error === "string" ? record.error : null,
      };
      return event;
    }
    default:
      return null;
  }
}

/**
 * Subscribe to a task's SSE stream. Closes the EventSource on unmount, when the
 * task changes, or after the terminal `done` event, and never throws on
 * malformed frames or connection errors.
 */
export function useTaskEvents(
  taskId: string | null,
  options: UseTaskEventsOptions,
): UseTaskEventsResult {
  const { enabled = true, onEvent } = options;
  const handlerRef = useRef(onEvent);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    handlerRef.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    if (!taskId || !enabled) {
      setConnected(false);
      setError(null);
      return;
    }
    if (typeof EventSource === "undefined") {
      setConnected(false);
      setError("当前环境不支持实时任务推送（EventSource 不可用），请手动刷新。");
      return;
    }

    let source: EventSource;
    try {
      source = new EventSource(taskEventsUrl(taskId));
    } catch {
      setConnected(false);
      setError("无法建立实时任务连接，请手动刷新。");
      return;
    }

    setConnected(false);
    setError(null);

    const dispatch = (raw: string) => {
      const event = parseTaskEvent(raw);
      if (event && event.taskId === taskId) {
        handlerRef.current(event);
      }
    };

    const handleOpen = () => {
      setConnected(true);
      setError(null);
    };
    const handleError = () => {
      setConnected(false);
      setError("实时连接已中断，正在自动重连…");
    };
    const handleFrame = (frame: Event) => {
      dispatch((frame as MessageEvent<string>).data);
    };
    const handleDone = (frame: Event) => {
      dispatch((frame as MessageEvent<string>).data);
      // The server closes the stream after `done`; close explicitly so the
      // browser does not attempt to reconnect.
      source.close();
      setConnected(false);
    };

    source.addEventListener("open", handleOpen);
    source.addEventListener("error", handleError);
    source.addEventListener("status", handleFrame);
    source.addEventListener("step", handleFrame);
    source.addEventListener("log", handleFrame);
    source.addEventListener("done", handleDone);

    return () => {
      source.removeEventListener("open", handleOpen);
      source.removeEventListener("error", handleError);
      source.removeEventListener("status", handleFrame);
      source.removeEventListener("step", handleFrame);
      source.removeEventListener("log", handleFrame);
      source.removeEventListener("done", handleDone);
      source.close();
    };
  }, [taskId, enabled]);

  return { connected, error };
}
