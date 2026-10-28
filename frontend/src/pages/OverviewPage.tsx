import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { describeError } from "../api/errors";
import { getServers, getSshConfig, importServers, testConnection } from "../api/client";
import type {
  ConnectionTestResult,
  Host,
  ImportResult,
  ServerList,
  SshConfig,
} from "../api/types";
import {
  EmptyState,
  ErrorBanner,
  InfoBanner,
  LoadingBlock,
  PageHeader,
  StatusBadge,
  SuccessBanner,
} from "../components/ui";
import { usePageTitle } from "../hooks/usePageTitle";
import { usePolling } from "../hooks/usePolling";
import {
  CONNECT_STATUS_LABELS,
  CONNECT_STATUS_TONES,
  SERVER_SOURCE_LABELS,
} from "../lib/labels";
import { formatDateTime, formatDurationMs, formatRelativeTime } from "../lib/format";

type ConnectionTestState =
  | { status: "pending" }
  | { status: "done"; result: ConnectionTestResult }
  | { status: "error"; message: string };

interface ImportState {
  pending: boolean;
  result: ImportResult | null;
  error: string | null;
}

const INITIAL_IMPORT_STATE: ImportState = { pending: false, result: null, error: null };

export function OverviewPage() {
  usePageTitle("主机总览");

  const loadServers = useCallback((signal: AbortSignal) => getServers(signal), []);
  const { data: serverData, error: serversError, loading: serversLoading, refreshing, refresh: refreshServers } =
    usePolling<ServerList>(loadServers);

  const loadSshConfig = useCallback((signal: AbortSignal) => getSshConfig(signal), []);
  const {
    data: sshConfig,
    error: sshError,
    loading: sshLoading,
    refreshing: sshRefreshing,
    refresh: refreshSshConfig,
  } = usePolling<SshConfig>(loadSshConfig);

  const [importOpen, setImportOpen] = useState(false);
  const [selectedAliases, setSelectedAliases] = useState<string[]>([]);
  const [testStates, setTestStates] = useState<Record<number, ConnectionTestState>>({});
  const [importState, setImportState] = useState<ImportState>(INITIAL_IMPORT_STATE);

  const servers = serverData?.servers ?? [];

  useEffect(() => {
    if (!sshConfig) return;
    setSelectedAliases((current) => {
      const available = new Set(sshConfig.hosts.map((host) => host.alias));
      const kept = current.filter((alias) => available.has(alias));
      if (kept.length > 0) return kept;
      return sshConfig.hosts.filter((host) => !host.imported).map((host) => host.alias);
    });
  }, [sshConfig]);

  const total = servers.length;
  const online = servers.filter(
    (host) => host.enabled && host.lastConnect?.status === "ok",
  ).length;
  const abnormal = servers.filter(
    (host) => host.enabled && host.lastConnect?.status === "failed",
  ).length;
  const lastActivityAt = servers.reduce<string | null>((latest, host) => {
    const at = host.lastConnect?.at;
    if (!at) return latest;
    if (!latest) return at;
    return Date.parse(at) > Date.parse(latest) ? at : latest;
  }, null);

  const handleTestConnection = useCallback(async (host: Host) => {
    setTestStates((previous) => ({ ...previous, [host.id]: { status: "pending" } }));
    try {
      const result = await testConnection(host.id);
      setTestStates((previous) => ({
        ...previous,
        [host.id]: { status: "done", result },
      }));
      refreshServers();
    } catch (error) {
      setTestStates((previous) => ({
        ...previous,
        [host.id]: { status: "error", message: describeError(error) },
      }));
    }
  }, [refreshServers]);

  const runImport = useCallback(
    async (aliases: string[] | null) => {
      setImportState({ pending: true, result: null, error: null });
      try {
        const result = await importServers({ aliases, pruneMissing: false });
        setImportState({ pending: false, result, error: null });
        refreshServers();
        refreshSshConfig();
      } catch (error) {
        setImportState({ pending: false, result: null, error: describeError(error) });
      }
    },
    [refreshServers, refreshSshConfig],
  );

  const sshHosts = sshConfig?.hosts ?? [];
  const allSelected = sshHosts.length > 0 && selectedAliases.length === sshHosts.length;

  const toggleAlias = (alias: string, checked: boolean) => {
    setSelectedAliases((current) =>
      checked ? Array.from(new Set([...current, alias])) : current.filter((item) => item !== alias),
    );
  };

  return (
    <div className="stack">
      <PageHeader
        eyebrow="主机清单"
        title="主机总览"
        description="查看已导入的 Linux 主机、连接状态与最近活动，并从本机 OpenSSH 配置导入新的主机。"
        actions={
          <>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={refreshServers}
              disabled={refreshing || serversLoading}
            >
              {refreshing ? "刷新中…" : "刷新"}
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => setImportOpen((open) => !open)}
              aria-expanded={importOpen}
              aria-controls="ssh-import-panel"
            >
              导入 SSH 主机
            </button>
          </>
        }
      />

      {serversError ? (
        <ErrorBanner
          title="无法加载主机列表"
          message={serversError.message}
          onRetry={refreshServers}
        />
      ) : null}

      <section className="summary-strip" aria-label="主机概况">
        <div className="summary">
          <span className="summary__label">设备总数</span>
          <strong className="summary__value">{total}</strong>
          <span className="summary__hint">已导入主机</span>
        </div>
        <div className="summary">
          <span className="summary__label">在线</span>
          <strong className="summary__value summary__value--ok">{online}</strong>
          <span className="summary__hint">最近一次连接成功</span>
        </div>
        <div className="summary">
          <span className="summary__label">异常</span>
          <strong className="summary__value summary__value--danger">{abnormal}</strong>
          <span className="summary__hint">最近一次连接失败</span>
        </div>
        <div className="summary">
          <span className="summary__label">最近采集</span>
          <strong className="summary__value summary__value--time">
            {lastActivityAt ? formatRelativeTime(lastActivityAt) : "—"}
          </strong>
          <span className="summary__hint" title={formatDateTime(lastActivityAt)}>
            {lastActivityAt ? "按最近连接/采集时间" : "暂无连接记录"}
          </span>
        </div>
      </section>

      {importOpen ? (
        <section
          className="card import-panel"
          id="ssh-import-panel"
          aria-labelledby="ssh-import-title"
        >
          <header className="card__header">
            <div>
              <h2 className="card__title" id="ssh-import-title">
                从 SSH 配置导入
              </h2>
              <p className="card__description">
                {sshConfig ? (
                  <>
                    配置路径：<code className="mono">{sshConfig.path}</code>
                  </>
                ) : (
                  "读取 Windows OpenSSH 配置"
                )}
              </p>
            </div>
            <div className="card__actions">
              <button
                type="button"
                className="btn btn--ghost btn--small"
                onClick={refreshSshConfig}
                disabled={sshRefreshing}
              >
                {sshRefreshing ? "重新解析中…" : "重新解析"}
              </button>
            </div>
          </header>

          <div className="card__body stack">
            {sshError ? (
              <ErrorBanner
                title="无法读取 SSH 配置"
                message={sshError.message}
                onRetry={refreshSshConfig}
              />
            ) : null}

            {sshLoading && !sshConfig ? <LoadingBlock label="正在解析 SSH 配置…" /> : null}

            {sshConfig && !sshConfig.exists ? (
              <InfoBanner tone="warning">
                未找到 SSH 配置文件（<code className="mono">{sshConfig.path}</code>）。
                请先在 Windows 上创建 OpenSSH 配置，然后重新解析。
              </InfoBanner>
            ) : null}

            {sshConfig?.exists && sshHosts.length === 0 ? (
              <EmptyState
                title="配置中没有可导入的显式主机"
                description="仅支持包含明确 Host 名称的条目；含 *、?、! 的条目会被忽略。"
              />
            ) : null}

            {sshHosts.length > 0 ? (
              <>
                <div className="import-toolbar">
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={(event) => {
                        setSelectedAliases(
                          event.target.checked ? sshHosts.map((host) => host.alias) : [],
                        );
                      }}
                    />
                    全选
                  </label>
                  <span className="muted">
                    已选 {selectedAliases.length} / {sshHosts.length} 台
                  </span>
                  <div className="import-toolbar__actions">
                    <button
                      type="button"
                      className="btn btn--ghost btn--small"
                      onClick={() => void runImport(null)}
                      disabled={importState.pending}
                    >
                      导入全部
                    </button>
                    <button
                      type="button"
                      className="btn btn--primary btn--small"
                      onClick={() => void runImport(selectedAliases)}
                      disabled={importState.pending || selectedAliases.length === 0}
                    >
                      {importState.pending ? "导入中…" : `导入选中 (${selectedAliases.length})`}
                    </button>
                  </div>
                </div>

                <div className="table-wrap">
                  <table className="data-table">
                    <caption className="sr-only">SSH 配置中的主机</caption>
                    <thead>
                      <tr>
                        <th scope="col">选择</th>
                        <th scope="col">别名</th>
                        <th scope="col">地址</th>
                        <th scope="col">状态</th>
                        <th scope="col">配置行</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sshHosts.map((host) => {
                        const checked = selectedAliases.includes(host.alias);
                        return (
                          <tr key={host.alias}>
                            <td>
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={(event) => toggleAlias(host.alias, event.target.checked)}
                                aria-label={`选择 ${host.alias}`}
                              />
                            </td>
                            <td className="mono">{host.alias}</td>
                            <td>
                              <span className="mono">{host.hostname ?? "未配置 HostName"}</span>
                              <span className="muted">
                                {" "}
                                · {host.user ?? "未配置 User"}@{host.port ?? 22}
                              </span>
                            </td>
                            <td>
                              {host.imported ? (
                                <StatusBadge tone="info">已导入</StatusBadge>
                              ) : (
                                <StatusBadge tone="neutral">新主机</StatusBadge>
                              )}
                            </td>
                            <td className="mono muted">第 {host.lineNumber} 行</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            ) : null}

            {sshConfig && sshConfig.includes.length > 0 ? (
              <details className="includes">
                <summary>包含的配置文件（{sshConfig.includes.length}）</summary>
                <ul className="includes__list">
                  {sshConfig.includes.map((include) => (
                    <li key={include.pattern}>
                      <code className="mono">{include.pattern}</code>
                      <span className="muted"> → 解析到 {include.resolved.length} 个文件</span>
                      {include.error ? (
                        <span className="text-danger"> 解析失败：{include.error}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}

            {sshConfig && sshConfig.errors.length > 0 ? (
              <div className="banner banner--warning" role="alert">
                <span className="banner__mark" aria-hidden="true">
                  !
                </span>
                <div className="banner__body">
                  <strong className="banner__title">SSH 配置解析问题</strong>
                  <ul className="error-list">
                    {sshConfig.errors.map((error, index) => (
                      <li key={`${error.path}-${error.line ?? "x"}-${index}`}>
                        <code className="mono">{error.path}</code>
                        {error.line !== null ? ` 第 ${error.line} 行` : ""}：{error.message}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : null}

            {importState.error ? (
              <ErrorBanner title="导入失败" message={importState.error} />
            ) : null}

            {importState.result ? (
              <SuccessBanner
                message={`导入完成：新增 ${importState.result.imported.length} 台，更新 ${importState.result.updated.length} 台，跳过 ${importState.result.skipped.length} 台。`}
              >
                {importState.result.imported.length > 0 ? (
                  <p className="banner__detail">
                    新增：{importState.result.imported.join("、")}
                  </p>
                ) : null}
                {importState.result.updated.length > 0 ? (
                  <p className="banner__detail">
                    更新：{importState.result.updated.join("、")}
                  </p>
                ) : null}
                {importState.result.skipped.length > 0 ? (
                  <ul className="error-list">
                    {importState.result.skipped.map((item) => (
                      <li key={item.alias}>
                        跳过 <span className="mono">{item.alias}</span>：{item.reason}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {importState.result.errors.length > 0 ? (
                  <ul className="error-list">
                    {importState.result.errors.map((error, index) => (
                      <li key={`${error.path}-${index}`}>
                        <code className="mono">{error.path}</code>
                        {error.line !== null ? ` 第 ${error.line} 行` : ""}：{error.message}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </SuccessBanner>
            ) : null}
          </div>
        </section>
      ) : null}

      {serversLoading && !serverData ? (
        <LoadingBlock label="正在加载主机列表…" />
      ) : servers.length === 0 && !serversError ? (
        <EmptyState
          title="还没有导入任何主机"
          description="展开 SSH 配置面板，导入本机 OpenSSH 配置中的显式 Host。"
          action={
            <button type="button" className="btn btn--primary" onClick={() => setImportOpen(true)}>
              导入 SSH 主机
            </button>
          }
        />
      ) : (
        <div className="host-grid">
          {servers.map((host) => (
            <HostCard
              key={host.id}
              host={host}
              testState={testStates[host.id]}
              onTest={() => void handleTestConnection(host)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface HostCardProps {
  host: Host;
  testState: ConnectionTestState | undefined;
  onTest: () => void;
}

function HostCard({ host, testState, onTest }: HostCardProps) {
  const connectStatus = host.lastConnect?.status ?? "unknown";
  const testing = testState?.status === "pending";
  const titleId = `host-${host.id}-title`;

  return (
    <article className="card host-card" aria-labelledby={titleId}>
      <header className="host-card__head">
        <div className="host-card__identity">
          <h2 className="host-card__alias" id={titleId}>
            {host.alias}
          </h2>
          <p className="host-card__address mono">
            {host.user}@{host.hostname}:{host.port}
          </p>
        </div>
        <div className="host-card__badges">
          <StatusBadge tone={host.enabled ? "success" : "muted"}>
            {host.enabled ? "已启用" : "已停用"}
          </StatusBadge>
          <StatusBadge tone={CONNECT_STATUS_TONES[connectStatus]}>
            {CONNECT_STATUS_LABELS[connectStatus]}
          </StatusBadge>
        </div>
      </header>

      <dl className="host-card__meta">
        <div>
          <dt>最近连接</dt>
          <dd>
            {host.lastConnect?.at ? (
              <>
                <span title={formatDateTime(host.lastConnect.at)}>
                  {formatRelativeTime(host.lastConnect.at)}
                </span>
                {host.lastConnect.error ? (
                  <span className="text-danger"> · {host.lastConnect.error}</span>
                ) : null}
              </>
            ) : (
              "尚未测试"
            )}
          </dd>
        </div>
        <div>
          <dt>sudo 密码</dt>
          <dd>
            <StatusBadge tone={host.hasSudoPassword ? "success" : "neutral"}>
              {host.hasSudoPassword ? "已设置" : "未设置"}
            </StatusBadge>
          </dd>
        </div>
        <div>
          <dt>来源</dt>
          <dd>{SERVER_SOURCE_LABELS[host.source] ?? host.source}</dd>
        </div>
      </dl>

      <p className={`host-card__notes${host.notes ? "" : " host-card__notes--empty"}`}>
        {host.notes || "暂无备注"}
      </p>

      <div className="host-card__footer">
        <button
          type="button"
          className="btn btn--primary btn--small"
          onClick={onTest}
          disabled={testing}
        >
          {testing ? "测试中…" : "测试连接"}
        </button>
        <Link className="btn btn--ghost btn--small" to={`/hosts/${host.id}`}>
          查看详情
        </Link>
        <Link className="btn btn--ghost btn--small" to={`/hosts/${host.id}/settings`}>
          设置
        </Link>
      </div>

      <div className="host-card__test" aria-live="polite">
        {testState?.status === "pending" ? (
          <span className="muted">正在测试 SSH 连接…</span>
        ) : null}
        {testState?.status === "done" ? (
          testState.result.ok ? (
            <span className="text-success">
              连接正常 · {formatDurationMs(testState.result.durationMs)}
            </span>
          ) : (
            <span className="text-danger">
              连接失败：{testState.result.error ?? "未知原因"}
            </span>
          )
        ) : null}
        {testState?.status === "error" ? (
          <span className="text-danger">连接测试失败：{testState.message}</span>
        ) : null}
      </div>
    </article>
  );
}
