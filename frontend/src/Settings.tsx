import { useEffect, useRef, useState } from "react";
import { Check, FileText, Plus, Save, Trash2 } from "lucide-react";
import { request, uploadFile } from "./api";
import type { FileRef } from "./api";
import { Busy, ErrorMessage, Field, Panel, Upload } from "./components";
import type { Template } from "./PngTool";
import ConfirmationDialog from "./ConfirmationDialog";

type Preferences = {
  sds: { file: FileRef | null; available: boolean; pages: number; custom: boolean; title_template: string };
  templates: Template[];
  deleted_templates?: { id: string; name: string }[];
};
type Confirmation = { title: string; message: string; label: string; work: () => Promise<unknown>; success: string; danger?: boolean };
export default function Settings({ maxUpload, referencesVersion, onChanged }: {
  maxUpload: number;
  referencesVersion: number;
  onChanged: () => void;
}) {
  const [data, setData] = useState<Preferences | null>(null);
  const [title, setTitle] = useState("");
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [pdf, setPdf] = useState<File | null>(null);
  const [image, setImage] = useState<File | null>(null);
  const [carMask, setCarMask] = useState<File | null>(null);
  const [markerMask, setMarkerMask] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [key, setKey] = useState("#1E9E52");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const initialized = useRef(false);
  function applyData(next: Preferences, initial: boolean) {
    setData(next);
    if (initial) setTitle(next.sds.title_template);
    setKeys(previous => Object.fromEntries(next.templates.map(t => [
      t.id, initial ? (t.config.key_color ?? "") : (previous[t.id] ?? t.config.key_color ?? ""),
    ])));
    initialized.current = true;
  }
  async function load(initial = false) {
    const next = await request<Preferences>("/api/settings");
    applyData(next, initial);
  }
  useEffect(() => {
    let active = true;
    request<Preferences>("/api/settings")
      .then(next => { if (active) applyData(next, !initialized.current); })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [referencesVersion]);
  async function action(work: () => Promise<unknown>, success: string) {
    setBusy(true); setError(""); setMessage("");
    try {
      await work();
      onChanged();
      await load();
      setMessage(success);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function put<T>(path: string, value: unknown) {
    return request<T>(path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
  }
  function checked(file: File, extension: string) {
    if (file.size > maxUpload) throw new Error(`Максимальний розмір: ${Math.round(maxUpload / 1024 / 1024)} МБ.`);
    if (!file.name.toLowerCase().endsWith(extension)) throw new Error(`Потрібен файл ${extension.toUpperCase()}.`);
  }
  async function addTemplate() {
    if (!image) throw new Error("Завантажте PNG-шаблон.");
    checked(image, ".png");
    if (carMask) checked(carMask, ".png");
    if (markerMask) checked(markerMask, ".png");
    const imageRef = await uploadFile(image);
    const carRef = carMask ? await uploadFile(carMask) : null;
    const markerRef = markerMask ? await uploadFile(markerMask) : null;
    const saved = await request<Template>("/api/settings/templates", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() || image.name.replace(/\.png$/i, ""),
        image_token: imageRef.token, car_mask_token: carRef?.token, marker_mask_token: markerRef?.token,
        config: { brand_name: "Asturcolor", key_color: key.trim() || null } }),
    });
    setKeys(prev => ({ ...prev, [saved.id]: saved.config.key_color ?? "" }));
    setImage(null); setCarMask(null); setMarkerMask(null); setName("");
  }
  return <div className="settings-layout">
    <div className="settings-feedback">
      <ErrorMessage message={error} />
      {!data && !error && <Busy text="Завантажуємо налаштування…" />}
      {!data && error && <button onClick={() => void load(true).catch(e => setError(e.message))}>Повторити</button>}
      {busy && <Busy text="Зберігаємо…" />}
      {message && <p className="settings-success" role="status"><Check size={16} />{message}</p>}
    </div>
    <fieldset disabled={busy || !data} className="settings-fields">
      <div className="settings-defaults">
        <Panel title="Типова назва SDS">
          <Field label="Шаблон назви">
            <textarea rows={3} value={title} maxLength={2000} onChange={e => setTitle(e.target.value)} />
          </Field>
          <details className="settings-template-help"><summary>Змінні назви</summary><div className="settings-tokens">{[
            ["{BRAND}", "Марка"], ["{VALUE}", "Весь рядок"], ["{CODE}", "Код"], ["{COLOR}", "Назва кольору"],
          ].map(([token, description]) => <span key={token}><code>{token}</code> {description}</span>)}</div></details>
          <button className="primary" disabled={!title.trim() || title === data?.sds.title_template}
            onClick={() => void action(async () => {
              const saved = await put<Preferences["sds"]>("/api/settings/sds/title", { title_template: title });
              setTitle(saved.title_template);
            }, "Назву збережено.")}><Save size={16} />Зберегти</button>
        </Panel>
        <Panel title="Типовий PDF">
          {data && <div className="settings-reference"><FileText size={22} /><div><strong>{data.sds.file?.name ?? "PDF-шаблон видалено"}</strong><small>{data.sds.file ? `${data.sds.pages} стор. · ${data.sds.custom ? "Власний" : "Вбудований"}` : "Встановіть новий PDF або відновіть вбудований."}</small></div>{data.sds.file && <a href={data.sds.file.url} target="_blank" rel="noreferrer">Відкрити</a>}</div>}
          <Upload label="Замінити PDF" accept=".pdf" file={pdf} onFile={setPdf} compact />
          <p className="hint">Сумісний SDS на 41 сторінку: зони назв і дати як в оригіналі.</p>
          <div className="settings-actions">
            <button className="primary" disabled={!pdf} onClick={() => void action(async () => {
              if (!pdf) return; checked(pdf, ".pdf");
              const ref = await uploadFile(pdf);
              await put("/api/settings/sds/reference", { token: ref.token }); setPdf(null);
            }, "Типовий PDF встановлено.")}><Save size={16} />Встановити PDF</button>
            {data?.sds.file && <button className="settings-delete" onClick={() => {
              setConfirmation({ title: "Видалити PDF-шаблон?", message: "Генерація SDS буде недоступна, поки не встановите новий PDF або не відновите вбудований. Готові документи в історії залишаться.",
                label: "Видалити PDF", danger: true, work: () => request("/api/settings/sds/reference", { method: "DELETE" }), success: "PDF-шаблон видалено." });
            }}><Trash2 size={15} />Видалити PDF</button>}
            {data && (!data.sds.file || data.sds.custom) && <button onClick={() => {
              if (data.sds.file) setConfirmation({ title: "Відновити вбудований PDF?", message: "Поточний власний PDF буде замінено вбудованим еталоном. Готові документи в історії залишаться.",
                label: "Замінити PDF", work: () => request("/api/settings/sds/reference/restore", { method: "POST" }), success: "Вбудований PDF відновлено." });
              else void action(() => request("/api/settings/sds/reference/restore", { method: "POST" }), "Вбудований PDF відновлено.");
            }}>Відновити вбудований</button>}
          </div>
        </Panel>
      </div>
      <Panel title="Шаблони машин" action={<span className="count">{data?.templates.length ?? 0}</span>}>
        <details className="settings-add-template">
          <summary><Plus size={16} />Додати машину</summary>
          <div className="settings-add-body">
            <div className="settings-upload-grid">
              <Field label="Назва"><input value={name} onChange={e => setName(e.target.value)} maxLength={180} placeholder="BMW X5" /></Field>
              <Field label="HEX кузова" hint="Вихідний колір шаблону. Потрібен, якщо немає маски."><div className="settings-color"><span style={{ background: /^#[0-9a-f]{6}$/i.test(key) ? key : "transparent" }} /><input value={key} onChange={e => setKey(e.target.value)} placeholder="#1E9E52" maxLength={7} spellCheck={false} /></div></Field>
              <Upload label="Машина · PNG" accept=".png" file={image} onFile={setImage} compact />
              <Upload label="Маска кузова · опційно" accept=".png" file={carMask} onFile={setCarMask} compact />
              <Upload label="Маска маркера · опційно" accept=".png" file={markerMask} onFile={setMarkerMask} compact />
            </div>
            <p className="hint">Маски — PNG розміру шаблону. Біла область фарбується.</p>
            <button className="primary" disabled={!image || (!key.trim() && !carMask)} onClick={() => void action(addTemplate, "Машину додано.")}><Plus size={16} />Додати</button>
          </div>
        </details>
        <div className="settings-library">{data?.templates.map(t => <article className="settings-template" key={t.id}>
          <div className="settings-template-image"><img src={t.image.url} alt={t.name} loading="lazy" /></div>
          <div className="settings-template-body"><strong>{t.name}</strong><small>{t.width} × {t.height} · {t.builtin ? "Вбудований" : "Власний"}</small>
            <Field label="HEX кузова"><div className="settings-color"><span style={{ background: /^#[0-9a-f]{6}$/i.test(keys[t.id] ?? "") ? keys[t.id] : "transparent" }} /><input aria-label={`HEX кузова: ${t.name}`} value={keys[t.id] ?? ""} placeholder="Не задано" maxLength={7} spellCheck={false} onChange={e => setKeys(prev => ({ ...prev, [t.id]: e.target.value }))} /></div></Field>
            <div className="settings-actions"><button disabled={(keys[t.id] ?? "") === (t.config.key_color ?? "")} onClick={() => void action(async () => {
              const saved = await put<{key_color: string | null}>(`/api/settings/templates/${t.id}/key`, { key_color: keys[t.id]?.trim() || null });
              setKeys(prev => ({ ...prev, [t.id]: saved.key_color ?? "" }));
            }, `Колір для «${t.name}» збережено.`)}><Save size={14} />Зберегти</button>
              <button className="settings-delete" aria-label={`Видалити шаблон ${t.name}`} onClick={() => {
                setConfirmation({ title: `Видалити «${t.name}»?`,
                  message: t.builtin ? "Вбудований шаблон буде прибрано зі списку. Його можна відновити в налаштуваннях. Готові результати в історії залишаться." : "Шаблон та його маски буде видалено. Готові результати в історії залишаться. Цю дію не можна скасувати.",
                  label: "Видалити шаблон", danger: true, work: () => request(`/api/settings/templates/${t.id}`, { method: "DELETE" }), success: `Шаблон «${t.name}» видалено.` });
              }}><Trash2 size={14} />Видалити</button>
            </div>
          </div>
        </article>)}</div>
        {!!data?.deleted_templates?.length && <details className="settings-removed-templates"><summary>Видалені вбудовані шаблони · {data.deleted_templates.length}</summary><div>{data.deleted_templates.map(t => <div key={t.id}><span>{t.name}</span><button onClick={() => void action(() => request(`/api/settings/templates/${t.id}/restore`, { method: "POST" }), `Шаблон «${t.name}» відновлено.`)}>Відновити</button></div>)}</div></details>}
      </Panel>
    </fieldset>
    {confirmation && <ConfirmationDialog title={confirmation.title} message={confirmation.message} confirmLabel={confirmation.label} danger={confirmation.danger} busy={busy}
      onCancel={() => setConfirmation(null)} onConfirm={async () => { await action(confirmation.work, confirmation.success); setConfirmation(null); }} />}
  </div>;
}
