import { useEffect, useRef, type ReactNode } from "react";

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (open && !dialog.current?.open) {
      dialog.current?.showModal();
      cancel.current?.focus();
    } else if (!open) dialog.current?.close();
  }, [open]);
  return (
    <dialog
      ref={dialog}
      className="confirm"
      aria-labelledby="confirm-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <form
        method="dialog"
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy) onConfirm();
        }}
      >
        <h2 id="confirm-title">{title}</h2>
        {children}
        {error && (
          <p className="bad" role="alert">
            {error}
          </p>
        )}
        <div className="row end">
          <button ref={cancel} type="button" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="danger" disabled={busy}>
            {confirmLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}
