import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelStorageScan,
  createStorageScan,
  getMounts,
  getStorageScan,
  getStorageScanChildren,
} from "../../api/client";
import { describeError } from "../../api/errors";
import type {
  MountsResponse,
  ScanStatus,
  StorageNode,
  StorageScan,
} from "../../api/types";
import { usePolling } from "../../hooks/usePolling";
import { useStorageScanEvents } from "../../hooks/useStorageScanEvents";
import { formatBytes, formatDateTime, formatRelativeTime } from "../../lib/format";
import {
  NOT_AVAILABLE,
  SCAN_STATUS_LABELS,
  SCAN_STATUS_TONES,
  buildScanTree,
  childrenOf,
  formatBytesOrNa,
  isScanActive,
  layoutTreemap,
  mountLabel,
  nodeSize,
  scanStageLabel,
  treemapColor,
  truncateLabel,
} from "../../lib/observability";
import {
  EmptyState,
  ErrorBanner,
  InfoBanner,
  LoadingBlock,
  SectionCard,
  StatusBadge,
} from "../ui";
import { SectionNotice } from "./SectionNotice";

const MAX_DEPTH_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8];
const SIZE_UNITS = ["B", "KiB", "MiB", "GiB"] as const;
type SizeUnit = (typeof SIZE_UNITS)[number];
const SIZE_MULTIPLIER: Record<SizeUnit, number> = {
  B: 1,
  KiB: 1024,
  MiB: 1024 ** 2,
  GiB: 1024 ** 3,
};

const TREEMAP_WIDTH = 1000;
const TREEMAP_HEIGHT = 420;
const MAX_TABLE_ROWS = 200;

export interface StorageTabProps {
  serverId: number;
  /** Mount sizes from the snapshot, shown in the selector label when known. */
  mountSizes?: Record<string, number>;
  /** Scan created from another tab (用户 → 扫描空间). */
  adoptedScanId?: string | null;
}

export function StorageTab({ serverId, mountSizes = {}, adoptedScanId = null }: StorageTabProps) {
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const loadMounts = useCallback(
    (signal: AbortSignal) => getMounts(serverId, { signal }),
    [serverId],
  );
  const {
    data: mountsData,
    error: mountsError,
    refresh: refreshMounts,
  } = usePolling<MountsResponse>(loadMounts);
  const mounts = mountsData?.mounts ?? [];

  const [selectedMount, setSelectedMount] = useState("");
  const effectiveMount =
    selectedMount ||
    mounts.find((mount) => mount.mount === "/")?.mount ||
    mounts[0]?.mount ||
    "";

  const [oneFileSystem, setOneFileSystem] = useState(true);
  const [followSymlinks, setFollowSymlinks] = useState(false);
  const [maxDepth, setMaxDepth] = useState(3);
  const [minFileSize, setMinFileSize] = useState("0");
  const [sizeUnit, setSizeUnit] = useState<SizeUnit>("B");
  const [extension, setExtension] = useState("");
  const [includeDeletedOpen, setIncludeDeletedOpen] = useState(true);

  const [scan, setScan] = useState<StorageScan | null>(null);
  const [previousScan, setPreviousScan] = useState<StorageScan | null>(null);
  const [liveNodes, setLiveNodes] = useState<StorageNode[]>([]);
  const [liveProgress, setLiveProgress] = useState<{
    status: ScanStatus;
    stage: string;
    progress: number;
  } | null>(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const reloadScan = useCallback(async (scanId: string) => {
    try {
      const result = await getStorageScan(scanId);
      if (!aliveRef.current) return;
      setScan((current) => (current && current.scanId === result.scanId ? result : current));
      setLiveNodes(result.nodes ?? []);
      setLiveProgress({
        status: result.status,
        stage: result.stage,
        progress: result.progress,
      });
    } catch (error) {
      if (!aliveRef.current) return;
      setActionError(describeError(error));
    }
  }, []);

  // Adopt a scan created from the 用户 tab (or a deep link) and follow it live.
  useEffect(() => {
    if (!adoptedScanId) return;
    let cancelled = false;
    void (async () => {
      try {
        const result = await getStorageScan(adoptedScanId);
        if (cancelled || !aliveRef.current) return;
        setPreviousScan(null);
        setScan(result);
        setLiveNodes(result.nodes ?? []);
        setLiveProgress({
          status: result.status,
          stage: result.stage,
          progress: result.progress,
        });
      } catch (error) {
        if (cancelled || !aliveRef.current) return;
        setActionError(describeError(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [adoptedScanId]);

  const active = scan !== null && isScanActive(scan.status);
  const { connected: streamConnected, error: streamError } = useStorageScanEvents(
    active && scan ? scan.scanId : null,
    {
      onProgress: (event) => {
        setLiveProgress({
          status: event.status,
          stage: event.stage,
          progress: event.progress,
        });
        setScan((current) =>
          current && current.scanId === event.scanId
            ? {
                ...current,
                status: event.status,
                stage: event.stage,
                progress: event.progress,
              }
            : current,
        );
      },
      onNodes: (event) => {
        setLiveNodes((current) => mergeNodes(current, event.chunk));
      },
      onDone: (event) => {
        setLiveProgress({
          status: event.status,
          stage: "done",
          progress: event.progress,
        });
        setScan((current) =>
          current && current.scanId === event.scanId
            ? {
                ...current,
                status: event.status,
                stage: "done",
                progress: event.progress,
                error: event.error,
                nodeCount: event.nodeCount,
                truncated: event.truncated,
                incomplete: event.incomplete,
                summaryByUid: event.summary ?? current.summaryByUid,
              }
            : current,
        );
        void reloadScan(event.scanId);
      },
    },
  );

  const minFileSizeBytes = Math.max(
    0,
    Math.round((Number.parseFloat(minFileSize) || 0) * SIZE_MULTIPLIER[sizeUnit]),
  );

  const startScan = async () => {
    if (!effectiveMount || starting) return;
    setStarting(true);
    setActionError(null);
    try {
      const created = await createStorageScan(serverId, {
        mount: effectiveMount,
        oneFileSystem,
        followSymlinks,
        maxDepth,
        minFileSize: minFileSizeBytes,
        extension: extension.trim() ? extension.trim() : null,
        modifiedAfter: null,
        includeDeletedOpen,
      });
      if (!aliveRef.current) return;
      if (scan) setPreviousScan(scan);
      setScan(created);
      setLiveNodes([]);
      setLiveProgress({
        status: created.status,
        stage: created.stage,
        progress: created.progress,
      });
    } catch (error) {
      if (!aliveRef.current) return;
      setActionError(describeError(error));
    } finally {
      if (aliveRef.current) setStarting(false);
    }
  };

  const cancelScan = async () => {
    if (!scan || !isScanActive(scan.status) || cancelling) return;
    setCancelling(true);
    setActionError(null);
    try {
      const result = await cancelStorageScan(scan.scanId);
      if (!aliveRef.current) return;
      setScan(result);
      setLiveProgress({
        status: result.status,
        stage: result.stage,
        progress: result.progress,
      });
    } catch (error) {
      if (!aliveRef.current) return;
      setActionError(describeError(error));
    } finally {
      if (aliveRef.current) setCancelling(false);
    }
  };

  const displayedScan = scan;
  const showScanResult = displayedScan !== null && !isScanActive(displayedScan.status);
  const showLivePreview = active && liveNodes.length > 0;

  return (
    <div className="stack" data-testid="storage-tab">
      {mountsError ? (
        <ErrorBanner
          title="无法读取挂载点"
          message={mountsError.message}
          onRetry={refreshMounts}
        />
      ) : null}
      {actionError ? <ErrorBanner title="磁盘扫描操作失败" message={actionError} /> : null}

      <SectionCard
        title="新建扫描"
        testId="storage-form"
        className="card--span-2"
        actions={
          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={refreshMounts}
            disabled={starting}
          >
            刷新挂载点
          </button>
        }
      >
        {!mountsData ? (
          mountsError ? null : <LoadingBlock label="正在读取挂载点…" />
        ) : mountsData.status !== "ok" ? (
          <SectionNotice
            status={mountsData.status}
            error={mountsData.error}
            requiresSudo={mountsData.requiresSudo}
            testId="storage-mounts-notice"
            title="挂载点"
          />
        ) : mounts.length === 0 ? (
          <EmptyState title="没有发现可扫描的挂载点" />
        ) : (
          <div className="stack-sm">
            <div className="scan-form">
              <label className="field">
                <span>挂载点</span>
                <select
                  value={effectiveMount}
                  onChange={(event) => setSelectedMount(event.target.value)}
                  disabled={starting}
                >
                  {mounts.map((mount) => (
                    <option key={mount.mount} value={mount.mount}>
                      {mountLabel(mount, mountSizes[mount.mount])}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field">
                <span>最大深度</span>
                <select
                  aria-label="最大深度"
                  value={maxDepth}
                  onChange={(event) => setMaxDepth(Number(event.target.value))}
                  disabled={starting}
                >
                  {MAX_DEPTH_OPTIONS.map((depth) => (
                    <option key={depth} value={depth}>
                      {depth}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field">
                <span>最小文件大小</span>
                <span className="field-group">
                  <input
                    type="number"
                    min={0}
                    step={1}
                    value={minFileSize}
                    aria-label="最小文件大小"
                    onChange={(event) => setMinFileSize(event.target.value)}
                    disabled={starting}
                  />
                  <select
                    aria-label="文件大小单位"
                    value={sizeUnit}
                    onChange={(event) => setSizeUnit(event.target.value as SizeUnit)}
                    disabled={starting}
                  >
                    {SIZE_UNITS.map((unit) => (
                      <option key={unit} value={unit}>
                        {unit}
                      </option>
                    ))}
                  </select>
                </span>
              </label>

              <label className="field">
                <span>扩展名过滤</span>
                <input
                  type="text"
                  value={extension}
                  placeholder=".log（留空不过滤）"
                  aria-label="扩展名过滤"
                  onChange={(event) => setExtension(event.target.value)}
                  disabled={starting}
                />
              </label>
            </div>

            <div className="scan-options">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={oneFileSystem}
                  onChange={(event) => setOneFileSystem(event.target.checked)}
                  disabled={starting}
                />
                单文件系统（不跨入其他挂载点）
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={followSymlinks}
                  onChange={(event) => setFollowSymlinks(event.target.checked)}
                  disabled={starting}
                />
                跟随符号链接
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={includeDeletedOpen}
                  onChange={(event) => setIncludeDeletedOpen(event.target.checked)}
                  disabled={starting}
                />
                统计已删除但仍打开的文件
              </label>
            </div>

            {followSymlinks ? (
              <InfoBanner tone="warning">
                跟随符号链接可能进入其他文件系统或形成循环，扫描更慢且结果可能跨设备。
              </InfoBanner>
            ) : null}

            <div className="form-actions">
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => void startScan()}
                disabled={starting || !effectiveMount}
                aria-busy={starting}
              >
                {starting ? "创建中…" : "开始扫描"}
              </button>
              <span className="muted">
                扫描只读取挂载点内的元数据；不可读目录会保留权限错误并标记结果不完整。
              </span>
            </div>
          </div>
        )}
      </SectionCard>

      {showLivePreview || active ? (
        <SectionCard
          title="扫描进度"
          testId="storage-progress"
          className="card--span-2"
          actions={
            active ? (
              <button
                type="button"
                className="btn btn--danger-ghost btn--small"
                onClick={() => void cancelScan()}
                disabled={cancelling}
              >
                {cancelling ? "取消中…" : "取消扫描"}
              </button>
            ) : null
          }
        >
          {scan ? (
            <ScanProgress
              scan={scan}
              liveProgress={liveProgress}
              connected={streamConnected}
              streamError={streamError}
              nodeCount={liveNodes.length}
            />
          ) : null}
          {showLivePreview && scan ? (
            <div className="stack-sm">
              <p className="subheading">扫描中预览</p>
              <ScanResultView scan={scan} nodes={liveNodes} partial />
            </div>
          ) : null}
        </SectionCard>
      ) : null}

      {showScanResult && displayedScan ? (
        <SectionCard
          title="扫描结果"
          testId="storage-result"
          className="card--span-2"
          description={`${displayedScan.mount} · 创建于 ${formatDateTime(displayedScan.createdAt)}`}
        >
          <ScanResultView
            scan={displayedScan}
            nodes={liveNodes.length > 0 ? liveNodes : undefined}
          />
        </SectionCard>
      ) : null}

      {previousScan && active ? (
        <SectionCard
          title="上次扫描结果"
          testId="storage-previous-result"
          className="card--span-2"
          description={`${previousScan.mount} · 完成于 ${
            previousScan.finishedAt ? formatDateTime(previousScan.finishedAt) : NOT_AVAILABLE
          }`}
        >
          <ScanResultView scan={previousScan} />
        </SectionCard>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Progress                                                                   */
/* -------------------------------------------------------------------------- */

function ScanProgress({
  scan,
  liveProgress,
  connected,
  streamError,
  nodeCount,
}: {
  scan: StorageScan;
  liveProgress: { status: ScanStatus; stage: string; progress: number } | null;
  connected: boolean;
  streamError: string | null;
  nodeCount: number;
}) {
  const status = liveProgress?.status ?? scan.status;
  const stage = liveProgress?.stage ?? scan.stage;
  const progress = Math.min(100, Math.max(0, liveProgress?.progress ?? scan.progress));
  return (
    <div className="scan-progress" role="status" aria-live="polite" data-testid="storage-progress-status">
      <div className="scan-progress__head">
        <span>
          {SCAN_STATUS_LABELS[status] ?? status} · {scanStageLabel(stage)}
        </span>
        <span className="mono" data-testid="storage-progress-value">
          {progress}%
        </span>
      </div>
      <div
        className="usage__track"
        role="progressbar"
        aria-label="扫描进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
        aria-valuetext={`${progress}%`}
      >
        <div
          className={`usage__fill usage__fill--${status === "running" ? "ok" : "warning"}`}
          style={{ width: `${progress}%` }}
        />
      </div>
      <p className="muted">
        已接收 {nodeCount} 个节点
        {connected ? " · 实时连接已建立" : ""}
        {streamError ? ` · ${streamError}` : ""}
      </p>
      {scan.error ? <p className="text-danger">原因：{scan.error}</p> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Results                                                                    */
/* -------------------------------------------------------------------------- */

export function ScanResultView({
  scan,
  nodes,
  partial = false,
}: {
  scan: StorageScan;
  nodes?: StorageNode[];
  partial?: boolean;
}) {
  const allNodes = nodes ?? scan.nodes;
  const tree = useMemo(() => buildScanTree(allNodes, scan.mount), [allNodes, scan.mount]);
  const [currentPath, setCurrentPath] = useState(scan.mount);

  useEffect(() => {
    setCurrentPath(scan.mount);
  }, [scan.scanId, scan.mount]);

  const rootPath = tree.byPath.has(currentPath) ? currentPath : tree.root?.path ?? scan.mount;
  const rootNode = tree.byPath.get(rootPath) ?? null;
  const currentChildren = childrenOf(tree, rootPath);
  const crumbs = breadcrumbTrail(scan.mount, rootPath);
  const rects = useMemo(
    () =>
      layoutTreemap(currentChildren, {
        x: 0,
        y: 0,
        width: TREEMAP_WIDTH,
        height: TREEMAP_HEIGHT,
      }),
    [currentChildren],
  );

  const status = SCAN_STATUS_LABELS[scan.status] ?? scan.status;
  const failed = scan.status === "failed" || scan.status === "cancelled" || scan.status === "interrupted";

  return (
    <div className="stack-sm">
      {failed ? (
        <InfoBanner tone="warning">
          扫描{status}：{scan.error ?? "未提供原因"}
        </InfoBanner>
      ) : null}

      <div className="scan-summary" data-testid="storage-summary">
        <span>
          节点 <strong className="mono">{scan.nodeCount || allNodes.length}</strong>
        </span>
        <span>
          跨设备 <strong className="mono">{scan.crossDeviceCount}</strong>
        </span>
        <span>
          已删除打开 <strong className="mono">{formatBytes(scan.deletedOpenBytes)}</strong> ·{" "}
          {scan.deletedOpenCount} 个
        </span>
        <StatusBadge tone={SCAN_STATUS_TONES[scan.status]}>{status}</StatusBadge>
        {scan.truncated ? (
          <span data-testid="storage-truncated">
            <StatusBadge tone="warning">结果已截断</StatusBadge>
          </span>
        ) : null}
        {scan.incomplete ? (
          <span data-testid="storage-incomplete">
            <StatusBadge tone="warning">结果不完整</StatusBadge>
          </span>
        ) : null}
        {scan.finishedAt ? (
          <span className="muted">完成于 {formatRelativeTime(scan.finishedAt)}</span>
        ) : null}
      </div>

      {allNodes.length === 0 ? (
        <EmptyState
          title={partial ? "正在等待扫描数据" : "扫描没有返回节点"}
          description="不可读目录或空挂载点都会导致结果为空。"
        />
      ) : (
        <>
          <nav className="breadcrumb" aria-label="空间树路径">
            {crumbs.map((crumb, index) => (
              <span key={crumb.path} className="breadcrumb__item">
                {index > 0 ? <span className="breadcrumb__sep">/</span> : null}
                <button
                  type="button"
                  className="link-button mono"
                  onClick={() => setCurrentPath(crumb.path)}
                  aria-current={crumb.path === rootPath ? "location" : undefined}
                >
                  {crumb.label}
                </button>
              </span>
            ))}
            <span className="muted">
              · {rootNode ? `${formatBytes(nodeSize(rootNode))} 已分配` : "根节点尚未返回"}
            </span>
          </nav>

          <svg
            className="treemap"
            viewBox={`0 0 ${TREEMAP_WIDTH} ${TREEMAP_HEIGHT}`}
            role="img"
            aria-label={`磁盘空间树：${rootPath}，共 ${currentChildren.length} 个直接子项`}
            data-testid="treemap"
          >
            {rects.length === 0 ? (
              <text x={24} y={48} className="treemap__empty">
                该目录没有可绘制的子项
              </text>
            ) : null}
            {rects.map((rect) => {
              const isDir = rect.node.kind === "dir";
              const label = truncateLabel(rect.node.name, Math.max(4, Math.floor(rect.width / 8)));
              return (
                <g
                  key={rect.node.path}
                  className={`treemap__cell${isDir ? " treemap__cell--dir" : ""}`}
                  data-testid="treemap-cell"
                  role={isDir ? "button" : undefined}
                  tabIndex={isDir ? 0 : undefined}
                  aria-label={`${rect.node.name}，${formatBytes(nodeSize(rect.node))}${
                    isDir ? "，进入目录" : ""
                  }`}
                  onClick={isDir ? () => setCurrentPath(rect.node.path) : undefined}
                  onKeyDown={
                    isDir
                      ? (event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            setCurrentPath(rect.node.path);
                          }
                        }
                      : undefined
                  }
                >
                  <rect
                    x={rect.x}
                    y={rect.y}
                    width={Math.max(0, rect.width - 2)}
                    height={Math.max(0, rect.height - 2)}
                    fill={treemapColor(rect.depth)}
                    stroke="#0b0f14"
                    strokeWidth={1}
                  />
                  {rect.width > 46 && rect.height > 34 ? (
                    <>
                      <text x={rect.x + 7} y={rect.y + 19} className="treemap__label">
                        {label}
                      </text>
                      <text x={rect.x + 7} y={rect.y + 35} className="treemap__size">
                        {formatBytes(nodeSize(rect.node))}
                      </text>
                    </>
                  ) : null}
                  <title>{`${rect.node.path} · ${formatBytes(nodeSize(rect.node))}`}</title>
                </g>
              );
            })}
          </svg>

          <p className="muted">
            点击目录矩形或名称可下钻；颜色仅用于区分层级深度。
          </p>

          {!partial ? (
            <DirectoryTable
              scanId={scan.scanId}
              nodes={currentChildren}
              onDrill={(path) => setCurrentPath(path)}
            />
          ) : null}

          {!partial && scan.largestFiles.length > 0 ? (
            <LargestFilesTable files={scan.largestFiles} />
          ) : null}

          {!partial && scan.summaryByUid.length > 0 ? (
            <UidSummaryTable summary={scan.summaryByUid} />
          ) : null}
        </>
      )}
    </div>
  );
}

function breadcrumbTrail(mount: string, path: string): Array<{ label: string; path: string }> {
  const trail = [{ label: mount, path: mount }];
  if (path === mount) return trail;
  const base = mount.endsWith("/") ? mount.slice(0, -1) : mount;
  const prefix = `${base}/`;
  if (!path.startsWith(prefix) && path !== base) return trail;
  const relative = path.slice(prefix.length);
  let current = base;
  for (const segment of relative.split("/").filter(Boolean)) {
    current = `${current}/${segment}`;
    trail.push({ label: segment, path: current });
  }
  return trail;
}

function NodeStatusBadges({ node }: { node: StorageNode }) {
  return (
    <>
      {node.incomplete ? <StatusBadge tone="warning">不完整</StatusBadge> : null}
      {node.unscanned ? <StatusBadge tone="muted">未扫描</StatusBadge> : null}
      {node.deletedOpen ? <StatusBadge tone="info">已删除打开</StatusBadge> : null}
      {node.hardlinkDuplicateOf ? <StatusBadge tone="muted">硬链接</StatusBadge> : null}
      {node.error ? (
        <StatusBadge tone="danger" title={node.error}>
          权限错误
        </StatusBadge>
      ) : null}
    </>
  );
}

function DirectoryTable({
  scanId,
  nodes,
  onDrill,
}: {
  scanId: string;
  nodes: StorageNode[];
  onDrill: (path: string) => void;
}) {
  const visible = nodes.slice(0, MAX_TABLE_ROWS);
  return (
    <div className="stack-sm">
      <p className="subheading">目录明细</p>
      {nodes.length === 0 ? (
        <EmptyState title="当前目录没有子项" />
      ) : (
        <div className="table-wrap table-wrap--tall">
          <table className="data-table data-table--observability">
            <caption className="sr-only">目录明细</caption>
            <thead>
              <tr>
                <th scope="col">名称</th>
                <th scope="col">已分配</th>
                <th scope="col">表观大小</th>
                <th scope="col">文件</th>
                <th scope="col">目录</th>
                <th scope="col">UID</th>
                <th scope="col">状态</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((node) => (
                <ScanNodeRow key={node.path} scanId={scanId} node={node} depth={0} onDrill={onDrill} />
              ))}
            </tbody>
          </table>
          {nodes.length > visible.length ? (
            <p className="muted">仅显示前 {visible.length} 项，共 {nodes.length} 项。</p>
          ) : null}
        </div>
      )}
    </div>
  );
}

function ScanNodeRow({
  scanId,
  node,
  depth,
  onDrill,
}: {
  scanId: string;
  node: StorageNode;
  depth: number;
  onDrill: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<StorageNode[] | null>(node.children ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isDir = node.kind === "dir";

  const toggle = async () => {
    if (!isDir) return;
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (children !== null || loading) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getStorageScanChildren(scanId, node.path);
      setChildren(result);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <tr data-testid={`storage-row-${node.path}`}>
        <td style={{ paddingLeft: `${11 + depth * 16}px` }}>
          <span className="row-inline">
            {isDir ? (
              <button
                type="button"
                className="btn btn--tiny btn--ghost"
                aria-expanded={open}
                aria-label={`${open ? "折叠" : "展开"} ${node.name}`}
                onClick={() => void toggle()}
              >
                {open ? "▾" : "▸"}
              </button>
            ) : (
              <span className="disclosure" aria-hidden="true">
                ·
              </span>
            )}
            <button
              type="button"
              className="link-button mono"
              onClick={() => {
                if (isDir) onDrill(node.path);
              }}
              disabled={!isDir}
              title={node.path}
            >
              {node.name}
            </button>
            <NodeStatusBadges node={node} />
          </span>
        </td>
        <td>{formatBytesOrNa(node.allocatedBytes)}</td>
        <td>{formatBytesOrNa(node.apparentBytes)}</td>
        <td>{node.fileCount ?? NOT_AVAILABLE}</td>
        <td>{node.directoryCount ?? NOT_AVAILABLE}</td>
        <td className="mono">{node.uid ?? NOT_AVAILABLE}</td>
        <td>{node.kind}</td>
      </tr>
      {open && loading ? (
        <tr>
          <td colSpan={7} style={{ paddingLeft: `${27 + depth * 16}px` }}>
            正在读取子目录…
          </td>
        </tr>
      ) : null}
      {open && error ? (
        <tr>
          <td colSpan={7} className="text-danger" style={{ paddingLeft: `${27 + depth * 16}px` }}>
            {error}
          </td>
        </tr>
      ) : null}
      {open && children
        ? children.slice(0, MAX_TABLE_ROWS).map((child) => (
            <ScanNodeRow
              key={child.path}
              scanId={scanId}
              node={child}
              depth={depth + 1}
              onDrill={onDrill}
            />
          ))
        : null}
    </>
  );
}

function LargestFilesTable({ files }: { files: StorageNode[] }) {
  return (
    <div className="stack-sm">
      <p className="subheading">最大文件</p>
      <div className="table-wrap table-wrap--tall">
        <table className="data-table data-table--observability" data-testid="storage-largest-files">
          <caption className="sr-only">最大文件</caption>
          <thead>
            <tr>
              <th scope="col">路径</th>
              <th scope="col">已分配</th>
              <th scope="col">表观大小</th>
              <th scope="col">UID</th>
              <th scope="col">修改时间</th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file.path}>
                <td className="mono cell-clamp" title={file.path}>
                  {file.path}
                </td>
                <td>{formatBytesOrNa(file.allocatedBytes)}</td>
                <td>{formatBytesOrNa(file.apparentBytes)}</td>
                <td className="mono">{file.uid ?? NOT_AVAILABLE}</td>
                <td>{file.modifiedAt ? formatDateTime(file.modifiedAt) : NOT_AVAILABLE}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function UidSummaryTable({ summary }: { summary: StorageScan["summaryByUid"] }) {
  return (
    <div className="stack-sm">
      <p className="subheading">按 UID 归属</p>
      <div className="table-wrap">
        <table className="data-table data-table--observability" data-testid="storage-uid-summary">
          <caption className="sr-only">按 UID 的空间归属</caption>
          <thead>
            <tr>
              <th scope="col">UID</th>
              <th scope="col">已分配</th>
              <th scope="col">表观大小</th>
              <th scope="col">文件数</th>
              <th scope="col">目录数</th>
              <th scope="col">状态</th>
            </tr>
          </thead>
          <tbody>
            {summary.map((row) => (
              <tr key={row.uid}>
                <td className="mono">{row.uid}</td>
                <td>{formatBytesOrNa(row.allocatedBytes)}</td>
                <td>{formatBytesOrNa(row.apparentBytes)}</td>
                <td>{row.fileCount ?? NOT_AVAILABLE}</td>
                <td>{row.directoryCount ?? NOT_AVAILABLE}</td>
                <td>
                  {row.incomplete ? (
                    <StatusBadge tone="warning">不完整</StatusBadge>
                  ) : (
                    <StatusBadge tone="success">完整</StatusBadge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* -------------------------------------------------------------------------- */

function mergeNodes(current: StorageNode[], chunk: StorageNode[]): StorageNode[] {
  const seen = new Set(current.map((node) => node.path));
  const merged = [...current];
  for (const node of chunk) {
    if (seen.has(node.path)) continue;
    seen.add(node.path);
    merged.push(node);
  }
  return merged;
}
