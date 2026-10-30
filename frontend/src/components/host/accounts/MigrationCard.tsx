import { useCallback, useEffect, useState } from "react";
import type { MigrationJob, MigrationPreflight } from "../../../api/accounts";
import {
  cancelMigration,
  cleanupMigration,
  createMigration,
  getMigration,
  listMigrations,
  preflightMigration,
  resumeMigration,
  rollbackMigration,
} from "../../../api/accounts";
import { describeError } from "../../../api/errors";
import { ConfirmDialog } from "../../ConfirmDialog";
import {
  DetailList,
  EmptyState,
  ErrorBanner,
  InfoBanner,
  SectionCard,
  StatusBadge,
  SuccessBanner,
} from "../../ui";
import { formatBytes, formatDateTime } from "../../../lib/format";
import { CheckList, FieldNote, NumberField, SudoBadge } from "./shared";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted", "rolled_back"]);

const STAGE_LABELS: Record<string, string> = {
  preflight: "预检",
  space_check: "空间计算",
  freeze: "冻结账号",
  rename: "原地改名",
  copy: "复制到新盘",
  verify: "前后校验",
  switch: "切换链接",
  restore: "恢复与试验",
  observe: "保留观察",
  rolled_back: "已回退",
  interrupted: "已中断",
};

const STATUS_TONES: Record<string, "success" | "danger" | "warning" | "info" | "muted"> = {
  queued: "info",
  running: "info",
  succeeded: "success",
  failed: "danger",
  cancelled: "warning",
  interrupted: "warning",
  rolled_back: "muted",
};

export interface MigrationCardProps {
  serverId: number;
  defaultTargetRoot: string;
  hasSudoPassword: boolean;
}

interface RestoreSummary {
  functional?: string;
  sshDirMode?: string | null;
  sshKeyMode?: string | null;
  sshOwner?: number | null;
}

function restoreSummary(job: MigrationJob): RestoreSummary | null {
  const restore = job.result["restore"];
  return typeof restore === "object" && restore !== null ? (restore as RestoreSummary) : null;
}

export function MigrationCard({ serverId, defaultTargetRoot, hasSudoPassword }: MigrationCardProps) {
  const [login, setLogin] = useState("");
  const [targetRoot, setTargetRoot] = useState(defaultTargetRoot);
  const [margin, setMargin] = useState(10);
  const [fullChecksum, setFullChecksum] = useState(false);
  const [retentionDays, setRetentionDays] = useState(7);
  const [preflight, setPreflight] = useState<MigrationPreflight | null>(null);
  const [job, setJob] = useState<MigrationJob | null>(null);
  const [history, setHistory] = useState<MigrationJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [discardNewData, setDiscardNewData] = useState(false);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [confirmPath, setConfirmPath] = useState("");
  const [dialogError, setDialogError] = useState<string | null>(null);

  useEffect(() => {
    setTargetRoot(defaultTargetRoot);
  }, [defaultTargetRoot]);

  const loadHistory = useCallback(
    (signal?: AbortSignal) => {
      listMigrations(serverId, signal)
        .then((result) => setHistory(result.migrations))
        .catch(() => undefined);
    },
    [serverId],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadHistory(controller.signal);
    return () => controller.abort();
  }, [loadHistory]);

  useEffect(() => {
    if (!job || TERMINAL.has(job.status)) return;
    const timer = window.setInterval(() => {
      getMigration(job.id)
        .then((next) => {
          setJob(next);
          if (TERMINAL.has(next.status)) loadHistory();
        })
        .catch(() => undefined);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [job, loadHistory]);

  const runPreflight = async () => {
    setPending("preflight");
    setError(null);
    setMessage(null);
    setPreflight(null);
    try {
      const result = await preflightMigration(serverId, login.trim(), targetRoot.trim() || null);
      setPreflight(result);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setPending(null);
    }
  };

  const handleCreate = async () => {
    setPending("create");
    setDialogError(null);
    try {
      const created = await createMigration(serverId, {
        login: login.trim(),
        targetRoot: targetRoot.trim() || null,
        safetyMarginPercent: margin,
        fullChecksum,
        retentionDays,
      });
      setJob(created);
      setCreateOpen(false);
      setPreflight(null);
      setMessage("迁移作业已提交，将在后台按阶段执行。");
      loadHistory();
    } catch (reason) {
      setDialogError(describeError(reason));
    } finally {
      setPending(null);
    }
  };

  const handleResume = async () => {
    if (!job) return;
    setPending("resume");
    setError(null);
    try {
      setJob(await resumeMigration(job.id));
      setMessage("已从安全阶段继续执行。");
      loadHistory();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setPending(null);
    }
  };

  const handleRollback = async () => {
    if (!job) return;
    setPending("rollback");
    setDialogError(null);
    try {
      setJob(await rollbackMigration(job.id, discardNewData));
      setRollbackOpen(false);
      setMessage("已回退到原目录，账号状态已恢复。");
      loadHistory();
    } catch (reason) {
      setDialogError(describeError(reason));
    } finally {
      setPending(null);
    }
  };

  const handleCleanup = async () => {
    if (!job) return;
    setPending("cleanup");
    setDialogError(null);
    try {
      setJob(await cleanupMigration(job.id, confirmPath.trim()));
      setCleanupOpen(false);
      setMessage("备份目录已删除。");
      loadHistory();
    } catch (reason) {
      setDialogError(describeError(reason));
    } finally {
      setPending(null);
    }
  };

  const handleCancel = async () => {
    if (!job) return;
    try {
      setJob(await cancelMigration(job.id));
    } catch (reason) {
      setError(describeError(reason));
    }
  };

  return (
    <SectionCard
      title="迁移用户目录"
      testId="migration-card"
      description="把 /home/<user> 迁移到数据盘，并让原路径成为指向新位置的符号链接；首版只允许本地 ext4。"
      actions={job ? <StatusBadge tone={STATUS_TONES[job.status]}>{job.status}</StatusBadge> : null}
    >
      <div className="stack">
        {!hasSudoPassword ? (
          <InfoBanner tone="warning">
            迁移需要 sudo 提权（锁定账号、改名、rsync、建链接），请先保存运维 sudo 密码。
          </InfoBanner>
        ) : null}
        {error ? <ErrorBanner title="迁移操作失败" message={error} /> : null}
        {message ? <SuccessBanner message={message} /> : null}

        <div className="form-grid">
          <div className="field">
            <label htmlFor="migration-login">要迁移的账户</label>
            <input
              id="migration-login"
              className="mono"
              value={login}
              placeholder="zhangsan"
              onChange={(event) => {
                setLogin(event.target.value);
                setPreflight(null);
              }}
            />
          </div>
          <div className="field">
            <label htmlFor="migration-root">迁移根目录</label>
            <input
              id="migration-root"
              className="mono"
              value={targetRoot}
              onChange={(event) => {
                setTargetRoot(event.target.value);
                setPreflight(null);
              }}
            />
            <FieldNote>最终目标为 &lt;根目录&gt;/&lt;登录名&gt;，必须是本地 ext4。</FieldNote>
          </div>
          <NumberField
            id="migration-margin"
            label="安全余量（%）"
            value={margin}
            min={0}
            max={100}
            onChange={setMargin}
            hint="目标可用空间必须大于「实际占用 ×（1+余量）+ 256 MiB」"
          />
          <NumberField
            id="migration-retention"
            label="备份保留天数"
            value={retentionDays}
            min={1}
            max={90}
            onChange={setRetentionDays}
            hint="只是展示与提醒，不会自动删除备份"
          />
        </div>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={fullChecksum}
            onChange={(event) => setFullChecksum(event.target.checked)}
          />
          复制后做全量内容校验（较慢）
        </label>

        <div className="form-actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void runPreflight()}
            disabled={!login.trim() || pending !== null}
          >
            {pending === "preflight" ? "预检中…" : "只读预检"}
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={() => {
              setDialogError(null);
              setCreateOpen(true);
            }}
            disabled={!preflight?.allowed || pending !== null}
          >
            开始迁移 <SudoBadge />
          </button>
        </div>

        {preflight ? (
          <div className="stack-sm" data-testid="migration-preflight">
            <div className="snapshot-meta">
              <StatusBadge tone={preflight.allowed ? "success" : "danger"}>
                {preflight.allowed ? "预检通过" : "预检未通过"}
              </StatusBadge>
              <span className="muted">采集于 {formatDateTime(preflight.collectedAt)}</span>
            </div>
            <DetailList
              items={[
                { label: "源目录", value: <span className="mono">{preflight.paths.source}</span> },
                { label: "目标目录", value: <span className="mono">{preflight.paths.target}</span> },
                {
                  label: "临时目录",
                  value: <span className="mono">{preflight.paths.staging}</span>,
                },
                { label: "备份目录", value: <span className="mono">{preflight.paths.backup}</span> },
                {
                  label: "预计实际占用",
                  value: formatBytes(Number(preflight.space["allocatedBytes"] ?? 0)),
                },
                {
                  label: "需要空间（含余量）",
                  value: formatBytes(Number(preflight.space["requiredBytes"] ?? 0)),
                },
                {
                  label: "目标可用空间",
                  value: formatBytes(Number(preflight.space["freeBytes"] ?? 0)),
                },
                {
                  label: "文件 / 目录",
                  value: `${preflight.counts["files"] ?? "—"} / ${preflight.counts["dirs"] ?? "—"}`,
                },
              ]}
            />
            <CheckList items={preflight.checks} testId="migration-checks" />
          </div>
        ) : null}

        {job ? (
          <div className="stack-sm" data-testid="migration-job">
            <div className="snapshot-meta">
              <span className="mono">{job.id}</span>
              <StatusBadge tone={STATUS_TONES[job.status]}>{job.status}</StatusBadge>
              <span className="muted">
                阶段 {job.stageIndex + 1}/{job.stages.length} · {STAGE_LABELS[job.stage] ?? job.stage}
              </span>
              <span className="muted">进度 {job.progress}%</span>
            </div>

            <ol className="stage-list" data-testid="migration-stages">
              {job.stages.map((stage, index) => (
                <li
                  key={stage}
                  className={
                    index < job.stageIndex
                      ? "stage-list__item stage-list__item--done"
                      : index === job.stageIndex
                        ? "stage-list__item stage-list__item--active"
                        : "stage-list__item"
                  }
                >
                  {STAGE_LABELS[stage] ?? stage}
                </li>
              ))}
            </ol>

            <DetailList
              items={[
                { label: "源目录", value: <span className="mono">{job.paths.source}</span> },
                { label: "新位置", value: <span className="mono">{job.paths.target}</span> },
                { label: "备份目录", value: <span className="mono">{job.paths.backup}</span> },
                {
                  label: "备份占用（迁移前统计）",
                  value: formatBytes(Number(job.counts["allocatedBytes"] ?? 0)),
                },
                { label: "备份保留", value: `${job.backupRetentionDays} 天（不会自动删除）` },
                {
                  label: "账号锁状态（迁移前）",
                  value: job.accountStatusBefore ?? "未知",
                },
              ]}
            />

            {restoreSummary(job) ? (
              <DetailList
                items={[
                  {
                    label: "以该用户身份功能验证",
                    value: (
                      <StatusBadge
                        tone={restoreSummary(job)?.functional === "ok" ? "success" : "danger"}
                      >
                        {restoreSummary(job)?.functional === "ok" ? "通过" : "未通过"}
                      </StatusBadge>
                    ),
                  },
                  {
                    label: ".ssh 权限",
                    value: restoreSummary(job)?.sshDirMode ?? "无 .ssh 目录",
                  },
                  {
                    label: "authorized_keys 权限",
                    value: restoreSummary(job)?.sshKeyMode ?? "无公钥文件",
                  },
                ]}
              />
            ) : null}

            {job.error ? <ErrorBanner title="迁移未完成" message={job.error} /> : null}

            {job.checks.length > 0 ? <CheckList items={job.checks} /> : null}

            <div className="form-actions">
              {!TERMINAL.has(job.status) ? (
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => void handleCancel()}
                >
                  请求取消
                </button>
              ) : null}
              {job.status === "failed" || job.status === "interrupted" ? (
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => void handleResume()}
                  disabled={pending !== null}
                >
                  从中断处继续 <SudoBadge />
                </button>
              ) : null}
              {job.rollbackAvailable ? (
                <button
                  type="button"
                  className="btn btn--danger-ghost"
                  onClick={() => {
                    setDialogError(null);
                    setDiscardNewData(false);
                    setRollbackOpen(true);
                  }}
                  disabled={pending !== null}
                >
                  回退到原目录 <SudoBadge />
                </button>
              ) : null}
            </div>

            {job.cleanupAvailable ? (
              <div className="stack-sm">
                <InfoBanner tone="warning">
                  新位置功能正常后才清理旧目录。此操作不可撤销，且只在确认路径与备份路径一致时执行。
                </InfoBanner>
                <div className="field">
                  <label htmlFor="cleanup-path">输入备份路径以确认删除</label>
                  <input
                    id="cleanup-path"
                    className="mono"
                    value={confirmPath}
                    placeholder={job.paths.backup}
                    onChange={(event) => setConfirmPath(event.target.value)}
                  />
                </div>
                <button
                  type="button"
                  className="btn btn--danger"
                  onClick={() => {
                    setDialogError(null);
                    setCleanupOpen(true);
                  }}
                  disabled={confirmPath.trim() !== job.paths.backup || pending !== null}
                >
                  删除备份目录 <SudoBadge />
                </button>
              </div>
            ) : null}
          </div>
        ) : null}

        <details className="details">
          <summary>历史迁移</summary>
          {history.length === 0 ? (
            <EmptyState title="还没有迁移记录" />
          ) : (
            <div className="table-wrap">
              <table className="data-table data-table--compact">
                <caption className="sr-only">历史迁移</caption>
                <thead>
                  <tr>
                    <th scope="col">账户</th>
                    <th scope="col">状态</th>
                    <th scope="col">阶段</th>
                    <th scope="col">时间</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((item) => (
                    <tr key={item.id}>
                      <td className="mono">{item.login}</td>
                      <td>
                        <StatusBadge tone={STATUS_TONES[item.status]}>{item.status}</StatusBadge>
                      </td>
                      <td>{STAGE_LABELS[item.stage] ?? item.stage}</td>
                      <td className="muted">{formatDateTime(item.createdAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </details>
      </div>

      <ConfirmDialog
        open={createOpen}
        title="确认迁移用户目录"
        description="迁移会锁定账号、终止该用户的全部进程、改名原目录并复制数据，期间该用户无法登录。"
        details={[
          { label: "账户", value: <span className="mono">{login}</span> },
          { label: "源目录", value: <span className="mono">{preflight?.paths.source ?? "—"}</span> },
          { label: "目标目录", value: <span className="mono">{preflight?.paths.target ?? "—"}</span> },
          { label: "备份目录", value: <span className="mono">{preflight?.paths.backup ?? "—"}</span> },
          { label: "全量校验", value: fullChecksum ? "是" : "否" },
          { label: "权限要求", value: <SudoBadge /> },
        ]}
        warning={
          <p>
            迁移完成后原目录保留为备份，不会自动删除。若自动验证失败，本应用会自动回退并恢复账号。
          </p>
        }
        confirmLabel="确认开始迁移"
        danger
        pending={pending === "create"}
        error={dialogError}
        onConfirm={() => void handleCreate()}
        onCancel={() => {
          if (pending === "create") return;
          setCreateOpen(false);
          setDialogError(null);
        }}
      />

      <ConfirmDialog
        open={rollbackOpen}
        title="回退到原目录"
        description="重新冻结账号、移除符号链接，并把备份目录改名回原 home，然后恢复登录。"
        details={[
          { label: "账户", value: <span className="mono">{job?.login ?? "—"}</span> },
          { label: "备份目录", value: <span className="mono">{job?.paths.backup ?? "—"}</span> },
          { label: "当前链接", value: <span className="mono">{job?.paths.source ?? "—"}</span> },
        ]}
        warning={
          <div className="stack-sm">
            <p>回退会丢弃切换后写入新位置的数据，除非你确认这些数据可以丢弃。</p>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={discardNewData}
                onChange={(event) => setDiscardNewData(event.target.checked)}
              />
              我确认丢弃新位置的新数据
            </label>
          </div>
        }
        confirmLabel="确认回退"
        danger
        pending={pending === "rollback"}
        error={dialogError}
        onConfirm={() => void handleRollback()}
        onCancel={() => {
          if (pending === "rollback") return;
          setRollbackOpen(false);
          setDialogError(null);
        }}
      />

      <ConfirmDialog
        open={cleanupOpen}
        title="删除迁移备份"
        description="只有确认新位置功能正常后才应删除备份；该操作不可撤销。"
        details={[
          { label: "账户", value: <span className="mono">{job?.login ?? "—"}</span> },
          { label: "备份目录", value: <span className="mono">{confirmPath}</span> },
          { label: "新位置", value: <span className="mono">{job?.paths.target ?? "—"}</span> },
        ]}
        warning={<p>删除前会再次核对任务 ID、备份路径、目标路径与当前链接关系。</p>}
        confirmLabel="确认删除备份"
        danger
        pending={pending === "cleanup"}
        error={dialogError}
        onConfirm={() => void handleCleanup()}
        onCancel={() => {
          if (pending === "cleanup") return;
          setCleanupOpen(false);
          setDialogError(null);
        }}
      />
    </SectionCard>
  );
}
