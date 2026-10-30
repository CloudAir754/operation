import { useEffect, useRef, useState } from "react";
import { storageScanEventsUrl } from "../api/client";
import type { ScanDoneEvent, ScanNodesEvent, ScanProgressEvent } from "../api/types";

export interface UseStorageScanEventsOptions {
  enabled?: boolean;
  onProgress?: (event: ScanProgressEvent) => void;
  onNodes?: (event: ScanNodesEvent) => void;
  onDone?: (event: ScanDoneEvent) => void;
}

export interface UseStorageScanEventsResult {
  connected: boolean;
  error: string | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

function parseFrame(raw: string): Record<string, unknown> | null {
  try {
    return asRecord(JSON.parse(raw));
  } catch {
    return null;
  }
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function parseScanProgress(record: Record<string, unknown>): ScanProgressEvent | null {
  if (typeof record.scanId !== "string" || typeof record.status !== "string") return null;
  const progress = numberOrNull(record.progress);
  if (progress === null) return null;
  return {
    type: "progress",
    scanId: record.scanId,
    status: record.status as ScanProgressEvent["status"],
    stage: typeof record.stage === "string" ? record.stage : "",
    progress,
    at: typeof record.at === "string" ? record.at : undefined,
  };
}

/**
 * Subscribes to a storage scan's SSE stream. The stream is closed on unmount,
 * when the scan id changes, and after the terminal `done` frame; malformed
 * frames are ignored instead of tearing the page down.
 */
export function useStorageScanEvents(
  scanId: string | null,
  options: UseStorageScanEventsOptions = {},
): UseStorageScanEventsResult {
  const { enabled = true } = options;
  const handlers = useRef(options);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    handlers.current = options;
  });

  useEffect(() => {
    if (!scanId || !enabled) {
      setConnected(false);
      setError(null);
      return;
    }
    if (typeof EventSource === "undefined") {
      setConnected(false);
      setError("当前环境不支持实时扫描推送，请手动刷新扫描结果。");
      return;
    }

    let source: EventSource;
    try {
      source = new EventSource(storageScanEventsUrl(scanId));
    } catch {
      setConnected(false);
      setError("无法建立扫描事件连接，请手动刷新。");
      return;
    }

    setConnected(false);
    setError(null);

    const handleProgress = (frame: Event) => {
      const record = parseFrame((frame as MessageEvent<string>).data);
      if (!record) return;
      const event = parseScanProgress(record);
      if (event && event.scanId === scanId) handlers.current.onProgress?.(event);
    };

    const handleNodes = (frame: Event) => {
      const record = parseFrame((frame as MessageEvent<string>).data);
      if (!record || !Array.isArray(record.chunk)) return;
      const event: ScanNodesEvent = {
        type: "nodes",
        scanId: typeof record.scanId === "string" ? record.scanId : scanId,
        offset: numberOrNull(record.offset) ?? 0,
        chunk: record.chunk as ScanNodesEvent["chunk"],
        at: typeof record.at === "string" ? record.at : undefined,
      };
      if (event.scanId === scanId) handlers.current.onNodes?.(event);
    };

    const handleDone = (frame: Event) => {
      const record = parseFrame((frame as MessageEvent<string>).data);
      if (record && typeof record.scanId === "string") {
        const event: ScanDoneEvent = {
          type: "done",
          scanId: record.scanId,
          status: (record.status as ScanDoneEvent["status"]) ?? "succeeded",
          progress: numberOrNull(record.progress) ?? 100,
          error: typeof record.error === "string" ? record.error : null,
          nodeCount: numberOrNull(record.nodeCount) ?? 0,
          truncated: record.truncated === true,
          incomplete: record.incomplete === true,
          summary: Array.isArray(record.summary)
            ? (record.summary as ScanDoneEvent["summary"])
            : undefined,
          at: typeof record.at === "string" ? record.at : undefined,
        };
        if (event.scanId === scanId) handlers.current.onDone?.(event);
      }
      source.close();
      setConnected(false);
    };

    const handleOpen = () => {
      setConnected(true);
      setError(null);
    };
    const handleError = () => {
      setConnected(false);
      setError("实时扫描连接已中断，正在自动重连…");
    };

    source.addEventListener("open", handleOpen);
    source.addEventListener("error", handleError);
    source.addEventListener("progress", handleProgress);
    source.addEventListener("nodes", handleNodes);
    source.addEventListener("done", handleDone);

    return () => {
      source.removeEventListener("open", handleOpen);
      source.removeEventListener("error", handleError);
      source.removeEventListener("progress", handleProgress);
      source.removeEventListener("nodes", handleNodes);
      source.removeEventListener("done", handleDone);
      source.close();
    };
  }, [scanId, enabled]);

  return { connected, error };
}
