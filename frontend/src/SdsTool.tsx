import { useEffect, useRef, useState } from "react";
import {
  ArrowUpRight,
  FileText,
  Play,
  Search,
} from "lucide-react";
import { post, request, uploadFile } from "./api";
import type { FileRef } from "./api";
import { Busy, ErrorMessage, Field, Panel, Upload } from "./components";
import Results from "./Results";
import type { Result } from "./Results";
import TimestampFields, { planLastWrittenAt, type TimestampConfig } from "./TimestampFields";

type Item = {
  code: string;
  brand: string;
  color: string;
  title: string;
  filename: string;
};
type Reference = {
  file: FileRef | null;
  available: boolean;
  pages: number;
  title_template: string;
  description: string;
};
type Generated = {
  file: FileRef;
  previews: FileRef[];
  validation: {
    pages: number;
    paint_title_regions_exact: number;
    clear_varnish_regions_exact: number;
    dates_preserved: boolean;
    protected_text_preserved: boolean;
  };
};
type Match = { page: number; text: string; rect: number[] };
export default function SdsTool({ maxUpload, settingsVersion }: { maxUpload: number; settingsVersion: number }) {
  const [tab, setTab] = useState<"generate" | "edit">("generate");
  const [mode, setMode] = useState<"template" | "exact">("template");
  const [reference, setReference] = useState<Reference | null>(null);
  const [customReferenceFile, setCustomReferenceFile] = useState<File | null>(
    null,
  );
  const [customReference, setCustomReference] = useState<FileRef | null>(null);
  const [titleTemplate, setTitleTemplate] = useState("");
  const [brand, setBrand] = useState("Seat");
  const [values, setValues] = useState("");
  const [exact, setExact] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [results, setResults] = useState<Result[]>([]);
  const [editResults, setEditResults] = useState<Result[]>([]);
  const [stale, setStale] = useState(false);
  const [editStale, setEditStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [progress, setProgress] = useState(0);
  const cancelled = useRef(false);
  const [file, setFile] = useState<File | null>(null);
  const [fileRef, setFileRef] = useState<FileRef | null>(null);
  const [search, setSearch] = useState("");
  const [replacement, setReplacement] = useState("");
  const [pages, setPages] = useState("all");
  const [caseSensitive, setCaseSensitive] = useState(true);
  const [replaceAll, setReplaceAll] = useState(true);
  const [matches, setMatches] = useState<Match[] | null>(null);
  const [timestamp, setTimestamp] = useState<TimestampConfig>({ enabled: false, start: "", step: "6-8" });
  useEffect(() => {
    let active = true;
    request<Reference>("/api/sds/reference")
      .then((r) => {
        if (!active) return;
        setReference(r);
        setTitleTemplate(previous => !previous || previous === reference?.title_template ? r.title_template : previous);
      })
      .catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [settingsVersion]);
  useEffect(() => {
    setItems([]);
    setStale(true);
  }, [mode, titleTemplate, brand, values, exact, customReference, timestamp]);
  useEffect(() => {
    setMatches(null);
    setEditStale(true);
  }, [fileRef, search, replacement, pages, caseSensitive, replaceAll]);
  useEffect(() => { setEditStale(true); }, [timestamp]);
  async function parse() {
    const r = await post<{ items: Item[] }>("/api/sds/parse", {
      mode,
      brand,
      title_template: titleTemplate,
      values_text: values,
      exact_text: exact,
    });
    setItems(r.items);
    return r.items;
  }
  async function generate() {
    setBusy(true);
    setGenerating(true);
    setError("");
    setStatus("Перевіряємо назви документів…");
    setProgress(0);
    cancelled.current = false;
    try {
      const list = await parse();
      // Freeze the selected source for this batch; do not silently mix defaults.
      const batchReference = customReference ?? (await request<Reference>("/api/sds/reference")).file;
      if (!batchReference) throw new Error("PDF-шаблон видалено. Встановіть новий у налаштуваннях або завантажте власний PDF.");
      const batchId = crypto.randomUUID();
      const writtenTimes = planLastWrittenAt(timestamp, list.length);
      setResults([]);
      setStale(false);
      for (let i = 0; i < list.length; i++) {
        if (cancelled.current) break;
        const item = list[i];
        setStatus(
          `Генеруємо ${item.code || item.title} · ${i + 1} з ${list.length}`,
        );
        const r = await post<Generated>("/api/sds/generate", {
          item,
          reference_token: batchReference.token,
          render_previews: true,
          preview_count: 1,
          batch_id: batchId,
          batch_index: i,
          batch_size: list.length,
          zip_path: item.filename,
          last_written_at: writtenTimes[i],
        }).catch((e: Error) => {
          throw new Error(`Рядок ${i + 1} (${item.code}): ${e.message}`);
        });
        setResults((previous) => [
          ...previous,
          {
            file: r.file,
            preview: r.previews[0],
            label: item.code || String(i + 1),
            zipPath: item.filename,
            lastWrittenAt: writtenTimes[i],
            metadata: {
              item,
              reference: batchReference.name,
              validation: r.validation,
              batch_id: batchId,
              last_written_at: writtenTimes[i],
            },
            detail: `${r.validation.pages} стор. · перевірено ${r.validation.paint_title_regions_exact} назв`,
            warnings:
              !r.validation.dates_preserved ||
              !r.validation.protected_text_preserved
                ? [
                    "Перевірка збереження тексту не пройдена. Перегляньте документ перед використанням.",
                  ]
                : [],
          },
        ]);
        setProgress(Math.round(((i + 1) / list.length) * 100));
      }
      setStatus(
        cancelled.current
          ? "Зупинено. Готові PDF доступні для завантаження."
          : "Генерацію завершено.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setGenerating(false);
    }
  }
  async function upload(file: File, target: "editor" | "reference" = "editor") {
    setError("");
    try {
      if (file.size > maxUpload)
        throw new Error(
          `Максимальний розмір: ${Math.round(maxUpload / 1024 / 1024)} МБ.`,
        );
      if (!file.name.toLowerCase().endsWith(".pdf"))
        throw new Error("Оберіть PDF-файл.");
      if (!file.size) throw new Error("PDF-файл порожній.");
      const header = await file.slice(0, 1024).text();
      if (!header.includes("%PDF-"))
        throw new Error(
          "Файл не містить заголовка PDF. Оберіть справжній PDF-документ.",
        );
      setBusy(true);
      setStatus(
        target === "reference"
          ? "Завантаження PDF-еталона…"
          : "Завантаження PDF…",
      );
      const ref = await uploadFile(file);
      if (target === "reference") {
        setCustomReferenceFile(file);
        setCustomReference(ref);
        setStatus(
          "Власний еталон завантажено. Сервер перевірить його структуру під час генерації.",
        );
      } else {
        setFile(file);
        setFileRef(ref);
        setStatus("PDF завантажено. Вкажіть текст для пошуку.");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function editInput() {
    return {
      file_token: fileRef?.token,
      search_text: search,
      replacement,
      pages_spec: pages,
      exact_case: caseSensitive,
      replace_all: replaceAll,
    };
  }
  async function find() {
    setBusy(true);
    setError("");
    setStatus("Шукаємо текст у документі…");
    try {
      const { replacement: _replacement, ...query } = editInput();
      const r = await post<{ matches: Match[] }>("/api/sds/find", query);
      setMatches(r.matches);
      setStatus(`Знайдено збігів: ${r.matches.length}.`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function edit() {
    setBusy(true);
    setError("");
    setStatus("Застосовуємо заміни до PDF…");
    try {
      const batchId = crypto.randomUUID();
      const writtenAt = planLastWrittenAt(timestamp, 1)[0];
      const zipPath = `${file?.name.replace(/\.pdf$/i, "") ?? "document"}_edited.pdf`;
      const r = await post<{
        file: FileRef;
        preview: FileRef;
        replacements: number;
        pages: number;
      }>("/api/sds/edit", { ...editInput(), batch_id: batchId, batch_index: 0, batch_size: 1,
        zip_path: zipPath, last_written_at: writtenAt });
      setEditResults([
        {
          file: r.file,
          preview: r.preview,
          label: "Відредагований PDF",
          zipPath,
          lastWrittenAt: writtenAt,
          metadata: { batch_id: batchId, last_written_at: writtenAt },
          detail: `${r.replacements} замін · ${r.pages} стор.`,
        },
      ]);
      setEditStale(false);
      setStatus(`Готово: ${r.replacements} замін.`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="tool-tabs" role="group" aria-label="Режим SDS">
        <button
          aria-pressed={tab === "generate"}
          className={tab === "generate" ? "active" : ""}
          disabled={busy}
          onClick={() => {
            setTab("generate");
            setError("");
            setStatus("");
          }}
        >
          Генерація
        </button>
        <button
          aria-pressed={tab === "edit"}
          className={tab === "edit" ? "active" : ""}
          disabled={busy}
          onClick={() => {
            setTab("edit");
            setError("");
            setStatus("");
          }}
        >
          Заміна тексту
        </button>
      </div>
      <div className="tool-grid sds-grid" data-mode={tab}>
        <div className="form-column">
          <div className="form-scroll">
          <fieldset disabled={busy}>
            {tab === "generate" ? (
              <>
                <Panel>
                  <div className="reference-card" title="Змінюються лише назви товарів; вихідний зміст SDS зберігається.">
                    <span className="document-icon">
                      <FileText size={25} />
                    </span>
                    <div>
                      <strong>
                        {customReference?.name ?? reference?.file?.name ?? (reference ? "PDF-шаблон не встановлено" : "Safety Data Sheet")}
                      </strong>
                      <small>
                        {customReference
                          ? "Власний PDF-еталон"
                          : reference
                            ? reference.file ? `${reference.pages} стор.` : "Встановіть PDF у налаштуваннях"
                            : "Завантаження шаблону…"}
                      </small>
                    </div>
                    {(customReference || reference?.file) && (
                      <a
                        className="icon-button"
                        href={customReference?.url ?? reference!.file!.url}
                        target="_blank"
                        rel="noreferrer"
                        aria-label="Відкрити PDF-шаблон"
                      >
                        <ArrowUpRight size={19} />
                      </a>
                    )}
                  </div>
                  {reference && !reference.file && !customReference && <button className="text-button" onClick={() => { window.location.hash = "settings"; }}>Налаштування PDF</button>}
                  <details>
                    <summary>Змінити PDF-еталон</summary>
                    <Upload
                      label="Власний PDF"
                      accept="application/pdf,.pdf"
                      file={customReferenceFile}
                      onFile={(next) => upload(next, "reference")}
                      compact
                    />
                    <p className="hint">
                      SDS: 41 стор. або 3-Marker: 49 стор. · до {Math.round(maxUpload / 1024 / 1024)} МБ.
                      Для інших PDF — «Заміна тексту».
                    </p>
                    {customReference && (
                      <button
                        className="full-width"
                        onClick={() => {
                          setCustomReference(null);
                          setCustomReferenceFile(null);
                          setError("");
                          setStatus("Обрано типовий еталон із налаштувань.");
                        }}
                      >
                        Повернути типовий еталон
                      </button>
                    )}
                  </details>
                </Panel>
                <Panel>
                  <div className="segmented">
                    <button
                      className={mode === "template" ? "active" : ""}
                      onClick={() => setMode("template")}
                    >
                      За шаблоном
                    </button>
                    <button
                      className={mode === "exact" ? "active" : ""}
                      onClick={() => setMode("exact")}
                    >
                      Готові назви
                    </button>
                  </div>
                  {mode === "template" ? (
                    <>
                      <Field label="Марка">
                        <input
                          value={brand}
                          onChange={(e) => setBrand(e.target.value)}
                        />
                      </Field>
                      <details className="title-options">
                        <summary title={titleTemplate}>Шаблон назви</summary>
                      <Field
                        label="Назва з підстановками"
                        hint="{BRAND} · {VALUE} · {CODE} · {COLOR}"
                      >
                        <textarea
                          rows={3}
                          value={titleTemplate}
                          onChange={(e) => setTitleTemplate(e.target.value)}
                        />
                      </Field>
                      </details>
                      <Field
                        label="Коди та кольори"
                        hint="Код і назва кольору · 1 товар на рядок"
                      >
                        <textarea
                          className="code-input"
                          rows={4}
                          value={values}
                          onChange={(e) => setValues(e.target.value)}
                          placeholder={
                            "1KGA Blue Metallic\n2DB Silver\n575 Deep Blue"
                          }
                        />
                      </Field>
                    </>
                  ) : (
                    <Field
                      label="Повні назви"
                      hint="Формат: CODE | BRAND | COLOR | TITLE. Один документ на рядок."
                    >
                      <textarea
                        className="code-input"
                        rows={6}
                        value={exact}
                        onChange={(e) => setExact(e.target.value)}
                        placeholder="1KGA | Seat | Blue | Повна назва продукту"
                      />
                    </Field>
                  )}
                  <button
                    className="full-width"
                    onClick={async () => {
                      try {
                        await parse();
                        setError("");
                      } catch (e) {
                        setError((e as Error).message);
                      }
                    }}
                    disabled={
                      mode === "template" ? !values.trim() : !exact.trim()
                    }
                  >
                    <Search size={16} />
                    Перевірити назви
                  </button>
                  {items.length > 0 && (
                    <details className="parsed-preview">
                      <summary>Перевірені назви · {items.length}</summary>
                    <div className="parsed-list">
                      {items.map((item, i) => (
                        <div key={i}>
                          <span className="badge">{item.code || i + 1}</span>
                          <p>
                            {item.title}
                            <small>{item.filename}</small>
                          </p>
                        </div>
                      ))}
                    </div>
                    </details>
                  )}
                </Panel>
              </>
            ) : (
              <>
                <Panel>
                  <Upload
                    label="Завантажити PDF"
                    accept="application/pdf,.pdf"
                    file={file}
                    onFile={upload}
                    compact
                  />
                  <p className="hint">
                    До {Math.round(maxUpload / 1024 / 1024)} МБ · потрібен текстовий шар.
                  </p>
                  {fileRef && (
                    <a
                      className="text-link"
                      href={fileRef.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Відкрити PDF
                      <ArrowUpRight size={15} />
                    </a>
                  )}
                </Panel>
                <Panel>
                  <Field label="Знайти текст">
                    <textarea
                      rows={2}
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      placeholder="Текст у вихідному PDF"
                    />
                  </Field>
                  <Field label="Замінити на">
                    <textarea
                      rows={2}
                      value={replacement}
                      onChange={(e) => setReplacement(e.target.value)}
                      placeholder="Новий текст"
                    />
                  </Field>
                  <Field
                    label="Сторінки"
                    hint="all — увесь документ, або номери: 1,3-5"
                  >
                    <input
                      value={pages}
                      onChange={(e) => setPages(e.target.value)}
                    />
                  </Field>
                  <label className="check-label">
                    <input
                      type="checkbox"
                      checked={caseSensitive}
                      onChange={(e) => setCaseSensitive(e.target.checked)}
                    />
                    Враховувати регістр
                  </label>
                  <label className="check-label">
                    <input
                      type="checkbox"
                      checked={replaceAll}
                      onChange={(e) => setReplaceAll(e.target.checked)}
                    />
                    Замінити всі збіги
                  </label>
                  <button
                    className="full-width"
                    disabled={!fileRef || !search.trim()}
                    onClick={find}
                  >
                    <Search size={16} />
                    Знайти збіги
                  </button>
                  {matches !== null && (
                    <div className="match-list">
                      <strong>
                        {matches.length
                          ? `Знайдено ${matches.length} збігів`
                          : "Збігів не знайдено"}
                      </strong>
                      {matches.slice(0, 40).map((m, i) => (
                        <div key={i}>
                          <span className="badge">Стор. {m.page}</span>
                          <span>{m.text}</span>
                        </div>
                      ))}
                      {matches.length > 40 && (
                        <small>Показано перші 40 збігів.</small>
                      )}
                    </div>
                  )}
                </Panel>
              </>
            )}
            <TimestampFields value={timestamp} onChange={setTimestamp} />
          </fieldset>
          <ErrorMessage message={error} />
          {busy && (
            <Busy text={status} progress={generating ? progress : undefined} />
          )}
          </div>
          <div className="form-footer">
          <div className="action-bar">
            {generating ? (
              <button
                onClick={() => {
                  cancelled.current = true;
                  setStatus("Зупиняємо після поточного PDF…");
                }}
              >
                Зупинити
              </button>
            ) : (
              <button
                className="primary full-width"
                disabled={
                  busy ||
                  (tab === "generate"
                    ? (!reference?.file && !customReference) ||
                      !(mode === "template" ? values.trim() : exact.trim())
                    : !matches?.length)
                }
                onClick={tab === "generate" ? generate : edit}
              >
                <Play size={16} />
                {tab === "generate" ? "Створити SDS" : "Застосувати"}
              </button>
            )}
          </div>
          {!busy && status && (
            <p className="hint" role="status">
              {status}
            </p>
          )}
          </div>
        </div>
        <aside className="preview-column">
          <Results
            items={tab === "generate" ? results : editResults}
            type="pdf"
            stale={tab === "generate" ? stale : editStale}
          />
          {(tab === "generate" ? results.length : editResults.length) > 0 && (
            <p className="hint">
              Прев’ю — перша сторінка. Повний документ — у PDF.
            </p>
          )}
        </aside>
      </div>
    </>
  );
}
