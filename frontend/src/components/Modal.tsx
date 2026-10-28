import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

export interface ModalProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: "md" | "lg";
  /** Blocks closing (Escape/backdrop/close button) while a request runs. */
  pending?: boolean;
}

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Accessible modal: Escape closes, focus is trapped while open, focus returns
 * to the trigger on close. No portal dependency.
 */
export function Modal({
  open,
  title,
  description,
  onClose,
  children,
  footer,
  size = "md",
  pending = false,
}: ModalProps) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const pendingRef = useRef(pending);

  onCloseRef.current = onClose;
  pendingRef.current = pending;

  useEffect(() => {
    if (!open) return;
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !pendingRef.current) {
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    const timer = window.setTimeout(() => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      const preferred = dialog.querySelector<HTMLElement>('[data-autofocus="true"]');
      const first = dialog.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
      (preferred ?? first ?? dialog).focus();
    }, 0);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      window.clearTimeout(timer);
      if (previousFocus && document.contains(previousFocus)) {
        previousFocus.focus();
      }
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !pending) onClose();
      }}
    >
      <div
        className={`modal modal--${size}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        ref={dialogRef}
        tabIndex={-1}
      >
        <header className="modal__header">
          <h2 className="modal__title" id={titleId}>
            {title}
          </h2>
          <button
            type="button"
            className="icon-btn"
            onClick={onClose}
            disabled={pending}
            aria-label="关闭对话框"
          >
            ×
          </button>
        </header>
        {description ? (
          <p className="modal__description" id={descriptionId}>
            {description}
          </p>
        ) : null}
        <div className="modal__body">{children}</div>
        {footer ? <footer className="modal__footer">{footer}</footer> : null}
      </div>
    </div>
  );
}
