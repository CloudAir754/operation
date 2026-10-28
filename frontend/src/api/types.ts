/**
 * Wire types for the LabOpsPanel HTTP API (contract v1).
 *
 * Field names are camelCase exactly as they appear on the wire. Timestamps are
 * ISO 8601 strings in UTC (`Z` suffix). Keep this file in sync with
 * `docs/requirements/api.md`.
 */

export type IsoDateTime = string;

/** Risk level attached to a structured action. */
export type Risk = "low" | "medium" | "high";

/** Per-section probe status. A failed probe must never be rendered as zero. */
export type SectionStatus = "ok" | "permission_denied" | "unavailable" | "error";

export type TaskStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";

export type TaskStepStatus = "pending" | "running" | "ok" | "failed" | "skipped";

export type ConnectStatus = "ok" | "failed" | "unknown";

export type ServerSource = "ssh_config" | "manual";

export interface FieldError {
  field: string;
  message: string;
}

/** Shape of `detail` inside an error response. */
export interface ApiErrorPayload {
  code: string;
  message: string;
  fields?: FieldError[];
  existingTaskId?: string;
}

export interface Health {
  status: string;
  version: string;
  time: IsoDateTime;
}

export interface LastConnect {
  status: ConnectStatus;
  error: string | null;
  at: IsoDateTime | null;
}

export interface Host {
  id: number;
  alias: string;
  hostname: string;
  user: string | null;
  port: number;
  identityFile: string | null;
  proxyJump: string | null;
  proxyCommand: string | null;
  connectTimeout: number;
  source: ServerSource;
  enabled: boolean;
  notes: string;
  hasSudoPassword: boolean;
  lastConnect: LastConnect | null;
}

export interface ServerList {
  servers: Host[];
  count: number;
}

export interface UpdateServerRequest {
  enabled?: boolean;
  notes?: string;
}

export interface ConnectionTestResult {
  ok: boolean;
  status: ConnectStatus;
  error: string | null;
  durationMs: number;
  exitCode: number | null;
  testedAt: IsoDateTime;
}

export interface SystemInfo {
  hostname: string;
  os: string;
  kernel: string;
  arch: string;
  uptimeSeconds: number;
  bootTime: IsoDateTime | null;
}

export interface CpuInfo {
  model: string;
  cores: number;
  usagePercent: number | null;
  load1: number | null;
  load5: number | null;
  load15: number | null;
}

export interface MemoryInfo {
  totalBytes: number;
  availableBytes: number;
  usedPercent: number | null;
  swapTotalBytes: number;
  swapUsedBytes: number;
}

export interface DiskMount {
  mount: string;
  filesystem: string;
  totalBytes: number;
  usedBytes: number;
  freeBytes: number;
  usedPercent: number | null;
}

export interface DiskInfo {
  mounts: DiskMount[];
}

export interface NetworkInterface {
  name: string;
  state: string;
  speedMbps: number | null;
  rxBytes: number | null;
  txBytes: number | null;
  rxErrors: number | null;
  txErrors: number | null;
  addresses: string[];
}

export interface NetworkInfo {
  defaultInterface: string | null;
  interfaces: NetworkInterface[];
  gateway: string | null;
}

/**
 * GPU payload follows the contract: memory is reported in MiB and the driver
 * may omit individual counters. Missing values must render as "N/A".
 */
export interface GpuDevice {
  index: number;
  name: string | null;
  driverVersion: string | null;
  memoryTotalMb: number | null;
  memoryUsedMb: number | null;
  utilizationPercent: number | null;
  temperatureC: number | null;
}

export interface GpuInfo {
  devices: GpuDevice[];
}

export interface ServiceUnit {
  name: string;
  load: string;
  active: string;
  sub: string;
  description: string;
  protected: boolean;
}

export interface ServicesInfo {
  runningCount: number;
  failedCount: number;
  failedUnits: string[];
  units: ServiceUnit[];
}

export interface SnapshotSection<T> {
  status: SectionStatus;
  error: string | null;
  data: T | null;
}

export interface SnapshotSections {
  system: SnapshotSection<SystemInfo>;
  cpu: SnapshotSection<CpuInfo>;
  memory: SnapshotSection<MemoryInfo>;
  disks: SnapshotSection<DiskInfo>;
  network: SnapshotSection<NetworkInfo>;
  gpu: SnapshotSection<GpuInfo>;
  services: SnapshotSection<ServicesInfo>;
}

export interface Snapshot {
  serverId: number;
  collectedAt: IsoDateTime;
  ageSeconds: number;
  stale: boolean;
  status: SectionStatus;
  error: string | null;
  data: SnapshotSections;
}

export interface ServicesResponse {
  serverId: number;
  collectedAt: IsoDateTime;
  status: SectionStatus;
  error: string | null;
  units: ServiceUnit[];
}

export interface LogsResponse {
  serverId: number;
  unit: string | null;
  collectedAt: IsoDateTime;
  lines: string[];
  exitCode: number | null;
  truncated: boolean;
  error: string | null;
  command: string;
}

export interface SecretStatus {
  serverId: number;
  hasPassword: boolean;
  updatedAt: IsoDateTime | null;
}

export interface SetSecretRequest {
  password: string;
}

export type TaskParams = Record<string, string | number | boolean>;

export interface TaskStep {
  seq: number;
  name: string;
  status: TaskStepStatus;
  exitCode: number | null;
  outputTail: string;
  startedAt: IsoDateTime | null;
  finishedAt: IsoDateTime | null;
}

export interface Task {
  id: string;
  serverId: number;
  serverAlias: string;
  action: string;
  actionLabel: string;
  risk: Risk;
  params: TaskParams;
  status: TaskStatus;
  progress: number;
  exitCode: number | null;
  error: string | null;
  outputTail: string;
  idempotencyKey: string | null;
  createdAt: IsoDateTime;
  startedAt: IsoDateTime | null;
  finishedAt: IsoDateTime | null;
  steps: TaskStep[];
}

export interface TaskList {
  tasks: Task[];
  count: number;
}

export interface CreateTaskRequest {
  serverId: number;
  action: string;
  params: TaskParams;
  confirm: true;
  idempotencyKey?: string;
}

export interface CancelTaskResponse {
  task: Task;
}

export interface ListTasksQuery {
  serverId?: number;
  status?: TaskStatus;
  limit?: number;
}

export interface TaskStatusEvent {
  type: "status";
  taskId: string;
  status: TaskStatus;
  progress: number;
  at: IsoDateTime;
}

export interface TaskStepEvent {
  type: "step";
  taskId: string;
  seq: number;
  name: string;
  status: TaskStepStatus;
}

export interface TaskLogEvent {
  type: "log";
  taskId: string;
  channel: string;
  chunk: string;
  at: IsoDateTime;
}

export interface TaskDoneEvent {
  type: "done";
  taskId: string;
  status: TaskStatus;
  progress: number;
  exitCode: number | null;
  error: string | null;
}

export type TaskEvent =
  | TaskStatusEvent
  | TaskStepEvent
  | TaskLogEvent
  | TaskDoneEvent;

export interface SshConfigHost {
  alias: string;
  hostname: string | null;
  user: string | null;
  port: number | null;
  identityFile: string | null;
  proxyJump: string | null;
  proxyCommand: string | null;
  connectTimeout: number | null;
  imported: boolean;
  lineNumber: number;
}

export interface SshConfigInclude {
  pattern: string;
  resolved: string[];
  error: string | null;
}

export interface SshConfigError {
  path: string;
  line: number | null;
  message: string;
}

export interface SshConfig {
  path: string;
  exists: boolean;
  hosts: SshConfigHost[];
  includes: SshConfigInclude[];
  errors: SshConfigError[];
}

/** Alias matching the contract's short name for a parsed SSH host. */
export type SshHost = SshConfigHost;

export interface ImportRequest {
  aliases?: string[] | null;
  pruneMissing?: boolean;
}

export interface ImportSkipped {
  alias: string;
  reason: string;
}

export interface ImportResult {
  imported: string[];
  updated: string[];
  skipped: ImportSkipped[];
  errors: SshConfigError[];
}

export interface Settings {
  allowArbitraryCommands: boolean;
  snapshotTtlSeconds: number;
  maxOutputBytes: number;
  sshConfigPath: string;
  appDataDir: string;
  bindHost: string;
  bindPort: number;
  transport: string;
}

export interface ActionField {
  name: string;
  required: boolean;
  type: string;
  enum?: string[];
}

export interface ActionSpec {
  id: string;
  label: string;
  description: string;
  risk: Risk;
  requiresConfirmation: boolean;
  requiresIdempotencyKey: boolean;
  fields: ActionField[];
}

export interface ActionList {
  actions: ActionSpec[];
}

export interface AuditEntry {
  id: number;
  action: string;
  target: string;
  outcome: string;
  detail: string | null;
  createdAt: IsoDateTime;
}

export interface AuditList {
  entries: AuditEntry[];
}
