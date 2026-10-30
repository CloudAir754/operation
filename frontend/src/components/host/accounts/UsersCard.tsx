import { useCallback, useEffect, useMemo, useState } from "react";
import type { AccountBatch, AccountBatchUser, GeneratorInfo } from "../../../api/accounts";
import {
  cancelAccountBatch,
  createAccountBatch,
  getAccountBatch,
  listAccountBatches,
  revealAccountPasswords,
} from "../../../api/accounts";
import { describeError } from "../../../api/errors";
import { ConfirmDialog } from "../../ConfirmDialog";
import { Modal } from "../../Modal";
import {
  EmptyState,
  ErrorBanner,
  InfoBanner,
  SectionCard,
  StatusBadge,
  SuccessBanner,
} from "../../ui";
import { formatDateTime } from "../../../lib/format";
import { SudoBadge } from "./shared";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

interface DraftRow {
  key: string;
  login: string;
  comment: string;
  useGenerated: boolean;
  password: string;
}

function nextRow(index: number): DraftRow {
  return {
    key: `row-${index}-${Math.random().toString(36).slice(2, 8)}`,
    login: "",
    comment: "",
    useGenerated: true,
    password: "",
  };
}

export interface UsersCardProps {
  serverId: number;
  hasSudoPassword: boolean;
  generator: GeneratorInfo | null;
  onNeedSudo: () => void;
}

/** 批量新建用户；生成的目标账户密码只在完成后一次性显示。 */
export function UsersCard({ serverId, hasSudoPassword, generator, onNeedSudo }: UsersCardProps) {
  const [rows, setRows] = useState<DraftRow[]>([nextRow(0)]);
  const [batch, setBatch] = useState<AccountBatch | null>(null);
  const [history, setHistory] = useState<AccountBatch[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [revealed, setRevealed] = useState<Record<string, string> | null>(null);
  const [revealError, setRevealError] = useState<string | null>(null);

  const loadHistory = useCallback(
    (signal?: AbortSignal) => {
      listAccountBatches(serverId, signal)
        .then((result) => setHistory(result.batches))
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
    if (!batch || TERMINAL.has(batch.status)) return;
    const timer = window.setInterval(() => {
      getAccountBatch(batch.id)
        .then((next) => {
          setBatch(next);
          if (TERMINAL.has(next.status)) loadHistory();
        })
        .catch(() => undefined);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [batch, loadHistory]);

  const users = useMemo<AccountBatchUser[]>(
    () =>
      rows
        .filter((row) => row.login.trim())
        .map((row) => ({
          login: row.login.trim(),
          comment: row.comment.trim(),
          shell: "/bin/bash",
          groups: [],
          password: row.useGenerated ? null : row.password,
        })),
    [rows],
  );

  const invalid = rows.some(
    (row) =>
      row.login.trim().length > 0 &&
      !/^[a-z_][a-z0-9_-]{0,31}$/.test(row.login.trim()),
  );
  const missingPassword = rows.some(
    (row) => row.login.trim() && !row.useGenerated && !row.password,
  );

  const handleCreate = async () => {
    setPending(true);
    setError(null);
    setMessage(null);
    try {
      const created = await createAccountBatch(serverId, users, generator?.rules ?? undefined);
      setBatch(created);
      setConfirmOpen(false);
      setRows([nextRow(rows.length)]);
      loadHistory();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setPending(false);
    }
  };

  const handleReveal = async () => {
    if (!batch) return;
    setRevealError(null);
    try {
      const result = await revealAccountPasswords(batch.id);
      setRevealed(result.passwords);
      loadHistory();
    } catch (reason) {
      setRevealError(describeError(reason));
    }
  };

  const handleCancel = async () => {
    if (!batch) return;
    try {
      setBatch(await cancelAccountBatch(batch.id));
    } catch (reason) {
      setError(describeError(reason));
    }
  };

  const updateRow = (key: string, patch: Partial<DraftRow>) => {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  };

  return (
    <SectionCard
      title="批量新建用户"
      testId="users-card"
      description="一次最多 50 个账户；逐个执行并逐个记录结果，失败的账户不会影响已成功的账户。"
      actions={batch ? <StatusBadge tone="info">最近批次 {batch.status}</StatusBadge> : null}
    >
      <div className="stack">
        {!hasSudoPassword ? (
          <InfoBanner tone="warning">
            该主机尚未保存运维 sudo 密码；新建用户需要 sudo，请先在上方「运维 sudo 密码」中填写。
            <button type="button" className="btn btn--tiny btn--ghost" onClick={onNeedSudo}>
              前往填写
            </button>
          </InfoBanner>
        ) : null}
        {error ? <ErrorBanner title="新建用户失败" message={error} /> : null}
        {message ? <SuccessBanner message={message} /> : null}

        <div className="table-wrap">
          <table className="data-table data-table--compact">
            <caption className="sr-only">待创建账户</caption>
            <thead>
              <tr>
                <th scope="col">登录名</th>
                <th scope="col">备注</th>
                <th scope="col">初始密码</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key}>
                  <td>
                    <input
                      aria-label="登录名"
                      className="mono"
                      value={row.login}
                      placeholder="zhangsan"
                      onChange={(event) => updateRow(row.key, { login: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      aria-label="备注"
                      value={row.comment}
                      placeholder="可选"
                      onChange={(event) => updateRow(row.key, { comment: event.target.value })}
                    />
                  </td>
                  <td>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={row.useGenerated}
                        onChange={(event) =>
                          updateRow(row.key, { useGenerated: event.target.checked })
                        }
                      />
                      随机生成
                    </label>
                    {row.useGenerated ? (
                      <span className="muted">按当前规则生成</span>
                    ) : (
                      <input
                        aria-label="手工密码"
                        type="password"
                        autoComplete="new-password"
                        value={row.password}
                        onChange={(event) =>
                          updateRow(row.key, { password: event.target.value })
                        }
                      />
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn btn--tiny btn--ghost"
                      onClick={() =>
                        setRows((current) =>
                          current.length > 1 ? current.filter((item) => item.key !== row.key) : current,
                        )
                      }
                      disabled={rows.length <= 1}
                    >
                      移除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {invalid ? (
          <p className="text-danger">
            用户名必须匹配 ^[a-z_][a-z0-9_-]&#123;0,31&#125;（小写字母或下划线开头）。
          </p>
        ) : null}
        {missingPassword ? <p className="text-danger">手工密码不能为空。</p> : null}

        <div className="form-actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => setRows((current) => [...current, nextRow(current.length)])}
          >
            添加一行
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => setConfirmOpen(true)}
            disabled={users.length === 0 || invalid || missingPassword || pending}
          >
            创建 {users.length} 个账户 <SudoBadge />
          </button>
        </div>

        {batch ? (
          <div className="stack-sm" data-testid="batch-progress">
            <div className="snapshot-meta">
              <span className="mono">{batch.id}</span>
              <StatusBadge
                tone={
                  batch.status === "succeeded"
                    ? "success"
                    : batch.status === "failed"
                      ? "danger"
                      : "info"
                }
              >
                {batch.status}
              </StatusBadge>
              <span className="muted">进度 {batch.progress}%</span>
              {batch.hasGeneratedPasswords ? (
                <StatusBadge tone="warning">含一次性密码</StatusBadge>
              ) : null}
            </div>
            {batch.error ? <InfoBanner tone="warning">{batch.error}</InfoBanner> : null}
            {batch.results.length > 0 ? (
              <div className="table-wrap">
                <table className="data-table data-table--compact">
                  <caption className="sr-only">创建结果</caption>
                  <thead>
                    <tr>
                      <th scope="col">登录名</th>
                      <th scope="col">状态</th>
                      <th scope="col">UID</th>
                      <th scope="col">home</th>
                      <th scope="col">说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {batch.results.map((result) => (
                      <tr key={result.login}>
                        <td className="mono">{result.login}</td>
                        <td>
                          <StatusBadge
                            tone={result.status === "succeeded" ? "success" : "danger"}
                          >
                            {result.status === "succeeded" ? "成功" : "失败"}
                          </StatusBadge>
                        </td>
                        <td className="mono">{result.uid ?? "—"}</td>
                        <td className="mono">{result.home ?? "—"}</td>
                        <td>{result.error ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            <div className="form-actions">
              {batch.hasGeneratedPasswords ? (
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => void handleReveal()}
                >
                  查看生成的密码（仅一次）
                </button>
              ) : null}
              {!TERMINAL.has(batch.status) ? (
                <button type="button" className="btn btn--ghost" onClick={() => void handleCancel()}>
                  取消剩余账户
                </button>
              ) : null}
            </div>
            {revealError ? <ErrorBanner title="无法读取密码" message={revealError} /> : null}
          </div>
        ) : null}

        <p className="muted">
          运行「查看生成的密码」后，密码会从内存清除；关闭后无法再次查看，只能重置该账户密码。
        </p>

        <details className="details">
          <summary>历史批次</summary>
          {history.length === 0 ? (
            <EmptyState title="还没有批次记录" />
          ) : (
            <ul className="info-list">
              {history.map((item) => (
                <li key={item.id}>
                  <span className="mono">{item.id.slice(0, 8)}</span> · {item.status} ·{" "}
                  {formatDateTime(item.createdAt)} · {item.results.length} 个账户
                </li>
              ))}
            </ul>
          )}
        </details>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title="确认批量新建用户"
        description="将在目标主机上创建账户并设置初始密码；该操作需要 sudo 提权。"
        details={[
          { label: "账户数量", value: String(users.length) },
          {
            label: "登录名",
            value: <span className="mono">{users.map((user) => user.login).join(", ")}</span>,
          },
          { label: "密码来源", value: "随机生成（按当前规则）或手工指定" },
          { label: "权限要求", value: <SudoBadge /> },
        ]}
        warning={
          <p>
            生成密码只会显示一次，请准备好安全的分发方式。系统密码策略会限制初始化密码强度。
          </p>
        }
        confirmLabel="确认创建"
        danger
        pending={pending}
        error={error}
        onConfirm={() => void handleCreate()}
        onCancel={() => {
          if (pending) return;
          setConfirmOpen(false);
          setError(null);
        }}
      />

      <Modal
        open={revealed !== null}
        title="生成的账户密码（仅显示一次）"
        description="关闭后无法再次查看；请立即安全分发，并提醒用户尽快修改。"
        onClose={() => setRevealed(null)}
        size="md"
        footer={
          <button type="button" className="btn btn--primary" onClick={() => setRevealed(null)}>
            我已记录，关闭
          </button>
        }
      >
        <div className="table-wrap">
          <table className="data-table" data-testid="revealed-passwords">
            <thead>
              <tr>
                <th scope="col">登录名</th>
                <th scope="col">初始密码</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(revealed ?? {}).map(([login, password]) => (
                <tr key={login}>
                  <td className="mono">{login}</td>
                  <td className="mono">{password}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Modal>
    </SectionCard>
  );
}
