import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

export default function ConfirmationDialog({ title, message, confirmLabel, busy, danger = false, onCancel, onConfirm }: {
  title: string;
  message: string;
  confirmLabel: string;
  busy: boolean;
  danger?: boolean;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  const titleId = useId();
  const messageId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const current = useRef({ busy, onCancel });
  current.current = { busy, onCancel };
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const root = document.getElementById("root");
    const oldInert = root?.inert ?? false;
    const oldOverflow = document.body.style.overflow;
    if (root) root.inert = true;
    document.body.style.overflow = "hidden";
    cancelButton.current?.focus();
    function keydown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!current.current.busy) current.current.onCancel();
        return;
      }
      if (event.key !== "Tab") return;
      const elements = Array.from(dialog.current?.querySelectorAll<HTMLElement>("button:not(:disabled)") ?? []);
      const first = elements[0], last = elements.at(-1);
      if (!elements.length) { event.preventDefault(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      if (root) root.inert = oldInert;
      document.body.style.overflow = oldOverflow;
      if (opener?.isConnected && !opener.matches(":disabled")) opener.focus();
      else root?.querySelector<HTMLElement>('button:not(:disabled), a[href]')?.focus();
    };
  }, []);
  return createPortal(
    <div className="modal-backdrop confirmation-backdrop" onMouseDown={event => {
      if (event.target === event.currentTarget && !busy) onCancel();
    }}>
      <div ref={dialog} className="confirmation-dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={messageId} aria-busy={busy}>
        <h2 id={titleId}>{title}</h2>
        <p id={messageId}>{message}</p>
        <div className="confirmation-actions">
          <button ref={cancelButton} disabled={busy} onClick={onCancel}>Скасувати</button>
          <button className={danger ? "confirmation-danger" : "primary"} disabled={busy} onClick={() => void onConfirm()}>{busy ? "Виконуємо…" : confirmLabel}</button>
        </div>
      </div>
    </div>, document.body,
  );
}
