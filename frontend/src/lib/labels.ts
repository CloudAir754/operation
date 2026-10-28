import type {
  ConnectStatus,
  Risk,
  SectionStatus,
  ServerSource,
  TaskStatus,
  TaskStepStatus,
} from "../api/types";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "muted";

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "执行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

export const TASK_STATUS_TONES: Record<TaskStatus, Tone> = {
  queued: "neutral",
  running: "info",
  succeeded: "success",
  failed: "danger",
  cancelled: "muted",
  interrupted: "warning",
};

export const TASK_STEP_STATUS_LABELS: Record<TaskStepStatus, string> = {
  pending: "等待中",
  running: "执行中",
  ok: "完成",
  failed: "失败",
  skipped: "已跳过",
};

export const TASK_STEP_STATUS_TONES: Record<TaskStepStatus, Tone> = {
  pending: "muted",
  running: "info",
  ok: "success",
  failed: "danger",
  skipped: "neutral",
};

export const RISK_LABELS: Record<Risk, string> = {
  low: "低风险",
  medium: "中风险",
  high: "高风险",
};

export const RISK_TONES: Record<Risk, Tone> = {
  low: "success",
  medium: "warning",
  high: "danger",
};

export const SECTION_STATUS_LABELS: Record<SectionStatus, string> = {
  ok: "正常",
  permission_denied: "权限不足",
  unavailable: "不可用",
  error: "采集失败",
};

export const SECTION_STATUS_TONES: Record<SectionStatus, Tone> = {
  ok: "success",
  permission_denied: "warning",
  unavailable: "neutral",
  error: "danger",
};

export const CONNECT_STATUS_LABELS: Record<ConnectStatus, string> = {
  ok: "连接正常",
  failed: "连接失败",
  unknown: "尚未测试",
};

export const CONNECT_STATUS_TONES: Record<ConnectStatus, Tone> = {
  ok: "success",
  failed: "danger",
  unknown: "muted",
};

export const SERVER_SOURCE_LABELS: Record<ServerSource, string> = {
  ssh_config: "SSH 配置",
  manual: "手动添加",
};

const ACTION_LABELS: Record<string, string> = {
  collect_snapshot: "采集快照",
  host_check: "主机检查",
  query_logs: "查询日志",
  service_start: "启动服务",
  service_stop: "停止服务",
  service_restart: "重启服务",
  service_enable: "启用服务",
  service_disable: "禁用服务",
};

/** Risk defaults used when the action registry is not loaded. */
export const ACTION_RISK: Record<string, Risk> = {
  collect_snapshot: "low",
  host_check: "low",
  query_logs: "low",
  service_start: "medium",
  service_stop: "medium",
  service_restart: "medium",
  service_enable: "medium",
  service_disable: "high",
};

export function actionLabel(action: string, fallback?: string): string {
  return ACTION_LABELS[action] ?? fallback ?? action;
}

export function riskLabel(risk: Risk | string): string {
  return RISK_LABELS[risk as Risk] ?? String(risk);
}

export function isActiveTask(status: TaskStatus): boolean {
  return status === "queued" || status === "running";
}
