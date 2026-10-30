import { useCallback, useEffect, useRef, useState } from "react";
import { createUserStorageScan, getUsers } from "../../api/client";
import { describeError } from "../../api/errors";
import type { HomeStorage, StorageScan, SudoState, UserInfo, UsersResponse } from "../../api/types";
import { usePolling } from "../../hooks/usePolling";
import { formatDateTime } from "../../lib/format";
import { SECTION_STATUS_LABELS, SECTION_STATUS_TONES } from "../../lib/labels";
import {
  NOT_AVAILABLE,
  formatBytesOrNa,
  formatCountOrNa,
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

const SCAN_HOME_MOUNT = "/";
const SCAN_HOME_MAX_DEPTH = 8;

function sudoEffectiveBadge(effective: SudoState["effective"]) {
  if (effective === "true") return <StatusBadge tone="success">可用</StatusBadge>;
  if (effective === "false") return <StatusBadge tone="danger">不可用</StatusBadge>;
  return <StatusBadge tone="muted">未知</StatusBadge>;
}

function sudoGroupBadge(groupMember: boolean | null) {
  if (groupMember === true) return <StatusBadge tone="info">组成员</StatusBadge>;
  if (groupMember === false) return <StatusBadge tone="muted">非组成员</StatusBadge>;
  return <StatusBadge tone="muted">未知</StatusBadge>;
}

function sudoPolicyBadge(policyPresent: boolean | null) {
  if (policyPresent === true) return <StatusBadge tone="info">策略存在</StatusBadge>;
  if (policyPresent === false) return <StatusBadge tone="muted">无策略</StatusBadge>;
  return <StatusBadge tone="muted">未知</StatusBadge>;
}

function HomeStorageCell({ storage }: { storage: HomeStorage | null }) {
  if (!storage) {
    return <span data-testid="home-storage">{NOT_AVAILABLE}</span>;
  }
  return (
    <span className="stack-sm" data-testid="home-storage">
      <span>
        {formatBytesOrNa(storage.allocatedBytes)} · {formatCountOrNa(storage.fileCount)} 文件
        {storage.directoryCount !== null ? ` · ${storage.directoryCount} 目录` : ""}
      </span>
      <span className="badge-row">
        {storage.truncated ? <StatusBadge tone="warning">已截断</StatusBadge> : null}
        {storage.incomplete ? <StatusBadge tone="warning">不完整</StatusBadge> : null}
        {storage.error ? (
          <StatusBadge tone="danger" title={storage.error}>
            采集错误
          </StatusBadge>
        ) : null}
      </span>
    </span>
  );
}

export interface UsersTabProps {
  serverId: number;
  /** Called after a per-UID scan was created so the page can switch tabs. */
  onScanStarted: (scan: StorageScan) => void;
}

export function UsersTab({ serverId, onScanStarted }: UsersTabProps) {
  const [includeSystem, setIncludeSystem] = useState(false);
  const [pendingUid, setPendingUid] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const loadUsers = useCallback(
    (signal: AbortSignal) => getUsers(serverId, { includeSystem, signal }),
    [serverId, includeSystem],
  );
  const { data, error, refreshing, refresh } = usePolling<UsersResponse>(loadUsers, {
    reloadKey: `${serverId}:${includeSystem}`,
  });

  const users = data && data.serverId === serverId ? data.users : [];
  const notes = data && data.serverId === serverId ? data.notes : [];

  const startUserScan = async (user: UserInfo) => {
    if (pendingUid !== null) return;
    setPendingUid(user.uid);
    setActionError(null);
    try {
      const scan = await createUserStorageScan(serverId, {
        uid: user.uid,
        mount: SCAN_HOME_MOUNT,
        maxDepth: SCAN_HOME_MAX_DEPTH,
        minFileSize: 0,
        includeDeletedOpen: true,
      });
      onScanStarted(scan);
    } catch (reason) {
      if (aliveRef.current) setActionError(describeError(reason));
    } finally {
      if (aliveRef.current) setPendingUid(null);
    }
  };

  return (
    <div className="stack" data-testid="users-tab">
      {error ? (
        <ErrorBanner title="无法读取用户清单" message={error.message} onRetry={refresh} />
      ) : null}
      {actionError ? <ErrorBanner title="无法创建用户空间扫描" message={actionError} /> : null}

      <div className="toolbar">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={includeSystem}
            onChange={(event) => setIncludeSystem(event.target.checked)}
          />
          包含系统账号
        </label>
        <span className="muted">
          默认只显示可登录用户（shell 不在 nologin/false 黑名单）。
        </span>
        <button
          type="button"
          className="btn btn--ghost btn--small"
          onClick={refresh}
          disabled={refreshing}
        >
          {refreshing ? "刷新中…" : "刷新"}
        </button>
      </div>

      {!data ? (
        error ? null : <LoadingBlock label="正在读取用户清单…" />
      ) : (
        <>
          <div className="snapshot-meta" aria-live="polite">
            <span>
              用户 <strong className="mono">{users.length}</strong>
            </span>
            <span className="muted">采集于 {formatDateTime(data.collectedAt)}</span>
            <StatusBadge tone={SECTION_STATUS_TONES[data.status]}>
              {SECTION_STATUS_LABELS[data.status]}
            </StatusBadge>
            {data.requiresSudo ? <StatusBadge tone="warning">需要 sudo</StatusBadge> : null}
            {data.includeSystem ? (
              <StatusBadge tone="info">包含系统账号</StatusBadge>
            ) : null}
          </div>

          {data.status !== "ok" ? (
            <SectionNotice
              status={data.status}
              error={data.error}
              requiresSudo={data.requiresSudo}
              testId="users-notice"
              title="用户清单"
            />
          ) : null}

          {notes.length > 0 ? (
            <InfoBanner>
              <ul className="info-list" data-testid="users-notes">
                {notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </InfoBanner>
          ) : null}

          <SectionCard
            title="可登录用户"
            testId="users-table"
            className="card--span-2"
            description="sudo 权限分为组成员、策略存在与有效权限三态；无法验证时显示「未知」而不是「否」。"
          >
            {users.length === 0 ? (
              <EmptyState title="没有符合筛选条件的用户" />
            ) : (
              <div className="table-wrap table-wrap--tall">
                <table className="data-table data-table--observability data-table--users">
                  <caption className="sr-only">可登录用户与空间摘要</caption>
                  <thead>
                    <tr>
                      <th scope="col">登录名</th>
                      <th scope="col">UID / GID</th>
                      <th scope="col">主组</th>
                      <th scope="col">附加组</th>
                      <th scope="col">Shell</th>
                      <th scope="col">主目录</th>
                      <th scope="col">sudo</th>
                      <th scope="col">Home 空间</th>
                      <th scope="col">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.map((user) => (
                      <tr key={user.login} data-testid={`user-row-${user.login}`}>
                        <td>
                          <span className="mono">{user.login}</span>
                          {user.system ? (
                            <StatusBadge tone="muted" className="badge--inline">
                              系统
                            </StatusBadge>
                          ) : null}
                        </td>
                        <td className="mono">
                          {user.uid} / {user.gid}
                        </td>
                        <td>{user.primaryGroup ?? NOT_AVAILABLE}</td>
                        <td>
                          {user.supplementaryGroups.length === 0 ? (
                            <span className="muted">—</span>
                          ) : (
                            <span className="chip-row chip-row--static">
                              {user.supplementaryGroups.map((group) => (
                                <span key={group} className="chip chip--static">
                                  {group}
                                </span>
                              ))}
                            </span>
                          )}
                        </td>
                        <td className="mono">{user.shell}</td>
                        <td className="mono">{user.home}</td>
                        <td>
                          <span className="badge-row">
                            <span data-testid={`sudo-group-${user.login}`} title="组成员">
                              {sudoGroupBadge(user.sudo.groupMember)}
                            </span>
                            <span data-testid={`sudo-policy-${user.login}`} title="策略存在">
                              {sudoPolicyBadge(user.sudo.policyPresent)}
                            </span>
                            <span data-testid={`sudo-effective-${user.login}`} title="有效 sudo">
                              {sudoEffectiveBadge(user.sudo.effective)}
                            </span>
                          </span>
                          {user.sudo.error ? (
                            <span className="service-description">{user.sudo.error}</span>
                          ) : null}
                        </td>
                        <td>
                          <HomeStorageCell storage={user.homeStorage} />
                        </td>
                        <td>
                          <button
                            type="button"
                            className="btn btn--tiny btn--ghost"
                            onClick={() => void startUserScan(user)}
                            disabled={pendingUid !== null}
                            title={`按 UID ${user.uid} 扫描全盘空间归属`}
                          >
                            {pendingUid === user.uid ? "创建中…" : "扫描空间"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </SectionCard>

          {users.length > 0 ? (
            <p className="muted">
              home 空间来自只读扫描，可能因权限不足而不完整；「扫描空间」会在「磁盘空间」页签创建按 UID
              的全盘归属扫描。
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
