import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

export default function ExportSheet({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const root = document.getElementById("root");
    const oldInert = root?.inert ?? false;
    const oldOverflow = document.body.style.overflow;
    if (root) root.inert = true;
    document.body.style.overflow = "hidden";
    const focusable = () => Array.from(dialog.current?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled):not([type="file"]), select:not(:disabled), textarea:not(:disabled), summary, a[href]',
    ) ?? []).filter(el => el.getClientRects().length > 0);
    focusable()[0]?.focus();
    function keydown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); close.current(); return; }
      if (event.key !== "Tab") return;
      const elements = focusable();
      const first = elements[0], last = elements.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      if (root) root.inert = oldInert;
      document.body.style.overflow = oldOverflow;
      opener?.focus();
    };
  }, []);
  return createPortal(
    <div className="modal-backdrop export-backdrop" onMouseDown={event => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <div ref={dialog} className="export-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header><h2 id={titleId}>Експорт PNG</h2><button className="icon-button" aria-label="Закрити експорт" onClick={onClose}><X size={18} /></button></header>
        <div className="export-sheet-body">{children}</div>
        <footer><button className="primary" onClick={onClose}>Готово</button></footer>
      </div>
    </div>, document.body,
  );
}
