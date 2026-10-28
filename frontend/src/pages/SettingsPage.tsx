import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  clearSecret,
  getHost,
  getSecret,
  getSettings,
  setSecret,
  testConnection,
  updateServer,
} from "../api/client";
import { describeError } from "../api/errors";
import type {
  ConnectionTestResult,
  Host,
  SecretStatus,
  Settings,
} from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  DetailList,
  ErrorBanner,
  InfoBanner,
  LoadingBlock,
  PageHeader,
  SectionCard,
  StatusBadge,
  SuccessBanner,
} from "../components/ui";
import { usePageTitle } from "../hooks/usePageTitle";
import { usePolling } from "../hooks/usePolling";
import { SERVER_SOURCE_LABELS } from "../lib/labels";
import { formatBytes, formatDateTime, formatDurationMs } from "../lib/format";

type SecretAction = "set" | "clear";

interface SectionFeedback {
  tone: "success" | "error";
  message: string;
}

export function SettingsPage() {
  const params = useParams<{ id: string }>();
  const serverId = Number(params.id);
  const validServerId = Number.isInteger(serverId) && serverId > 0;

  const loadHost = useCallback((signal: AbortSignal) => getHost(serverId, signal), [serverId]);
  const {
    data: loadedHost,
    error: hostError,
    loading: hostLoading,
    refreshing: hostRefreshing,
    refresh: refreshHost,
  } = usePolling<Host>(loadHost, { enabled: validServerId });

  const loadSecret = useCallback(
    (signal: AbortSignal) => getSecret(serverId, signal),
    [serverId],
  );
  const {
    data: secretStatus,
    error: secretLoadError,
    refresh: refreshSecret,
  } = usePolling<SecretStatus>(loadSecret, { enabled: validServerId });

  const loadSettings = useCallback((signal: AbortSignal) => getSettings(signal), []);
  const {
    data: settings,
    error: settingsError,
    loading: settingsLoading,
    refreshing: settingsRefreshing,
    refresh: refreshSettings,
  } = usePolling<Settings>(loadSettings);

  const [hostOverride, setHostOverride] = useState<Host | null>(null);
  const [secretOverride, setSecretOverride] = useState<boolean | null>(null);
  const [notesDraft, setNotesDraft] = useState("");
  const [password, setPassword] = useState("");
  const [togglePending, setTogglePending] = useState(false);
  const [notesPending, setNotesPending] = useState(false);
  const [secretPending, setSecretPending] = useState<SecretAction | null>(null);
  const [testPending, setTestPending] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
  const [confirmClearOpen, setConfirmClearOpen] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);
  const [basicFeedback, setBasicFeedback] = useState<SectionFeedback | null>(null);
  const [secretFeedback, setSecretFeedback] = useState<SectionFeedback | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const hostCandidate = hostOverride ?? loadedHost;
  const host = hostCandidate && hostCandidate.id === serverId ? hostCandidate : null;
  const secret = secretStatus && secretStatus.serverId === serverId ? secretStatus : null;
  usePageTitle(host ? `主机设置 ${host.alias}` : "主机设置");

  useEffect(() => {
    if (host) setNotesDraft(host.notes);
  }, [host?.id, host?.notes]);

  useEffect(() => {
    setHostOverride(null);
    setSecretOverride(null);
    setNotesDraft("");
    setPassword("");
    setBasicFeedback(null);
    setSecretFeedback(null);
    setTestResult(null);
    setTestError(null);
    setConfirmClearOpen(false);
    setClearError(null);
  }, [serverId]);

  const hasPassword =
    secretOverride ?? secret?.hasPassword ?? host?.hasSudoPassword ?? false;

  const handleToggleEnabled = async (next: boolean) => {
    if (!host) return;
    setTogglePending(true);
    setBasicFeedback(null);
    try {
      const updated = await updateServer(serverId, { enabled: next });
      setHostOverride(updated);
      setBasicFeedback({
        tone: "success",
        message: next ? "主机已启用。" : "主机已停用，总览仍会保留该记录。",
      });
    } catch (error) {
      setBasicFeedback({ tone: "error", message: describeError(error) });
    } finally {
      setTogglePending(false);
    }
  };

  const handleSaveNotes = async () => {
    if (!host) return;
    setNotesPending(true);
    setBasicFeedback(null);
    try {
      const updated = await updateServer(serverId, { notes: notesDraft });
      setHostOverride(updated);
      setBasicFeedback({ tone: "success", message: "备注已保存。" });
    } catch (error) {
      setBasicFeedback({ tone: "error", message: describeError(error) });
    } finally {
      setNotesPending(false);
    }
  };

  const handleSetPassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password || secretPending) return;
    setSecretPending("set");
    setSecretFeedback(null);
    try {
      const result = await setSecret(serverId, password);
      setSecretOverride(result.hasPassword);
      setPassword("");
      setSecretFeedback({ tone: "success", message: "sudo 密码已加密保存。" });
      refreshSecret();
    } catch (error) {
      setSecretFeedback({ tone: "error", message: describeError(error) });
    } finally {
      setSecretPending(null);
    }
  };

  const handleClearPassword = async () => {
    setSecretPending("clear");
    setClearError(null);
    setSecretFeedback(null);
    try {
      await clearSecret(serverId);
      setSecretOverride(false);
      setSecretFeedback({ tone: "success", message: "sudo 密码已清除。" });
      setConfirmClearOpen(false);
      refreshSecret();
    } catch (error) {
      setClearError(describeError(error));
    } finally {
      setSecretPending(null);
    }
  };

  const handleTestConnection = async () => {
    setTestPending(true);
    setTestError(null);
    try {
      const result = await testConnection(serverId);
      setTestResult(result);
      refreshHost();
    } catch (error) {
      setTestResult(null);
      setTestError(describeError(error));
    } finally {
      setTestPending(false);
    }
  };

  if (!validServerId) {
    return (
      <div className="stack">
        <PageHeader eyebrow="主机设置" title="主机设置" />
        <ErrorBanner title="无效的主机地址" message="主机 ID 必须是正整数。" />
        <Link className="btn btn--ghost" to="/">
          返回主机总览
        </Link>
      </div>
    );
  }

  return (
    <div className="stack">
      <Link className="back-link" to={`/hosts/${serverId}`}>
        ← 返回主机详情
      </Link>

      <PageHeader
        eyebrow="主机设置"
        title={host ? host.alias : `主机 #${serverId}`}
        description={
          host ? (
            <span className="mono">
              {host.user}@{host.hostname}:{host.port} · {SERVER_SOURCE_LABELS[host.source]}
            </span>
          ) : (
            "加载主机配置…"
          )
        }
        actions={
          <>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={refreshHost}
              disabled={hostRefreshing}
            >
              {hostRefreshing ? "刷新中…" : "刷新"}
            </button>
            <Link className="btn btn--primary" to={`/hosts/${serverId}`}>
              查看快照
            </Link>
          </>
        }
      />

      {hostError ? (
        <ErrorBanner
          title="无法加载主机信息"
          message={hostError.message}
          onRetry={refreshHost}
        />
      ) : null}

      {hostLoading && !host ? (
        <LoadingBlock label="正在加载主机设置…" />
      ) : !host && !hostError ? (
        <LoadingBlock label="正在读取主机配置…" />
      ) : host ? (
        <div className="settings-grid">
          <SectionCard title="基础设置" description="启用状态与运维备注">
            <div className="stack">
              {basicFeedback ? (
                basicFeedback.tone === "success" ? (
                  <SuccessBanner message={basicFeedback.message} />
                ) : (
                  <ErrorBanner title="保存失败" message={basicFeedback.message} />
                )
              ) : null}

              <div className="switch-row">
                <label className="switch" htmlFor="host-enabled">
                  <input
                    id="host-enabled"
                    type="checkbox"
                    role="switch"
                    checked={host.enabled}
                    onChange={(event) => void handleToggleEnabled(event.target.checked)}
                    disabled={togglePending}
                  />
                  <span className="switch__track" aria-hidden="true">
                    <span className="switch__thumb" />
                  </span>
                  <span className="switch__text">启用主机</span>
                </label>
                <StatusBadge tone={host.enabled ? "success" : "muted"}>
                  {host.enabled ? "已启用" : "已停用"}
                </StatusBadge>
                {togglePending ? <span className="muted">保存中…</span> : null}
              </div>
              <p className="muted">
                停用后保留主机记录与历史任务，但总览会标记为已停用，采集与动作前请先启用。
              </p>

              <div className="field">
                <label htmlFor="host-notes">备注</label>
                <textarea
                  id="host-notes"
                  rows={4}
                  value={notesDraft}
                  onChange={(event) => setNotesDraft(event.target.value)}
                  placeholder="例如：维护窗口、负责人、特殊说明"
                  disabled={notesPending}
                />
              </div>
              <div className="form-actions">
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => void handleSaveNotes()}
                  disabled={notesPending || notesDraft === host.notes}
                >
                  {notesPending ? "保存中…" : "保存备注"}
                </button>
                {notesDraft !== host.notes ? (
                  <span className="muted">有未保存的修改</span>
                ) : null}
              </div>
            </div>
          </SectionCard>

          <SectionCard
            title="sudo 密码"
            description="按主机保存，使用 Windows DPAPI 加密"
            actions={
              <span data-testid="sudo-status">
                <StatusBadge tone={hasPassword ? "success" : "neutral"}>
                  {hasPassword ? "已设置" : "未设置"}
                </StatusBadge>
              </span>
            }
          >
            <div className="stack">
              {secretLoadError ? (
                <ErrorBanner
                  title="无法读取密码状态"
                  message={secretLoadError.message}
                  onRetry={refreshSecret}
                />
              ) : null}
              {secretFeedback ? (
                secretFeedback.tone === "success" ? (
                  <SuccessBanner message={secretFeedback.message} />
                ) : (
                  <ErrorBanner title="密码操作失败" message={secretFeedback.message} />
                )
              ) : null}

              <p className="muted">
                密码仅用于远端 sudo 提权，接口不会回显明文。未设置时，需要提权的动作可能失败。
              </p>

              <form className="stack" onSubmit={(event) => void handleSetPassword(event)}>
                <div className="field">
                  <label htmlFor="sudo-password">新 sudo 密码</label>
                  <input
                    id="sudo-password"
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="输入后保存，不会显示已有密码"
                    disabled={secretPending !== null}
                  />
                </div>
                <div className="form-actions">
                  <button
                    type="submit"
                    className="btn btn--primary"
                    disabled={!password || secretPending !== null}
                  >
                    {secretPending === "set" ? "保存中…" : "设置密码"}
                  </button>
                  <button
                    type="button"
                    className="btn btn--danger-ghost"
                    onClick={() => {
                      setClearError(null);
                      setConfirmClearOpen(true);
                    }}
                    disabled={!hasPassword || secretPending !== null}
                  >
                    清除密码
                  </button>
                </div>
              </form>

              <p className="muted">
                状态更新时间：{formatDateTime(secret?.updatedAt)}
              </p>
            </div>
          </SectionCard>

          <SectionCard title="连接测试" description="同步执行，最长 20 秒">
            <div className="stack">
              <div className="form-actions">
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => void handleTestConnection()}
                  disabled={testPending}
                >
                  {testPending ? "测试中…" : "测试连接"}
                </button>
                {testResult ? (
                  <span className="muted">
                    上次测试：{formatDateTime(testResult.testedAt)}
                  </span>
                ) : null}
              </div>

              <div aria-live="polite" className="test-result">
                {testError ? (
                  <ErrorBanner title="连接测试失败" message={testError} />
                ) : null}
                {testResult ? (
                  testResult.ok ? (
                    <SuccessBanner
                      message={`连接正常 · ${formatDurationMs(testResult.durationMs)}`}
                    />
                  ) : (
                    <ErrorBanner
                      title="连接失败"
                      message={testResult.error ?? "远端返回失败，未提供原因。"}
                    />
                  )
                ) : null}
                {!testResult && !testError ? (
                  <p className="muted">尚未测试。测试结果会写入主机最近连接状态。</p>
                ) : null}
              </div>

              <DetailList
                items={[
                  {
                    label: "最近连接状态",
                    value: host.lastConnect
                      ? `${host.lastConnect.status} · ${formatDateTime(host.lastConnect.at)}`
                      : "尚未记录",
                  },
                  {
                    label: "sudo 密码",
                    value: hasPassword ? "已配置" : "未配置",
                  },
                ]}
              />
            </div>
          </SectionCard>

          <SectionCard title="SSH 配置摘要" description="只读，来源为本机 OpenSSH 配置">
            <DetailList
              items={[
                { label: "别名", value: <span className="mono">{host.alias}</span> },
                { label: "HostName", value: <span className="mono">{host.hostname}</span> },
                { label: "User", value: <span className="mono">{host.user}</span> },
                { label: "Port", value: <span className="mono">{host.port}</span> },
                {
                  label: "IdentityFile",
                  value: (
                    <span className="mono">{host.identityFile ?? "—"}</span>
                  ),
                },
                {
                  label: "ProxyJump",
                  value: <span className="mono">{host.proxyJump ?? "—"}</span>,
                },
                {
                  label: "ProxyCommand",
                  value: <span className="mono">{host.proxyCommand ?? "—"}</span>,
                },
                { label: "连接超时", value: `${host.connectTimeout} 秒` },
                { label: "来源", value: SERVER_SOURCE_LABELS[host.source] },
              ]}
            />
          </SectionCard>

          <SectionCard
            className="card--span-2"
            title="本机安全设置"
            description="来自后端 GET /api/settings，只读"
            actions={
              <button
                type="button"
                className="btn btn--ghost btn--small"
                onClick={refreshSettings}
                disabled={settingsLoading || settingsRefreshing}
              >
                {settingsLoading || settingsRefreshing ? "刷新中…" : "刷新"}
              </button>
            }
          >
            <div className="stack">
              {settingsError ? (
                <ErrorBanner
                  title="无法读取本机设置"
                  message={settingsError.message}
                  onRetry={refreshSettings}
                />
              ) : null}

              {settings ? (
                <>
                  <DetailList
                    items={[
                      {
                        label: "监听地址",
                        value: (
                          <span className="mono">
                            {settings.bindHost}:{settings.bindPort}
                          </span>
                        ),
                      },
                      {
                        label: "任意命令",
                        value: (
                          <StatusBadge
                            tone={settings.allowArbitraryCommands ? "warning" : "success"}
                          >
                            {settings.allowArbitraryCommands ? "已启用" : "已关闭"}
                          </StatusBadge>
                        ),
                      },
                      {
                        label: "输出上限",
                        value: `${formatBytes(settings.maxOutputBytes)}（${settings.maxOutputBytes} 字节）`,
                      },
                      { label: "快照缓存", value: `${settings.snapshotTtlSeconds} 秒` },
                      {
                        label: "传输后端",
                        value: <span className="mono">{settings.transport}</span>,
                      },
                      {
                        label: "SSH 配置路径",
                        value: <span className="mono">{settings.sshConfigPath}</span>,
                      },
                      {
                        label: "应用数据目录",
                        value: <span className="mono">{settings.appDataDir}</span>,
                      },
                    ]}
                  />
                  {settings.allowArbitraryCommands ? (
                    <InfoBanner tone="warning">
                      任意命令入口当前已启用。请确认这台管理机仅在可信环境中使用。
                    </InfoBanner>
                  ) : (
                    <InfoBanner>
                      任意命令入口保持关闭；所有远程变更仅通过结构化动作白名单执行。
                    </InfoBanner>
                  )}
                  <p className="muted">
                    这些设置由后端配置文件决定，前端只读。修改后需要重启本地服务。
                  </p>
                </>
              ) : settingsLoading ? (
                <LoadingBlock label="正在读取本机设置…" />
              ) : null}
            </div>
          </SectionCard>
        </div>
      ) : null}

      <ConfirmDialog
        open={confirmClearOpen}
        title="清除 sudo 密码"
        description="清除后，该主机需要 sudo 提权的动作可能失败，直到重新设置密码。"
        details={[
          { label: "目标主机", value: host ? host.alias : `主机 #${serverId}` },
          { label: "当前状态", value: hasPassword ? "已设置" : "未设置" },
        ]}
        warning={<p>该操作不可撤销，密码不会保留副本。</p>}
        confirmLabel="确认清除"
        danger
        pending={secretPending === "clear"}
        error={clearError}
        onConfirm={() => void handleClearPassword()}
        onCancel={() => {
          if (secretPending === "clear") return;
          setConfirmClearOpen(false);
          setClearError(null);
        }}
      />
    </div>
  );
}
