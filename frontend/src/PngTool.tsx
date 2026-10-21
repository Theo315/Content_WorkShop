import { useEffect, useRef, useState } from "react";
import {
  Check,
  Crop,
  Paintbrush,
  Play,
  Sparkles,
  Upload as UploadIcon,
} from "lucide-react";
import { post, readDataUrl, request, uploadFile } from "./api";
import type { FileRef } from "./api";
import { Busy, ErrorMessage, Field, Panel, Upload } from "./components";
import MaskEditor from "./MaskEditor";
import Results from "./Results";
import type { Result } from "./Results";
import TimestampFields, { planLastWrittenAt, type TimestampConfig } from "./TimestampFields";
import ExportSheet from "./ExportSheet";
import { catalogWithConcurrentSaves, rememberSavedReference, referenceSaveWarnings, upsertReference, type AutoSavedReference } from "./auto-reference";
import { plateLabel, type PlateTextMode } from "./plate-label";

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
export default function PngTool({ maxUpload, settingsVersion, onTemplatesChanged }: {
  maxUpload: number;
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
  const [lightness, setLightness] = useState("match");
  const [feather, setFeather] = useState(1);
  const [plate, setPlate] = useState(true);
  const [pattern, setPattern] = useState("{brand}/{model}/{code}");
  const [timestamp, setTimestamp] = useState<TimestampConfig>({ enabled: false, start: "", step: "6-8" });
  const [exportOpen, setExportOpen] = useState(false);
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
    lightness,
    feather,
    plate,
    car,
    marker,
    pattern,
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
    const t = templates.find((t) => t.id === id);
    setSource(t?.image.url ?? "");
    setConfig(t?.config ?? { brand_name: "Asturcolor", model_name: "" });
  }
  function checked(file: File) {
    if (file.size > maxUpload)
      throw new Error(
        `Максимальний розмір файлу: ${Math.round(maxUpload / 1024 / 1024)} МБ.`,
      );
  }
  async function setTemplate(file: File) {
    try {
      checked(file);
      if (!file.name.toLowerCase().endsWith(".png"))
        throw new Error("Шаблон має бути PNG.");
      setBusy(true);
      setStatus("Завантаження шаблону…");
      setSavedReference(null);
      setReferenceWarning("");
      const src = await readDataUrl(file);
      const ref = await uploadFile(file);
      selectedSource.current = "";
      setSelected("");
      setCustom(file);
      setSource(src);
      setCustomRef(ref);
      setCar(null);
      setMarker(null);
      setConfig({
        brand_name: "Asturcolor",
        model_name: file.name.replace(/\.png$/i, ""),
      });
      setError("");
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
  async function importConfig(file: File) {
    try {
      checked(file);
      const raw = JSON.parse(await file.text());
      if (typeof raw.brand_name !== "string")
        throw new Error(
          "JSON має містити brand_name. Файл кліків маски не є конфігом.",
        );
      const { font_path: _ignored, ...rest } = raw;
      setConfig(rest);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function parseColors() {
    const r = await post<{ colors: Color[] }>("/api/png/colors", {
      text: colorsText,
      format: colorsFormat,
    });
    setColors(r.colors);
    return r.colors;
  }
  async function autoMask() {
    try {
      setBusy(true);
      setStatus("Будуємо маску за ключовим кольором…");
      const r = await post<{ file: FileRef }>("/api/png/mask", {
        ...input(),
        kind: "car",
        backend: "chroma",
        key_color: config.key_color,
        positives: [],
        negatives: [],
        rects: [],
        tolerance: 30,
      });
      setCar(r.file);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function input() {
    return selected
      ? { template_id: selected }
      : { template_token: customRef?.token };
  }
  function zipPath(color: Color) {
    const safe = (s: string) =>
      s
        .replace(/[<>:"/\\|?*\x00-\x1f]/g, "-")
        .replace(/[. ]+$/, "")
        .slice(0, 100) || "_";
    const values: Record<string, string> = {
      brand: config.brand_name,
      model: config.model_name ?? "",
      code: color.code,
      hex: color.hex.slice(1),
      template: custom?.name.replace(/\.png$/i, "") ?? selected,
    };
    if (
      pattern.startsWith("/") ||
      pattern.split("/").includes("..") ||
      /\{([^}]+)\}/g.test(
        pattern.replace(/\{(brand|model|code|hex|template)\}/g, ""),
      )
    )
      throw new Error(
        "Некоректний шаблон назви. Доступні {brand}, {model}, {code}, {hex}, {template}.",
      );
    return (
      pattern
        .split("/")
        .map((part) =>
          safe(
            part.replace(/\{([^}]+)\}/g, (_, key: string) => values[key] ?? ""),
          ),
        )
        .join("/") + ".png"
    );
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
      if (!selected && !car && !config.key_color)
        throw new Error(
          "Для власного шаблону потрібна маска кузова або ключовий колір.",
        );
      const list = await parseColors();
      if (list.some((c) => c.error))
        throw new Error("Виправте помилки кольорів перед генерацією.");
      const batch = preview ? list.slice(0, 2) : list;
      const batchId = crypto.randomUUID();
      const paths = batch.map(zipPath);
      const writtenTimes = planLastWrittenAt(timestamp, batch.length);
      if (new Set(paths.map((p) => p.toLowerCase())).size !== paths.length)
        throw new Error(
          "Назви файлів повторюються. Додайте {code} до шаблону назви.",
        );
      setResults([]);
      setStale(false);
      const refreshedReferences = new Set<string>();
      for (let index = 0; index < batch.length; index++) {
        if (cancelled.current) break;
        const color = batch[index];
        const writtenAt = writtenTimes[index];
        setStatus(`Генеруємо ${color.code} · ${index + 1} з ${batch.length}`);
        const r = await post<Render>("/api/png/render", {
          ...input(),
          car_mask_token: car?.token,
          marker_mask_token: marker?.token,
          config,
          color: { code: color.code, hex: color.hex, metallic: color.metallic },
          lightness_mode: lightness,
          feather,
          draw_plate: plate,
          batch_id: batchId,
          batch_index: index,
          batch_size: batch.length,
          zip_path: paths[index],
          last_written_at: writtenAt,
        });
        if (r.auto_saved_template) {
          const saved = r.auto_saved_template;
          catalogSaves.current.set(saved.template.id, { revision: ++catalogRevision.current, template: saved.template });
          setTemplates(previous => upsertReference(previous, saved.template));
          setSavedReference(previous => rememberSavedReference(previous, saved));
          setReferenceWarning("");
          if (!refreshedReferences.has(saved.template.id)) {
            refreshedReferences.add(saved.template.id);
            onTemplatesChanged();
          }
        }
        if (r.reference_save_warning) setReferenceWarning(r.reference_save_warning);
        setResults((previous) => [
          ...previous,
          {
            ...r,
            warnings: referenceSaveWarnings(r.warnings, r.reference_save_warning),
            label: color.code,
            detail: `ΔE кузова ${r.metrics.body_delta_e.toFixed(2)}${r.metrics.marker_delta_e !== null ? ` · маркера ${r.metrics.marker_delta_e.toFixed(2)}` : ""}`,
            zipPath: paths[index],
            lastWrittenAt: writtenAt,
            metadata: { color, metrics: r.metrics, plate_text: r.plate_text, batch_id: batchId, last_written_at: writtenAt },
          },
        ]);
        setProgress(Math.round(((index + 1) / batch.length) * 100));
        setPreviewMode("result");
      }
      setStatus(
        cancelled.current
          ? "Зупинено. Готові файли доступні для завантаження."
          : "Генерацію завершено.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const template = templates.find((t) => t.id === selected);
  return (
    <div className="tool-grid">
      <div className="form-column">
        <div className="form-scroll">
        <fieldset disabled={busy}>
          <Panel title="Шаблон">
            <Field label="Машина">
              <select value={selected} onChange={(e) => choose(e.target.value)}>
                <option value="">Оберіть машину</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </Field>
            <Upload
              label="Власний PNG"
              accept="image/png,.png"
              file={custom}
              onFile={setTemplate}
              compact
            />
            {savedReference && <p className="settings-success" role="status" aria-live="polite">
              <Check size={14} />{savedReference.created ? "Референс збережено" : "Референс уже збережений"}
            </p>}
            {referenceWarning && <p className="notice" role="alert">{referenceWarning}</p>}
            {source && (
              <details className="source-options">
                <summary>Маски</summary>
                <div className="mask-controls">
                  <div>
                    <span className="mask-check">
                      {car || template?.car_mask ? (
                        <Check size={15} />
                      ) : (
                        <Paintbrush size={15} />
                      )}
                      Кузов
                    </span>
                    <button onClick={() => setEditor("car")}>
                      Редагувати
                    </button>
                    <label className="upload-link">
                      Завантажити
                      <input
                        type="file"
                        accept="image/png"
                        onChange={(e) => {
                          if (e.target.files?.[0])
                            void setMask("car", e.target.files[0]);
                        }}
                      />
                    </label>
                  </div>
                  <div>
                    <span className="mask-check">
                      {marker || template?.marker_mask ? (
                        <Check size={15} />
                      ) : (
                        <Paintbrush size={15} />
                      )}
                      Маркер
                    </span>
                    <button onClick={() => setEditor("marker")}>
                      Редагувати
                    </button>
                    <label className="upload-link">
                      Завантажити
                      <input
                        type="file"
                        accept="image/png"
                        onChange={(e) => {
                          if (e.target.files?.[0])
                            void setMask("marker", e.target.files[0]);
                        }}
                      />
                    </label>
                  </div>
                </div>
                <div className="key-mask-row">
                  <Field
                    label="Ключовий колір кузова"
                  >
                    <input
                      placeholder="#1E9E52"
                      value={config.key_color ?? ""}
                      onChange={(e) =>
                        setConfig({ ...config, key_color: e.target.value })
                      }
                    />
                  </Field>
                  <button
                    className="auto-mask-button"
                    disabled={!config.key_color}
                    onClick={autoMask}
                  >
                    <Sparkles size={16} />
                    Автомаска
                  </button>
                  <p className="hint">HEX вихідного кузова</p>
                </div>
              </details>
            )}
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
              {(!config.plate_text_mode || config.plate_text_mode === "brand_code" || config.plate_text_mode === "brand") &&
                <Field label="Бренд на номері"><input value={config.brand_name} maxLength={120} placeholder="Будь-яка назва" onChange={e => setConfig({ ...config, brand_name: e.target.value })} /></Field>}
              {config.plate_text_mode === "custom" && <Field label="Власний напис" hint="Саме цей текст, без автоматичного додавання бренду чи коду.">
                <input value={config.plate_text ?? ""} maxLength={200} placeholder="Наприклад: MY PAINT" onChange={e => setConfig({ ...config, plate_text: e.target.value })} />
              </Field>}
              <div className="plate-label-preview"><span>Напис</span><output aria-label="Попередній текст номера">{plateLabel(config, colors.find(c => !c.error)?.code ?? "КОД") || "(порожній)"}</output></div>
              {(!config.plate_text_mode || config.plate_text_mode === "brand_code" || config.plate_text_mode === "code") && <p className="hint">Код підставляється з кожного рядка списку кольорів.</p>}
              {!config.plate_box && <p className="hint">Виділіть область номерного знака. Текст автоматично впишеться в неї.</p>}
            </> : <p className="hint">Залишиться оригінальний напис із шаблону.</p>}
          </Panel>}
          <Panel
            title="Кольори"
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
                rows={4}
                placeholder={"1KGA #7FA9B5\n2DB #9B9D9B metallic\n575 #245B8E"}
                value={colorsText}
                onChange={(e) => setColorsText(e.target.value)}
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
          <button className="export-trigger" onClick={() => setExportOpen(true)} aria-haspopup="dialog">
            Експорт <span>{timestamp.enabled ? "Last written time · увімкнено" : "Назви · Last written time · параметри"}</span>
          </button>
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
                <img src={source} alt="Обраний PNG-шаблон" />
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
          <Results items={results} type="png" stale={stale} />
        )}
      </aside>
      {exportOpen && <ExportSheet onClose={() => setExportOpen(false)}>
        <fieldset disabled={busy} className="export-grid">
            <div className="two-fields">
              <Field label="Бренд">
                <input
                  value={config.brand_name}
                  onChange={(e) =>
                    setConfig({ ...config, brand_name: e.target.value })
                  }
                />
              </Field>
              <Field label="Модель">
                <input
                  value={config.model_name ?? ""}
                  onChange={(e) =>
                    setConfig({ ...config, model_name: e.target.value })
                  }
                />
              </Field>
            </div>
            <Field label="Яскравість">
              <select
                value={lightness}
                onChange={(e) => setLightness(e.target.value)}
              >
                <option value="match">Відповідно до HEX-кольору</option>
                <option value="keep">Зберегти яскравість шаблону</option>
              </select>
            </Field>
              <Field
                label="Папки та назви у ZIP"
                hint="Підстановки: {brand}, {model}, {code}, {hex}, {template}"
              >
                <input
                  className="mono"
                  value={pattern}
                  onChange={(e) => setPattern(e.target.value)}
                />
              </Field>
              <div className="two-fields">
                <Field label="Згладжування маски, px">
                  <input
                    type="number"
                    min="0"
                    max="10"
                    step="0.5"
                    value={feather}
                    onChange={(e) => setFeather(Number(e.target.value))}
                  />
                </Field>
                <Field label="Розмір тексту номера" hint="Текст автоматично вписується в ширину й висоту області.">
                  <select
                    value={config.auto_font_size === false ? "manual" : "auto"}
                    onChange={(e) => setConfig({ ...config, auto_font_size: e.target.value === "auto" })}
                  >
                    <option value="auto">Автоматично · вписати в область</option>
                    <option value="manual">Задати максимальний розмір</option>
                  </select>
                  {config.auto_font_size === false && <input
                    aria-label="Максимальний розмір тексту номера, px"
                    type="number"
                    min="6"
                    max="300"
                    value={config.font_size ?? 34}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        font_size: Number(e.target.value),
                      })
                    }
                  />}
                </Field>
              </div>
              <TimestampFields value={timestamp} onChange={setTimestamp} />
              <label className="upload-link">
                <UploadIcon size={15} />
                Імпортувати конфіг JSON
                <input
                  type="file"
                  accept=".json"
                  onChange={(e) => {
                    if (e.target.files?.[0])
                      void importConfig(e.target.files[0]);
                  }}
                />
              </label>
        </fieldset>
      </ExportSheet>}
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
