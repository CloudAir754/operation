import { useCallback, useState } from "react";
import { getMetrics } from "../../api/client";
import type {
  CpuMetricData,
  DiskMetricData,
  MemoryMetricData,
  MetricPoint,
  Metrics,
  MetricsWindow,
  NetworkMetricData,
} from "../../api/types";
import { useDocumentVisibility } from "../../hooks/useDocumentVisibility";
import { usePolling } from "../../hooks/usePolling";
import { formatDateTime, formatRelativeTime } from "../../lib/format";
import { SECTION_STATUS_LABELS, SECTION_STATUS_TONES } from "../../lib/labels";
import {
  METRICS_POLL_INTERVAL_MS,
  METRICS_WINDOWS,
  chartColor,
  formatBytesOrNa,
  formatCountOrNa,
  formatMbps,
  formatMs,
  formatOptionalNumber,
  formatPercentOrNa,
  formatRate,
  resetReasonLabel,
} from "../../lib/observability";
import {
  DetailList,
  ErrorBanner,
  LoadingBlock,
  SectionCard,
  StatusBadge,
} from "../ui";
import { LineChart } from "./LineChart";
import type { ChartSeries } from "./LineChart";
import { SectionNotice } from "./SectionNotice";

const PERCENT_AXIS = (value: number): string =>
  `${value >= 10 || Number.isInteger(value) ? Math.round(value) : value.toFixed(1)}%`;

function percentSeries(
  points: MetricPoint[],
  key: string,
  label: string,
  colorIndex: number,
  pick: (point: MetricPoint) => number | null,
): ChartSeries {
  return {
    key,
    label,
    color: chartColor(colorIndex),
    values: points.map(pick),
  };
}

export interface MetricsTabProps {
  serverId: number;
}

export function MetricsTab({ serverId }: MetricsTabProps) {
  const visible = useDocumentVisibility();
  const [window, setWindow] = useState<MetricsWindow>("5m");
  const [showHiddenDevices, setShowHiddenDevices] = useState(false);
  const [deviceSelection, setDeviceSelection] = useState<string[]>([]);
  const [interfaceSelection, setInterfaceSelection] = useState<string[]>([]);

  const loadMetrics = useCallback(
    (signal: AbortSignal) => getMetrics(serverId, { window, signal }),
    [serverId, window],
  );
  const { data, error, refreshing, refresh } = usePolling<Metrics>(loadMetrics, {
    enabled: visible,
    intervalMs: METRICS_POLL_INTERVAL_MS,
    reloadKey: window,
  });

  const metrics = data && data.serverId === serverId ? data : null;
  const points = metrics?.points ?? [];
  const lastPoint = points.length > 0 ? points[points.length - 1] : null;
  const latestSampleAt = lastPoint?.receivedAt ?? lastPoint?.collectedAt ?? metrics?.collectedAt ?? null;

  const resetIndices = points.reduce<number[]>((indices, point, index) => {
    if (point.reset) indices.push(index);
    return indices;
  }, []);
  const resetReasons: Record<number, string | null> = {};
  const resetReasonTexts: string[] = [];
  for (const index of resetIndices) {
    const reason = resetReasonLabel(points[index].resetReason) ?? "原因未提供";
    resetReasons[index] = reason;
    if (!resetReasonTexts.includes(reason)) resetReasonTexts.push(reason);
  }

  const cpuData: CpuMetricData | null = metrics?.current?.cpu.data ?? null;
  const memoryData: MemoryMetricData | null = metrics?.current?.memory.data ?? null;
  const diskData: DiskMetricData | null = metrics?.current?.disk.data ?? null;
  const networkData: NetworkMetricData | null = metrics?.current?.network.data ?? null;

  const timestamps = points.map((point) => point.collectedAt);

  const toggleDevice = (name: string) => {
    setDeviceSelection((current) =>
      current.includes(name) ? current.filter((item) => item !== name) : [...current, name],
    );
  };
  const toggleInterface = (name: string) => {
    setInterfaceSelection((current) =>
      current.includes(name) ? current.filter((item) => item !== name) : [...current, name],
    );
  };

  const diskDevices = diskData?.devices ?? [];
  const availableDevices = diskDevices.filter(
    (device) => showHiddenDevices || !device.hidden,
  );
  const chosenDevices = availableDevices.filter((device) =>
    deviceSelection.includes(device.name),
  );
  const activeDevices =
    chosenDevices.length > 0 ? chosenDevices : availableDevices.slice(0, 1);

  const interfaces = networkData?.interfaces ?? [];
  const chosenInterfaces = interfaces.filter((item) => interfaceSelection.includes(item.name));
  const activeInterfaces =
    chosenInterfaces.length > 0
      ? chosenInterfaces
      : interfaces.filter(
          (item) => item.name === networkData?.defaultInterface,
        ).length > 0
        ? interfaces.filter((item) => item.name === networkData?.defaultInterface)
        : interfaces.slice(0, 1);

  const cpuSeries: ChartSeries[] = [
    percentSeries(points, "cpu-total", "总利用率", 0, (point) => point.cpuPercent),
    percentSeries(points, "cpu-user", "用户态", 1, (point) => point.userPercent),
    percentSeries(points, "cpu-system", "系统态", 2, (point) => point.systemPercent),
    percentSeries(points, "cpu-iowait", "IO 等待", 3, (point) => point.iowaitPercent),
  ];
  const memorySeries: ChartSeries[] = [
    percentSeries(points, "memory", "内存使用率", 0, (point) => point.memoryUsedPercent),
    percentSeries(points, "swap", "Swap 使用率", 4, (point) => point.swapUsedPercent),
  ];
  const diskSeries: ChartSeries[] = activeDevices.flatMap((device, index) => [
    {
      key: `disk-${device.name}-read`,
      label: `${device.name} 读`,
      color: chartColor(index * 2),
      values: points.map((point) => point.disk?.[device.name]?.readBytesPerSecond ?? null),
    },
    {
      key: `disk-${device.name}-write`,
      label: `${device.name} 写`,
      color: chartColor(index * 2 + 1),
      values: points.map((point) => point.disk?.[device.name]?.writeBytesPerSecond ?? null),
    },
  ]);
  const networkSeries: ChartSeries[] = activeInterfaces.flatMap((item, index) => [
    {
      key: `net-${item.name}-rx`,
      label: `${item.name} RX`,
      color: chartColor(index * 2),
      values: points.map((point) => point.network?.[item.name]?.rxBytesPerSecond ?? null),
    },
    {
      key: `net-${item.name}-tx`,
      label: `${item.name} TX`,
      color: chartColor(index * 2 + 1),
      values: points.map((point) => point.network?.[item.name]?.txBytesPerSecond ?? null),
    },
  ]);

  const chartCaption = "速率为累计计数差分值；计数器回绕或采样间隔异常时曲线断开并标记原因。";

  return (
    <div className="stack" data-testid="metrics-tab">
      {error ? (
        <ErrorBanner
          title="资源指标采集失败"
          message={error.message}
          onRetry={refresh}
        />
      ) : null}

      {!metrics ? (
        error ? null : <LoadingBlock label="正在采集资源指标…" />
      ) : (
        <>
          <div className="snapshot-meta" aria-live="polite" data-testid="metrics-meta">
            <span data-testid="metrics-sample-interval">
              采样间隔 <strong className="mono">{metrics.sampleIntervalMs} ms</strong>
            </span>
            <span data-testid="metrics-point-count">
              采样点数 <strong className="mono">{points.length}</strong>
            </span>
            <span data-testid="metrics-last-sample">
              最近采样{" "}
              <span title={formatDateTime(latestSampleAt)}>
                {formatRelativeTime(latestSampleAt)}
              </span>
            </span>
            <span className="muted">窗口 {metrics.windowSeconds} 秒</span>
            <StatusBadge tone={SECTION_STATUS_TONES[metrics.status]}>
              {SECTION_STATUS_LABELS[metrics.status]}
            </StatusBadge>
            {metrics.stale ? <StatusBadge tone="warning">非实时</StatusBadge> : null}
            {metrics.requiresSudo ? <StatusBadge tone="warning">需要 sudo</StatusBadge> : null}
            {refreshing ? <span className="muted">刷新中…</span> : null}
          </div>

          <div className="toolbar">
            <div className="segmented" role="group" aria-label="采样窗口">
              {METRICS_WINDOWS.map((option) => (
                <button
                  key={option}
                  type="button"
                  className={`segmented__item${option === window ? " segmented__item--active" : ""}`}
                  aria-pressed={option === window}
                  title={`最近 ${option}`}
                  onClick={() => setWindow(option)}
                >
                  {option}
                </button>
              ))}
            </div>
            <span className="muted">
              每 {METRICS_POLL_INTERVAL_MS / 1000} 秒采样一次，仅在页签可见时刷新。
            </span>
          </div>

          {(["cpu", "memory", "disk", "network", "gpu"] as const).map((key) => {
            const section = metrics.current?.[key];
            if (!section || section.status === "ok") return null;
            const titles: Record<typeof key, string> = {
              cpu: "CPU",
              memory: "内存",
              disk: "磁盘",
              network: "网络",
              gpu: "GPU",
            };
            return (
              <SectionNotice
                key={key}
                status={section.status}
                error={section.error}
                requiresSudo={section.requiresSudo}
                title={titles[key]}
                testId={`metrics-${key}-notice`}
              />
            );
          })}

          {resetReasonTexts.length > 0 ? (
            <div className="banner banner--warning" role="status" data-testid="metrics-reset-reasons">
              <span className="banner__mark" aria-hidden="true">
                !
              </span>
              <div className="banner__body">
                <strong className="banner__title">部分采样点已重建基线</strong>
                <span className="banner__message">
                  计数器回绕或采样间隔异常的点不会绘制为 0：{resetReasonTexts.join("；")}
                </span>
              </div>
            </div>
          ) : null}

          <div className="chart-grid">
            <div className="chart-block">
              <LineChart
                title="CPU 使用率"
                testId="chart-cpu"
                series={cpuSeries}
                timestamps={timestamps}
                yMax={100}
                yFormat={PERCENT_AXIS}
                resetIndices={resetIndices}
                resetReasons={resetReasons}
                ariaLabel="CPU 使用率趋势图，包含总利用率、用户态、系统态与 IO 等待四条曲线"
                caption={chartCaption}
              />
            </div>
            <div className="chart-block">
              <LineChart
                title="内存与 Swap"
                testId="chart-memory"
                series={memorySeries}
                timestamps={timestamps}
                yMax={100}
                yFormat={PERCENT_AXIS}
                resetIndices={resetIndices}
                resetReasons={resetReasons}
                ariaLabel="内存与 Swap 使用率趋势图"
                caption="内存使用率来自 /proc/meminfo；Swap 为专用交换分区使用率。"
              />
            </div>

            <div className="chart-block chart-block--span-2">
              <div className="chart-controls">
                <div className="chip-row" role="group" aria-label="磁盘设备选择">
                  {availableDevices.length === 0 ? (
                    <span className="muted">没有可绘制的块设备</span>
                  ) : (
                    availableDevices.map((device) => {
                      const on = activeDevices.some((item) => item.name === device.name);
                      return (
                        <button
                          key={device.name}
                          type="button"
                          className={`chip${on ? " chip--on" : ""}`}
                          aria-pressed={on}
                          onClick={() => toggleDevice(device.name)}
                        >
                          {device.name}
                          {device.hidden ? <span className="muted"> · 隐藏</span> : null}
                        </button>
                      );
                    })
                  )}
                </div>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={showHiddenDevices}
                    onChange={(event) => setShowHiddenDevices(event.target.checked)}
                  />
                  显示隐藏设备
                </label>
              </div>
              <LineChart
                title="磁盘 I/O"
                testId="chart-disk"
                series={diskSeries}
                timestamps={timestamps}
                yFormat={(value) => formatRate(value)}
                resetIndices={resetIndices}
                resetReasons={resetReasons}
                ariaLabel="磁盘读写速率趋势图，单位为字节每秒"
                caption="单位：字节/秒（自动缩放 B/KiB/MiB/GiB）；隐藏设备默认折叠。"
              />
            </div>

            <div className="chart-block chart-block--span-2">
              <div className="chart-controls">
                <div className="chip-row" role="group" aria-label="网络接口选择">
                  {interfaces.length === 0 ? (
                    <span className="muted">没有可绘制的网络接口</span>
                  ) : (
                    interfaces.map((item) => {
                      const on = activeInterfaces.some((active) => active.name === item.name);
                      return (
                        <button
                          key={item.name}
                          type="button"
                          className={`chip${on ? " chip--on" : ""}`}
                          aria-pressed={on}
                          onClick={() => toggleInterface(item.name)}
                        >
                          {item.name}
                          {item.kind ? <span className="muted"> · {item.kind}</span> : null}
                        </button>
                      );
                    })
                  )}
                </div>
              </div>
              <LineChart
                title="网络 I/O"
                testId="chart-network"
                series={networkSeries}
                timestamps={timestamps}
                yFormat={(value) => formatRate(value)}
                resetIndices={resetIndices}
                resetReasons={resetReasons}
                ariaLabel="网络接口收发速率趋势图，单位为字节每秒"
                caption="仅展示所选接口的 RX/TX 速率；接口级错误与丢包见下表。"
              />
            </div>
          </div>

          <div className="detail-grid">
            <SectionCard title="CPU 负载与逻辑核" testId="metrics-cpu-detail">
              {cpuData ? (
                <DetailList
                  items={[
                    { label: "逻辑核数", value: formatCountOrNa(cpuData.logicalCpus) },
                    {
                      label: "1 分钟负载",
                      value: formatOptionalNumber(cpuData.load1, (value) => value.toFixed(2)),
                    },
                    {
                      label: "5 分钟负载",
                      value: formatOptionalNumber(cpuData.load5, (value) => value.toFixed(2)),
                    },
                    {
                      label: "15 分钟负载",
                      value: formatOptionalNumber(cpuData.load15, (value) => value.toFixed(2)),
                    },
                    { label: "总利用率", value: formatPercentOrNa(cpuData.totalPercent) },
                    { label: "用户态", value: formatPercentOrNa(cpuData.userPercent) },
                    { label: "系统态", value: formatPercentOrNa(cpuData.systemPercent) },
                    { label: "IO 等待", value: formatPercentOrNa(cpuData.iowaitPercent) },
                    { label: "Steal", value: formatPercentOrNa(cpuData.stealPercent) },
                    { label: "空闲", value: formatPercentOrNa(cpuData.idlePercent) },
                  ]}
                />
              ) : (
                <p className="muted">CPU 明细不可用，请查看上方采集状态。</p>
              )}
            </SectionCard>

            <SectionCard title="内存明细" testId="metrics-memory-detail">
              {memoryData ? (
                <DetailList
                  items={[
                    { label: "总内存", value: formatBytesOrNa(memoryData.totalBytes) },
                    { label: "可用", value: formatBytesOrNa(memoryData.availableBytes) },
                    { label: "已用", value: formatBytesOrNa(memoryData.usedBytes) },
                    { label: "使用率", value: formatPercentOrNa(memoryData.usedPercent) },
                    { label: "Buffers", value: formatBytesOrNa(memoryData.buffersBytes) },
                    { label: "Cache", value: formatBytesOrNa(memoryData.cachedBytes) },
                    { label: "Commit", value: formatBytesOrNa(memoryData.commitBytes) },
                    { label: "Swap 总量", value: formatBytesOrNa(memoryData.swapTotalBytes) },
                    { label: "Swap 已用", value: formatBytesOrNa(memoryData.swapUsedBytes) },
                    { label: "Swap 空闲", value: formatBytesOrNa(memoryData.swapFreeBytes) },
                    { label: "Swap in", value: formatRate(memoryData.swapInPerSecond) },
                    { label: "Swap out", value: formatRate(memoryData.swapOutPerSecond) },
                  ]}
                />
              ) : (
                <p className="muted">内存明细不可用，请查看上方采集状态。</p>
              )}
            </SectionCard>
          </div>

          <SectionCard
            title="设备明细"
            testId="metrics-devices"
            className="card--span-2"
            description="隐藏设备（loop、device-mapper 等重复视图）默认不在图表中绘制，但仍列出。"
          >
            {diskData && diskDevices.length > 0 ? (
              <div className="table-wrap table-wrap--tall">
                <table className="data-table data-table--observability">
                  <caption className="sr-only">块设备 I/O 明细</caption>
                  <thead>
                    <tr>
                      <th scope="col">设备</th>
                      <th scope="col">类型</th>
                      <th scope="col">父设备</th>
                      <th scope="col">读</th>
                      <th scope="col">写</th>
                      <th scope="col">读 IOPS</th>
                      <th scope="col">写 IOPS</th>
                      <th scope="col">await</th>
                      <th scope="col">util</th>
                      <th scope="col">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {diskDevices.map((device) => (
                      <tr key={device.name}>
                        <td className="mono">
                          {device.name}
                          {device.hidden ? (
                            <StatusBadge tone="muted" className="badge--inline">
                              隐藏
                            </StatusBadge>
                          ) : null}
                          {device.model ? (
                            <span className="service-description">{device.model}</span>
                          ) : null}
                        </td>
                        <td>{device.kind ?? "N/A"}</td>
                        <td className="mono">{device.parent ?? "N/A"}</td>
                        <td>{formatRate(device.readBytesPerSecond)}</td>
                        <td>{formatRate(device.writeBytesPerSecond)}</td>
                        <td>{formatOptionalNumber(device.readIops, (value) => value.toFixed(1))}</td>
                        <td>{formatOptionalNumber(device.writeIops, (value) => value.toFixed(1))}</td>
                        <td>{formatMs(device.awaitMs)}</td>
                        <td>{formatPercentOrNa(device.utilPercent)}</td>
                        <td>
                          {device.reset ? (
                            <StatusBadge tone="warning">计数器重置</StatusBadge>
                          ) : (
                            <span className="muted">正常</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">没有可用的设备明细。</p>
            )}
          </SectionCard>

          <SectionCard title="挂载点 I/O" testId="metrics-mounts" className="card--span-2">
            {diskData && diskData.mounts.length > 0 ? (
              <div className="table-wrap">
                <table className="data-table data-table--observability">
                  <caption className="sr-only">挂载点 I/O</caption>
                  <thead>
                    <tr>
                      <th scope="col">挂载点</th>
                      <th scope="col">设备</th>
                      <th scope="col">文件系统</th>
                      <th scope="col">读</th>
                      <th scope="col">写</th>
                    </tr>
                  </thead>
                  <tbody>
                    {diskData.mounts.map((mount) => (
                      <tr key={mount.mount}>
                        <td className="mono">{mount.mount}</td>
                        <td className="mono muted">{mount.deviceName ?? mount.device ?? "N/A"}</td>
                        <td>{mount.filesystem ?? "N/A"}</td>
                        <td>{formatRate(mount.readBytesPerSecond)}</td>
                        <td>{formatRate(mount.writeBytesPerSecond)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">没有可用的挂载点 I/O 数据。</p>
            )}
          </SectionCard>

          <SectionCard title="接口明细" testId="metrics-interfaces" className="card--span-2">
            {networkData && interfaces.length > 0 ? (
              <div className="table-wrap table-wrap--tall">
                <table className="data-table data-table--observability">
                  <caption className="sr-only">网络接口明细</caption>
                  <thead>
                    <tr>
                      <th scope="col">接口</th>
                      <th scope="col">类型</th>
                      <th scope="col">状态</th>
                      <th scope="col">速率</th>
                      <th scope="col">地址</th>
                      <th scope="col">RX</th>
                      <th scope="col">TX</th>
                      <th scope="col">错误</th>
                      <th scope="col">丢包</th>
                    </tr>
                  </thead>
                  <tbody>
                    {interfaces.map((item) => (
                      <tr key={item.name}>
                        <td className="mono">
                          {item.name}
                          {item.name === networkData.defaultInterface ? (
                            <StatusBadge tone="info" className="badge--inline">
                              默认
                            </StatusBadge>
                          ) : null}
                        </td>
                        <td>{item.kind ?? "N/A"}</td>
                        <td>
                          <StatusBadge tone={item.state === "up" ? "success" : "muted"}>
                            {item.state ?? "N/A"}
                          </StatusBadge>
                        </td>
                        <td>{formatMbps(item.speedMbps)}</td>
                        <td className="mono">{item.addresses.length > 0 ? item.addresses.join(", ") : "N/A"}</td>
                        <td>{formatRate(item.rxBytesPerSecond)}</td>
                        <td>{formatRate(item.txBytesPerSecond)}</td>
                        <td>
                          {formatOptionalNumber(item.rxErrors, (value) => String(value))} /{" "}
                          {formatOptionalNumber(item.txErrors, (value) => String(value))}
                        </td>
                        <td>
                          {formatOptionalNumber(item.rxDrops, (value) => String(value))} /{" "}
                          {formatOptionalNumber(item.txDrops, (value) => String(value))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">没有可用的接口明细。</p>
            )}
          </SectionCard>
        </>
      )}
    </div>
  );
}
