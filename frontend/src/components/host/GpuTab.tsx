import { useCallback, useMemo, useState } from "react";
import { getGpus, getMetrics } from "../../api/client";
import type { GpuDeviceDetail, GpuMonitor, GpuProcess, Metrics } from "../../api/types";
import { useDocumentVisibility } from "../../hooks/useDocumentVisibility";
import { usePolling } from "../../hooks/usePolling";
import { formatDateTime, formatRelativeTime } from "../../lib/format";
import { SECTION_STATUS_LABELS, SECTION_STATUS_TONES } from "../../lib/labels";
import {
  METRICS_POLL_INTERVAL_MS,
  METRICS_WINDOWS,
  NOT_AVAILABLE,
  chartColor,
  formatMiB,
  formatOptionalNumber,
  formatPercentOrNa,
} from "../../lib/observability";
import {
  EmptyState,
  ErrorBanner,
  InfoBanner,
  LoadingBlock,
  SectionCard,
  StatusBadge,
} from "../ui";
import { LineChart } from "./LineChart";
import type { ChartSeries } from "./LineChart";
import { SectionNotice } from "./SectionNotice";

type ProcessSort = "memory" | "utilization" | "pid";

const SORT_LABELS: Record<ProcessSort, string> = {
  memory: "显存",
  utilization: "利用率",
  pid: "PID",
};

function mhz(value: number | null | undefined): string {
  return formatOptionalNumber(value, (input) => `${Math.round(input)} MHz`);
}

function watts(value: number | null | undefined): string {
  return formatOptionalNumber(value, (input) =>
    Number.isInteger(input) ? `${input} W` : `${input.toFixed(1)} W`,
  );
}

export interface GpuTabProps {
  serverId: number;
}

export function GpuTab({ serverId }: GpuTabProps) {
  const visible = useDocumentVisibility();
  const [refreshToken, setRefreshToken] = useState(0);
  const [window, setWindow] = useState<"1m" | "5m" | "15m" | "1h">("5m");
  const [userFilter, setUserFilter] = useState("all");
  const [minMemory, setMinMemory] = useState("0");
  const [onlyVisibleGpu, setOnlyVisibleGpu] = useState(false);
  const [sortBy, setSortBy] = useState<ProcessSort>("memory");
  const [sortDesc, setSortDesc] = useState(true);

  const loadGpus = useCallback(
    (signal: AbortSignal) =>
      getGpus(serverId, { refresh: refreshToken > 0, signal }),
    [serverId, refreshToken],
  );
  const {
    data: gpuData,
    error: gpuError,
    refreshing: gpuRefreshing,
    refresh: refreshGpus,
  } = usePolling<GpuMonitor>(loadGpus, {
    enabled: visible,
    reloadKey: `${serverId}:${refreshToken}`,
  });

  const loadMetrics = useCallback(
    (signal: AbortSignal) => getMetrics(serverId, { window, signal }),
    [serverId, window],
  );
  const { data: metricsData } = usePolling<Metrics>(loadMetrics, {
    enabled: visible,
    intervalMs: METRICS_POLL_INTERVAL_MS,
    reloadKey: window,
  });

  const gpu = gpuData && gpuData.serverId === serverId ? gpuData : null;
  const metrics = metricsData && metricsData.serverId === serverId ? metricsData : null;
  const devices = gpu?.devices ?? [];
  const processes = gpu?.processes ?? [];
  const points = metrics?.points ?? [];
  const timestamps = points.map((point) => point.collectedAt);

  const users = useMemo(() => {
    const unique = new Set<string>();
    for (const process of processes) {
      if (process.user) unique.add(process.user);
    }
    return Array.from(unique).sort((left, right) => left.localeCompare(right));
  }, [processes]);

  const minMemoryMb = Number.parseFloat(minMemory);
  const filteredProcesses = useMemo(() => {
    const threshold = Number.isFinite(minMemoryMb) && minMemoryMb > 0 ? minMemoryMb : 0;
    const list = processes.filter((process) => {
      if (userFilter !== "all" && process.user !== userFilter) return false;
      if (threshold > 0 && (process.memoryUsedMb ?? -1) < threshold) return false;
      if (onlyVisibleGpu && process.gpuIndex === null && process.gpuUuid === null) return false;
      return true;
    });
    const direction = sortDesc ? -1 : 1;
    return [...list].sort((left, right) => {
      const pick = (process: GpuProcess): number => {
        if (sortBy === "pid") return process.pid;
        if (sortBy === "utilization") return process.utilizationPercent ?? -1;
        return process.memoryUsedMb ?? -1;
      };
      const diff = pick(left) - pick(right);
      if (diff !== 0) return diff * direction;
      return left.pid - right.pid;
    });
  }, [processes, userFilter, minMemoryMb, onlyVisibleGpu, sortBy, sortDesc]);

  const gpuIndexes = useMemo(() => {
    const indexes = new Set<number>();
    for (const point of points) {
      for (const sample of point.gpu ?? []) indexes.add(sample.index);
    }
    for (const device of devices) indexes.add(device.index);
    return Array.from(indexes).sort((left, right) => left - right);
  }, [points, devices]);

  const deviceName = (index: number): string => {
    const device = devices.find((item) => item.index === index);
    return device?.name ?? `GPU ${index}`;
  };

  const utilizationSeries: ChartSeries[] = gpuIndexes.map((index, order) => ({
    key: `gpu-util-${index}`,
    label: deviceName(index),
    color: chartColor(order),
    values: points.map((point) => {
      const sample = (point.gpu ?? []).find((item) => item.index === index);
      return sample?.utilizationPercent ?? null;
    }),
  }));

  const memorySeries: ChartSeries[] = gpuIndexes.map((index, order) => ({
    key: `gpu-mem-${index}`,
    label: deviceName(index),
    color: chartColor(order),
    values: points.map((point) => {
      const sample = (point.gpu ?? []).find((item) => item.index === index);
      if (!sample) return null;
      if (sample.memoryUsedMb === null || sample.memoryTotalMb === null) return null;
      if (sample.memoryTotalMb <= 0) return null;
      return (sample.memoryUsedMb / sample.memoryTotalMb) * 100;
    }),
  }));

  const collectTime = gpu?.collectedAt ?? null;

  return (
    <div className="stack" data-testid="gpu-tab">
      {gpuError ? (
        <ErrorBanner title="GPU 信息采集失败" message={gpuError.message} onRetry={refreshGpus} />
      ) : null}

      <div className="toolbar">
        <span className="muted" aria-live="polite">
          {collectTime ? `采集于 ${formatRelativeTime(collectTime)}` : "尚未采集"}
          {collectTime ? <span className="mono"> · {formatDateTime(collectTime)}</span> : null}
        </span>
        <button
          type="button"
          className="btn btn--ghost btn--small"
          onClick={() => {
            setRefreshToken((value) => value + 1);
            refreshGpus();
          }}
          disabled={gpuRefreshing}
        >
          {gpuRefreshing ? "刷新中…" : "刷新 GPU"}
        </button>
      </div>

      {!gpu ? (
        gpuError ? null : <LoadingBlock label="正在读取 GPU 信息…" />
      ) : (
        <>
          <div className="snapshot-meta" aria-live="polite">
            <span>
              <StatusBadge tone={SECTION_STATUS_TONES[gpu.status]}>
                {SECTION_STATUS_LABELS[gpu.status]}
              </StatusBadge>
            </span>
            <span>设备 <strong className="mono">{devices.length}</strong></span>
            <span>进程 <strong className="mono">{processes.length}</strong></span>
            <span className="muted">
              NVML {gpu.capabilities.nvml ? "可用" : "不可用"} · MIG{" "}
              {gpu.capabilities.mig ? "支持" : "不支持"} · 进程级利用率{" "}
              {gpu.capabilities.perProcessUtilization ? "可用" : "不可用"}
            </span>
            {gpu.requiresSudo ? <StatusBadge tone="warning">需要 sudo</StatusBadge> : null}
          </div>

          {gpu.status !== "ok" ? (
            <SectionNotice
              status={gpu.status}
              error={gpu.error}
              requiresSudo={gpu.requiresSudo}
              testId="gpu-monitor-notice"
              title="GPU"
            />
          ) : null}

          {gpu.notes.length > 0 ? (
            <InfoBanner>
              <ul className="info-list">
                {gpu.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </InfoBanner>
          ) : null}

          {devices.length === 0 ? (
            <EmptyState
              title="未检测到 GPU 设备"
              description="主机没有 NVIDIA GPU，或驱动/设备节点对当前 SSH 用户不可见。"
            />
          ) : (
            <div className="gpu-card-grid">
              {devices.map((device) => (
                <GpuDeviceCard key={`${device.index}-${device.uuid ?? ""}`} device={device} />
              ))}
            </div>
          )}

          <SectionCard title="GPU 趋势" testId="gpu-charts" className="card--span-2">
            <div className="toolbar">
              <div className="segmented" role="group" aria-label="GPU 采样窗口">
                {METRICS_WINDOWS.map((option) => (
                  <button
                    key={option}
                    type="button"
                    className={`segmented__item${option === window ? " segmented__item--active" : ""}`}
                    aria-pressed={option === window}
                    onClick={() => setWindow(option)}
                  >
                    {option}
                  </button>
                ))}
              </div>
              <span className="muted">
                数据来自 /metrics，每 {METRICS_POLL_INTERVAL_MS / 1000} 秒采样，页签不可见时暂停。
              </span>
            </div>
            <div className="chart-grid">
              <div className="chart-block">
                <LineChart
                  title="GPU 利用率"
                  testId="chart-gpu-utilization"
                  series={utilizationSeries}
                  timestamps={timestamps}
                  yMax={100}
                  yFormat={(value) => `${Math.round(value)}%`}
                  ariaLabel="GPU 利用率趋势图，每张显卡一条曲线"
                  caption="利用率为采样窗口平均值。"
                />
              </div>
              <div className="chart-block">
                <LineChart
                  title="GPU 显存使用率"
                  testId="chart-gpu-memory"
                  series={memorySeries}
                  timestamps={timestamps}
                  yMax={100}
                  yFormat={(value) => `${Math.round(value)}%`}
                  ariaLabel="GPU 显存使用率趋势图"
                  caption="显存为当前 used/total，未知值不绘制为 0。"
                />
              </div>
            </div>
          </SectionCard>

          <SectionCard
            title="GPU 进程"
            testId="gpu-processes"
            className="card--span-2"
            actions={
              <span className="muted">
                {filteredProcesses.length} / {processes.length}
              </span>
            }
          >
            <div className="stack-sm">
              {gpu.processesStatus === "partial" ? (
                <InfoBanner tone="warning">
                  部分 GPU 进程不可见（hidepid、容器或驱动隔离可能导致）。已展示当前可见的进程。
                  {gpu.processesError ? (
                    <span className="banner__detail">原因：{gpu.processesError}</span>
                  ) : null}
                </InfoBanner>
              ) : null}
              {gpu.processesStatus === "unavailable" ? (
                <SectionNotice
                  status="unavailable"
                  error={gpu.processesError}
                  testId="gpu-processes-notice"
                  title="GPU 进程列表"
                />
              ) : null}

              <div className="filters filters--compact">
                <label className="field field--inline">
                  <span>用户</span>
                  <select
                    value={userFilter}
                    onChange={(event) => setUserFilter(event.target.value)}
                  >
                    <option value="all">全部用户</option>
                    {users.map((user) => (
                      <option key={user} value={user}>
                        {user}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field field--inline">
                  <span>最小显存 (MiB)</span>
                  <input
                    type="number"
                    min={0}
                    step={128}
                    value={minMemory}
                    aria-label="最小显存 (MiB)"
                    onChange={(event) => setMinMemory(event.target.value)}
                  />
                </label>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={onlyVisibleGpu}
                    onChange={(event) => setOnlyVisibleGpu(event.target.checked)}
                  />
                  仅可见 GPU
                </label>
                <label className="field field--inline">
                  <span>排序</span>
                  <select
                    value={sortBy}
                    onChange={(event) => setSortBy(event.target.value as ProcessSort)}
                  >
                    {(Object.keys(SORT_LABELS) as ProcessSort[]).map((key) => (
                      <option key={key} value={key}>
                        {SORT_LABELS[key]}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  className="btn btn--ghost btn--small"
                  aria-label={sortDesc ? "切换为升序" : "切换为降序"}
                  onClick={() => setSortDesc((value) => !value)}
                >
                  {sortDesc ? "降序 ↓" : "升序 ↑"}
                </button>
              </div>

              {filteredProcesses.length === 0 ? (
                <EmptyState
                  title="没有匹配的 GPU 进程"
                  description="调整用户、最小显存或「仅可见 GPU」筛选条件。"
                />
              ) : (
                <div className="table-wrap table-wrap--tall">
                  <table className="data-table data-table--observability">
                    <caption className="sr-only">GPU 进程</caption>
                    <thead>
                      <tr>
                        <th scope="col">PID</th>
                        <th scope="col">USER</th>
                        <th scope="col">GPU</th>
                        <th scope="col">显存</th>
                        <th scope="col">利用率</th>
                        <th scope="col">程序</th>
                        <th scope="col">命令</th>
                        <th scope="col">启动时间</th>
                        <th scope="col">状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredProcesses.map((process) => (
                        <tr
                          key={`${process.pid}-${process.gpuUuid ?? process.gpuIndex ?? ""}`}
                          data-testid={`gpu-process-${process.pid}`}
                        >
                          <td className="mono">{process.pid}</td>
                          <td>{process.user ?? NOT_AVAILABLE}</td>
                          <td className="mono">
                            {process.gpuIndex !== null ? `#${process.gpuIndex}` : NOT_AVAILABLE}
                          </td>
                          <td>{formatMiB(process.memoryUsedMb)}</td>
                          <td>{formatPercentOrNa(process.utilizationPercent)}</td>
                          <td className="mono">
                            {process.name ?? NOT_AVAILABLE}
                            {process.programPath ? (
                              <span className="service-description">{process.programPath}</span>
                            ) : null}
                          </td>
                          <td className="mono cell-clamp" title={process.command ?? undefined}>
                            {process.command ?? NOT_AVAILABLE}
                          </td>
                          <td>{process.startedAt ? formatDateTime(process.startedAt) : NOT_AVAILABLE}</td>
                          <td>
                            {process.stale ? (
                              <StatusBadge tone="muted" title={process.error ?? "采样后进程已退出"}>
                                已退出
                              </StatusBadge>
                            ) : process.error ? (
                              <StatusBadge tone="warning" title={process.error}>
                                部分不可用
                              </StatusBadge>
                            ) : (
                              <StatusBadge tone="success">运行中</StatusBadge>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </SectionCard>
        </>
      )}
    </div>
  );
}

function GpuDeviceCard({ device }: { device: GpuDeviceDetail }) {
  const memoryPercent =
    device.memoryUsedMb !== null && device.memoryTotalMb !== null && device.memoryTotalMb > 0
      ? (device.memoryUsedMb / device.memoryTotalMb) * 100
      : device.utilizationMemoryPercent;
  const migEnabled =
    device.migMode !== null && device.migMode !== "" && device.migMode.toLowerCase() !== "disabled";

  return (
    <article className="gpu-card" data-testid={`gpu-device-${device.index}`}>
      <header className="gpu-card__head">
        <div>
          <h3 className="gpu-card__title">{device.name ?? `GPU ${device.index}`}</h3>
          <p className="gpu-card__sub mono">
            #{device.index} · {device.pciBusId ?? NOT_AVAILABLE}
          </p>
        </div>
        <div className="gpu-card__badges">
          {migEnabled ? <StatusBadge tone="info">MIG {device.migMode}</StatusBadge> : null}
          {device.computeMode ? <StatusBadge tone="muted">{device.computeMode}</StatusBadge> : null}
        </div>
      </header>

      <div className="usage">
        <div className="usage__head">
          <span className="usage__label">显存</span>
          <span className="usage__value">{formatPercentOrNa(memoryPercent)}</span>
        </div>
        <div
          className="usage__track"
          role="progressbar"
          aria-label={`GPU ${device.index} 显存使用率`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={
            memoryPercent === null || memoryPercent === undefined
              ? undefined
              : Math.round(Math.min(100, Math.max(0, memoryPercent)))
          }
          aria-valuetext={formatPercentOrNa(memoryPercent)}
        >
          <div
            className={`usage__fill usage__fill--${
              (memoryPercent ?? 0) >= 90 ? "danger" : (memoryPercent ?? 0) >= 75 ? "warning" : "ok"
            }`}
            style={{ width: `${memoryPercent === null || memoryPercent === undefined ? 0 : Math.min(100, Math.max(0, memoryPercent))}%` }}
          />
        </div>
        <div className="usage__detail">
          {formatMiB(device.memoryUsedMb)} / {formatMiB(device.memoryTotalMb)}
        </div>
      </div>

      <dl className="gpu-card__metrics">
        <div>
          <dt>GPU 利用率</dt>
          <dd>{formatPercentOrNa(device.utilizationGpuPercent)}</dd>
        </div>
        <div>
          <dt>显存利用率</dt>
          <dd>{formatPercentOrNa(device.utilizationMemoryPercent)}</dd>
        </div>
        <div>
          <dt>温度</dt>
          <dd>{formatOptionalNumber(device.temperatureC, (value) => `${value.toFixed(0)} °C`)}</dd>
        </div>
        <div>
          <dt>功耗</dt>
          <dd>
            {watts(device.powerDrawW)} / {watts(device.powerLimitW)}
          </dd>
        </div>
        <div>
          <dt>编码 / 解码</dt>
          <dd>
            {formatPercentOrNa(device.encoderPercent)} / {formatPercentOrNa(device.decoderPercent)}
          </dd>
        </div>
        <div>
          <dt>图形 / 显存时钟</dt>
          <dd>
            {mhz(device.clockGraphicsMhz)} / {mhz(device.clockMemoryMhz)}
          </dd>
        </div>
        <div>
          <dt>驱动版本</dt>
          <dd className="mono">{device.driverVersion ?? NOT_AVAILABLE}</dd>
        </div>
        <div>
          <dt>MIG 模式</dt>
          <dd>{device.migMode ?? NOT_AVAILABLE}</dd>
        </div>
      </dl>

      {device.migDevices.length > 0 ? (
        <div className="gpu-card__mig">
          <span className="gpu-card__mig-title">MIG 实例</span>
          <ul className="info-list">
            {device.migDevices.map((instance) => (
              <li key={instance} className="mono">
                {instance}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </article>
  );
}
