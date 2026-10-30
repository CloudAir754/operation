/**
 * Display helpers for the stage-3 observability surfaces.
 *
 * Everything here is presentation-only: the backend already localises probe
 * errors and reset reasons, so unknown strings are shown verbatim instead of
 * being coerced into zero or a fabricated label.
 */

import type { MetricsWindow, ScanStatus, StorageNode, StorageNodeKind } from "../api/types";
import type { Tone } from "./labels";
import { formatBytes, formatPercent } from "./format";

/** Value used whenever a probe did not return a number. Never "0". */
export const NOT_AVAILABLE = "N/A";

export const METRICS_POLL_INTERVAL_MS = 2000;

export const METRICS_WINDOWS: readonly MetricsWindow[] = ["1m", "5m", "15m", "1h"];

export const METRICS_WINDOW_LABELS: Record<MetricsWindow, string> = {
  "1m": "1 分钟",
  "5m": "5 分钟",
  "15m": "15 分钟",
  "1h": "1 小时",
};

export const METRICS_WINDOW_SECONDS: Record<MetricsWindow, number> = {
  "1m": 60,
  "5m": 300,
  "15m": 900,
  "1h": 3600,
};

export function isMetricsWindow(value: string | null | undefined): value is MetricsWindow {
  return value !== null && value !== undefined && (METRICS_WINDOWS as readonly string[]).includes(value);
}

export const SCAN_STATUS_LABELS: Record<ScanStatus, string> = {
  queued: "排队中",
  running: "扫描中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

export const SCAN_STATUS_TONES: Record<ScanStatus, Tone> = {
  queued: "neutral",
  running: "info",
  succeeded: "success",
  failed: "danger",
  cancelled: "muted",
  interrupted: "warning",
};

export function isScanFinished(status: ScanStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled" || status === "interrupted";
}

export function isScanActive(status: ScanStatus): boolean {
  return status === "queued" || status === "running";
}

const SCAN_STAGE_LABELS: Record<string, string> = {
  queued: "排队中",
  scanning: "扫描目录",
  aggregating: "汇总结果",
  chunking: "推送结果",
  done: "完成",
  cancelled: "已取消",
  failed: "失败",
  interrupted: "已中断",
};

export function scanStageLabel(stage: string | null | undefined): string {
  if (!stage) return NOT_AVAILABLE;
  return SCAN_STAGE_LABELS[stage] ?? stage;
}

/**
 * Known counter-reset reasons. The backend emits localised Chinese reasons
 * (e.g. "计数器回绕", "采样间隔过短") and may prefix them with the device name;
 * unknown text is passed through unchanged.
 */
const RESET_REASON_LABELS: Record<string, string> = {
  counter_wrap: "计数器回绕",
  counter_reset: "计数器回绕",
  interval_too_short: "采样间隔过短",
  interval_too_long: "采样间隔过长",
  missing_previous: "缺少上一次采样",
  baseline: "基线重建",
  "计数器回绕": "计数器回绕",
  "采样间隔过短": "采样间隔过短",
  "采样间隔过长": "采样间隔过长",
  "缺少上一次采样": "缺少上一次采样",
  "基线重建": "基线重建",
};

export function resetReasonLabel(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const trimmed = reason.trim();
  if (!trimmed) return null;
  const direct = RESET_REASON_LABELS[trimmed];
  if (direct) return direct;
  // "nvme0n1: 计数器回绕; eth0: 采样间隔过短" — map each segment.
  return trimmed
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const separator = part.indexOf(":");
      if (separator <= 0) return RESET_REASON_LABELS[part] ?? part;
      const device = part.slice(0, separator).trim();
      const text = part.slice(separator + 1).trim();
      return `${device}：${RESET_REASON_LABELS[text] ?? text}`;
    })
    .join("；");
}

export const STORAGE_NODE_KIND_LABELS: Record<StorageNodeKind, string> = {
  dir: "目录",
  file: "文件",
  symlink: "链接",
  other: "其他",
};

/** `N/A` for missing numbers, formatted value otherwise. */
export function formatOptionalNumber(
  value: number | null | undefined,
  formatter: (input: number) => string,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NOT_AVAILABLE;
  return formatter(value);
}

export function formatPercentOrNa(value: number | null | undefined, digits = 1): string {
  return formatOptionalNumber(value, (input) => formatPercent(input, digits));
}

export function formatBytesOrNa(value: number | null | undefined): string {
  return formatOptionalNumber(value, (input) => formatBytes(input));
}

/** Byte-per-second rate with automatic B/KiB/MiB/GiB scaling. */
export function formatRate(value: number | null | undefined): string {
  return formatOptionalNumber(value, (input) => `${formatBytes(input)}/s`);
}

export function formatMiB(value: number | null | undefined): string {
  return formatOptionalNumber(value, (input) => `${Math.round(input)} MiB`);
}

export function formatCountOrNa(value: number | null | undefined): string {
  return formatOptionalNumber(value, (input) => String(input));
}

export function formatMs(value: number | null | undefined): string {
  return formatOptionalNumber(value, (input) => `${input.toFixed(input >= 10 ? 0 : 1)} ms`);
}

export function formatMbps(value: number | null | undefined): string {
  return formatOptionalNumber(value, (input) => `${input} Mbps`);
}

/** Child lists keyed by parent path, built from the flat `nodes` array. */
export interface ScanTree {
  byPath: Map<string, StorageNode>;
  childrenByParent: Map<string, StorageNode[]>;
  root: StorageNode | null;
}

export function buildScanTree(nodes: StorageNode[], mount?: string): ScanTree {
  const byPath = new Map<string, StorageNode>();
  const childrenByParent = new Map<string, StorageNode[]>();
  for (const node of nodes) {
    byPath.set(node.path, node);
  }
  for (const node of nodes) {
    const parent = node.parentPath;
    if (!parent) continue;
    const list = childrenByParent.get(parent) ?? [];
    list.push(node);
    childrenByParent.set(parent, list);
  }
  for (const list of childrenByParent.values()) {
    list.sort(compareNodesBySize);
  }
  const root = (mount ? byPath.get(mount) : undefined) ?? nodes.find((node) => !node.parentPath) ?? null;
  return { byPath, childrenByParent, root };
}

export function compareNodesBySize(left: StorageNode, right: StorageNode): number {
  const leftSize = left.allocatedBytes ?? left.apparentBytes ?? 0;
  const rightSize = right.allocatedBytes ?? right.apparentBytes ?? 0;
  if (rightSize !== leftSize) return rightSize - leftSize;
  return left.name.localeCompare(right.name);
}

export function nodeSize(node: StorageNode): number {
  return Math.max(0, node.allocatedBytes ?? node.apparentBytes ?? 0);
}

export function childrenOf(tree: ScanTree, parentPath: string): StorageNode[] {
  return tree.childrenByParent.get(parentPath) ?? [];
}

export interface TreemapRect {
  node: StorageNode;
  x: number;
  y: number;
  width: number;
  height: number;
  depth: number;
}

/**
 * Deterministic binary-slice treemap. Rectangles are laid out by splitting the
 * remaining area in the longer direction, which keeps aspect ratios usable for
 * directory listings without pulling in a chart library.
 */
export function layoutTreemap(
  nodes: StorageNode[],
  region: { x: number; y: number; width: number; height: number },
  depth = 0,
): TreemapRect[] {
  const items = nodes
    .filter((node) => nodeSize(node) > 0)
    .slice(0, 60)
    .sort(compareNodesBySize);
  const rects: TreemapRect[] = [];
  split(items, region, depth, rects);
  return rects;
}

function split(
  items: StorageNode[],
  region: { x: number; y: number; width: number; height: number },
  depth: number,
  out: TreemapRect[],
): void {
  if (items.length === 0 || region.width <= 0 || region.height <= 0) return;
  if (items.length === 1) {
    out.push({ node: items[0], ...region, depth });
    return;
  }
  const total = items.reduce((sum, node) => sum + nodeSize(node), 0);
  if (total <= 0) return;

  let accumulated = 0;
  let splitIndex = 1;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let index = 1; index < items.length; index += 1) {
    accumulated += nodeSize(items[index - 1]);
    const distance = Math.abs(accumulated - total / 2);
    if (distance < bestDistance) {
      bestDistance = distance;
      splitIndex = index;
    }
  }
  const first = items.slice(0, splitIndex);
  const second = items.slice(splitIndex);
  if (second.length === 0) {
    out.push({ node: first[0], ...region, depth });
    return;
  }
  const firstWeight =
    first.reduce((sum, node) => sum + nodeSize(node), 0) / total;
  const ratio = Math.min(0.92, Math.max(0.08, firstWeight));

  if (region.width >= region.height) {
    const firstWidth = region.width * ratio;
    split(first, { ...region, width: firstWidth }, depth + 1, out);
    split(
      second,
      { x: region.x + firstWidth, y: region.y, width: region.width - firstWidth, height: region.height },
      depth + 1,
      out,
    );
  } else {
    const firstHeight = region.height * ratio;
    split(first, { ...region, height: firstHeight }, depth + 1, out);
    split(
      second,
      { x: region.x, y: region.y + firstHeight, width: region.width, height: region.height - firstHeight },
      depth + 1,
      out,
    );
  }
}

const TREEMAP_COLORS = [
  "#1d4ed8",
  "#0ea5e9",
  "#0d9488",
  "#15803d",
  "#a16207",
  "#9a3412",
  "#7e22ce",
  "#be185d",
];

export function treemapColor(depth: number): string {
  return TREEMAP_COLORS[depth % TREEMAP_COLORS.length];
}

export function truncateLabel(label: string, maxLength = 18): string {
  if (label.length <= maxLength) return label;
  return `${label.slice(0, Math.max(1, maxLength - 1))}…`;
}

/** Join a mount path with a child segment without doubling slashes. */
export function joinPath(base: string, segment: string): string {
  if (base.endsWith("/")) return `${base}${segment}`;
  return `${base}/${segment}`;
}

/** Mount label used in selectors: `mount · filesystem · size`. */
export function mountLabel(
  mount: { mount: string; filesystem?: string | null },
  totalBytes?: number,
): string {
  const parts = [mount.mount];
  if (mount.filesystem) parts.push(mount.filesystem);
  if (totalBytes && Number.isFinite(totalBytes)) parts.push(formatBytes(totalBytes));
  return parts.join(" · ");
}

/** Palette used by the SVG line charts (readable on the dark theme). */
export const CHART_COLORS = [
  "#38bdf8",
  "#34d399",
  "#fbbf24",
  "#f87171",
  "#a78bfa",
  "#22d3ee",
  "#f472b6",
  "#a3e635",
] as const;

export function chartColor(index: number): string {
  return CHART_COLORS[index % CHART_COLORS.length];
}
