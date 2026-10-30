import { ApiError, isAbortError } from "./errors";
import type {
  ActionList,
  AuditList,
  CancelTaskResponse,
  ConnectionTestResult,
  CreateTaskRequest,
  GpuMonitor,
  Health,
  Host,
  ImportRequest,
  ImportResult,
  ListTasksQuery,
  LogsResponse,
  Metrics,
  MetricsWindow,
  MountsResponse,
  SecretStatus,
  ServerList,
  ServicesResponse,
  Settings,
  SetSecretRequest,
  Snapshot,
  SshConfig,
  StorageNode,
  StorageScan,
  StorageScanList,
  StorageScanRequest,
  Task,
  TaskList,
  UpdateServerRequest,
  UsersResponse,
  UserStorageScanRequest,
} from "./types";

export const API_BASE_PATH = "/api";

/** Shown when fetch itself rejects (backend down, DNS, refused connection). */
export const NETWORK_ERROR_MESSAGE =
  "无法连接到本机服务（127.0.0.1:8765）。请确认后端已启动，然后重试。";

export { ApiError } from "./errors";

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface RequestOptions {
  method?: HttpMethod;
  /** Serialised as JSON. Omit for GET/DELETE. */
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Perform a JSON request against the local API and map every failure to an
 * `ApiError` carrying the contract error `code` and `fields`.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(`${API_BASE_PATH}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      credentials: "same-origin",
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError(NETWORK_ERROR_MESSAGE, { code: "network_error", status: 0 });
  }

  if (response.status === 204) {
    return undefined as T;
  }

  let raw: string;
  try {
    raw = await response.text();
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError(NETWORK_ERROR_MESSAGE, { code: "network_error", status: 0 });
  }
  const payload = parseJson(raw);

  if (!response.ok) {
    throw toApiError(response.status, payload);
  }
  return payload as T;
}

function parseJson(raw: string): unknown {
  if (!raw.trim()) return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
}

function toApiError(status: number, payload: unknown): ApiError {
  const detail =
    typeof payload === "object" && payload !== null
      ? (payload as Record<string, unknown>).detail
      : undefined;

  if (typeof detail === "string" && detail.trim()) {
    return new ApiError(detail, { code: "http_error", status });
  }

  if (typeof detail === "object" && detail !== null) {
    const record = detail as Record<string, unknown>;
    const code = typeof record.code === "string" ? record.code : "http_error";
    const message =
      typeof record.message === "string" && record.message.trim()
        ? record.message
        : `请求失败（HTTP ${status}）`;
    const existingTaskId =
      typeof record.existingTaskId === "string" ? record.existingTaskId : null;
    return new ApiError(message, {
      code,
      status,
      fields: toFieldErrors(record.fields),
      existingTaskId,
    });
  }

  return new ApiError(`请求失败（HTTP ${status}）`, { code: "http_error", status });
}

function toFieldErrors(value: unknown): { field: string; message: string }[] {
  if (!Array.isArray(value)) return [];
  const result: { field: string; message: string }[] = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null) continue;
    const record = item as Record<string, unknown>;
    if (typeof record.field === "string" && typeof record.message === "string") {
      result.push({ field: record.field, message: record.message });
    }
  }
  return result;
}

/* -------------------------------------------------------------------------- */
/* System                                                                     */
/* -------------------------------------------------------------------------- */

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return request<Health>("/health", { signal });
}

export function getSettings(signal?: AbortSignal): Promise<Settings> {
  return request<Settings>("/settings", { signal });
}

export function getActions(signal?: AbortSignal): Promise<ActionList> {
  return request<ActionList>("/actions", { signal });
}

/* -------------------------------------------------------------------------- */
/* SSH config & inventory                                                     */
/* -------------------------------------------------------------------------- */

export function getSshConfig(signal?: AbortSignal): Promise<SshConfig> {
  return request<SshConfig>("/ssh-config", { signal });
}

export function importServers(
  payload: ImportRequest = {},
  signal?: AbortSignal,
): Promise<ImportResult> {
  return request<ImportResult>("/servers/import", {
    method: "POST",
    body: { aliases: payload.aliases ?? null, pruneMissing: payload.pruneMissing ?? false },
    signal,
  });
}

export function getServers(signal?: AbortSignal): Promise<ServerList> {
  return request<ServerList>("/servers", { signal });
}

/** Convenience helper: the contract exposes hosts via the list endpoint. */
export async function getHost(serverId: number, signal?: AbortSignal): Promise<Host> {
  const { servers } = await getServers(signal);
  const host = servers.find((item) => item.id === serverId);
  if (!host) {
    throw new ApiError("主机不存在或已被移除。", { code: "server_not_found", status: 404 });
  }
  return host;
}

export function updateServer(
  serverId: number,
  patch: UpdateServerRequest,
  signal?: AbortSignal,
): Promise<Host> {
  return request<Host>(`/servers/${encodeURIComponent(String(serverId))}`, {
    method: "PATCH",
    body: patch,
    signal,
  });
}

export function testConnection(
  serverId: number,
  signal?: AbortSignal,
): Promise<ConnectionTestResult> {
  return request<ConnectionTestResult>(
    `/servers/${encodeURIComponent(String(serverId))}/connection-test`,
    { method: "POST", signal },
  );
}

/* -------------------------------------------------------------------------- */
/* Snapshots, services, logs, secrets                                         */
/* -------------------------------------------------------------------------- */

export function getSnapshot(
  serverId: number,
  options: { refresh?: boolean; signal?: AbortSignal } = {},
): Promise<Snapshot> {
  const query = options.refresh ? "?refresh=true" : "";
  return request<Snapshot>(
    `/servers/${encodeURIComponent(String(serverId))}/snapshot${query}`,
    { signal: options.signal },
  );
}

/**
 * One remote sample per call. `window` only trims the returned ring buffer, so
 * switching it must re-request immediately.
 */
export function getMetrics(
  serverId: number,
  options: { window?: MetricsWindow; signal?: AbortSignal } = {},
): Promise<Metrics> {
  const window = options.window ?? "5m";
  return request<Metrics>(
    `/servers/${encodeURIComponent(String(serverId))}/metrics?window=${window}`,
    { signal: options.signal },
  );
}

export function getGpus(
  serverId: number,
  options: { refresh?: boolean; signal?: AbortSignal } = {},
): Promise<GpuMonitor> {
  const query = options.refresh ? "?refresh=true" : "";
  return request<GpuMonitor>(`/servers/${encodeURIComponent(String(serverId))}/gpus${query}`, {
    signal: options.signal,
  });
}

export function getUsers(
  serverId: number,
  options: { includeSystem?: boolean; signal?: AbortSignal } = {},
): Promise<UsersResponse> {
  const includeSystem = options.includeSystem ?? false;
  return request<UsersResponse>(
    `/servers/${encodeURIComponent(String(serverId))}/users?includeSystem=${includeSystem}`,
    { signal: options.signal },
  );
}

export function getMounts(
  serverId: number,
  options: { refresh?: boolean; signal?: AbortSignal } = {},
): Promise<MountsResponse> {
  const query = options.refresh ? "?refresh=true" : "";
  return request<MountsResponse>(`/servers/${encodeURIComponent(String(serverId))}/mounts${query}`, {
    signal: options.signal,
  });
}

export function listStorageScans(
  serverId: number,
  signal?: AbortSignal,
): Promise<StorageScanList> {
  return request<StorageScanList>(
    `/servers/${encodeURIComponent(String(serverId))}/storage/scans`,
    { signal },
  );
}

export function createStorageScan(
  serverId: number,
  payload: StorageScanRequest,
  signal?: AbortSignal,
): Promise<StorageScan> {
  return request<StorageScan>(`/servers/${encodeURIComponent(String(serverId))}/storage/scans`, {
    method: "POST",
    body: payload,
    signal,
  });
}

/** Per-UID full-disk scan started from the 用户 tab. */
export function createUserStorageScan(
  serverId: number,
  payload: UserStorageScanRequest,
  signal?: AbortSignal,
): Promise<StorageScan> {
  return request<StorageScan>(
    `/servers/${encodeURIComponent(String(serverId))}/users/storage-scan`,
    { method: "POST", body: payload, signal },
  );
}

export function getStorageScan(scanId: string, signal?: AbortSignal): Promise<StorageScan> {
  return request<StorageScan>(`/storage/scans/${encodeURIComponent(scanId)}`, { signal });
}

export function cancelStorageScan(scanId: string): Promise<StorageScan> {
  return request<StorageScan>(`/storage/scans/${encodeURIComponent(scanId)}/cancel`, {
    method: "POST",
  });
}

export function getStorageScanChildren(
  scanId: string,
  path: string,
  signal?: AbortSignal,
): Promise<StorageNode[]> {
  const params = new URLSearchParams({ path });
  return request<StorageNode[]>(
    `/storage/scans/${encodeURIComponent(scanId)}/children?${params.toString()}`,
    { signal },
  );
}

export function storageScanEventsUrl(scanId: string): string {
  return `${API_BASE_PATH}/storage/scans/${encodeURIComponent(scanId)}/events`;
}

export function getServices(
  serverId: number,
  options: { refresh?: boolean; signal?: AbortSignal } = {},
): Promise<ServicesResponse> {
  const query = options.refresh ? "?refresh=true" : "";
  return request<ServicesResponse>(
    `/servers/${encodeURIComponent(String(serverId))}/services${query}`,
    { signal: options.signal },
  );
}

export function getLogs(
  serverId: number,
  options: { unit?: string | null; lines?: number; signal?: AbortSignal } = {},
): Promise<LogsResponse> {
  const params = new URLSearchParams();
  if (options.unit) params.set("unit", options.unit);
  params.set("lines", String(options.lines ?? 200));
  return request<LogsResponse>(
    `/servers/${encodeURIComponent(String(serverId))}/logs?${params.toString()}`,
    { signal: options.signal },
  );
}

export function getSecret(serverId: number, signal?: AbortSignal): Promise<SecretStatus> {
  return request<SecretStatus>(`/servers/${encodeURIComponent(String(serverId))}/secret`, {
    signal,
  });
}

export function setSecret(serverId: number, password: string): Promise<SecretStatus> {
  const body: SetSecretRequest = { password };
  return request<SecretStatus>(`/servers/${encodeURIComponent(String(serverId))}/secret`, {
    method: "PUT",
    body,
  });
}

export function clearSecret(serverId: number): Promise<void> {
  return request<void>(`/servers/${encodeURIComponent(String(serverId))}/secret`, {
    method: "DELETE",
  });
}

/* -------------------------------------------------------------------------- */
/* Tasks                                                                      */
/* -------------------------------------------------------------------------- */

export function listTasks(query: ListTasksQuery = {}, signal?: AbortSignal): Promise<TaskList> {
  const params = new URLSearchParams();
  if (query.serverId !== undefined) params.set("serverId", String(query.serverId));
  if (query.status !== undefined) params.set("status", query.status);
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  const search = params.toString();
  return request<TaskList>(`/tasks${search ? `?${search}` : ""}`, { signal });
}

export function getTask(taskId: string, signal?: AbortSignal): Promise<Task> {
  return request<Task>(`/tasks/${encodeURIComponent(taskId)}`, { signal });
}

export function createTask(payload: CreateTaskRequest, signal?: AbortSignal): Promise<Task> {
  return request<Task>("/tasks", { method: "POST", body: payload, signal });
}

export function cancelTask(taskId: string): Promise<CancelTaskResponse> {
  return request<CancelTaskResponse>(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST",
  });
}

/** Absolute path of the SSE stream for a task, suitable for `EventSource`. */
export function taskEventsUrl(taskId: string): string {
  return `${API_BASE_PATH}/tasks/${encodeURIComponent(taskId)}/events`;
}

/* -------------------------------------------------------------------------- */
/* Audit                                                                      */
/* -------------------------------------------------------------------------- */

export function getAudit(limit = 50, signal?: AbortSignal): Promise<AuditList> {
  const params = new URLSearchParams({ limit: String(limit) });
  return request<AuditList>(`/audit?${params.toString()}`, { signal });
}
