import { describe, expect, it } from "vitest";
import {
  clampPercent,
  formatBytes,
  formatDuration,
  formatDurationMs,
  formatPercent,
  formatRelativeTime,
} from "./format";

describe("format helpers", () => {
  it("formats byte sizes with binary units and never invents values", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1024)).toBe("1.00 KiB");
    expect(formatBytes(262144)).toBe("256 KiB");
    expect(formatBytes(68719476736)).toBe("64.0 GiB");
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(undefined)).toBe("—");
  });

  it("formats percentages and missing metrics", () => {
    expect(formatPercent(12.5)).toBe("12.5%");
    expect(formatPercent(0)).toBe("0.0%");
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(Number.NaN)).toBe("—");
    expect(clampPercent(140)).toBe(100);
    expect(clampPercent(-2)).toBe(0);
    expect(clampPercent(null)).toBe(0);
  });

  it("formats durations for uptime and synchronous operations", () => {
    expect(formatDuration(864000)).toBe("10 天 0 小时");
    expect(formatDuration(3660)).toBe("1 小时 1 分");
    expect(formatDuration(125)).toBe("2 分 5 秒");
    expect(formatDuration(12)).toBe("12 秒");
    expect(formatDuration(null)).toBe("—");
    expect(formatDurationMs(842)).toBe("842 ms");
    expect(formatDurationMs(2500)).toBe("2.50 s");
  });

  it("formats relative times in Chinese", () => {
    const now = Date.parse("2026-10-25T12:00:00Z");
    expect(formatRelativeTime("2026-10-25T11:59:57Z", now)).toBe("刚刚");
    expect(formatRelativeTime("2026-10-25T11:59:30Z", now)).toBe("30 秒前");
    expect(formatRelativeTime("2026-10-25T11:55:00Z", now)).toBe("5 分钟前");
    expect(formatRelativeTime("2026-10-25T09:00:00Z", now)).toBe("3 小时前");
    expect(formatRelativeTime("2026-10-23T12:00:00Z", now)).toBe("2 天前");
    expect(formatRelativeTime(null, now)).toBe("—");
    expect(formatRelativeTime("not-a-date", now)).toBe("—");
  });
});
