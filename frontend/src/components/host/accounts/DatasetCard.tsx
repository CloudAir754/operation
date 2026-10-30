import { useCallback, useEffect, useState } from "react";
import type { Dataset, StorageSettings } from "../../../api/accounts";
import {
  createDataset,
  listDatasets,
  unlinkDataset,
  updateStorageSettings,
} from "../../../api/accounts";
import { describeError } from "../../../api/errors";
import { ConfirmDialog } from "../../ConfirmDialog";
import {
  EmptyState,
  ErrorBanner,
  InfoBanner,
  LoadingBlock,
  SectionCard,
  StatusBadge,
  SuccessBanner,
} from "../../ui";
import { SudoBadge } from "./shared";

export interface DatasetCardProps {
  serverId: number;
  settings: StorageSettings | null;
  onSettingsSaved: () => void;
  hasSudoPassword: boolean;
}

/** 给某个用户在数据盘上分配新空间，并在 home 下建立符号链接。 */
export function DatasetCard({
  serverId,
  settings,
  onSettingsSaved,
  hasSudoPassword,
}: DatasetCardProps) {
  const [dataRoot, setDataRoot] = useState(settings?.dataRoot ?? "");
  const [migrationRoot, setMigrationRoot] = useState(settings?.migrationRoot ?? "");
  const [settingsPending, setSettingsPending] = useState(false);
  const [login, setLogin] = useState("");
  const [linkName, setLinkName] = useState("data2_disk");
  const [reuseExisting, setReuseExisting] = useState(false);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [unlinkTarget, setUnlinkTarget] = useState<Dataset | null>(null);
  const [unlinkPending, setUnlinkPending] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);

  useEffect(() => {
    setDataRoot(settings?.dataRoot ?? "");
    setMigrationRoot(settings?.migrationRoot ?? "");
  }, [settings]);

  const loadDatasets = useCallback(
    (signal?: AbortSignal) => {
      listDatasets(serverId, signal)
        .then((result) => setDatasets(result.datasets))
        .catch(() => undefined);
    },
    [serverId],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadDatasets(controller.signal);
    return () => controller.abort();
  }, [loadDatasets]);

  const handleSaveSettings = async () => {
    setSettingsPending(true);
    setSettingsError(null);
    setMessage(null);
    try {
      await updateStorageSettings(serverId, {
        dataRoot: dataRoot.trim() || null,
        migrationRoot: migrationRoot.trim() || null,
      });
      setMessage("默认路径已保存。");
      onSettingsSaved();
    } catch (reason) {
      setSettingsError(describeError(reason));
    } finally {
      setSettingsPending(false);
    }
  };

  const handleAllocate = async () => {
    setPending(true);
    setDialogError(null);
    try {
      await createDataset(serverId, {
        login: login.trim(),
        linkName: linkName.trim(),
        dataRoot: dataRoot.trim() || null,
        reuseExisting,
      });
      setConfirmOpen(false);
      setLogin("");
      setMessage("空间已分配并建立符号链接。");
      loadDatasets();
    } catch (reason) {
      setDialogError(describeError(reason));
    } finally {
      setPending(false);
    }
  };

  const handleUnlink = async () => {
    if (!unlinkTarget) return;
    setUnlinkPending(true);
    setDialogError(null);
    try {
      await unlinkDataset(unlinkTarget.id);
      setUnlinkTarget(null);
      setMessage("符号链接已解除，数据目录保留。");
      loadDatasets();
    } catch (reason) {
      setDialogError(describeError(reason));
    } finally {
      setUnlinkPending(false);
    }
  };

  return (
    <SectionCard
      title="给用户分配新空间"
      testId="dataset-card"
      description="在数据盘上为该用户创建独立目录，并在 home 下建立指向它的符号链接。"
    >
      <div className="stack">
        {settingsError ? <ErrorBanner title="无法保存路径设置" message={settingsError} /> : null}
        {message ? <SuccessBanner message={message} /> : null}

        {!settings ? (
          <LoadingBlock label="正在读取路径设置…" />
        ) : (
          <>
            <div className="form-grid">
              <div className="field">
                <label htmlFor="data-root">数据盘挂载路径</label>
                <input
                  id="data-root"
                  className="mono"
                  value={dataRoot}
                  placeholder={settings.defaultDataRoot}
                  onChange={(event) => setDataRoot(event.target.value)}
                />
                <span className="form-hint">
                  用于新建用户目录；默认 {settings.defaultDataRoot}，首版只支持本地 ext4。
                </span>
              </div>
              <div className="field">
                <label htmlFor="migration-root">迁移根目录</label>
                <input
                  id="migration-root"
                  className="mono"
                  value={migrationRoot}
                  placeholder={settings.defaultMigrationRoot}
                  onChange={(event) => setMigrationRoot(event.target.value)}
                />
                <span className="form-hint">
                  目录迁移的默认父目录，例如 {settings.defaultMigrationRoot}。
                </span>
              </div>
            </div>
            <div className="form-actions">
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => void handleSaveSettings()}
                disabled={settingsPending}
              >
                {settingsPending ? "保存中…" : "保存默认路径"}
              </button>
            </div>
          </>
        )}

        <div className="form-grid">
          <div className="field">
            <label htmlFor="dataset-login">账户</label>
            <input
              id="dataset-login"
              className="mono"
              value={login}
              placeholder="zhangsan"
              onChange={(event) => setLogin(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="dataset-link">home 下的链接名</label>
            <input
              id="dataset-link"
              className="mono"
              value={linkName}
              onChange={(event) => setLinkName(event.target.value)}
            />
            <span className="form-hint">
              链接会创建为 &lt;home&gt;/{linkName || "data2_disk"}，指向 &lt;数据盘&gt;/&lt;账户&gt;。
            </span>
          </div>
        </div>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={reuseExisting}
            onChange={(event) => setReuseExisting(event.target.checked)}
          />
          复用已存在的空目录（非空目录一律拒绝）
        </label>

        {!hasSudoPassword ? (
          <InfoBanner tone="warning">
            创建目录与符号链接需要 sudo 提权，请先在「运维 sudo 密码」中填写。
          </InfoBanner>
        ) : null}

        <div className="form-actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => {
              setDialogError(null);
              setConfirmOpen(true);
            }}
            disabled={!login.trim() || !dataRoot.trim() || pending}
          >
            分配空间 <SudoBadge />
          </button>
        </div>

        <div className="stack-sm">
          <h3 className="subsection-title">已分配的链接</h3>
          {datasets.length === 0 ? (
            <EmptyState title="还没有分配记录" />
          ) : (
            <div className="table-wrap">
              <table className="data-table data-table--compact">
                <caption className="sr-only">已分配空间</caption>
                <thead>
                  <tr>
                    <th scope="col">账户</th>
                    <th scope="col">数据目录</th>
                    <th scope="col">链接</th>
                    <th scope="col">状态</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {datasets.map((item) => (
                    <tr key={item.id}>
                      <td className="mono">{item.login}</td>
                      <td className="mono">{item.dataPath}</td>
                      <td className="mono">{item.linkPath}</td>
                      <td>
                        <StatusBadge
                          tone={
                            item.status === "succeeded"
                              ? item.writable
                                ? "success"
                                : "warning"
                              : item.status === "unlinked"
                                ? "muted"
                                : "danger"
                          }
                        >
                          {item.status === "succeeded"
                            ? item.writable
                              ? "可写"
                              : "已创建（写入验证未通过）"
                            : item.status === "unlinked"
                              ? "已解除"
                              : "失败"}
                        </StatusBadge>
                        {item.error ? (
                          <span className="service-description">{item.error}</span>
                        ) : null}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn btn--tiny btn--ghost"
                          onClick={() => {
                            setDialogError(null);
                            setUnlinkTarget(item);
                          }}
                          disabled={item.status !== "succeeded"}
                        >
                          解除链接 <SudoBadge />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <p className="muted">
          解除链接只删除符号链接，不会删除数据目录里的内容。
        </p>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title="确认分配新空间"
        description="将在数据盘创建属于该用户的目录，并在 home 下建立符号链接。"
        details={[
          { label: "账户", value: <span className="mono">{login}</span> },
          {
            label: "数据目录",
            value: <span className="mono">{(dataRoot || "—") + "/" + login}</span>,
          },
          {
            label: "链接路径",
            value: <span className="mono">/home/{login || "<user>"}/{linkName || "data2_disk"}</span>,
          },
          { label: "权限要求", value: <SudoBadge /> },
        ]}
        warning={<p>目录属主会被设置为该账户的 UID/GID，权限模式为 0750。</p>}
        confirmLabel="确认分配"
        pending={pending}
        error={dialogError}
        onConfirm={() => void handleAllocate()}
        onCancel={() => {
          if (pending) return;
          setConfirmOpen(false);
          setDialogError(null);
        }}
      />

      <ConfirmDialog
        open={unlinkTarget !== null}
        title="解除空间链接"
        description="只删除 home 下的符号链接，数据目录与其中内容保持不变。"
        details={
          unlinkTarget
            ? [
                { label: "账户", value: <span className="mono">{unlinkTarget.login}</span> },
                { label: "链接", value: <span className="mono">{unlinkTarget.linkPath}</span> },
                { label: "数据目录", value: <span className="mono">{unlinkTarget.dataPath}</span> },
              ]
            : []
        }
        confirmLabel="确认解除"
        danger
        pending={unlinkPending}
        error={dialogError}
        onConfirm={() => void handleUnlink()}
        onCancel={() => {
          if (unlinkPending) return;
          setUnlinkTarget(null);
          setDialogError(null);
        }}
      />
    </SectionCard>
  );
}
