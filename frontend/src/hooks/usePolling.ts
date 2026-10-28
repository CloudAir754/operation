import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { ApiError } from "../api/errors";
import { isAbortError, normalizeApiError } from "../api/errors";

export interface UsePollingOptions {
  /** Fixed refresh interval in ms. 0 / omitted disables polling. */
  intervalMs?: number;
  enabled?: boolean;
  /** Changing this value forces an immediate reload (filters, refresh flags). */
  reloadKey?: string | number;
}

export interface UsePollingResult<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  refreshing: boolean;
  /** Epoch ms of the last successful load. */
  updatedAt: number | null;
  refresh: () => void;
  setData: Dispatch<SetStateAction<T | null>>;
}

/**
 * Abortable loader with optional polling. The request is aborted when the
 * component unmounts, when filters change, or when a newer request starts.
 */
export function usePolling<T>(
  load: (signal: AbortSignal) => Promise<T>,
  options: UsePollingOptions = {},
): UsePollingResult<T> {
  const { intervalMs = 0, enabled = true, reloadKey = "" } = options;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [tick, setTick] = useState(0);

  const hasLoadedRef = useRef(false);

  const refresh = useCallback(() => {
    setTick((value) => value + 1);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    const controller = new AbortController();

    if (hasLoadedRef.current) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    load(controller.signal)
      .then((value) => {
        if (!active) return;
        hasLoadedRef.current = true;
        setData(value);
        setError(null);
        setUpdatedAt(Date.now());
      })
      .catch((reason: unknown) => {
        if (!active || isAbortError(reason)) return;
        setError(normalizeApiError(reason));
      })
      .finally(() => {
        if (!active) return;
        setLoading(false);
        setRefreshing(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
    // `load` must be memoised by the caller (useCallback); a new identity
    // means the resource changed and the request should be re-issued.
  }, [enabled, reloadKey, tick, load]);

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return;
    const timer = window.setInterval(() => {
      setTick((value) => value + 1);
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, intervalMs]);

  return { data, error, loading, refreshing, updatedAt, refresh, setData };
}
