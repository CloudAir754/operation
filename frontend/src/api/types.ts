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

/* -------------------------------------------------------------------------- */
/* Stage 3 — resource observability                                           */
/* -------------------------------------------------------------------------- */

/**
 * Section envelope used by every stage-3 probe. When `status !== "ok"` the
 * backend forces `data` to `null`; a missing value must render as "N/A" and
 * never as zero.
 */
export interface ObservableSection<T> {
  status: SectionStatus;
  error: string | null;
  requiresSudo: boolean;
  collectedAt: IsoDateTime | null;
  data: T | null;
}

export interface CpuMetricData {
  totalPercent: number | null;
  userPercent: number | null;
  systemPercent: number | null;
  iowaitPercent: number | null;
  stealPercent: number | null;
  idlePercent: number | null;
  logicalCpus: number | null;
  load1: number | null;
  load5: number | null;
  load15: number | null;
}

export interface MemoryMetricData {
  totalBytes: number | null;
  availableBytes: number | null;
  usedBytes: number | null;
  usedPercent: number | null;
  buffersBytes: number | null;
  cachedBytes: number | null;
  commitBytes: number | null;
  swapTotalBytes: number | null;
  swapUsedBytes: number | null;
  swapFreeBytes: number | null;
  swapInPerSecond: number | null;
  swapOutPerSecond: number | null;
}

export interface DiskDeviceMetric {
  name: string;
  kind: string | null;
  parent: string | null;
  model: string | null;
  sizeBytes: number | null;
  hidden: boolean;
  readBytesPerSecond: number | null;
  writeBytesPerSecond: number | null;
  readIops: number | null;
  writeIops: number | null;
  awaitMs: number | null;
  utilPercent: number | null;
  reset: boolean;
}

export interface DiskMountMetric {
  mount: string;
  device: string | null;
  filesystem: string | null;
  deviceName: string | null;
  readBytesPerSecond: number | null;
  writeBytesPerSecond: number | null;
}

export interface DiskMetricData {
  devices: DiskDeviceMetric[];
  mounts: DiskMountMetric[];
}

export interface NetworkInterfaceMetric {
  name: string;
  kind: string | null;
  state: string | null;
  speedMbps: number | null;
  addresses: string[];
  rxBytesPerSecond: number | null;
  txBytesPerSecond: number | null;
  rxPacketsPerSecond: number | null;
  txPacketsPerSecond: number | null;
  rxErrors: number | null;
  txErrors: number | null;
  rxDrops: number | null;
  txDrops: number | null;
  reset: boolean;
}

export interface NetworkMetricData {
  defaultInterface: string | null;
  interfaces: NetworkInterfaceMetric[];
}

export interface GpuSampleMetric {
  index: number;
  uuid: string | null;
  name: string | null;
  utilizationPercent: number | null;
  memoryUsedMb: number | null;
  memoryTotalMb: number | null;
  encoderPercent: number | null;
  decoderPercent: number | null;
  temperatureC: number | null;
  powerDrawW: number | null;
}

export interface GpuMetricData {
  devices: GpuSampleMetric[];
}

export interface MetricsCurrent {
  cpu: ObservableSection<CpuMetricData>;
  memory: ObservableSection<MemoryMetricData>;
  disk: ObservableSection<DiskMetricData>;
  network: ObservableSection<NetworkMetricData>;
  gpu: ObservableSection<GpuMetricData>;
}

export interface DiskRateSample {
  readBytesPerSecond: number | null;
  writeBytesPerSecond: number | null;
  readIops: number | null;
  writeIops: number | null;
  reset: boolean;
}

export interface NetworkRateSample {
  rxBytesPerSecond: number | null;
  txBytesPerSecond: number | null;
  rxPacketsPerSecond: number | null;
  txPacketsPerSecond: number | null;
  reset: boolean;
}

/** One point of the in-memory ring buffer. Rates are null on `reset`. */
export interface MetricPoint {
  collectedAt: IsoDateTime;
  receivedAt: IsoDateTime;
  sampleIntervalMs: number;
  reset: boolean;
  resetReason: string | null;
  cpuPercent: number | null;
  userPercent: number | null;
  systemPercent: number | null;
  iowaitPercent: number | null;
  memoryUsedPercent: number | null;
  swapUsedPercent: number | null;
  disk: Record<string, DiskRateSample>;
  network: Record<string, NetworkRateSample>;
  gpu: GpuSampleMetric[];
}

export type MetricsWindow = "1m" | "5m" | "15m" | "1h";

export interface Metrics {
  serverId: number;
  collectedAt: IsoDateTime;
  windowSeconds: number;
  retentionSeconds: number;
  sampleIntervalMs: number;
  stale: boolean;
  status: SectionStatus;
  error: string | null;
  requiresSudo: boolean;
  current: MetricsCurrent;
  points: MetricPoint[];
  raw: Record<string, unknown> | null;
}

/* -------------------------------- GPU ------------------------------------- */

export type GpuProcessesStatus = "ok" | "partial" | "unavailable";

export interface GpuCapabilities {
  nvml: boolean;
  processes: GpuProcessesStatus;
  mig: boolean;
  perProcessUtilization: boolean;
}

export interface GpuDeviceDetail {
  index: number;
  uuid: string | null;
  pciBusId: string | null;
  name: string | null;
  driverVersion: string | null;
  memoryTotalMb: number | null;
  memoryUsedMb: number | null;
  utilizationGpuPercent: number | null;
  utilizationMemoryPercent: number | null;
  encoderPercent: number | null;
  decoderPercent: number | null;
  temperatureC: number | null;
  powerDrawW: number | null;
  powerLimitW: number | null;
  clockGraphicsMhz: number | null;
  clockMemoryMhz: number | null;
  migMode: string | null;
  computeMode: string | null;
  migDevices: string[];
}

export interface GpuProcess {
  pid: number;
  user: string | null;
  name: string | null;
  command: string | null;
  memoryUsedMb: number | null;
  utilizationPercent: number | null;
  gpuUuid: string | null;
  gpuIndex: number | null;
  programPath: string | null;
  cwd: string | null;
  startedAt: IsoDateTime | null;
  stale: boolean;
  error: string | null;
}

export interface GpuMonitor {
  serverId: number;
  collectedAt: IsoDateTime;
  status: SectionStatus;
  error: string | null;
  requiresSudo: boolean;
  capabilities: GpuCapabilities;
  devices: GpuDeviceDetail[];
  processes: GpuProcess[];
  processesStatus: GpuProcessesStatus;
  processesError: string | null;
  notes: string[];
}

/* -------------------------------- Users ----------------------------------- */

export type SudoEffective = "true" | "false" | "unknown";

export interface SudoState {
  groupMember: boolean | null;
  policyPresent: boolean | null;
  effective: SudoEffective;
  checkedAt: IsoDateTime | null;
  error: string | null;
}

export interface HomeStorage {
  allocatedBytes: number | null;
  apparentBytes: number | null;
  fileCount: number | null;
  directoryCount: number | null;
  depth: number | null;
  truncated: boolean;
  incomplete: boolean;
  error: string | null;
  collectedAt: IsoDateTime | null;
}

export interface UserInfo {
  login: string;
  uid: number;
  gid: number;
  primaryGroup: string | null;
  supplementaryGroups: string[];
  home: string;
  shell: string;
  system: boolean;
  recentLogin: string | null;
  sudo: SudoState;
  homeStorage: HomeStorage | null;
}

export interface UsersResponse {
  serverId: number;
  collectedAt: IsoDateTime;
  includeSystem: boolean;
  status: SectionStatus;
  error: string | null;
  requiresSudo: boolean;
  users: UserInfo[];
  notes: string[];
}

/* -------------------------------- Mounts ---------------------------------- */

export interface MountInfo {
  mount: string;
  device: string | null;
  filesystem: string | null;
  options: string | null;
  uuid: string | null;
  majorMinor: string | null;
  physicalDevice: string | null;
  readOnly: boolean;
}

export interface MountsResponse {
  serverId: number;
  collectedAt: IsoDateTime;
  status: SectionStatus;
  error: string | null;
  requiresSudo: boolean;
  mounts: MountInfo[];
}

/* ----------------------------- Storage scans ------------------------------ */

export type ScanStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";

export type StorageNodeKind = "dir" | "file" | "symlink" | "other";

export interface StorageNode {
  id: string;
  path: string;
  parentPath: string | null;
  name: string;
  kind: StorageNodeKind;
  allocatedBytes: number | null;
  apparentBytes: number | null;
  fileCount: number | null;
  directoryCount: number | null;
  uid: number | null;
  gid: number | null;
  inode: number | null;
  hardlinkDuplicateOf: string | null;
  deletedOpen: boolean;
  unscanned: boolean;
  incomplete: boolean;
  error: string | null;
  modifiedAt: IsoDateTime | null;
  /** Present when the backend inlines one level of children. */
  children?: StorageNode[];
}

export interface StorageUidSummary {
  uid: number;
  allocatedBytes: number;
  apparentBytes: number;
  fileCount: number;
  directoryCount: number;
  incomplete: boolean;
}

export interface StorageScan {
  scanId: string;
  serverId: number;
  serverAlias: string;
  mount: string;
  mountInfo: Record<string, string | null> | null;
  status: ScanStatus;
  stage: string;
  progress: number;
  oneFileSystem: boolean;
  followSymlinks: boolean;
  maxDepth: number;
  userFilter: number | null;
  minFileSize: number;
  extension: string | null;
  nodeCount: number;
  truncated: boolean;
  incomplete: boolean;
  crossDeviceCount: number;
  deletedOpenBytes: number;
  deletedOpenCount: number;
  nodes: StorageNode[];
  largestFiles: StorageNode[];
  summaryByUid: StorageUidSummary[];
  error: string | null;
  createdAt: IsoDateTime;
  startedAt: IsoDateTime | null;
  finishedAt: IsoDateTime | null;
}

export interface StorageScanList {
  scans: StorageScan[];
  count: number;
}

export interface StorageScanRequest {
  mount: string;
  oneFileSystem: boolean;
  followSymlinks: boolean;
  maxDepth: number;
  minFileSize: number;
  extension: string | null;
  modifiedAfter: string | null;
  includeDeletedOpen: boolean;
}

export interface UserStorageScanRequest {
  uid: number;
  mount: string;
  maxDepth: number;
  minFileSize: number;
  includeDeletedOpen: boolean;
}

export interface ScanProgressEvent {
  type: "progress";
  scanId: string;
  status: ScanStatus;
  stage: string;
  progress: number;
  at?: IsoDateTime;
}

export interface ScanNodesEvent {
  type: "nodes";
  scanId: string;
  offset: number;
  chunk: StorageNode[];
  at?: IsoDateTime;
}

export interface ScanDoneEvent {
  type: "done";
  scanId: string;
  status: ScanStatus;
  progress: number;
  error: string | null;
  nodeCount: number;
  truncated: boolean;
  incomplete: boolean;
  summary?: StorageUidSummary[];
  at?: IsoDateTime;
}

export type ScanEvent = ScanProgressEvent | ScanNodesEvent | ScanDoneEvent;

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
