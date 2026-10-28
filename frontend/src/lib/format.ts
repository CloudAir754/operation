/**
 * Display helpers. All functions are pure and return "—" (or "N/A" where the
 * contract demands it) instead of inventing a value.
 */

const EM_DASH = "—";

export function formatBytes(value: number | null | undefined, fractionDigits?: number): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) {
    return EM_DASH;
  }
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
  let size = value / 1024;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits =
    fractionDigits ?? (size >= 100 ? 0 : size >= 10 ? 1 : 2);
  return `${size.toFixed(digits)} ${units[unitIndex]}`;
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EM_DASH;
  return `${value.toFixed(digits)}%`;
}

/** Human readable duration from a value in seconds. */
export function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || !Number.isFinite(totalSeconds)) {
    return EM_DASH;
  }
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const restSeconds = seconds % 60;

  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${restSeconds} 秒`;
  return `${restSeconds} 秒`;
}

/** Duration for synchronous operations measured in milliseconds. */
export function formatDurationMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EM_DASH;
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value >= 10000 ? 1 : 2)} s`;
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EM_DASH;
  return String(value);
}

export function formatRelativeTime(
  input: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!input) return EM_DASH;
  const timestamp = Date.parse(input);
  if (!Number.isFinite(timestamp)) return EM_DASH;
  const diffSeconds = Math.max(0, Math.floor((now - timestamp) / 1000));

  if (diffSeconds < 5) return "刚刚";
  if (diffSeconds < 60) return `${diffSeconds} 秒前`;
  const minutes = Math.floor(diffSeconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} 个月前`;
  return `${Math.floor(months / 12)} 年前`;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** Local `YYYY-MM-DD HH:mm:ss` — stable across test environments. */
export function formatDateTime(input: string | null | undefined): string {
  if (!input) return EM_DASH;
  const timestamp = Date.parse(input);
  if (!Number.isFinite(timestamp)) return EM_DASH;
  const date = new Date(timestamp);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function formatClock(input: string | null | undefined): string {
  if (!input) return EM_DASH;
  const timestamp = Date.parse(input);
  if (!Number.isFinite(timestamp)) return EM_DASH;
  const date = new Date(timestamp);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function formatSource(source: string): string {
  if (source === "ssh_config") return "SSH 配置";
  if (source === "manual") return "手动添加";
  return source || EM_DASH;
}

export function truncate(value: string, maxLength = 120): string {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength - 1)}…`;
}

/** Clamp a usage percentage into 0..100 for progress bars. */
export function clampPercent(value: number | null | undefined): number {
  if (value === null || value === undefined || !Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, value));
}
