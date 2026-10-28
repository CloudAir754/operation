import { useCallback, useEffect, useState } from "react";
import { cancelTask, getServers } from "../api/client";
import { describeError } from "../api/errors";
import type { ServerList, Task, TaskEvent, TaskStatus } from "../api/types";
import {
  DetailList,
  EmptyState,
  ErrorBanner,
  InfoBanner,
  LoadingBlock,
  PageHeader,
  SectionCard,
  StatusBadge,
  UsageBar,
} from "../components/ui";
import { usePageTitle } from "../hooks/usePageTitle";
import { usePolling } from "../hooks/usePolling";
import { useTaskEvents } from "../hooks/useTaskEvents";
import { useTasks } from "../hooks/useTasks";
import {
  RISK_TONES,
  TASK_STATUS_LABELS,
  TASK_STATUS_TONES,
  TASK_STEP_STATUS_LABELS,
  TASK_STEP_STATUS_TONES,
  actionLabel,
  isActiveTask,
  riskLabel,
} from "../lib/labels";
import { formatDateTime, formatDuration, formatRelativeTime } from "../lib/format";

const STATUS_OPTIONS: TaskStatus[] = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
];

export function TasksPage() {
  usePageTitle("维护任务");

  const [statusFilter, setStatusFilter] = useState<TaskStatus | "all">("all");
  const [serverFilter, setServerFilter] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [cancelPending, setCancelPending] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [streamLogs, setStreamLogs] = useState<string[]>([]);

  const {
    tasks,
    count,
    loading,
    error,
    refreshing: updating,
    updatedAt,
    refresh,
    updateTask,
  } = useTasks({ status: statusFilter, serverId: serverFilter }, 5000);

  const loadServers = useCallback((signal: AbortSignal) => getServers(signal), []);
  const { data: serverData, error: serversError } = usePolling<ServerList>(loadServers);

  useEffect(() => {
    if (tasks.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !tasks.some((task) => task.id === selectedId)) {
      setSelectedId(tasks[0].id);
    }
  }, [tasks, selectedId]);

  useEffect(() => {
    setStreamLogs([]);
    setCancelError(null);
  }, [selectedId]);

  const selectedTask = tasks.find((task) => task.id === selectedId) ?? null;
  const selectedActive = selectedTask ? isActiveTask(selectedTask.status) : false;

  const handleEvent = useCallback(
    (event: TaskEvent) => {
      if (!selectedId) return;
      switch (event.type) {
        case "status":
          updateTask(selectedId, (task) => ({
            ...task,
            status: event.status,
            progress: event.progress,
          }));
          break;
        case "step":
          updateTask(selectedId, (task) => {
            const existing = task.steps.find((step) => step.seq === event.seq);
            if (!existing) {
              return {
                ...task,
                steps: [
                  ...task.steps,
                  {
                    seq: event.seq,
                    name: event.name,
                    status: event.status,
                    exitCode: null,
                    outputTail: "",
                    startedAt: null,
                    finishedAt: null,
                  },
                ].sort((left, right) => left.seq - right.seq),
              };
            }
            return {
              ...task,
              steps: task.steps.map((step) =>
                step.seq === event.seq ? { ...step, status: event.status } : step,
              ),
            };
          });
          break;
        case "log":
          setStreamLogs((previous) => [...previous.slice(-199), event.chunk]);
          break;
        case "done":
          updateTask(selectedId, (task) => ({
            ...task,
            status: event.status,
            progress: event.progress,
            exitCode: event.exitCode,
            error: event.error,
            finishedAt: task.finishedAt ?? new Date().toISOString(),
          }));
          break;
        default:
          break;
      }
    },
    [selectedId, updateTask],
  );

  const stream = useTaskEvents(selectedActive ? selectedId : null, {
    enabled: selectedActive,
    onEvent: handleEvent,
  });

  const handleCancel = async () => {
    if (!selectedTask) return;
    setCancelPending(true);
    setCancelError(null);
    try {
      const response = await cancelTask(selectedTask.id);
      updateTask(response.task.id, () => response.task);
    } catch (cancelFailure) {
      setCancelError(describeError(cancelFailure));
    } finally {
      setCancelPending(false);
    }
  };

  return (
    <div className="stack">
      <PageHeader
        eyebrow="任务队列"
        title="维护任务"
        description="查看后台维护动作的进度、步骤与输出。选中执行中的任务会建立实时推送连接。"
        actions={
          <button
            type="button"
            className="btn btn--ghost"
            onClick={refresh}
            disabled={updating}
          >
            {updating ? "刷新中…" : "刷新"}
          </button>
        }
      />

      {error ? (
        <ErrorBanner title="无法加载任务列表" message={error.message} onRetry={refresh} />
      ) : null}
      {serversError ? (
        <InfoBanner tone="warning">
          主机列表加载失败（{serversError.message}），主机筛选暂不可用。
        </InfoBanner>
      ) : null}

      <section className="filters" aria-label="任务筛选">
        <div className="field field--inline">
          <label htmlFor="task-status-filter">状态</label>
          <select
            id="task-status-filter"
            value={statusFilter}
            onChange={(event) =>
              setStatusFilter(event.target.value === "all" ? "all" : (event.target.value as TaskStatus))
            }
          >
            <option value="all">全部状态</option>
            {STATUS_OPTIONS.map((status) => (
              <option key={status} value={status}>
                {TASK_STATUS_LABELS[status]}
              </option>
            ))}
          </select>
        </div>

        <div className="field field--inline">
          <label htmlFor="task-server-filter">主机</label>
          <select
            id="task-server-filter"
            value={serverFilter === null ? "" : String(serverFilter)}
            onChange={(event) =>
              setServerFilter(event.target.value === "" ? null : Number(event.target.value))
            }
            disabled={Boolean(serversError)}
          >
            <option value="">全部主机</option>
            {(serverData?.servers ?? []).map((host) => (
              <option key={host.id} value={host.id}>
                {host.alias}
              </option>
            ))}
          </select>
        </div>

        <p className="filters__meta">
          共 <strong>{count}</strong> 个任务
          {updatedAt ? (
            <span className="muted"> · 更新于 {formatRelativeTime(new Date(updatedAt).toISOString())}</span>
          ) : null}
          <span className="muted"> · 每 5 秒自动刷新</span>
        </p>
      </section>

      {loading && tasks.length === 0 ? (
        <LoadingBlock label="正在加载任务列表…" />
      ) : tasks.length === 0 && !error ? (
        <EmptyState
          title="暂无维护任务"
          description="在主机详情页执行采集、检查或服务操作后，任务会显示在这里。"
        />
      ) : (
        <SectionCard
          title="任务列表"
          description="选择任务查看步骤与输出"
          actions={
            <span className="muted">
              {selectedTask ? `已选择 ${selectedTask.serverAlias}` : "未选择任务"}
            </span>
          }
        >
          <div className="table-wrap table-wrap--tall">
            <table className="data-table data-table--tasks">
              <caption className="sr-only">维护任务列表</caption>
              <thead>
                <tr>
                  <th scope="col">状态</th>
                  <th scope="col">动作</th>
                  <th scope="col">主机</th>
                  <th scope="col">进度</th>
                  <th scope="col">创建时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => {
                  const selected = task.id === selectedTask?.id;
                  return (
                    <tr key={task.id} className={selected ? "is-selected" : undefined}>
                      <td>
                        <StatusBadge tone={TASK_STATUS_TONES[task.status]}>
                          {TASK_STATUS_LABELS[task.status]}
                        </StatusBadge>
                      </td>
                      <td>
                        <span>{task.actionLabel || actionLabel(task.action)}</span>
                        <StatusBadge tone={RISK_TONES[task.risk]} className="badge--inline">
                          {riskLabel(task.risk)}
                        </StatusBadge>
                      </td>
                      <td className="mono">{task.serverAlias}</td>
                      <td>
                        <div className="progress-inline">
                          <div
                            className="progress-inline__track"
                            role="progressbar"
                            aria-label={`${task.actionLabel} 进度`}
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-valuenow={task.progress}
                          >
                            <div
                              className={`progress-inline__fill progress-inline__fill--${
                                task.status === "failed"
                                  ? "danger"
                                  : task.status === "succeeded"
                                    ? "ok"
                                    : "info"
                              }`}
                              style={{ width: `${Math.min(100, Math.max(0, task.progress))}%` }}
                            />
                          </div>
                          <span className="progress-inline__value">{task.progress}%</span>
                        </div>
                      </td>
                      <td>
                        <span title={formatDateTime(task.createdAt)}>
                          {formatRelativeTime(task.createdAt)}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn btn--tiny btn--ghost"
                          onClick={() => setSelectedId(task.id)}
                          aria-label={`查看任务 ${task.actionLabel} ${task.serverAlias}`}
                        >
                          {selected ? "已选择" : "查看"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </SectionCard>
      )}

      {selectedTask ? (
        <SectionCard
          title="任务详情"
          description={`${selectedTask.actionLabel || actionLabel(selectedTask.action)} · ${
            selectedTask.serverAlias
          }`}
          actions={
            <span className="task-live" aria-live="polite">
              {selectedActive
                ? stream.connected
                  ? "实时推送已连接"
                  : "正在建立实时推送…"
                : "任务已结束"}
            </span>
          }
        >
          <div className="stack">
            <div className="task-detail__top">
              <div className="task-detail__status" data-testid="task-detail-status">
                <StatusBadge tone={TASK_STATUS_TONES[selectedTask.status]}>
                  {TASK_STATUS_LABELS[selectedTask.status]}
                </StatusBadge>
                <span className="sr-only" aria-live="polite">
                  任务状态：{TASK_STATUS_LABELS[selectedTask.status]}
                </span>
              </div>
              <UsageBar
                label="任务进度"
                percent={selectedTask.progress}
                testId="task-detail-progress"
              />
            </div>

            <DetailList
              items={[
                { label: "任务 ID", value: <span className="mono">{selectedTask.id}</span> },
                { label: "动作", value: selectedTask.actionLabel || actionLabel(selectedTask.action) },
                {
                  label: "风险等级",
                  value: (
                    <StatusBadge tone={RISK_TONES[selectedTask.risk]}>
                      {riskLabel(selectedTask.risk)}
                    </StatusBadge>
                  ),
                },
                { label: "创建时间", value: formatDateTime(selectedTask.createdAt) },
                {
                  label: "开始时间",
                  value: formatDateTime(selectedTask.startedAt),
                },
                {
                  label: "结束时间",
                  value: formatDateTime(selectedTask.finishedAt),
                },
                {
                  label: "退出码",
                  value:
                    selectedTask.exitCode === null ? (
                      "—"
                    ) : (
                      <span
                        className={
                          selectedTask.exitCode === 0 ? "text-success" : "text-danger"
                        }
                      >
                        {selectedTask.exitCode}
                      </span>
                    ),
                },
              ]}
            />

            {selectedTask.error ? (
              <ErrorBanner title="任务失败原因" message={selectedTask.error} />
            ) : null}

            <div>
              <h3 className="subheading">执行步骤</h3>
              {selectedTask.steps.length === 0 ? (
                <EmptyState title="暂无步骤信息" />
              ) : (
                <div className="table-wrap">
                  <table className="data-table">
                    <caption className="sr-only">任务执行步骤</caption>
                    <thead>
                      <tr>
                        <th scope="col">序号</th>
                        <th scope="col">步骤</th>
                        <th scope="col">状态</th>
                        <th scope="col">退出码</th>
                        <th scope="col">耗时</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedTask.steps.map((step) => (
                        <tr key={step.seq}>
                          <td className="mono">{step.seq}</td>
                          <td>{step.name}</td>
                          <td>
                            <StatusBadge tone={TASK_STEP_STATUS_TONES[step.status]}>
                              {TASK_STEP_STATUS_LABELS[step.status]}
                            </StatusBadge>
                          </td>
                          <td>{step.exitCode === null ? "—" : step.exitCode}</td>
                          <td>{formatStepDuration(step)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <div>
              <h3 className="subheading">输出摘要</h3>
              <pre className="console-output" data-testid="task-output" tabIndex={0}>
                {selectedTask.outputTail || "暂无输出"}
              </pre>
            </div>

            {streamLogs.length > 0 ? (
              <div>
                <h3 className="subheading">实时输出</h3>
                <pre className="console-output" aria-live="polite" tabIndex={0}>
                  {streamLogs.join("")}
                </pre>
              </div>
            ) : null}

            {stream.error && selectedActive ? (
              <InfoBanner tone="warning">{stream.error}</InfoBanner>
            ) : null}

            {cancelError ? (
              <ErrorBanner title="取消任务失败" message={cancelError} />
            ) : null}

            {isActiveTask(selectedTask.status) ? (
              <div className="task-actions">
                <button
                  type="button"
                  className="btn btn--danger"
                  onClick={() => void handleCancel()}
                  disabled={cancelPending}
                >
                  {cancelPending ? "取消中…" : "取消任务"}
                </button>
              </div>
            ) : null}
          </div>
        </SectionCard>
      ) : null}
    </div>
  );
}

function formatStepDuration(step: Task["steps"][number]): string {
  if (!step.startedAt) return step.status === "running" ? "进行中" : "—";
  const start = Date.parse(step.startedAt);
  if (!Number.isFinite(start)) return "—";
  const end = step.finishedAt ? Date.parse(step.finishedAt) : Date.now();
  if (!Number.isFinite(end)) return "—";
  return formatDuration(Math.max(0, (end - start) / 1000));
}
