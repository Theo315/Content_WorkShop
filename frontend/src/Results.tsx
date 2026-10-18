import { useEffect, useState } from "react";
import { Download, FileCheck2, Package } from "lucide-react";
import { downloadFile } from "./api";
import type { FileRef } from "./api";
import { Busy, ErrorMessage, Panel } from "./components";
import { downloadResultArchive, MAX_ARCHIVE_BYTES } from "./exportArchive";

export type Result = {
  file: FileRef;
  preview?: FileRef;
  label: string;
  detail?: string;
  warnings?: string[];
  zipPath?: string;
  metadata?: unknown;
  lastWrittenAt?: string;
};
export default function Results({
  items,
  type,
  stale = false,
  emptyTitle = "Поки немає файлів",
  emptyDescription = "Запустіть генерацію.",
  archiveName,
  forceArchive = false,
  archiveLabel = "Усі файли · ZIP",
}: {
  items: Result[];
  type: "png" | "pdf";
  stale?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  archiveName?: string;
  forceArchive?: boolean;
  archiveLabel?: string;
}) {
  const [active, setActive] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setActive(0);
    setError("");
  }, [items[0]?.file.token]);
  const item = items[Math.min(active, items.length - 1)];
  async function save(result: Result) {
    setBusy(true);
    try {
      setError("");
      if (result.lastWrittenAt) {
        await downloadResultArchive([{ ...result, zipPath: result.file.name }], result.file.name.replace(/\.[^.]+$/, "") + ".zip");
      } else await downloadFile(result.file);
    } catch (e) {
      setError((e as Error).message);
    } finally { setBusy(false); }
  }
  async function zip() {
    setBusy(true);
    setError("");
    try {
      await downloadResultArchive(items, archiveName ?? `content-${type}.zip`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Panel
      title="Результат"
      action={items.length > 0 ? <span className="count">{items.length}</span> : undefined}
      className="results-panel"
    >
      {!item ? (
        <div className="empty-preview">
          <div className="empty-icon">
            <FileCheck2 size={30} />
          </div>
          <h3>{emptyTitle}</h3>
          <p>{emptyDescription}</p>
        </div>
      ) : (
        <>
          {stale && (
            <p className="notice">
              Параметри змінено — це попередній результат.
            </p>
          )}
          {items.length > 1 && <div className="result-tabs" aria-label="Результати генерації">
            {items.map((r, i) => (
              <button
                className={i === active ? "active" : ""}
                key={`${r.file.token}-${i}`}
                onClick={() => setActive(i)}
                aria-pressed={i === active}
              >
                {r.label}
              </button>
            ))}
          </div>}
          <div
            className={`result-preview ${type === "pdf" ? "pdf-preview" : ""}`}
          >
            {type === "png" || item.preview ? (
              <img
                src={(item.preview ?? item.file).url}
                alt={`Результат: ${item.label}`}
              />
            ) : (
              <iframe title={`PDF: ${item.label}`} src={item.file.url} />
            )}
          </div>
          <div className="result-meta">
            <div>
              <strong>{item.file.name}</strong>
            </div>
            <button
              onClick={() => save(item)}
              disabled={busy}
              aria-label={`Завантажити ${item.file.name}${item.lastWrittenAt ? " з датою · ZIP" : ""}`}
            >
              <Download size={16} />
              {type.toUpperCase()}{item.lastWrittenAt ? " · ZIP" : ""}
            </button>
          </div>
          {item.lastWrittenAt && <p className="hint">LastWriteTime: {new Date(item.lastWrittenAt).toLocaleString("uk-UA", { timeZone: "Europe/Kyiv" })} · Київ · у ZIP</p>}
          {item.detail && <details className="result-detail"><summary>Параметри результату</summary><p>{item.detail}</p></details>}
          {item.warnings?.map((w, i) => (
            <p key={i} className="notice">
              {w}
            </p>
          ))}
          {(items.length > 1 || forceArchive) && (
            <button
              className="full-width"
              disabled={
                busy ||
                items.reduce((n, r) => n + r.file.size, 0) > MAX_ARCHIVE_BYTES
              }
              onClick={zip}
            >
              <Package size={17} />
              {archiveLabel}
            </button>
          )}
          {items.reduce((n, r) => n + r.file.size, 0) > MAX_ARCHIVE_BYTES && (
            <p className="notice">
              Понад 250 МБ: завантажуйте файли окремо, щоб не перевантажувати
              браузер.
            </p>
          )}
        </>
      )}
      {busy && <Busy text="Формуємо ZIP…" />}
      <ErrorMessage message={error} />
    </Panel>
  );
}
