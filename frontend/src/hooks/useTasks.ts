import { useCallback, useMemo } from "react";
import { listTasks } from "../api/client";
import type { ApiError } from "../api/errors";
import type { Task, TaskList, TaskStatus } from "../api/types";
import { usePolling } from "./usePolling";

export interface TaskFilters {
  status: TaskStatus | "all";
  serverId: number | null;
}

export interface UseTasksResult {
  tasks: Task[];
  count: number;
  loading: boolean;
  refreshing: boolean;
  error: ApiError | null;
  updatedAt: number | null;
  refresh: () => void;
  /** Merge a live SSE event or cancel response into the list without refetching. */
  updateTask: (taskId: string, updater: (task: Task) => Task) => void;
}

/**
 * Task list with periodic background refresh (default 5s) and local merging so
 * SSE updates are visible immediately.
 */
export function useTasks(filters: TaskFilters, pollMs = 5000): UseTasksResult {
  const { status, serverId } = filters;
  const query = useMemo(
    () => ({
      status: status === "all" ? undefined : status,
      serverId: serverId ?? undefined,
      limit: 100,
    }),
    [status, serverId],
  );

  const load = useCallback((signal: AbortSignal) => listTasks(query, signal), [query]);
  const { data, error, loading, refreshing, updatedAt, refresh, setData } =
    usePolling<TaskList>(load, {
      intervalMs: pollMs,
      reloadKey: `${status}:${serverId ?? "all"}`,
    });

  const updateTask = useCallback(
    (taskId: string, updater: (task: Task) => Task) => {
      setData((previous) => {
        if (!previous) return previous;
        let changed = false;
        const tasks = previous.tasks.map((task) => {
          if (task.id !== taskId) return task;
          changed = true;
          return updater(task);
        });
        return changed ? { ...previous, tasks } : previous;
      });
    },
    [setData],
  );

  return {
    tasks: data?.tasks ?? [],
    count: data?.count ?? 0,
    loading,
    refreshing,
    error,
    updatedAt,
    refresh,
    updateTask,
  };
}
