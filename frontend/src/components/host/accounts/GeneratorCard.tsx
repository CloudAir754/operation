import { useCallback, useEffect, useMemo, useState } from "react";
import type { GeneratorInfo, PasswordRules } from "../../../api/accounts";
import { previewGenerator, saveGenerator } from "../../../api/accounts";
import { describeError } from "../../../api/errors";
import { ErrorBanner, SectionCard, StatusBadge, SuccessBanner } from "../../ui";
import { CheckList, FieldNote, NumberField } from "./shared";

const DEFAULT_RULES: PasswordRules = {
  length: 20,
  minUpper: 1,
  minLower: 1,
  minDigits: 1,
  minSymbols: 1,
  symbols: "!#%+-_=.:?@^~*",
  excludeAmbiguous: true,
  excludeChars: "",
};

export interface GeneratorCardProps {
  serverId: number;
  initial: GeneratorInfo | null;
  onSaved: () => void;
}

/**
 * 目标账户密码规则编辑器：改动即调用后端预览，返回脱敏示例与一致性检查。
 * 所有随机数都在后端用 `secrets` 生成，前端只展示脱敏结果。
 */
export function GeneratorCard({ serverId, initial, onSaved }: GeneratorCardProps) {
  const [rules, setRules] = useState<PasswordRules>(initial?.rules ?? DEFAULT_RULES);
  const [preview, setPreview] = useState<GeneratorInfo | null>(initial ?? null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (initial?.rules) setRules(initial.rules);
  }, [initial?.rules]);

  useEffect(() => {
    const controller = new AbortController();
    previewGenerator(rules, serverId, controller.signal)
      .then((result) => {
        setPreview(result);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(describeError(reason));
      });
    return () => controller.abort();
  }, [rules, serverId]);

  const update = useCallback(<K extends keyof PasswordRules>(key: K, value: PasswordRules[K]) => {
    setRules((current) => ({ ...current, [key]: value }));
    setMessage(null);
  }, []);

  const blockingFailure = useMemo(
    () => (preview?.checks ?? []).some((check) => check.blocking && !check.ok),
    [preview],
  );

  const handleSave = async () => {
    setPending(true);
    setError(null);
    setMessage(null);
    try {
      await saveGenerator(rules);
      setMessage("生成规则已保存；之后新建账户都使用这套规则。");
      onSaved();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setPending(false);
    }
  };

  return (
    <SectionCard
      title="目标账户密码规则"
      testId="generator-card"
      description="只影响新建账户的初始密码；随机数由后端使用操作系统密码学安全随机源生成。"
      actions={
        <StatusBadge tone={preview?.configured ? "success" : "warning"}>
          {preview?.configured ? "已配置" : "首次使用：确认后保存"}
        </StatusBadge>
      }
    >
      <div className="stack">
        {error ? <ErrorBanner title="规则检查失败" message={error} /> : null}
        {message ? <SuccessBanner message={message} /> : null}

        <div className="form-grid">
          <NumberField
            id="rules-length"
            label="密码长度"
            value={rules.length}
            min={8}
            max={128}
            onChange={(value) => update("length", value)}
            hint={`至少 ${preview?.effectiveLength ?? rules.length} 位（受系统策略下限约束）`}
          />
          <NumberField
            id="rules-upper"
            label="最少大写字母"
            value={rules.minUpper}
            min={0}
            max={64}
            onChange={(value) => update("minUpper", value)}
          />
          <NumberField
            id="rules-lower"
            label="最少小写字母"
            value={rules.minLower}
            min={0}
            max={64}
            onChange={(value) => update("minLower", value)}
          />
          <NumberField
            id="rules-digits"
            label="最少数字"
            value={rules.minDigits}
            min={0}
            max={64}
            onChange={(value) => update("minDigits", value)}
          />
          <NumberField
            id="rules-symbols-count"
            label="最少符号"
            value={rules.minSymbols}
            min={0}
            max={64}
            onChange={(value) => update("minSymbols", value)}
          />
        </div>

        <div className="field">
          <label htmlFor="rules-symbols">允许的符号集合</label>
          <input
            id="rules-symbols"
            type="text"
            className="mono"
            value={rules.symbols}
            maxLength={64}
            onChange={(event) => update("symbols", event.target.value)}
          />
          <FieldNote>不允许出现空白字符；重复字符会自动去重。</FieldNote>
        </div>

        <div className="toolbar">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={rules.excludeAmbiguous}
              onChange={(event) => update("excludeAmbiguous", event.target.checked)}
            />
            排除易混淆字符（0/O、1/l/I）
          </label>
        </div>
        <div className="field">
          <label htmlFor="rules-exclude">额外排除的字符</label>
          <input
            id="rules-exclude"
            type="text"
            className="mono"
            value={rules.excludeChars}
            maxLength={64}
            onChange={(event) => update("excludeChars", event.target.value)}
            placeholder="例如 @#"
          />
        </div>

        <div className="rules-preview">
          <div>
            <span className="rules-preview__label">脱敏示例</span>
            <strong className="mono rules-preview__value" data-testid="password-example">
              {preview?.example || "—"}
            </strong>
          </div>
          <span className="muted">只显示前 2 位，完整密码在创建结果中一次性下发。</span>
        </div>

        <CheckList items={preview?.checks ?? []} testId="generator-checks" />

        {preview && preview.adjustments.length > 0 ? (
          <ul className="info-list" data-testid="generator-adjustments">
            {preview.adjustments.map((item) => (
              <li key={item.field}>
                系统策略更严格：{item.label} {item.requested} → {item.applied}（{item.reason}）
              </li>
            ))}
          </ul>
        ) : null}

        {preview?.floor ? (
          <p className="muted">
            当前系统下限：长度 ≥ {preview.floor.minLength}
            {preview.floor.minUpper > 0 ? ` · 大写 ≥ ${preview.floor.minUpper}` : ""}
            {preview.floor.minDigits > 0 ? ` · 数字 ≥ ${preview.floor.minDigits}` : ""}
            {preview.floor.minClasses > 0 ? ` · 至少 ${preview.floor.minClasses} 类字符` : ""}
            {preview.floor.rejectUsername ? " · 禁止包含用户名" : ""}
          </p>
        ) : null}

        <div className="form-actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void handleSave()}
            disabled={pending || blockingFailure}
          >
            {pending ? "保存中…" : "保存生成规则"}
          </button>
          {blockingFailure ? (
            <span className="text-danger">存在阻断性检查项，先修正规则</span>
          ) : null}
        </div>
      </div>
    </SectionCard>
  );
}
