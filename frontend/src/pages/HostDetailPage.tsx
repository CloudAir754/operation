import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { createTask, getHost, getSnapshot } from "../api/client";
import { describeError } from "../api/errors";
import type {
  GpuInfo,
  Host,
  SectionStatus,
  ServiceUnit,
  ServicesInfo,
  Snapshot,
  SnapshotSection,
  StorageScan,
  TaskParams,
} from "../api/types";
import { HostTabList, isHostTabId } from "../components/host/HostTabs";
import type { HostTabId } from "../components/host/HostTabs";
import { AccountsTab } from "../components/host/accounts/AccountsTab";
import { GpuTab } from "../components/host/GpuTab";
import { MetricsTab } from "../components/host/MetricsTab";
import { SECTION_STATUS_DESCRIPTIONS, SectionNotice } from "../components/host/SectionNotice";
import { StorageTab } from "../components/host/StorageTab";
import { UsersTab } from "../components/host/UsersTab";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { LogPanel } from "../components/LogPanel";
import {
  DetailList,
  EmptyState,
  ErrorBanner,
  InfoBanner,
  LoadingBlock,
  PageHeader,
  SectionCard,
  StatusBadge,
  SuccessBanner,
  UsageBar,
} from "../components/ui";
import { usePageTitle } from "../hooks/usePageTitle";
import { usePolling } from "../hooks/usePolling";
import { createIdempotencyKey } from "../lib/id";
import {
  ACTION_RISK,
  RISK_TONES,
  SECTION_STATUS_LABELS,
  SECTION_STATUS_TONES,
  SERVER_SOURCE_LABELS,
  actionLabel,
  riskLabel,
} from "../lib/labels";
import {
  clampPercent,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatPercent,
  formatRelativeTime,
} from "../lib/format";

type QuickAction = "collect_snapshot" | "host_check";

type ServiceAction =
  | "service_start"
  | "service_stop"
  | "service_restart"
  | "service_enable"
  | "service_disable";

interface PendingServiceAction {
  action: ServiceAction;
  unit: ServiceUnit;
}

interface TaskNotice {
  action: string;
  taskId: string;
  alias: string;
}

const SERVICE_ACTION_LABELS: Record<ServiceAction, string> = {
  service_start: "启动",
  service_stop: "停止",
  service_restart: "重启",
  service_enable: "启用",
  service_disable: "禁用",
};

const SERVICE_ACTIONS: ServiceAction[] = [
  "service_start",
  "service_stop",
  "service_restart",
  "service_enable",
  "service_disable",
];

export function HostDetailPage() {
  const params = useParams<{ id: string }>();
  const serverId = Number(params.id);
  const validServerId = Number.isInteger(serverId) && serverId > 0;

  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const activeTab: HostTabId = isHostTabId(tabParam) ? tabParam : "overview";
  const setTab = useCallback(
    (next: HostTabId) => {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.set("tab", next);
      setSearchParams(nextParams, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const loadHost = useCallback(
    (signal: AbortSignal) => getHost(serverId, signal),
    [serverId],
  );
  const {
    data: loadedHost,
    error: hostError,
    refresh: refreshHost,
  } = usePolling<Host>(loadHost, { enabled: validServerId });

  // 首次打开主机详情时立即采集（refresh=true），之后复用后端 15–30 秒缓存，
  // 只有点击「刷新」才强制重新采集。
  const [refreshToken, setRefreshToken] = useState(1);
  const loadSnapshot = useCallback(
    (signal: AbortSignal) =>
      getSnapshot(serverId, { refresh: refreshToken > 0, signal }),
    [serverId, refreshToken],
  );
  const {
    data: loadedSnapshot,
    error: snapshotError,
    loading: snapshotLoading,
    refreshing: snapshotRefreshing,
  } = usePolling<Snapshot>(loadSnapshot, {
    enabled: validServerId,
    reloadKey: `${serverId}:${refreshToken}`,
  });

  const [quickAction, setQuickAction] = useState<QuickAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [taskNotice, setTaskNotice] = useState<TaskNotice | null>(null);

  const [pendingService, setPendingService] = useState<PendingServiceAction | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  const [logTarget, setLogTarget] = useState<{ unit: string | null } | null>(null);
  const [exportPending, setExportPending] = useState<string | null>(null);

  // A per-UID scan created from the 用户 tab is handed to the 磁盘空间 tab.
  const [adoptedScanId, setAdoptedScanId] = useState<string | null>(null);

  // Never render data belonging to a previously selected host while the new
  // request is in flight.
  const host = loadedHost && loadedHost.id === serverId ? loadedHost : null;
  const snapshot =
    loadedSnapshot && loadedSnapshot.serverId === serverId ? loadedSnapshot : null;

  const hostAlias = host?.alias ?? `主机 #${serverId}`;
  usePageTitle(host ? `主机 ${host.alias}` : "主机详情");

  const mountSizes = useMemo(() => {
    const sizes: Record<string, number> = {};
    for (const mount of snapshot?.data.disks.data?.mounts ?? []) {
      sizes[mount.mount] = mount.totalBytes;
    }
    return sizes;
  }, [snapshot]);

  const handleRefresh = () => {
    if (!validServerId) return;
    setRefreshToken((value) => value + 1);
    refreshHost();
  };

  const runQuickAction = async (action: QuickAction) => {
    if (!validServerId) return;
    setQuickAction(action);
    setActionError(null);
    setTaskNotice(null);
    try {
      const task = await createTask({
        serverId,
        action,
        params: {},
        confirm: true,
        idempotencyKey: createIdempotencyKey(),
      });
      setTaskNotice({ action, taskId: task.id, alias: task.serverAlias || hostAlias });
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setQuickAction(null);
    }
  };

  const requestServiceAction = (action: ServiceAction, unit: ServiceUnit) => {
    setConfirmError(null);
    setPendingService({ action, unit });
  };

  const handleConfirmService = async () => {
    if (!pendingService) return;
    setConfirmPending(true);
    setConfirmError(null);
    try {
      const task = await createTask({
        serverId,
        action: pendingService.action,
        params: { unit: pendingService.unit.name },
        confirm: true,
        idempotencyKey: createIdempotencyKey(),
      });
      const notice: TaskNotice = {
        action: pendingService.action,
        taskId: task.id,
        alias: task.serverAlias || hostAlias,
      };
      setTaskNotice(notice);
      setPendingService(null);
    } catch (error) {
      setConfirmError(describeError(error));
    } finally {
      setConfirmPending(false);
    }
  };

  const handleExportLogs = async (unit: string | null) => {
    if (!validServerId) return;
    const key = unit ?? "__all__";
    setExportPending(key);
    setActionError(null);
    setTaskNotice(null);
    try {
      const params: TaskParams = unit ? { unit, lines: "200" } : { lines: "200" };
      const task = await createTask({
        serverId,
        action: "query_logs",
        params,
        confirm: true,
        idempotencyKey: createIdempotencyKey(),
      });
      setTaskNotice({ action: "query_logs", taskId: task.id, alias: task.serverAlias || hostAlias });
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setExportPending(null);
    }
  };

  const handleUserScanStarted = (scan: StorageScan) => {
    setAdoptedScanId(scan.scanId);
    setTab("storage");
  };

  if (!validServerId) {
    return (
      <div className="stack">
        <PageHeader eyebrow="主机详情" title="主机详情" />
        <ErrorBanner title="无效的主机地址" message="主机 ID 必须是正整数。" />
        <Link className="btn btn--ghost" to="/">
          返回主机总览
        </Link>
      </div>
    );
  }

  const sections = snapshot?.data;
  const pendingRisk = pendingService
    ? (ACTION_RISK[pendingService.action] ?? "medium")
    : "medium";

  return (
    <div className="stack">
      <Link className="back-link" to="/">
        ← 返回主机总览
      </Link>

      <PageHeader
        eyebrow={`主机详情${host ? ` · ${SERVER_SOURCE_LABELS[host.source]}` : ""}`}
        title={hostAlias}
        description={
          host ? (
            <span className="mono">
              {host.user}@{host.hostname}:{host.port}
              {host.proxyJump ? ` · ProxyJump ${host.proxyJump}` : ""}
            </span>
          ) : (
            "正在加载主机信息…"
          )
        }
        actions={
          <>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={handleRefresh}
              disabled={snapshotRefreshing}
            >
              {snapshotRefreshing ? "刷新中…" : "刷新"}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => void runQuickAction("host_check")}
              disabled={quickAction !== null}
            >
              {quickAction === "host_check" ? "检查中…" : "主机检查"}
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void runQuickAction("collect_snapshot")}
              disabled={quickAction !== null}
            >
              {quickAction === "collect_snapshot" ? "采集中…" : "采集快照"}
            </button>
            <Link className="btn btn--ghost" to={`/hosts/${serverId}/settings`}>
              主机设置
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

      {actionError ? <ErrorBanner title="任务创建失败" message={actionError} /> : null}

      {taskNotice ? (
        <SuccessBanner
          message={`已创建任务：${actionLabel(taskNotice.action)} · ${taskNotice.alias}`}
        >
          <p className="banner__detail">
            任务 ID：<span className="mono">{taskNotice.taskId}</span> ·{" "}
            <Link to="/tasks">在维护任务中查看</Link>
          </p>
        </SuccessBanner>
      ) : null}

      <HostTabList active={activeTab} onChange={setTab} />

      <div
        role="tabpanel"
        id={`host-panel-${activeTab}`}
        aria-labelledby={`host-tab-${activeTab}`}
        tabIndex={0}
        className="tabpanel"
      >
        {activeTab === "overview" ? (
          <>
            {snapshotError ? (
              <ErrorBanner
                title="无法获取主机快照"
                message={snapshotError.message}
                onRetry={handleRefresh}
              />
            ) : null}

            {snapshotLoading && !snapshot ? (
              <LoadingBlock label="正在采集主机状态…" />
            ) : !snapshot && !snapshotError ? (
              <LoadingBlock label="正在读取主机快照…" />
            ) : snapshot ? (
              <div className="stack">
                <div className="snapshot-meta" aria-live="polite">
                  <span>
                    采集于{" "}
                    <span title={formatDateTime(snapshot.collectedAt)}>
                      {formatRelativeTime(snapshot.collectedAt)}
                    </span>
                  </span>
                  <span className="muted">{formatDateTime(snapshot.collectedAt)}</span>
                  <span className="muted">数据年龄 {formatDuration(snapshot.ageSeconds)}</span>
                  {snapshot.stale ? <StatusBadge tone="warning">非实时缓存</StatusBadge> : null}
                  {snapshot.status !== "ok" ? (
                    <StatusBadge tone={SECTION_STATUS_TONES[snapshot.status]}>
                      {SECTION_STATUS_LABELS[snapshot.status]}
                    </StatusBadge>
                  ) : null}
                </div>

                {snapshot.status !== "ok" && snapshot.error ? (
                  <InfoBanner tone="warning">
                    整体采集状态异常：{snapshot.error}
                    {snapshot.stale ? "（当前展示的是非实时缓存）" : ""}
                  </InfoBanner>
                ) : null}

                <div className="detail-grid">
                  <SnapshotSectionView
                    title="系统信息"
                    testId="system-section"
                    section={sections?.system}
                  >
                    {(data) => (
                      <DetailList
                        items={[
                          { label: "主机名", value: data.hostname },
                          { label: "操作系统", value: data.os },
                          { label: "内核", value: data.kernel },
                          { label: "架构", value: data.arch },
                          { label: "运行时长", value: formatDuration(data.uptimeSeconds) },
                          { label: "启动时间", value: formatDateTime(data.bootTime) },
                        ]}
                      />
                    )}
                  </SnapshotSectionView>

                  <SnapshotSectionView title="CPU" testId="cpu-section" section={sections?.cpu}>
                    {(data) => (
                      <div className="stack-sm">
                        <UsageBar
                          label="CPU 使用率"
                          percent={data.usagePercent}
                          testId="cpu-usage"
                        />
                        <DetailList
                          items={[
                            { label: "型号", value: data.model },
                            { label: "核心数", value: data.cores },
                            { label: "1 分钟负载", value: data.load1 ?? "—" },
                            { label: "5 分钟负载", value: data.load5 ?? "—" },
                            { label: "15 分钟负载", value: data.load15 ?? "—" },
                          ]}
                        />
                      </div>
                    )}
                  </SnapshotSectionView>

                  <SnapshotSectionView
                    title="内存"
                    testId="memory-section"
                    section={sections?.memory}
                  >
                    {(data) => (
                      <div className="stack-sm">
                        <UsageBar
                          label="内存使用率"
                          percent={data.usedPercent}
                          testId="memory-usage"
                        />
                        <DetailList
                          items={[
                            {
                              label: "已用",
                              value: formatBytes(
                                Math.max(0, data.totalBytes - data.availableBytes),
                              ),
                            },
                            { label: "总内存", value: formatBytes(data.totalBytes) },
                            { label: "可用", value: formatBytes(data.availableBytes) },
                            {
                              label: "Swap",
                              value:
                                data.swapTotalBytes > 0
                                  ? `${formatBytes(data.swapUsedBytes)} / ${formatBytes(
                                      data.swapTotalBytes,
                                    )}`
                                  : "未启用",
                            },
                          ]}
                        />
                      </div>
                    )}
                  </SnapshotSectionView>

                  <GpuCard section={sections?.gpu} />

                  <SnapshotSectionView
                    title="磁盘"
                    testId="disks-section"
                    section={sections?.disks}
                    className="card--span-2"
                  >
                    {(data) =>
                      data.mounts.length === 0 ? (
                        <EmptyState title="没有可用的挂载点" />
                      ) : (
                        <div className="table-wrap">
                          <table className="data-table">
                            <caption className="sr-only">磁盘挂载点</caption>
                            <thead>
                              <tr>
                                <th scope="col">挂载点</th>
                                <th scope="col">文件系统</th>
                                <th scope="col">总容量</th>
                                <th scope="col">已用</th>
                                <th scope="col">可用</th>
                                <th scope="col">使用率</th>
                              </tr>
                            </thead>
                            <tbody>
                              {data.mounts.map((mount) => (
                                <tr key={`${mount.mount}-${mount.filesystem}`}>
                                  <td className="mono">{mount.mount}</td>
                                  <td className="mono muted">{mount.filesystem}</td>
                                  <td>{formatBytes(mount.totalBytes)}</td>
                                  <td>{formatBytes(mount.usedBytes)}</td>
                                  <td>{formatBytes(mount.freeBytes)}</td>
                                  <td>
                                    <div className="disk-usage">
                                      <span
                                        className={
                                          (mount.usedPercent ?? 0) >= 90
                                            ? "text-danger"
                                            : (mount.usedPercent ?? 0) >= 75
                                              ? "text-warning"
                                              : undefined
                                        }
                                      >
                                        {formatPercent(mount.usedPercent)}
                                      </span>
                                      <div
                                        className="mini-track"
                                        role="progressbar"
                                        aria-label={`${mount.mount} 使用率`}
                                        aria-valuemin={0}
                                        aria-valuemax={100}
                                        aria-valuenow={
                                          mount.usedPercent === null
                                            ? undefined
                                            : Math.round(clampPercent(mount.usedPercent))
                                        }
                                        aria-valuetext={formatPercent(mount.usedPercent)}
                                      >
                                        <div
                                          className={`mini-fill mini-fill--${
                                            (mount.usedPercent ?? 0) >= 90
                                              ? "danger"
                                              : (mount.usedPercent ?? 0) >= 75
                                                ? "warning"
                                                : "ok"
                                          }`}
                                          style={{ width: `${clampPercent(mount.usedPercent)}%` }}
                                        />
                                      </div>
                                    </div>
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )
                    }
                  </SnapshotSectionView>

                  <SnapshotSectionView
                    title="网络"
                    testId="network-section"
                    section={sections?.network}
                    className="card--span-2"
                  >
                    {(data) => (
                      <div className="stack-sm">
                        <DetailList
                          items={[
                            { label: "默认接口", value: data.defaultInterface ?? "—" },
                            { label: "网关", value: data.gateway ?? "—" },
                            { label: "接口数量", value: data.interfaces.length },
                          ]}
                        />
                        {data.interfaces.length === 0 ? (
                          <EmptyState title="没有检测到网络接口" />
                        ) : (
                          <div className="table-wrap">
                            <table className="data-table">
                              <caption className="sr-only">网络接口</caption>
                              <thead>
                                <tr>
                                  <th scope="col">接口</th>
                                  <th scope="col">状态</th>
                                  <th scope="col">速率</th>
                                  <th scope="col">接收</th>
                                  <th scope="col">发送</th>
                                  <th scope="col">错误</th>
                                  <th scope="col">地址</th>
                                </tr>
                              </thead>
                              <tbody>
                                {data.interfaces.map((iface) => (
                                  <tr key={iface.name}>
                                    <td className="mono">{iface.name}</td>
                                    <td>
                                      <StatusBadge
                                        tone={iface.state === "up" ? "success" : "muted"}
                                      >
                                        {iface.state}
                                      </StatusBadge>
                                    </td>
                                    <td>
                                      {iface.speedMbps === null
                                        ? "—"
                                        : `${iface.speedMbps} Mbps`}
                                    </td>
                                    <td>{formatBytes(iface.rxBytes)}</td>
                                    <td>{formatBytes(iface.txBytes)}</td>
                                    <td>
                                      <span
                                        className={
                                          (iface.rxErrors ?? 0) + (iface.txErrors ?? 0) > 0
                                            ? "text-danger"
                                            : undefined
                                        }
                                      >
                                        {(iface.rxErrors ?? 0) + (iface.txErrors ?? 0)}
                                      </span>
                                    </td>
                                    <td className="mono">
                                      {iface.addresses.length > 0
                                        ? iface.addresses.join(", ")
                                        : "—"}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </div>
                    )}
                  </SnapshotSectionView>

                  <SnapshotSectionView
                    title="服务"
                    testId="services-section"
                    section={sections?.services}
                    className="card--span-2"
                  >
                    {(data) => (
                      <ServicesTable
                        info={data}
                        exportPending={exportPending}
                        onShowLogs={(unit) => setLogTarget({ unit })}
                        onExportLogs={(unit) => void handleExportLogs(unit)}
                        onServiceAction={requestServiceAction}
                      />
                    )}
                  </SnapshotSectionView>
                </div>
              </div>
            ) : null}
          </>
        ) : null}

        {activeTab === "metrics" ? <MetricsTab serverId={serverId} /> : null}
        {activeTab === "gpu" ? <GpuTab serverId={serverId} /> : null}
        {activeTab === "storage" ? (
          <StorageTab
            serverId={serverId}
            mountSizes={mountSizes}
            adoptedScanId={adoptedScanId}
          />
        ) : null}
        {activeTab === "users" ? (
          <UsersTab serverId={serverId} onScanStarted={handleUserScanStarted} />
        ) : null}
        {activeTab === "accounts" ? (
          <AccountsTab
            serverId={serverId}
            hasSudoPassword={host?.hasSudoPassword ?? false}
            onSudoChanged={refreshHost}
          />
        ) : null}
      </div>

      <ConfirmDialog
        open={pendingService !== null}
        title={`确认${pendingService ? SERVICE_ACTION_LABELS[pendingService.action] : ""}服务`}
        description="该操作会立即在目标主机上执行，请核对目标主机、服务单元与风险等级。"
        details={
          pendingService
            ? [
                {
                  label: "目标主机",
                  value: (
                    <>
                      {hostAlias}
                      {host ? (
                        <span className="mono muted">
                          {" "}
                          {host.user}@{host.hostname}:{host.port}
                        </span>
                      ) : null}
                    </>
                  ),
                },
                {
                  label: "服务单元",
                  value: <span className="mono">{pendingService.unit.name}</span>,
                },
                {
                  label: "动作",
                  value: actionLabel(pendingService.action),
                },
                {
                  label: "风险等级",
                  value: (
                    <StatusBadge tone={RISK_TONES[pendingRisk]}>
                      {riskLabel(pendingRisk)}
                    </StatusBadge>
                  ),
                },
                {
                  label: "权限要求",
                  value: (
                    <>
                      需要 sudo
                      {host ? (
                        host.hasSudoPassword ? (
                          <span className="muted">（已配置 sudo 密码）</span>
                        ) : (
                          <span className="text-warning">（未配置 sudo 密码，可能失败）</span>
                        )
                      ) : null}
                    </>
                  ),
                },
              ]
            : []
        }
        warning={
          pendingService?.unit.protected ? (
            <p>该服务受保护，后端将拒绝危险的远程变更。</p>
          ) : host && !host.hasSudoPassword ? (
            <p>该主机尚未保存 sudo 密码，需要提权的操作可能失败。可先在主机设置中配置。</p>
          ) : (
            <p>确认后将生成带幂等键的后台任务，可在「维护任务」中查看进度与输出。</p>
          )
        }
        confirmLabel="确认执行"
        danger={pendingRisk === "high"}
        pending={confirmPending}
        error={confirmError}
        onConfirm={() => void handleConfirmService()}
        onCancel={() => {
          if (confirmPending) return;
          setPendingService(null);
          setConfirmError(null);
        }}
      />

      {logTarget ? (
        <LogPanel
          serverId={serverId}
          serverAlias={hostAlias}
          unit={logTarget.unit}
          onClose={() => setLogTarget(null)}
        />
      ) : null}
    </div>
  );
}

interface SnapshotSectionViewProps<T> {
  title: string;
  testId: string;
  section: SnapshotSection<T> | undefined;
  className?: string;
  children: (data: T) => ReactNode;
}

function SnapshotSectionView<T>({
  title,
  testId,
  section,
  className,
  children,
}: SnapshotSectionViewProps<T>) {
  const status: SectionStatus = section?.status ?? "error";
  const data = section?.data ?? null;
  const hasData = status === "ok" && data !== null;
  const label = hasData
    ? "正常"
    : status === "ok"
      ? "暂无数据"
      : SECTION_STATUS_LABELS[status];
  return (
    <SectionCard
      title={title}
      testId={testId}
      className={className}
      actions={
        <StatusBadge tone={hasData ? "success" : SECTION_STATUS_TONES[status]}>
          {label}
        </StatusBadge>
      }
    >
      {status === "ok" && data !== null ? (
        children(data)
      ) : (
        <SectionNotice status={status} error={section?.error ?? null} testId={`${testId}-notice`} />
      )}
    </SectionCard>
  );
}

function formatGpuMetric(value: number | null | undefined, formatter: (v: number) => string): string {
  return value === null || value === undefined ? "N/A" : formatter(value);
}

function formatMiB(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "N/A";
  return `${Math.round(value)} MiB`;
}

function GpuCard({ section }: { section: SnapshotSection<GpuInfo> | undefined }) {
  const status: SectionStatus = section?.status ?? "error";
  const devices = section?.data?.devices ?? [];
  const available = status === "ok" && devices.length > 0;
  const label = available
    ? "正常"
    : status === "ok"
      ? "暂无数据"
      : SECTION_STATUS_LABELS[status];

  return (
    <SectionCard
      title="GPU"
      testId="gpu-section"
      actions={
        <StatusBadge tone={available ? "success" : SECTION_STATUS_TONES[status]}>
          {label}
        </StatusBadge>
      }
    >
      <div className="gpu">
        <div className="gpu__headline">
          <span className="gpu__caption">检测结果</span>
          <strong className="gpu__value" data-testid="gpu-value">
            {available ? `${devices.length} 块 GPU` : "N/A"}
          </strong>
        </div>
        {!available ? (
          <div className="section-notice" role="status">
            <p className="section-notice__text">
              {status === "ok"
                ? "未检测到 GPU 设备。"
                : SECTION_STATUS_DESCRIPTIONS[status]}
            </p>
            {section?.error ? (
              <p className="section-notice__error">原因：{section.error}</p>
            ) : null}
          </div>
        ) : (
          <div className="gpu-devices">
            {devices.map((device, index) => (
              <div className="gpu-device" key={`${device.index ?? index}-${device.name ?? ""}`}>
                <div className="gpu-device__head">
                  <strong>{device.name ?? `GPU ${device.index ?? index}`}</strong>
                  <span className="muted">#{device.index ?? index}</span>
                </div>
                <DetailList
                  items={[
                    {
                      label: "显存",
                      value:
                        device.memoryTotalMb === null || device.memoryTotalMb === undefined
                          ? "N/A"
                          : `${formatMiB(device.memoryUsedMb)} / ${formatMiB(device.memoryTotalMb)}`,
                    },
                    {
                      label: "使用率",
                      value: formatGpuMetric(device.utilizationPercent, formatPercent),
                    },
                    {
                      label: "温度",
                      value: formatGpuMetric(device.temperatureC, (value) => `${value} °C`),
                    },
                    { label: "驱动", value: device.driverVersion ?? "N/A" },
                  ]}
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </SectionCard>
  );
}

interface ServicesTableProps {
  info: ServicesInfo;
  exportPending: string | null;
  onShowLogs: (unit: string | null) => void;
  onExportLogs: (unit: string | null) => void;
  onServiceAction: (action: ServiceAction, unit: ServiceUnit) => void;
}

function ServicesTable({
  info,
  exportPending,
  onShowLogs,
  onExportLogs,
  onServiceAction,
}: ServicesTableProps) {
  return (
    <div className="stack-sm">
      <div className="services-summary">
        <span>
          运行中 <strong>{info.runningCount}</strong>
        </span>
        <span>
          失败{" "}
          <strong className={info.failedCount > 0 ? "text-danger" : undefined}>
            {info.failedCount}
          </strong>
        </span>
        <div className="services-summary__actions">
          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={() => onShowLogs(null)}
          >
            查看全部日志
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={() => onExportLogs(null)}
            disabled={exportPending !== null}
          >
            {exportPending === "__all__" ? "导出中…" : "导出全部日志"}
          </button>
        </div>
      </div>

      {info.failedUnits.length > 0 ? (
        <InfoBanner tone="warning">
          失败的服务单元：<span className="mono">{info.failedUnits.join("、")}</span>
        </InfoBanner>
      ) : null}

      {info.units.length === 0 ? (
        <EmptyState title="没有检测到 systemd 服务" />
      ) : (
        <div className="table-wrap table-wrap--tall">
          <table className="data-table data-table--services">
            <caption className="sr-only">systemd 服务单元</caption>
            <thead>
              <tr>
                <th scope="col">服务单元</th>
                <th scope="col">状态</th>
                <th scope="col">保护</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {info.units.map((unit) => {
                const activeTone =
                  unit.active === "active"
                    ? "success"
                    : unit.active === "failed"
                      ? "danger"
                      : "neutral";
                return (
                  <tr key={unit.name}>
                    <td>
                      <span className="mono">{unit.name}</span>
                      {unit.description ? (
                        <span className="service-description">{unit.description}</span>
                      ) : null}
                    </td>
                    <td>
                      <StatusBadge tone={activeTone}>{unit.active}</StatusBadge>
                      <span className="muted mono service-sub"> {unit.sub}</span>
                    </td>
                    <td>
                      {unit.protected ? (
                        <StatusBadge tone="warning" title="受保护服务，禁止远程变更">
                          受保护
                        </StatusBadge>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <div className="row-actions">
                        {SERVICE_ACTIONS.map((action) => (
                          <button
                            key={action}
                            type="button"
                            className="btn btn--tiny btn--ghost"
                            onClick={() => onServiceAction(action, unit)}
                            disabled={unit.protected}
                            title={
                              unit.protected
                                ? "受保护服务，禁止远程变更"
                                : `${actionLabel(action)} ${unit.name}`
                            }
                            aria-label={`${SERVICE_ACTION_LABELS[action]} ${unit.name}`}
                          >
                            {SERVICE_ACTION_LABELS[action]}
                          </button>
                        ))}
                        <button
                          type="button"
                          className="btn btn--tiny btn--ghost"
                          onClick={() => onShowLogs(unit.name)}
                          aria-label={`查看日志 ${unit.name}`}
                        >
                          日志
                        </button>
                        <button
                          type="button"
                          className="btn btn--tiny btn--ghost"
                          onClick={() => onExportLogs(unit.name)}
                          disabled={exportPending !== null}
                          aria-label={`导出日志 ${unit.name}`}
                        >
                          {exportPending === unit.name ? "导出中…" : "导出"}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
