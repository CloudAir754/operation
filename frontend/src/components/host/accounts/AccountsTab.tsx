import { useCallback, useState } from "react";
import type { FormEvent } from "react";
import type { PasswordPolicy, StorageSettings } from "../../../api/accounts";
import { getPasswordPolicy, getStorageSettings } from "../../../api/accounts";
import { clearSecret, setSecret } from "../../../api/client";
import { describeError } from "../../../api/errors";
import { ConfirmDialog } from "../../ConfirmDialog";
import {
  ErrorBanner,
  InfoBanner,
  LoadingBlock,
  SectionCard,
  StatusBadge,
  SuccessBanner,
} from "../../ui";
import { usePolling } from "../../../hooks/usePolling";
import { formatDateTime } from "../../../lib/format";
import { DatasetCard } from "./DatasetCard";
import { GeneratorCard } from "./GeneratorCard";
import { MigrationCard } from "./MigrationCard";
import { PolicyCard } from "./PolicyCard";
import { SudoBadge } from "./shared";
import { UsersCard } from "./UsersCard";

export interface AccountsTabProps {
  serverId: number;
  hasSudoPassword: boolean;
  onSudoChanged: () => void;
}

/**
 * 账户页签：把「运维 sudo 密码」与「目标账户登录密码」两个层次分开呈现，
 * 每个提权操作旁都标注“需要 sudo”。
 */
export function AccountsTab({ serverId, hasSudoPassword, onSudoChanged }: AccountsTabProps) {
  const loadPolicy = useCallback(
    (signal: AbortSignal) => getPasswordPolicy(serverId, { signal }),
    [serverId],
  );
  const {
    data: policy,
    error: policyError,
    loading: policyLoading,
    refresh: refreshPolicy,
  } = usePolling<PasswordPolicy>(loadPolicy, { reloadKey: serverId });

  const loadSettings = useCallback(
    (signal: AbortSignal) => getStorageSettings(serverId, signal),
    [serverId],
  );
  const {
    data: settings,
    error: settingsError,
    refresh: refreshSettings,
  } = usePolling<StorageSettings>(loadSettings, { reloadKey: serverId });

  const [password, setPassword] = useState("");
  const [secretPending, setSecretPending] = useState<"set" | "clear" | null>(null);
  const [secretError, setSecretError] = useState<string | null>(null);
  const [secretMessage, setSecretMessage] = useState<string | null>(null);
  const [clearOpen, setClearOpen] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);
  const [sudoOverride, setSudoOverride] = useState<boolean | null>(null);

  const hasSudo = sudoOverride ?? hasSudoPassword;

  const handleSavePassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password || secretPending) return;
    setSecretPending("set");
    setSecretError(null);
    setSecretMessage(null);
    try {
      const result = await setSecret(serverId, password);
      setSudoOverride(result.hasPassword);
      setPassword("");
      setSecretMessage("运维 sudo 密码已加密保存。");
      onSudoChanged();
    } catch (reason) {
      setSecretError(describeError(reason));
    } finally {
      setSecretPending(null);
    }
  };

  const handleClearPassword = async () => {
    setSecretPending("clear");
    setClearError(null);
    try {
      await clearSecret(serverId);
      setSudoOverride(false);
      setClearOpen(false);
      setSecretMessage("运维 sudo 密码已清除。");
      onSudoChanged();
    } catch (reason) {
      setClearError(describeError(reason));
    } finally {
      setSecretPending(null);
    }
  };

  return (
    <div className="stack" data-testid="accounts-tab">
      <InfoBanner>
        <strong>这里有两层密码，请勿混淆：</strong>
        <ul className="info-list">
          <li>
            <strong>运维 sudo 密码</strong>：本应用登录远端后执行提权操作使用，Windows DPAPI 加密保存，
            只在本卡片录入。
          </li>
          <li>
            <strong>目标账户登录密码</strong>：新建出来的 Linux 账户将来登录使用，由本应用随机生成，
            只在创建完成后一次性展示，不写数据库。
          </li>
        </ul>
      </InfoBanner>

      <SectionCard
        title="运维 sudo 密码"
        testId="sudo-card"
        description="所有标注「需要 sudo」的操作都使用这里的密码；明文不会回显。"
        actions={
          <StatusBadge tone={hasSudo ? "success" : "warning"}>
            {hasSudo ? "已保存" : "未保存"}
          </StatusBadge>
        }
      >
        <div className="stack">
          {secretError ? <ErrorBanner title="密码操作失败" message={secretError} /> : null}
          {secretMessage ? <SuccessBanner message={secretMessage} /> : null}
          <p className="muted">
            需要 sudo 的操作：修改系统密码策略、批量新建用户、迁移用户目录、分配新空间、
            继续/回退/清理迁移。只读预检与策略读取不需要 sudo。
          </p>
          <form className="stack" onSubmit={(event) => void handleSavePassword(event)}>
            <div className="field">
              <label htmlFor="accounts-sudo-password">运维 sudo 密码</label>
              <input
                id="accounts-sudo-password"
                type="password"
                autoComplete="new-password"
                value={password}
                placeholder="输入后保存，不会显示已有密码"
                onChange={(event) => setPassword(event.target.value)}
                disabled={secretPending !== null}
              />
            </div>
            <div className="form-actions">
              <button
                type="submit"
                className="btn btn--primary"
                disabled={!password || secretPending !== null}
              >
                {secretPending === "set" ? "保存中…" : "保存 sudo 密码"}
              </button>
              <button
                type="button"
                className="btn btn--danger-ghost"
                onClick={() => {
                  setClearError(null);
                  setClearOpen(true);
                }}
                disabled={!hasSudo || secretPending !== null}
              >
                清除密码
              </button>
              {!hasSudo ? (
                <span className="text-warning">未保存时，所有提权操作会被后端拒绝（409）</span>
              ) : null}
            </div>
          </form>
        </div>
      </SectionCard>

      {policyError ? (
        <ErrorBanner
          title="无法读取系统密码策略"
          message={policyError.message}
          onRetry={refreshPolicy}
        />
      ) : null}
      {settingsError ? (
        <ErrorBanner
          title="无法读取路径设置"
          message={settingsError.message}
          onRetry={refreshSettings}
        />
      ) : null}

      {policyLoading && !policy ? (
        <LoadingBlock label="正在读取实际生效的密码策略…" />
      ) : policy ? (
        <>
          <GeneratorCard
            serverId={serverId}
            initial={policy.generator}
            onSaved={refreshPolicy}
          />
          <PolicyCard serverId={serverId} policy={policy} onChanged={refreshPolicy} />
        </>
      ) : null}

      <UsersCard
        serverId={serverId}
        hasSudoPassword={hasSudo}
        generator={policy?.generator ?? null}
        onNeedSudo={() => {
          document.getElementById("accounts-sudo-password")?.focus();
        }}
      />

      <MigrationCard
        serverId={serverId}
        defaultTargetRoot={settings?.migrationRoot ?? settings?.defaultMigrationRoot ?? "/data2/users"}
        hasSudoPassword={hasSudo}
      />

      <DatasetCard
        serverId={serverId}
        settings={settings}
        onSettingsSaved={refreshSettings}
        hasSudoPassword={hasSudo}
      />

      <p className="muted">
        策略读取时间：{policy ? formatDateTime(policy.collectedAt) : "—"} ·
        所有写操作都在后台任务中执行并写入审计，可随时在这里查看阶段与结果。
      </p>

      <ConfirmDialog
        open={clearOpen}
        title="清除运维 sudo 密码"
        description="清除后，所有需要提权的账户操作都会失败，直到重新保存密码。"
        details={[{ label: "权限要求", value: <SudoBadge /> }]}
        warning={<p>密码不会保留副本，需要时可以重新输入。</p>}
        confirmLabel="确认清除"
        danger
        pending={secretPending === "clear"}
        error={clearError}
        onConfirm={() => void handleClearPassword()}
        onCancel={() => {
          if (secretPending === "clear") return;
          setClearOpen(false);
          setClearError(null);
        }}
      />
    </div>
  );
}
