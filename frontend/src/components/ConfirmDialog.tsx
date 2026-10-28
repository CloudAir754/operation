import type { ReactNode } from "react";
import { Modal } from "./Modal";

export interface ConfirmDetail {
  label: string;
  value: ReactNode;
}

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  details?: ConfirmDetail[];
  warning?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  pending?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Confirmation dialog used before every remote write action. */
export function ConfirmDialog({
  open,
  title,
  description,
  details,
  warning,
  confirmLabel = "确认执行",
  cancelLabel = "取消",
  danger = false,
  pending = false,
  error,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal
      open={open}
      title={title}
      description={description}
      onClose={onCancel}
      pending={pending}
      size="md"
      footer={
        <>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onCancel}
            disabled={pending}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn ${danger ? "btn--danger" : "btn--primary"}`}
            onClick={onConfirm}
            disabled={pending}
            data-autofocus="true"
          >
            {pending ? "执行中…" : confirmLabel}
          </button>
        </>
      }
    >
      {details && details.length > 0 ? (
        <dl className="confirm-list">
          {details.map((item) => (
            <div className="confirm-list__row" key={item.label}>
              <dt>{item.label}</dt>
              <dd>{item.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {warning ? <div className="confirm-warning">{warning}</div> : null}
      {error ? (
        <div className="banner banner--error" role="alert">
          <span className="banner__mark" aria-hidden="true">
            !
          </span>
          <div className="banner__body">
            <span className="banner__message">{error}</span>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
