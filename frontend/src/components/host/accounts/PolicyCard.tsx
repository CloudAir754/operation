import { useEffect, useMemo, useState } from "react";
import type { PasswordPolicy, PolicyChange, PolicyDiff } from "../../../api/accounts";
import {
  applyPolicy,
  listPolicyChanges,
  previewPolicy,
  restorePolicy,
} from "../../../api/accounts";
import { describeError } from "../../../api/errors";
import { ConfirmDialog } from "../../ConfirmDialog";
import {
  EmptyState,
  ErrorBanner,
  InfoBanner,
  SectionCard,
  StatusBadge,
  SuccessBanner,
} from "../../ui";
import { formatDateTime } from "../../../lib/format";
import { FieldNote, SudoBadge } from "./shared";

const ORIGIN_LABELS: Record<PolicyChangeOrigin, string> = {
  pam: "PAM 行内参数",
  conf: "pwquality.conf",
  default: "默认值",
};

type PolicyChangeOrigin = "pam" | "conf" | "default";

const STATUS_TONES: Record<PolicyChange["status"], "success" | "danger" | "info"> = {
  applied: "success",
  failed: "danger",
  restored: "info",
};

export interface PolicyCardProps {
  serverId: number;
  policy: PasswordPolicy;
  onChanged: () => void;
}

/** 系统密码策略：展示实际生效值，改动前必须看 diff，写入会备份并在失败时自动恢复。 */
export function PolicyCard({ serverId, policy, onChanged }: PolicyCardProps) {
  const [draft, setDraft] = useState<Record<string, number>>({});
  const [diff, setDiff] = useState<PolicyDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [previewPending, setPreviewPending] = useState(false);
  const [applyPending, setApplyPending] = useState(false);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [changes, setChanges] = useState<PolicyChange[]>([]);
  const [restoreTarget, setRestoreTarget] = useState<PolicyChange | null>(null);
  const [restorePending, setRestorePending] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  useEffect(() => {
    const next: Record<string, number> = {};
    for (const field of policy.fields) next[field.key] = field.value;
    setDraft(next);
    setDiff(null);
  }, [policy]);

  useEffect(() => {
    const controller = new AbortController();
    listPolicyChanges(serverId, controller.signal)
      .then((result) => setChanges(result.changes))
      .catch(() => undefined);
    return () => controller.abort();
  }, [serverId, message, diff?.applicable]);

  const targets = useMemo(() => {
    const result: Record<string, number> = {};
    for (const field of policy.fields) {
      const value = draft[field.key];
      if (value !== undefined && value !== field.value) result[field.key] = value;
    }
    return result;
  }, [draft, policy.fields]);

  const handlePreview = async () => {
    if (Object.keys(targets).length === 0) {
      setError("没有需要修改的字段。");
      setDiff(null);
      return;
    }
    setPreviewPending(true);
    setError(null);
    setMessage(null);
    try {
      setDiff(await previewPolicy(serverId, targets));
    } catch (reason) {
      setDiff(null);
      setError(describeError(reason));
    } finally {
      setPreviewPending(false);
    }
  };

  const handleApply = async () => {
    setApplyPending(true);
    setApplyError(null);
    try {
      const change = await applyPolicy(serverId, targets);
      setApplyOpen(false);
      setDiff(null);
      setMessage(
        change.validation && change.validation["behavior"] === "ok"
          ? "策略已写入并通过行为验证。"
          : "策略已写入并完成配置校验。",
      );
      onChanged();
    } catch (reason) {
      setApplyError(describeError(reason));
    } finally {
      setApplyPending(false);
    }
  };

  const handleRestore = async () => {
    if (!restoreTarget) return;
    setRestorePending(true);
    setRestoreError(null);
    try {
      await restorePolicy(serverId, restoreTarget.id);
      setRestoreTarget(null);
      setMessage("已恢复该次修改前的配置。");
      onChanged();
    } catch (reason) {
      setRestoreError(describeError(reason));
    } finally {
      setRestorePending(false);
    }
  };

  return (
    <SectionCard
      title="系统密码策略（PAM / pwquality）"
      testId="policy-card"
      description="展示该主机实际生效的配置：PAM 行内参数优先于 pwquality.conf。"
      actions={
        <StatusBadge tone={policy.capabilities.readOnly ? "warning" : "success"}>
          {policy.capabilities.readOnly ? "只读" : "可修改"}
        </StatusBadge>
      }
    >
      <div className="stack">
        {error ? <ErrorBanner title="策略检查失败" message={error} /> : null}
        {message ? <SuccessBanner message={message} /> : null}

        {policy.capabilities.reasons.length > 0 ? (
          <InfoBanner tone="warning">
            <ul className="info-list">
              {policy.capabilities.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </InfoBanner>
        ) : null}

        <div className="table-wrap">
          <table className="data-table data-table--policy">
            <caption className="sr-only">实际生效的密码策略字段</caption>
            <thead>
              <tr>
                <th scope="col">字段</th>
                <th scope="col">生效值</th>
                <th scope="col">来源</th>
                <th scope="col">修改为</th>
              </tr>
            </thead>
            <tbody>
              {policy.fields.map((field) => (
                <tr key={field.key} data-testid={`policy-row-${field.key}`}>
                  <td>
                    <span>{field.label}</span>
                    <span className="mono muted"> {field.key}</span>
                  </td>
                  <td className="mono">{field.value}</td>
                  <td>
                    <StatusBadge tone={field.origin === "pam" ? "info" : "muted"}>
                      {ORIGIN_LABELS[field.origin]}
                    </StatusBadge>
                  </td>
                  <td>
                    <input
                      aria-label={`${field.label} 修改为`}
                      className="input--narrow mono"
                      type="number"
                      min={field.min}
                      max={field.max}
                      value={draft[field.key] ?? field.value}
                      disabled={!field.writable || applyPending}
                      onChange={(event) => {
                        const next = Number(event.target.value);
                        setDraft((current) => ({
                          ...current,
                          [field.key]: Number.isFinite(next) ? next : field.value,
                        }));
                        setDiff(null);
                        setMessage(null);
                      }}
                    />
                    {field.reason ? <FieldNote>{field.reason}</FieldNote> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="form-actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void handlePreview()}
            disabled={previewPending || Object.keys(targets).length === 0}
          >
            {previewPending ? "检查中…" : "检查差异"}
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={() => {
              setApplyError(null);
              setApplyOpen(true);
            }}
            disabled={!diff?.applicable || applyPending}
          >
            应用修改 <SudoBadge />
          </button>
        </div>

        {diff ? (
          <div className="stack" data-testid="policy-diff">
            {diff.blockers.length > 0 ? (
              <InfoBanner tone="warning">
                <ul className="info-list">
                  {diff.blockers.map((blocker) => (
                    <li key={blocker}>{blocker}</li>
                  ))}
                </ul>
              </InfoBanner>
            ) : null}
            {diff.warnings.length > 0 ? (
              <InfoBanner tone="warning">
                <ul className="info-list">
                  {diff.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </InfoBanner>
            ) : null}
            <pre className="diff-block mono">{diff.diff || "（没有文件差异）"}</pre>
            <FieldNote>
              写入前会把原文件备份到 /var/backups/labopspanel/，写入后重新读取并比对；
              验证失败会自动恢复备份。
            </FieldNote>
          </div>
        ) : null}

        <div className="stack-sm">
          <h3 className="subsection-title">策略修改历史</h3>
          {changes.length === 0 ? (
            <EmptyState title="还没有修改记录" />
          ) : (
            <div className="table-wrap">
              <table className="data-table data-table--compact">
                <caption className="sr-only">策略修改历史</caption>
                <thead>
                  <tr>
                    <th scope="col">时间</th>
                    <th scope="col">状态</th>
                    <th scope="col">字段</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {changes.map((change) => (
                    <tr key={change.id}>
                      <td className="muted">{formatDateTime(change.createdAt)}</td>
                      <td>
                        <StatusBadge tone={STATUS_TONES[change.status]}>
                          {change.status === "applied"
                            ? "已应用"
                            : change.status === "restored"
                              ? "已恢复"
                              : "失败"}
                        </StatusBadge>
                      </td>
                      <td className="mono">
                        {Object.entries(change.targets)
                          .map(([key, value]) => `${key}=${value}`)
                          .join(" ")}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn btn--tiny btn--ghost"
                          onClick={() => {
                            setRestoreError(null);
                            setRestoreTarget(change);
                          }}
                          disabled={change.status === "restored"}
                        >
                          恢复此修改前配置 <SudoBadge />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {policy.unknownKeys.length > 0 ? (
          <details className="details">
            <summary>无法识别的配置行（写入时原样保留）</summary>
            <pre className="diff-block mono">{policy.unknownKeys.join("\n")}</pre>
          </details>
        ) : null}

        {policy.managedLines.length > 0 ? (
          <details className="details">
            <summary>受管的 PAM 行</summary>
            <pre className="diff-block mono">{policy.managedLines.join("\n")}</pre>
          </details>
        ) : null}

        {policy.notes.length > 0 ? (
          <ul className="info-list">
            {policy.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        ) : null}
      </div>

      <ConfirmDialog
        open={applyOpen}
        title="确认修改系统密码策略"
        description="该修改会影响这台主机上的所有本地用户，并立即生效。"
        details={Object.entries(diff?.fields ?? {}).map(([key, value]) => ({
          label: key,
          value: (
            <span className="mono">
              {value.before ?? "—"} → {value.after ?? "—"}
            </span>
          ),
        }))}
        warning={
          <p>
            后端会先备份原文件、写入后校验；校验不通过会立即恢复备份。请确认已了解对现有用户的影响。
          </p>
        }
        confirmLabel="确认写入"
        danger
        pending={applyPending}
        error={applyError}
        onConfirm={() => void handleApply()}
        onCancel={() => {
          if (applyPending) return;
          setApplyOpen(false);
          setApplyError(null);
        }}
      />

      <ConfirmDialog
        open={restoreTarget !== null}
        title="恢复本次修改前的配置"
        description="把 /etc/security/pwquality.conf 与相关 PAM 文件恢复到该次修改之前的内容。"
        details={
          restoreTarget
            ? [
                { label: "记录", value: <span className="mono">{restoreTarget.id}</span> },
                { label: "修改时间", value: formatDateTime(restoreTarget.createdAt) },
                { label: "备份 ID", value: <span className="mono">{restoreTarget.backupId}</span> },
              ]
            : []
        }
        warning={<p>恢复会覆盖当前策略文件，请确认这段时间内没有其他人手工改过配置。</p>}
        confirmLabel="确认恢复"
        danger
        pending={restorePending}
        error={restoreError}
        onConfirm={() => void handleRestore()}
        onCancel={() => {
          if (restorePending) return;
          setRestoreTarget(null);
          setRestoreError(null);
        }}
      />
    </SectionCard>
  );
}
