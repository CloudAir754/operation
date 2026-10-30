import type { ReactNode } from "react";
import type { CheckItem } from "../../../api/accounts";
import { StatusBadge } from "../../ui";

/** 统一的「需要 sudo」标注：所有提权按钮旁都必须出现。 */
export function SudoBadge({ title }: { title?: string }) {
  return (
    <StatusBadge
      tone="warning"
      className="badge--inline"
      title={title ?? "该操作通过 sudo 提权执行，需要先保存运维 sudo 密码"}
    >
      需要 sudo
    </StatusBadge>
  );
}

export interface CheckListProps {
  items: CheckItem[];
  testId?: string;
  emptyLabel?: string;
}

export function CheckList({ items, testId, emptyLabel }: CheckListProps) {
  if (items.length === 0) {
    return emptyLabel ? <p className="muted">{emptyLabel}</p> : null;
  }
  return (
    <ul className="check-list" data-testid={testId}>
      {items.map((item) => (
        <li
          key={item.code}
          className={`check-list__item check-list__item--${item.ok ? "ok" : item.blocking ? "bad" : "warn"}`}
        >
          <span className="check-list__mark" aria-hidden="true">
            {item.ok ? "✓" : item.blocking ? "✗" : "!"}
          </span>
          <span className="check-list__label">{item.label}</span>
          {item.message ? <span className="check-list__detail">{item.message}</span> : null}
        </li>
      ))}
    </ul>
  );
}

export function FieldNote({ children }: { children: ReactNode }) {
  return <p className="form-hint">{children}</p>;
}

export interface NumberFieldProps {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  hint?: ReactNode;
  disabled?: boolean;
  onChange: (value: number) => void;
}

export function NumberField({
  id,
  label,
  value,
  min,
  max,
  hint,
  disabled,
  onChange,
}: NumberFieldProps) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        value={Number.isFinite(value) ? value : min}
        disabled={disabled}
        onChange={(event) => {
          const next = Number(event.target.value);
          onChange(Number.isFinite(next) ? next : min);
        }}
      />
      {hint ? <span className="form-hint">{hint}</span> : null}
    </div>
  );
}
