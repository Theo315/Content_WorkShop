import { useEffect, useRef, useState, type CSSProperties } from "react";
import {
  Check,
  Crop,
  Paintbrush,
  Play,
  Sparkles,
  Upload as UploadIcon,
} from "lucide-react";
import { ApiError, post, request, uploadFile } from "./api";
import type { FileRef } from "./api";
import { Busy, ErrorMessage, Field, Panel, Upload } from "./components";
import MaskEditor from "./MaskEditor";
import Results from "./Results";
import type { Result } from "./Results";
import TimestampFields, { planLastWrittenAt, type TimestampConfig } from "./TimestampFields";
import TemplatePicker from "./TemplatePicker";
import { catalogWithConcurrentSaves, rememberSavedReference, referenceSaveWarnings, upsertReference, type AutoSavedReference } from "./auto-reference";
import type { PlateTextMode } from "./plate-label";

const DEFAULT_KEY = "#1E9E52";

export type Config = {
  brand_name: string;
  plate_text_mode?: PlateTextMode;
  plate_text?: string;
  model_name?: string;
  plate_box?: number[] | null;
  key_color?: string | null;
  font_size?: number;
  auto_font_size?: boolean;
  text_color?: number[];
  plate_bg_color?: number[] | "auto" | null;
};
export type Template = {
  id: string;
  builtin: boolean;
  name: string;
  width: number;
  height: number;
  image: FileRef;
  car_mask: FileRef | null;
  marker_mask: FileRef | null;
  config: Config;
};
type Color = {
  code: string;
  hex: string;
  metallic: boolean;
  error: string | null;
};
type Render = {
  file: FileRef;
  preview: FileRef;
  metrics: { body_delta_e: number; marker_delta_e: number | null };
  warnings: string[];
  plate_text: string | null;
  auto_saved_template?: AutoSavedReference<Template>;
  reference_save_warning?: string;
};
export default function PngTool({ maxUpload, settingsVersion, onTemplatesChanged, active }: {
  maxUpload: number;
  active: boolean;
  settingsVersion: number;
  onTemplatesChanged: () => void;
}) {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [selected, setSelected] = useState("");
  const selectedSource = useRef("");
  const catalogRevision = useRef(0);
  const catalogSaves = useRef(new Map<string, { revision: number; template: Template }>());
  const [custom, setCustom] = useState<File | null>(null);
  const [source, setSource] = useState("");
  const [aspect, setAspect] = useState(1);
  const [maskFallback, setMaskFallback] = useState(false);
  const [fallbackKey, setFallbackKey] = useState("");
  const [customRef, setCustomRef] = useState<FileRef | null>(null);
  const [savedReference, setSavedReference] = useState<AutoSavedReference<Template> | null>(null);
  const [referenceWarning, setReferenceWarning] = useState("");
  const [config, setConfig] = useState<Config>({
    brand_name: "Asturcolor",
    model_name: "",
  });
  const [car, setCar] = useState<FileRef | null>(null);
  const [marker, setMarker] = useState<FileRef | null>(null);
  const [editor, setEditor] = useState<"car" | "marker" | "plate" | null>(null);
  const [colorsText, setColorsText] = useState("");
  const [colorsFormat, setColorsFormat] = useState<"list" | "csv">("list");
  const [colors, setColors] = useState<Color[]>([]);
  const [plate, setPlate] = useState(true);
  const [timestamp, setTimestamp] = useState<TimestampConfig>({ enabled: false, start: "", step: "6-8" });
  const [results, setResults] = useState<Result[]>([]);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [progress, setProgress] = useState(0);
  const cancelled = useRef(false);
  const [previewMode, setPreviewMode] = useState<"original" | "result">(
    "original",
  );
  useEffect(() => {
    let active = true;
    const requestRevision = catalogRevision.current;
    request<{ templates: Template[] }>("/api/png/templates")
      .then((r) => {
        if (!active) return;
        const catalog = catalogWithConcurrentSaves(r.templates, catalogSaves.current.values(), requestRevision);
        setTemplates(catalog);
        // The source may have changed while this request was in flight.
        if (settingsVersion && selectedSource.current) {
          const current = catalog.find(t => t.id === selectedSource.current);
          if (current) {
            setConfig(previous => ({ ...previous, key_color: current.config.key_color }));
            setSource(current.image.url);
          } else { selectedSource.current = ""; setSelected(""); setSource(""); setCar(null); setMarker(null); }
        }
      })
      .catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [settingsVersion]);
  useEffect(() => {
    setColors([]);
  }, [colorsText, colorsFormat]);
  useEffect(() => {
    setStale(true);
    setPreviewMode("original");
  }, [
    selected,
    customRef,
    config,
    colorsText,
    plate,
    car,
    marker,
    timestamp,
  ]);
  function choose(id: string) {
    selectedSource.current = id;
    setSelected(id);
    setCustom(null);
    setCustomRef(null);
    setSavedReference(null);
    setReferenceWarning("");
    setCar(null);
    setMarker(null);
    setError("");
    setStatus("");
    const t = templates.find((t) => t.id === id);
    setSource(t?.image.url ?? "");
    setAspect(t ? t.width / t.height : 1);
    setMaskFallback(false);
    setFallbackKey(t?.config.key_color ?? "");
    setConfig(t?.config ?? { brand_name: "Asturcolor", model_name: "" });
  }
  useEffect(() => () => { if (source.startsWith("blob:")) URL.revokeObjectURL(source); }, [source]);

  function checked(file: File) {
    if (file.size > maxUpload)
      throw new Error(
        `Максимальний розмір файлу: ${Math.round(maxUpload / 1024 / 1024)} МБ.`,
      );
  }
  async function setTemplate(file: File) {
    if (busy) return;
    try {
      checked(file);
      if (!file.name.toLowerCase().endsWith(".png"))
        throw new Error("Шаблон має бути PNG.");
      setBusy(true);
      setStatus("Завантаження шаблону…");
      setSavedReference(null);
      setReferenceWarning("");
      const ref = await uploadFile(file);
      selectedSource.current = "";
      setSelected("");
      setCustom(file);
      setSource(URL.createObjectURL(file));
      setMaskFallback(false);
      setFallbackKey("");
      setCustomRef(ref);
      setCar(null);
      setMarker(null);
      setConfig({
        brand_name: "Asturcolor",
        model_name: file.name.replace(/\.png$/i, ""),
      });
      setError("");
      setStatus("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function setMask(
    kind: "car" | "marker",
    file: Blob,
    filename?: string,
  ) {
    try {
      if (file.size > maxUpload)
        throw new Error("Маска перевищує ліміт файлу.");
      setBusy(true);
      setStatus("Збереження маски…");
      const ref = await uploadFile(file, filename);
      if (kind === "car") setCar(ref);
      else setMarker(ref);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function saveEditor(value: string | number[]) {
    if (editor === "plate") {
      setConfig((c) => ({ ...c, plate_box: value as number[] }));
      return;
    }
    const blob = await (await fetch(value as string)).blob();
    await setMask(editor as "car" | "marker", blob, `${editor}_mask.png`);
  }
  async function parseColors() {
    const r = await post<{ colors: Color[] }>("/api/png/colors", {
      text: colorsText,
      format: colorsFormat,
    });
    setColors(r.colors);
    return r.colors;
  }
  function showMaskFallback(e: unknown) {
    if (e instanceof ApiError && e.code === "key_color_not_found") setMaskFallback(true);
    setError((e as Error).message);
  }
  async function autoMask(key = DEFAULT_KEY) {
    try {
      setBusy(true);
      setStatus("Будуємо маску за ключовим кольором…");
      const r = await post<{ file: FileRef }>("/api/png/mask", {
        ...input(),
        kind: "car",
        backend: "chroma",
        key_color: key,
        positives: [],
        negatives: [],
        rects: [],
        tolerance: 30,
      });
      setCar(r.file);
      setConfig(previous => ({ ...previous, key_color: key }));
      if (key === DEFAULT_KEY) setMaskFallback(false);
      setError("");
    } catch (e) {
      showMaskFallback(e);
    } finally {
      setStatus("");
      setBusy(false);
    }
  }
  function input() {
    return selected
      ? { template_id: selected }
      : { template_token: customRef?.token };
  }
  function zipPath(color: Color) {
    const safe = color.code.replace(/[<>:"/\\|?*\x00-\x1f]/g, "-").replace(/[. ]+$/, "").slice(0, 100);
    return (safe || "_") + ".png";
  }
  async function generate(preview: boolean) {
    setBusy(true);
    setError("");
    setReferenceWarning("");
    setProgress(0);
    setStatus("Перевіряємо кольори…");
    cancelled.current = false;
    try {
      if (!selected && !customRef)
        throw new Error("Оберіть або завантажте PNG-шаблон.");
      const list = await parseColors();
      if (list.some((c) => c.error))
        throw new Error("Виправте помилки кольорів перед генерацією.");
      const batch = preview ? list.slice(0, 2) : list;
      const batchId = crypto.randomUUID();
      const paths = batch.map(zipPath);
      const writtenTimes = planLastWrittenAt(timestamp, batch.length);
      if (new Set(paths.map((p) => p.toLowerCase())).size !== paths.length)
        throw new Error(
          "Коди кольорів утворюють однакові назви файлів. Перевірте список.",
        );
      setResults([]);
      setStale(false);
      const batchStarted = performance.now();
      const template = templates.find(entry => entry.id === selected);
      const renderConfig = {
        ...config, auto_font_size: true,
        key_color: car || template?.car_mask ? config.key_color : DEFAULT_KEY,
      };
      async function renderColor(color: Color, index: number) {
        const started = performance.now();
        const r = await post<Render>("/api/png/render", {
          ...input(),
          car_mask_token: car?.token,
          marker_mask_token: marker?.token,
          config: renderConfig,
          color: { code: color.code, hex: color.hex, metallic: color.metallic },
          feather: 1,
          draw_plate: plate,
          save_reference: index === 0,
          batch_id: batchId,
          batch_index: index,
          batch_size: batch.length,
          zip_path: paths[index],
          last_written_at: writtenTimes[index],
        });
        return { r, elapsed: (performance.now() - started) / 1000 };
      }
      let completed = 0;
      function accept(index: number, { r, elapsed }: Awaited<ReturnType<typeof renderColor>>) {
        const color = batch[index];
        const writtenAt = writtenTimes[index];
        if (r.auto_saved_template) {
          const saved = r.auto_saved_template;
          catalogSaves.current.set(saved.template.id, { revision: ++catalogRevision.current, template: saved.template });
          setTemplates(previous => upsertReference(previous, saved.template));
          setSavedReference(previous => rememberSavedReference(previous, saved));
          setReferenceWarning("");
          onTemplatesChanged();
        }
        if (r.reference_save_warning) setReferenceWarning(r.reference_save_warning);
        setResults((previous) => [
          ...previous,
          {
            ...r,
            warnings: referenceSaveWarnings(r.warnings, r.reference_save_warning),
            label: color.code,
            detail: `ΔE кузова ${r.metrics.body_delta_e.toFixed(2)}${r.metrics.marker_delta_e !== null ? ` · маркера ${r.metrics.marker_delta_e.toFixed(2)}` : ""} · ${elapsed.toFixed(2)} с`,
            zipPath: paths[index],
            lastWrittenAt: writtenAt,
            metadata: { color, metrics: r.metrics, plate_text: r.plate_text, generation_seconds: elapsed, batch_id: batchId, last_written_at: writtenAt },
          },
        ]);
        setProgress(Math.round((++completed / batch.length) * 100));
        setPreviewMode("result");
      }
      for (let index = 0; index < batch.length && !cancelled.current;) {
        // Persist a custom source on the first successful image before the other
        // requests start. Two in flight overlap storage with image processing.
        const size = customRef && index === 0 ? 1 : 2;
        const group = batch.slice(index, index + size);
        setStatus(`Генеруємо ${group.map(color => color.code).join(", ")} · ${index + 1}–${index + group.length} з ${batch.length}`);
        const responses = await Promise.allSettled(group.map((color, offset) => renderColor(color, index + offset)));
        let failure: PromiseRejectedResult | undefined;
        responses.forEach((response, offset) => {
          if (response.status === "fulfilled") accept(index + offset, response.value);
          else failure ??= response;
        });
        if (failure) throw failure.reason;
        index += group.length;
      }
      const average = completed ? (performance.now() - batchStarted) / (1000 * completed) : 0;
      setStatus(cancelled.current ? "Зупинено. Готові файли доступні для завантаження."
        : `Генерацію завершено · ${average.toLocaleString("uk-UA", { maximumFractionDigits: 2 })} с/фото.`);
    } catch (e) {
      showMaskFallback(e);
    } finally {
      setBusy(false);
    }
  }
  const template = templates.find((t) => t.id === selected);
  const useBrand = !config.plate_text_mode || config.plate_text_mode === "brand_code" || config.plate_text_mode === "brand";
  return (
    <div className="tool-grid png-grid" data-preview={previewMode} style={{ "--png-aspect": aspect } as CSSProperties}>
      <div className="form-column">
        <div className="form-scroll">
        <fieldset disabled={busy}>
          <Panel title="Шаблон" className="png-source" action={source && <button type="button" onClick={() => void autoMask()}>
            <Sparkles size={15} />Автомаска{car && <Check size={14} aria-label="Маска готова" />}
          </button>}>
            <div className="png-source-row">
              <TemplatePicker templates={templates} selected={selected} onChoose={choose} disabled={busy} />
              <Upload label="Власний PNG" accept="image/png,.png" file={custom} onFile={setTemplate} compact />
            </div>
            {savedReference && <p className="settings-success" role="status" aria-live="polite">
              <Check size={14} />{savedReference.created ? "Референс збережено" : "Референс уже збережений"}
            </p>}
            {referenceWarning && <p className="notice" role="alert">{referenceWarning}</p>}
            {source && maskFallback && <div className="mask-fallback">
              <Field label="Інший ключовий HEX">
                <input value={fallbackKey} placeholder="#1E9E52" onChange={event => setFallbackKey(event.target.value)} />
              </Field>
              <button type="button" disabled={!/^#?[0-9a-f]{6}$/i.test(fallbackKey.trim())}
                onClick={() => void autoMask(fallbackKey.trim())}>Застосувати HEX</button>
              <button type="button" onClick={() => setEditor("car")}><Paintbrush size={15} />Маска кузова</button>
              <button type="button" onClick={() => setEditor("marker")}><Paintbrush size={15} />Маска маркера</button>
            </div>}
          </Panel>
          {source && <Panel title="Номерний знак" className="plate-options" action={
            <button onClick={() => setEditor("plate")}><Crop size={14} />{config.plate_box ? "Область" : "Виділити"}</button>
          }>
            <label className="check-label">
              <input type="checkbox" checked={plate} onChange={e => setPlate(e.target.checked)} />
              Змінювати напис
            </label>
            {plate ? <>
              <Field label="Що писати на номері">
                <select value={config.plate_text_mode ?? "brand_code"} onChange={e => setConfig({ ...config, plate_text_mode: e.target.value as PlateTextMode })}>
                  <option value="brand_code">Бренд + код</option>
                  <option value="code">Лише код</option>
                  <option value="brand">Лише бренд · без коду</option>
                  <option value="custom">Власний текст</option>
                  <option value="blank">Порожній номер</option>
                </select>
              </Field>
              {config.plate_text_mode === "custom" && <Field label="Власний напис" hint="Саме цей текст, без автоматичного додавання бренду чи коду.">
                <input value={config.plate_text ?? ""} maxLength={200} placeholder="Наприклад: MY PAINT" onChange={e => setConfig({ ...config, plate_text: e.target.value })} />
              </Field>}
              {!config.plate_box && <p className="hint">Виділіть область номерного знака. Текст автоматично впишеться в неї.</p>}
            </> : <p className="hint">Залишиться оригінальний напис із шаблону.</p>}
          </Panel>}
          <Panel
            title="Кольори"
            className="png-colors"
            action={
              <label className="upload-link">
                <UploadIcon size={14} />
                CSV
                <input
                  type="file"
                  accept=".csv,.txt"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (f) {
                      try {
                        checked(f);
                        setColorsText(await f.text());
                        setColorsFormat(
                          f.name.endsWith(".csv") ? "csv" : "list",
                        );
                      } catch (err) {
                        setError((err as Error).message);
                      }
                    }
                  }}
                />
              </label>
            }
          >
            <Field
              label="Код · HEX"
              hint="1 рядок = 1 колір · metallic необов’язково"
            >
              <textarea
                className="code-input"
                rows={7}
                placeholder={"1KGA #7FA9B5\n2DB #9B9D9B metallic\n575 #245B8E"}
                value={colorsText}
                onChange={(e) => { setColorsText(e.target.value); setColorsFormat("list"); }}
              />
            </Field>
            <div className="color-summary">
              <span>
                {colors.length
                  ? `${colors.filter((c) => !c.error).length} кольорів перевірено`
                  : "До 200 кольорів"}
              </span>
              <button
                className="text-button"
                onClick={async () => {
                  try {
                    await parseColors();
                    setError("");
                  } catch (e) {
                    setError((e as Error).message);
                  }
                }}
                disabled={!colorsText.trim()}
              >
                Перевірити
              </button>
            </div>
            {colors.length > 0 && (
              <div className="swatches">
                {colors.map((c, i) => (
                  <span
                    className={c.error ? "swatch invalid" : "swatch"}
                    key={i}
                    title={c.error ?? c.hex}
                  >
                    <i style={{ background: c.error ? "#d64949" : c.hex }} />
                    {c.code}
                    {c.error && <small>{c.error}</small>}
                  </span>
                ))}
              </div>
            )}
          </Panel>
          <div className="png-export-inline">
            <div className="png-brand">
              <label className="check-label"><input type="checkbox" checked={useBrand}
                disabled={!plate || config.plate_text_mode === "custom"}
                onChange={event => setConfig(previous => ({ ...previous, plate_text_mode: event.target.checked
                  ? previous.plate_text_mode === "blank" ? "brand" : "brand_code"
                  : previous.plate_text_mode === "brand" ? "blank" : "code" }))} />Бренд</label>
              <input aria-label="Бренд на номері" value={config.brand_name} maxLength={120}
                disabled={!plate || !useBrand}
                onChange={event => setConfig(previous => ({ ...previous, brand_name: event.target.value }))} />
            </div>
            <TimestampFields value={timestamp} onChange={setTimestamp} />
          </div>
        </fieldset>
        <ErrorMessage message={error} />
        {busy && <Busy text={status} progress={progress} />}
        </div>
        <div className="form-footer">
        <div className="action-bar">
          <button
            disabled={busy || !source || !colorsText.trim()}
            onClick={() => generate(true)}
          >
            Проба · 2 кольори
          </button>
          {busy ? (
            <button
              onClick={() => {
                cancelled.current = true;
                setStatus("Зупиняємо після поточного файлу…");
              }}
            >
              Зупинити
            </button>
          ) : (
            <button
              className="primary"
              disabled={!source || !colorsText.trim()}
              onClick={() => generate(false)}
            >
              <Play size={16} />
              Створити PNG
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
        <div className="preview-switch">
          <button
            className={previewMode === "original" ? "active" : ""}
            onClick={() => setPreviewMode("original")}
          >
            Шаблон
          </button>
          <button
            className={previewMode === "result" ? "active" : ""}
            disabled={!results.length}
            onClick={() => setPreviewMode("result")}
          >
            Результат{results.length > 0 && ` (${results.length})`}
          </button>
        </div>
        {previewMode === "original" ? (
          <Panel className="preview-panel">
            <div className="template-preview">
              {source ? (
                <img src={source} alt="Обраний PNG-шаблон" onLoad={event => {
                  const image = event.currentTarget;
                  if (image.naturalHeight) setAspect(image.naturalWidth / image.naturalHeight);
                }} />
              ) : (
                <div className="empty-preview">
                  <div className="empty-icon">
                    <Paintbrush size={30} />
                  </div>
                  <h3>Оберіть машину або PNG</h3>
                </div>
              )}
            </div>
            {source && (
              <div className="preview-caption">
                <span>{custom?.name ?? template?.name}</span>
                {template && <span>{template.width} × {template.height}</span>}
              </div>
            )}
          </Panel>
        ) : (
          <Results items={results} type="png" stale={stale} keyboardNavigation={active && !editor} />
        )}
      </aside>
      {editor && (
        <MaskEditor
          source={source}
          mode={editor}
          initial={
            editor === "car"
              ? (car ?? template?.car_mask)?.url
              : editor === "marker"
                ? (marker ?? template?.marker_mask)?.url
                : undefined
          }
          onClose={() => setEditor(null)}
          onSave={saveEditor}
        />
      )}
    </div>
  );
}
