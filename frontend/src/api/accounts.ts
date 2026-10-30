/** 账户生命周期 API 客户端与线协议类型（阶段四）。 */

import { request } from "./client";
import type { IsoDateTime, SectionStatus } from "./types";

export interface CheckItem {
  code: string;
  label: string;
  ok: boolean;
  message: string;
  blocking: boolean;
}

export interface PasswordRules {
  length: number;
  minUpper: number;
  minLower: number;
  minDigits: number;
  minSymbols: number;
  symbols: string;
  excludeAmbiguous: boolean;
  excludeChars: string;
}

export interface Adjustment {
  field: string;
  label: string;
  requested: number;
  applied: number;
  reason: string;
}

export interface PolicyFloor {
  minLength: number;
  minUpper: number;
  minLower: number;
  minDigits: number;
  minSymbols: number;
  minClasses: number;
  maxRepeat: number;
  maxSequence: number;
  maxClassRepeat: number;
  rejectUsername: boolean;
  source: string;
  notes: string[];
}

export interface GeneratorInfo {
  configured: boolean;
  rules: PasswordRules;
  effectiveLength: number;
  adjustments: Adjustment[];
  example: string;
  checks: CheckItem[];
  floor: PolicyFloor | null;
  serverId: number | null;
  updatedAt: IsoDateTime | null;
}

export interface PolicyField {
  key: string;
  label: string;
  value: number;
  origin: "pam" | "conf" | "default";
  writable: boolean;
  min: number;
  max: number;
  reason: string | null;
}

export interface PolicyCapabilities {
  pwqualityConf: boolean;
  pwqualityModule: boolean;
  pwqualityModuleEditable: boolean;
  pamRetryEditable: boolean;
  pwscore: boolean;
  chage: boolean;
  useradd: boolean;
  rsync: boolean;
  dropInDir: boolean;
  writableFields: string[];
  readOnly: boolean;
  reasons: string[];
}

export interface PasswordPolicy {
  serverId: number;
  collectedAt: IsoDateTime;
  status: SectionStatus;
  error: string | null;
  os: Record<string, string | null>;
  fields: PolicyField[];
  capabilities: PolicyCapabilities;
  managedLines: string[];
  unknownKeys: string[];
  loginDefs: Record<string, string>;
  notes: string[];
  floor: PolicyFloor | null;
  generator: GeneratorInfo | null;
}

export interface PolicyDiff {
  serverId: number;
  fields: Record<string, { before: number | null; after: number | null }>;
  diff: string;
  blockers: string[];
  warnings: string[];
  applicable: boolean;
}

export interface PolicyChange {
  id: string;
  serverId: number;
  status: "applied" | "failed" | "restored";
  targets: Record<string, number>;
  diff: string;
  backupId: string;
  validation: Record<string, unknown>;
  error: string | null;
  createdAt: IsoDateTime;
  restoredAt: IsoDateTime | null;
}

export interface AccountBatchUser {
  login: string;
  comment?: string;
  shell?: string;
  groups?: string[];
  uid?: number | null;
  password?: string | null;
}

export interface AccountBatchResult {
  login: string;
  status: string;
  error: string | null;
  uid: number | null;
  home: string | null;
  passwordGenerated: boolean;
}

export interface AccountBatch {
  id: string;
  serverId: number;
  serverAlias: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  stage: string;
  progress: number;
  results: AccountBatchResult[];
  error: string | null;
  hasGeneratedPasswords: boolean;
  createdAt: IsoDateTime;
  startedAt: IsoDateTime | null;
  finishedAt: IsoDateTime | null;
}

export interface AccountBatchList {
  batches: AccountBatch[];
  count: number;
}

export interface RevealResult {
  batchId: string;
  passwords: Record<string, string>;
  note: string;
}

export interface MigrationPaths {
  source: string;
  targetRoot: string;
  target: string;
  staging: string;
  backup: string;
}

export interface MigrationPreflight {
  serverId: number;
  login: string;
  paths: MigrationPaths;
  sourceInfo: Record<string, unknown>;
  targetInfo: Record<string, unknown>;
  counts: Record<string, number | boolean | null>;
  space: Record<string, number | boolean | string | null>;
  checks: CheckItem[];
  allowed: boolean;
  blockers: string[];
  requiresSudo: boolean;
  collectedAt: IsoDateTime;
}

export type MigrationStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "rolled_back";

export interface MigrationJob {
  id: string;
  serverId: number;
  serverAlias: string;
  login: string;
  uid: number;
  gid: number;
  status: MigrationStatus;
  stage: string;
  stageIndex: number;
  stages: string[];
  progress: number;
  paths: MigrationPaths;
  counts: Record<string, number | boolean | null>;
  space: Record<string, number | boolean | string | null>;
  checks: CheckItem[];
  result: Record<string, unknown>;
  error: string | null;
  accountStatusBefore: string | null;
  backupRetentionDays: number;
  rollbackAvailable: boolean;
  cleanupAvailable: boolean;
  cancelRequested: boolean;
  createdAt: IsoDateTime;
  startedAt: IsoDateTime | null;
  finishedAt: IsoDateTime | null;
  cleanedAt: IsoDateTime | null;
}

export interface MigrationList {
  migrations: MigrationJob[];
  count: number;
}

export interface StorageSettings {
  serverId: number;
  dataRoot: string | null;
  migrationRoot: string | null;
  defaultDataRoot: string;
  defaultMigrationRoot: string;
}

export interface Dataset {
  id: string;
  serverId: number;
  login: string;
  uid: number;
  gid: number;
  dataRoot: string;
  dataPath: string;
  linkPath: string;
  status: "succeeded" | "failed" | "unlinked";
  writable: boolean;
  error: string | null;
  createdAt: IsoDateTime;
}

export interface DatasetList {
  datasets: Dataset[];
  count: number;
}

/* -------------------------------------------------------------------------- */
/* 密码生成规则                                                               */
/* -------------------------------------------------------------------------- */

export function getGenerator(signal?: AbortSignal): Promise<GeneratorInfo> {
  return request<GeneratorInfo>("/accounts/generator", { signal });
}

export function saveGenerator(rules: PasswordRules): Promise<GeneratorInfo> {
  return request<GeneratorInfo>("/accounts/generator", { method: "PUT", body: { rules } });
}

export function previewGenerator(
  rules: PasswordRules,
  serverId?: number,
  signal?: AbortSignal,
): Promise<GeneratorInfo> {
  return request<GeneratorInfo>("/accounts/generator/preview", {
    method: "POST",
    body: { rules, serverId: serverId ?? null },
    signal,
  });
}

/* -------------------------------------------------------------------------- */
/* 系统密码策略                                                               */
/* -------------------------------------------------------------------------- */

export function getPasswordPolicy(
  serverId: number,
  options: { refresh?: boolean; signal?: AbortSignal } = {},
): Promise<PasswordPolicy> {
  const query = options.refresh ? "?refresh=true" : "";
  return request<PasswordPolicy>(
    `/servers/${encodeURIComponent(String(serverId))}/accounts/policy${query}`,
    { signal: options.signal },
  );
}

export function previewPolicy(
  serverId: number,
  targets: Record<string, number>,
  signal?: AbortSignal,
): Promise<PolicyDiff> {
  return request<PolicyDiff>(
    `/servers/${encodeURIComponent(String(serverId))}/accounts/policy/preview`,
    { method: "POST", body: { targets }, signal },
  );
}

export function applyPolicy(
  serverId: number,
  targets: Record<string, number>,
): Promise<PolicyChange> {
  return request<PolicyChange>(
    `/servers/${encodeURIComponent(String(serverId))}/accounts/policy`,
    { method: "PUT", body: { targets, confirm: true } },
  );
}

export function listPolicyChanges(
  serverId: number,
  signal?: AbortSignal,
): Promise<{ changes: PolicyChange[]; count: number }> {
  return request<{ changes: PolicyChange[]; count: number }>(
    `/servers/${encodeURIComponent(String(serverId))}/accounts/policy/changes`,
    { signal },
  );
}

export function restorePolicy(serverId: number, changeId: string): Promise<PolicyChange> {
  return request<PolicyChange>(
    `/servers/${encodeURIComponent(String(serverId))}/accounts/policy/changes/${encodeURIComponent(
      changeId,
    )}/restore`,
    { method: "POST" },
  );
}

/* -------------------------------------------------------------------------- */
/* 批量新建用户                                                               */
/* -------------------------------------------------------------------------- */

export function createAccountBatch(
  serverId: number,
  users: AccountBatchUser[],
  rules?: PasswordRules,
): Promise<AccountBatch> {
  return request<AccountBatch>(
    `/servers/${encodeURIComponent(String(serverId))}/account-batches`,
    {
      method: "POST",
      body: { users, rules: rules ?? null, confirm: true },
    },
  );
}

export function listAccountBatches(
  serverId: number,
  signal?: AbortSignal,
): Promise<AccountBatchList> {
  return request<AccountBatchList>(
    `/servers/${encodeURIComponent(String(serverId))}/account-batches`,
    { signal },
  );
}

export function getAccountBatch(batchId: string, signal?: AbortSignal): Promise<AccountBatch> {
  return request<AccountBatch>(`/account-batches/${encodeURIComponent(batchId)}`, { signal });
}

export function revealAccountPasswords(batchId: string): Promise<RevealResult> {
  return request<RevealResult>(
    `/account-batches/${encodeURIComponent(batchId)}/reveal`,
    { method: "POST" },
  );
}

export function cancelAccountBatch(batchId: string): Promise<AccountBatch> {
  return request<AccountBatch>(`/account-batches/${encodeURIComponent(batchId)}/cancel`, {
    method: "POST",
  });
}

/* -------------------------------------------------------------------------- */
/* 用户目录迁移                                                               */
/* -------------------------------------------------------------------------- */

export function preflightMigration(
  serverId: number,
  login: string,
  targetRoot?: string | null,
  signal?: AbortSignal,
): Promise<MigrationPreflight> {
  return request<MigrationPreflight>(
    `/servers/${encodeURIComponent(String(serverId))}/home-migrations/preflight`,
    { method: "POST", body: { login, targetRoot: targetRoot ?? null }, signal },
  );
}

export function createMigration(
  serverId: number,
  payload: {
    login: string;
    targetRoot?: string | null;
    safetyMarginPercent: number;
    fullChecksum: boolean;
    retentionDays: number;
  },
): Promise<MigrationJob> {
  return request<MigrationJob>(
    `/servers/${encodeURIComponent(String(serverId))}/home-migrations`,
    { method: "POST", body: { ...payload, confirm: true } },
  );
}

export function getMigration(id: string, signal?: AbortSignal): Promise<MigrationJob> {
  return request<MigrationJob>(`/home-migrations/${encodeURIComponent(id)}`, { signal });
}

export function listMigrations(serverId: number, signal?: AbortSignal): Promise<MigrationList> {
  return request<MigrationList>(
    `/servers/${encodeURIComponent(String(serverId))}/home-migrations`,
    { signal },
  );
}

export function cancelMigration(id: string): Promise<MigrationJob> {
  return request<MigrationJob>(`/home-migrations/${encodeURIComponent(id)}/cancel`, {
    method: "POST",
  });
}

export function resumeMigration(id: string): Promise<MigrationJob> {
  return request<MigrationJob>(`/home-migrations/${encodeURIComponent(id)}/resume`, {
    method: "POST",
  });
}

export function rollbackMigration(id: string, discardNewData: boolean): Promise<MigrationJob> {
  return request<MigrationJob>(`/home-migrations/${encodeURIComponent(id)}/rollback`, {
    method: "POST",
    body: { discardNewData },
  });
}

export function cleanupMigration(id: string, confirmPath: string): Promise<MigrationJob> {
  return request<MigrationJob>(`/home-migrations/${encodeURIComponent(id)}/cleanup`, {
    method: "POST",
    body: { confirmPath },
  });
}

/* -------------------------------------------------------------------------- */
/* 新空间分配                                                                 */
/* -------------------------------------------------------------------------- */

export function getStorageSettings(
  serverId: number,
  signal?: AbortSignal,
): Promise<StorageSettings> {
  return request<StorageSettings>(
    `/servers/${encodeURIComponent(String(serverId))}/storage-settings`,
    { signal },
  );
}

export function updateStorageSettings(
  serverId: number,
  payload: { dataRoot?: string | null; migrationRoot?: string | null },
): Promise<StorageSettings> {
  return request<StorageSettings>(
    `/servers/${encodeURIComponent(String(serverId))}/storage-settings`,
    { method: "PUT", body: payload },
  );
}

export function createDataset(
  serverId: number,
  payload: { login: string; linkName: string; dataRoot?: string | null; reuseExisting: boolean },
): Promise<Dataset> {
  return request<Dataset>(`/servers/${encodeURIComponent(String(serverId))}/datasets`, {
    method: "POST",
    body: { ...payload, confirm: true },
  });
}

export function listDatasets(serverId: number, signal?: AbortSignal): Promise<DatasetList> {
  return request<DatasetList>(`/servers/${encodeURIComponent(String(serverId))}/datasets`, {
    signal,
  });
}

export function unlinkDataset(id: string): Promise<Dataset> {
  return request<Dataset>(`/datasets/${encodeURIComponent(id)}/unlink`, { method: "POST" });
}
