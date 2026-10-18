import { useEffect, useRef, useState } from "react";
import { Clock3, FileText, Image, RefreshCw, Search } from "lucide-react";
import { request } from "./api";
import { Busy, ErrorMessage, Panel } from "./components";
import Results from "./Results";
import type { Result } from "./Results";

type SavedResult = Result & { zip_path?: string; last_written_at?: string };
type Batch = {
  id: string; kind: "png" | "sds" | "edit"; label: string; created_at: number; expires_at: number;
  count: number; expected_count?: number; partial?: boolean; total_size: number; items: SavedResult[]; search_text?: string;
};
type Page = { items: Batch[]; next_cursor: string | null; retention_days: number };
const kinds = { png: "Фото", sds: "SDS", edit: "Редактор PDF" };
const fileCount = (count: number) => `${count} ${count % 10 === 1 && count % 100 !== 11 ? "файл" :
  count % 10 >= 2 && count % 10 <= 4 && (count % 100 < 12 || count % 100 > 14) ? "файли" : "файлів"}`;
const readyResults = (batch: Batch) => batch.items.map(item => ({ ...item,
  zipPath: item.zip_path ?? item.zipPath, lastWrittenAt: item.last_written_at ?? item.lastWrittenAt }));

export default function History({ active }: { active: boolean }) {
  const [batches, setBatches] = useState<Batch[]>([]);
  const [selected, setSelected] = useState<Batch | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("all");
  const [busy, setBusy] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [error, setError] = useState("");
  const listRequest = useRef(0), selectionRequest = useRef(0);

  async function select(batch: Batch) {
    const generation = ++selectionRequest.current;
    setSelecting(true); setSelected(null); setError("");
    try {
      const fresh = await request<Batch>(`/api/history/batches/${encodeURIComponent(batch.id)}`);
      if (generation === selectionRequest.current) setSelected(fresh);
    } catch (e) {
      if (generation === selectionRequest.current) setError((e as Error).message);
    } finally { if (generation === selectionRequest.current) setSelecting(false); }
  }
  async function load(more = false) {
    const generation = ++listRequest.current;
    setBusy(true); setError("");
    try {
      const page = await request<Page>(`/api/history/batches?limit=20${more && cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      if (generation !== listRequest.current) return;
      setBatches(previous => more ? [...previous, ...page.items.filter(batch => !previous.some(p => p.id === batch.id))] : page.items);
      setCursor(page.next_cursor);
      if (!more) {
        ++selectionRequest.current; setSelected(null);
        if (page.items[0]) void select(page.items[0]);
        else setSelecting(false);
      }
    } catch (e) {
      if (generation === listRequest.current) setError((e as Error).message);
    } finally { if (generation === listRequest.current) setBusy(false); }
  }
  useEffect(() => { if (active) void load(); }, [active]);
  const filtered = batches.filter(batch => (kind === "all" || batch.kind === kind) &&
    `${batch.label} ${batch.search_text ?? ""} ${batch.items.map(i => `${i.label} ${i.file.name}`).join(" ")}`
      .toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const visibleSelected = filtered.some(batch => batch.id === selected?.id) ? selected : null;

  return <>
    <div className="history-toolbar">
      <label className="history-search"><Search size={18} /><input aria-label="Пошук в історії"
        placeholder="Пошук пакета, назви або коду" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <select aria-label="Тип результату" value={kind} onChange={e => setKind(e.target.value)}>
        <option value="all">Усі пакети</option><option value="png">Фото</option><option value="sds">SDS</option><option value="edit">Редактор PDF</option>
      </select>
      <button onClick={() => void load()} disabled={busy}><RefreshCw size={16} />Оновити</button>
    </div>
    <p className="history-caption hint">Один запуск — один пакет. Зберігання — 30 днів.</p>
    <ErrorMessage message={error} />
    {busy && <Busy text="Завантажуємо пакети…" />}
    <div className="history-layout">
      <Panel title="Пакети" action={<span className="count">{filtered.length}</span>}>
        <div className="history-list">{filtered.map(batch => <button key={batch.id}
          className={`history-row ${selected?.id === batch.id ? "active" : ""}`} aria-pressed={selected?.id === batch.id}
          onClick={() => void select(batch)}>
          <span className="history-icon">{batch.kind === "png" ? <Image size={20} /> : <FileText size={20} />}</span>
          <span className="history-info"><strong>{batch.label}</strong><small>{kinds[batch.kind]} · {batch.partial && batch.expected_count ?
            `${batch.count} / ${fileCount(batch.expected_count)}` : fileCount(batch.count)} · {(batch.total_size / 1024 / 1024).toFixed(1)} МБ
            {batch.partial ? " · Незавершений" : ""}</small></span>
          <time className="history-date" dateTime={new Date(batch.created_at * 1000).toISOString()}>
            {new Date(batch.created_at * 1000).toLocaleString("uk-UA", { timeZone: "Europe/Kyiv", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
          </time>
        </button>)}
          {!busy && !filtered.length && <div className="history-empty"><Clock3 size={32} />
            <h3>{batches.length ? "Збігів немає" : "Поки немає пакетів"}</h3>
            <p>{batches.length ? "Змініть пошук або завантажте ще." : "Завершені PNG і PDF з’являться тут автоматично."}</p>
          </div>}
        </div>
        {cursor && <><p className="hint">Пошук у {batches.length} завантажених пакетах.</p>
          <button className="full-width" disabled={busy} onClick={() => void load(true)}>Показати ще</button></>}
      </Panel>
      <div>{selecting && <Busy text="Відкриваємо пакет…" />}
        <Results items={visibleSelected ? readyResults(visibleSelected) : []} type={visibleSelected?.kind === "png" ? "png" : "pdf"}
          emptyTitle="Оберіть пакет" emptyDescription="Перегляд файлів і завантаження всього пакета."
          forceArchive archiveLabel="Завантажити пакет · ZIP"
          archiveName={visibleSelected ? `pack-${visibleSelected.kind}-${visibleSelected.id.slice(0, 12)}.zip` : undefined} />
        {visibleSelected && <p className="hint">{fileCount(visibleSelected.count)} у пакеті. Доступно до {new Date(visibleSelected.expires_at * 1000)
          .toLocaleDateString("uk-UA", { timeZone: "Europe/Kyiv" })}.</p>}
      </div>
    </div>
  </>;
}
