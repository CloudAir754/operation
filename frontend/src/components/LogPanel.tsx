import { useCallback, useEffect, useState } from "react";
import { describeError, isAbortError } from "../api/errors";
import { getLogs } from "../api/client";
import type { LogsResponse } from "../api/types";
import { Modal } from "./Modal";
import { EmptyState, ErrorBanner, InfoBanner, LoadingBlock } from "./ui";
import { formatDateTime } from "../lib/format";

export interface LogPanelProps {
  serverId: number;
  serverAlias: string;
  unit: string | null;
  onClose: () => void;
}

interface LogState {
  loading: boolean;
  data: LogsResponse | null;
  error: string | null;
}

/** Read-only journal view. Bounded to 200 lines by the API contract. */
export function LogPanel({ serverId, serverAlias, unit, onClose }: LogPanelProps) {
  const [state, setState] = useState<LogState>({ loading: true, data: null, error: null });
  const [reloadKey, setReloadKey] = useState(0);

  const reload = useCallback(() => {
    setReloadKey((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setState((previous) => ({ ...previous, loading: true, error: null }));
    getLogs(serverId, { unit, lines: 200, signal: controller.signal })
      .then((data) => {
        if (!active) return;
        setState({ loading: false, data, error: null });
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) return;
        setState({ loading: false, data: null, error: describeError(error) });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [serverId, unit, reloadKey]);

  const { data, loading, error } = state;

  return (
    <Modal
      open
      size="lg"
      title={unit ? `服务日志 · ${unit}` : "全部服务日志"}
      description={`${serverAlias} · 最近 200 行 · 只读`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            关闭
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={reload}
            disabled={loading}
          >
            {loading ? "读取中…" : "刷新日志"}
          </button>
        </>
      }
    >
      <div className="log-panel">
        {error ? (
          <ErrorBanner title="无法读取日志" message={error} onRetry={reload} />
        ) : null}
        {loading && !data ? <LoadingBlock label="正在读取远程日志…" /> : null}
        {data ? (
          <>
            {data.error ? (
              <ErrorBanner title="远端日志命令返回错误" message={data.error} />
            ) : null}
            {data.truncated ? (
              <InfoBanner tone="warning">输出已达到上限，仅显示前 200 行。</InfoBanner>
            ) : null}
            <p className="mono muted log-panel__command">{data.command}</p>
            {data.lines.length === 0 ? (
              <EmptyState title="没有日志行" description="该单元最近没有可读取的日志。" />
            ) : (
              <pre className="log-output" tabIndex={0} aria-label="日志内容">
                {data.lines.join("\n")}
              </pre>
            )}
            <p className="log-panel__meta">
              退出码：{data.exitCode ?? "—"} · 采集时间：{formatDateTime(data.collectedAt)}
            </p>
          </>
        ) : null}
      </div>
    </Modal>
  );
}
