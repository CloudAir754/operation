import { useId } from "react";
import type { ReactNode } from "react";
import { clampPercent, formatPercent } from "../lib/format";
import type { Tone } from "../lib/labels";

export interface StatusBadgeProps {
  tone?: Tone;
  children: ReactNode;
  title?: string;
  className?: string;
}

export function StatusBadge({ tone = "neutral", children, title, className }: StatusBadgeProps) {
  return (
    <span
      className={`badge badge--${tone}${className ? ` ${className}` : ""}`}
      title={title}
    >
      {children}
    </span>
  );
}

export interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}

export function PageHeader({ eyebrow, title, description, actions }: PageHeaderProps) {
  return (
    <div className="page-header">
      <div className="page-header__text">
        {eyebrow ? <p className="page-header__eyebrow">{eyebrow}</p> : null}
        <h1 className="page-header__title">{title}</h1>
        {description ? <div className="page-header__description">{description}</div> : null}
      </div>
      {actions ? <div className="page-header__actions">{actions}</div> : null}
    </div>
  );
}

export interface ErrorBannerProps {
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
  title?: string;
}

export function ErrorBanner({
  message,
  onRetry,
  retryLabel = "重试",
  title = "操作失败",
}: ErrorBannerProps) {
  return (
    <div className="banner banner--error" role="alert">
      <span className="banner__mark" aria-hidden="true">
        !
      </span>
      <div className="banner__body">
        {title ? <strong className="banner__title">{title}</strong> : null}
        <span className="banner__message">{message}</span>
      </div>
      {onRetry ? (
        <button type="button" className="btn btn--small btn--ghost" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

export interface SuccessBannerProps {
  message: string;
  children?: ReactNode;
}

export function SuccessBanner({ message, children }: SuccessBannerProps) {
  return (
    <div className="banner banner--success" role="status">
      <span className="banner__mark" aria-hidden="true">
        ✓
      </span>
      <div className="banner__body">
        <span className="banner__message">{message}</span>
        {children}
      </div>
    </div>
  );
}

export interface InfoBannerProps {
  children: ReactNode;
  tone?: "info" | "warning";
}

export function InfoBanner({ children, tone = "info" }: InfoBannerProps) {
  return (
    <div className={`banner banner--${tone}`} role="status">
      <span className="banner__mark" aria-hidden="true">
        {tone === "warning" ? "!" : "i"}
      </span>
      <div className="banner__body">{children}</div>
    </div>
  );
}

export interface SectionCardProps {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  testId?: string;
  className?: string;
}

export function SectionCard({
  title,
  description,
  actions,
  children,
  testId,
  className,
}: SectionCardProps) {
  const titleId = useId();
  return (
    <section
      className={`card${className ? ` ${className}` : ""}`}
      aria-labelledby={titleId}
      data-testid={testId}
    >
      <header className="card__header">
        <div>
          <h2 className="card__title" id={titleId}>
            {title}
          </h2>
          {description ? <p className="card__description">{description}</p> : null}
        </div>
        {actions ? <div className="card__actions">{actions}</div> : null}
      </header>
      <div className="card__body">{children}</div>
    </section>
  );
}

export interface UsageBarProps {
  label: string;
  percent: number | null | undefined;
  testId?: string;
  detail?: ReactNode;
}

export function UsageBar({ label, percent, testId, detail }: UsageBarProps) {
  const clamped = clampPercent(percent);
  const tone = clamped >= 90 ? "danger" : clamped >= 75 ? "warning" : "ok";
  const hasValue = percent !== null && percent !== undefined && Number.isFinite(percent);
  return (
    <div className="usage" data-testid={testId}>
      <div className="usage__head">
        <span className="usage__label">{label}</span>
        <span className="usage__value">{formatPercent(percent)}</span>
      </div>
      <div
        className="usage__track"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={hasValue ? Math.round(clamped) : undefined}
        aria-valuetext={hasValue ? formatPercent(percent) : "无数据"}
      >
        <div
          className={`usage__fill usage__fill--${tone}`}
          style={{ width: `${hasValue ? clamped : 0}%` }}
        />
      </div>
      {detail ? <div className="usage__detail">{detail}</div> : null}
    </div>
  );
}

export interface DetailItem {
  label: string;
  value: ReactNode;
}

export function DetailList({ items }: { items: DetailItem[] }) {
  return (
    <dl className="kv">
      {items.map((item) => (
        <div className="kv__row" key={item.label}>
          <dt className="kv__key">{item.label}</dt>
          <dd className="kv__value">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}

export function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <p className="empty-state__title">{title}</p>
      {description ? <p className="empty-state__description">{description}</p> : null}
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  );
}

export function LoadingBlock({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}
