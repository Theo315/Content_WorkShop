import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { ArrowRight, Clock3, FileText, Image, Layers2, LogOut, Moon, Settings2, Sun } from "lucide-react";
import { post, request } from "./api";
import { Busy, ErrorMessage, Field } from "./components";
import PngTool from "./PngTool";
import SdsTool from "./SdsTool";
import History from "./History";
import Settings from "./Settings";
import { clearThemeWave, prepareThemeWave } from "./theme-wave";
import "./theme-transition.css";

type Tool = "png" | "sds" | "history" | "settings";
type Theme = "dark" | "light";
type ThemeTransition = {
  ready: Promise<void>;
  finished: Promise<void>;
  updateCallbackDone: Promise<void>;
  skipTransition(): void;
};
type Status = { authenticated: boolean; auth_required: boolean; ready: boolean; storage: string; max_upload_bytes: number };
const titles: Record<Tool, string> = { png: "Фото", sds: "SDS", history: "Історія", settings: "Налаштування" };
function hashTool(): Tool {
  const value = location.hash.slice(1);
  return value === "sds" || value === "history" || value === "settings" ? value : "png";
}
function savedTheme(): Theme {
  try { return localStorage.getItem("workshop-theme") === "light" ? "light" : "dark"; }
  catch { return "dark"; }
}
function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#191919" : "#f5f5f5");
  try { localStorage.setItem("workshop-theme", theme); } catch { /* Theme still works with restricted storage. */ }
}

export default function App() {
  const [tool, setTool] = useState<Tool>(hashTool);
  const currentTool = useRef(tool);
  const [mode, setMode] = useState<"png" | "sds">(tool === "sds" ? "sds" : "png");
  const [leaving, setLeaving] = useState<Tool | null>(null);
  const [theme, setTheme] = useState<Theme>(savedTheme);
  const currentTheme = useRef(theme);
  const themeAnimating = useRef(false);
  const themeTransition = useRef<ThemeTransition | null>(null);
  const fallbackAnimation = useRef<Animation | null>(null);
  const [settingsVersion, setSettingsVersion] = useState(0);
  const [referencesVersion, setReferencesVersion] = useState(0);
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const header = useRef<HTMLElement>(null);

  function show(next: Tool) {
    if (currentTool.current === next) return;
    setLeaving(currentTool.current);
    currentTool.current = next;
    setTool(next);
    window.scrollTo({ top: 0, behavior: "auto" });
    if (next === "png" || next === "sds") setMode(next);
  }
  function navigate(next: Tool) { show(next); location.hash = next; }
  async function refresh() {
    try { setStatus(await request<Status>("/api/status")); setError(""); }
    catch (e) { setError((e as Error).message); }
  }
  useEffect(() => {
    void refresh();
    const syncHash = () => show(hashTool());
    window.addEventListener("hashchange", syncHash);
    return () => window.removeEventListener("hashchange", syncHash);
  }, []);
  useEffect(() => {
    if (!leaving) return;
    const timer = setTimeout(() => setLeaving(null), 240);
    return () => clearTimeout(timer);
  }, [leaving, tool]);
  useEffect(() => { applyTheme(theme); currentTheme.current = theme; }, [theme]);
  useEffect(() => () => {
    themeTransition.current?.skipTransition();
    fallbackAnimation.current?.cancel();
    clearThemeWave(document.documentElement);
  }, []);
  async function toggleTheme(e: React.MouseEvent<HTMLButtonElement>) {
    if (themeAnimating.current) return;
    const root = document.documentElement;
    const next: Theme = currentTheme.current === "dark" ? "light" : "dark";
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const startViewTransition = (document as unknown as {
      startViewTransition?: (update: () => void) => ThemeTransition;
    }).startViewTransition;
    const rect = e.currentTarget.getBoundingClientRect();
    // Keyboard activation has no pointer coordinates; reveal from the switch itself.
    const x = e.detail ? e.clientX : rect.left + rect.width / 2;
    const y = e.detail ? e.clientY : rect.top + rect.height / 2;
    const commit = () => {
      currentTheme.current = next;
      applyTheme(next);
      // Snapshot the new palette and icon together, without remounting either tool.
      flushSync(() => setTheme(next));
    };
    themeAnimating.current = true;
    if (reducedMotion || !startViewTransition) {
      commit();
      if (!reducedMotion && typeof document.body.animate === "function") {
        const animation = document.body.animate({ opacity: [.82, 1] }, { duration: 220, easing: "ease-out" });
        fallbackAnimation.current = animation;
        await animation.finished.catch(() => undefined);
        fallbackAnimation.current = null;
      }
      themeAnimating.current = false;
      return;
    }
    let transition: ThemeTransition | null = null;
    let committed = false;
    prepareThemeWave(root, x, y, innerWidth, innerHeight);
    try {
      transition = startViewTransition.call(document, () => { commit(); committed = true; });
      themeTransition.current = transition;
      // All transition promises are handled, including skipped/hidden-document cases.
      void transition.updateCallbackDone.catch(() => undefined);
      const finished = transition.finished.catch(() => undefined);
      await transition.ready;
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) transition.skipTransition();
      await finished;
    } catch {
      transition?.skipTransition();
      if (transition) await transition.updateCallbackDone.catch(() => undefined);
      if (!committed) commit();
    } finally {
      clearThemeWave(root);
      themeTransition.current = null;
      themeAnimating.current = false;
    }
  }
  async function login(e: React.FormEvent) {
    e.preventDefault(); setBusy(true);
    try { await post("/api/session", { password }); setPassword(""); await refresh(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function logout() {
    try { await request("/api/session", { method: "DELETE" }); await refresh(); }
    catch (e) { setError((e as Error).message); }
  }
  const locked = (status?.ready && !status.authenticated) || undefined;
  function viewProps(id: Tool) {
    return { id: `${id}-view`, className: "workspace-view", hidden: tool !== id && leaving !== id,
      "data-active": tool === id, "aria-hidden": tool !== id, inert: tool !== id || undefined,
      role: id === "png" || id === "sds" ? "tabpanel" : "region",
      "aria-labelledby": id === "png" || id === "sds" ? `${id}-tab` : "workspace-title" };
  }
  return <div className="app-shell">
    <header className="app-header" inert={locked} ref={header}>
      <a className="app-brand" href="#png" aria-label="Content Workshop — Фото" onClick={() => navigate("png")}><Layers2 size={20} /><span>Workshop</span></a>
      <div className="mode-switch" role="tablist" aria-label="Інструмент" data-utility={tool === "history" || tool === "settings"}
        onKeyDown={e => {
          const next = e.key === "ArrowLeft" || e.key === "Home" ? "png" : e.key === "ArrowRight" || e.key === "End" ? "sds" : null;
          if (next) { e.preventDefault(); navigate(next); header.current?.querySelector<HTMLButtonElement>(`#${next}-tab`)?.focus(); }
        }}>
        <span className="mode-switch-thumb" data-mode={mode} aria-hidden="true" />
        <button id="png-tab" role="tab" aria-controls="png-view" aria-selected={tool === "png"} tabIndex={mode === "png" ? 0 : -1}
          className={mode === "png" ? "active" : ""} onClick={() => navigate("png")}><Image size={16} />Фото</button>
        <button id="sds-tab" role="tab" aria-controls="sds-view" aria-selected={tool === "sds"} tabIndex={mode === "sds" ? 0 : -1}
          className={mode === "sds" ? "active" : ""} onClick={() => navigate("sds")}><FileText size={16} />SDS</button>
      </div>
      <nav className="header-actions" aria-label="Робочий простір">
        <button className={`icon-button utility-button ${tool === "history" ? "active" : ""}`} aria-label="Історія" title="Історія" aria-pressed={tool === "history"}
          onClick={() => navigate(tool === "history" ? mode : "history")}><Clock3 size={19} /></button>
        <button className={`icon-button utility-button ${tool === "settings" ? "active" : ""}`} aria-label="Налаштування" title="Налаштування" aria-pressed={tool === "settings"}
          onClick={() => navigate(tool === "settings" ? mode : "settings")}><Settings2 size={19} /></button>
        <button className="icon-button theme-toggle" aria-label={theme === "dark" ? "Увімкнути світлу тему" : "Увімкнути темну тему"}
          title={theme === "dark" ? "Світла тема" : "Темна тема"} onClick={toggleTheme}>{theme === "dark" ? <Sun size={19} /> : <Moon size={19} />}</button>
        {status?.auth_required && status.authenticated && <button className="icon-button" aria-label="Вийти" title="Вийти" onClick={logout}><LogOut size={18} /></button>}
      </nav>
    </header>
    <main className="workspace" inert={locked}>
      <div className={tool === "history" || tool === "settings" ? "workspace-header" : "sr-only"}><h1 id="workspace-title">{titles[tool]}</h1></div>
      {!status ? <div className="connection-state"><Busy text="Підключення…" /><ErrorMessage message={error} />{error && <button onClick={refresh}>Повторити</button>}</div>
        : !status.ready ? <div className="notice">Сервіс не налаштований. Перевірте доступ і сховище файлів.</div> : null}
      {status?.authenticated && status.ready && <div className="view-stack">
        <div {...viewProps("png")}><PngTool maxUpload={status.max_upload_bytes} settingsVersion={settingsVersion} onTemplatesChanged={() => setReferencesVersion(v => v + 1)} /></div>
        <div {...viewProps("sds")}><SdsTool maxUpload={status.max_upload_bytes} settingsVersion={settingsVersion} /></div>
        <div {...viewProps("history")}><History active={tool === "history"} /></div>
        <div {...viewProps("settings")}><Settings maxUpload={status.max_upload_bytes} referencesVersion={referencesVersion} onChanged={() => setSettingsVersion(v => v + 1)} /></div>
      </div>}
    </main>
    {status?.ready && !status.authenticated && <div className="modal-backdrop">
      <form className="login-panel" onSubmit={login} role="dialog" aria-modal="true" aria-labelledby="login-title"
        onKeyDown={e => {
          if (e.key !== "Tab") return;
          const targets = Array.from(e.currentTarget.querySelectorAll<HTMLElement>("input:not(:disabled),button:not(:disabled)"));
          const first = targets[0], last = targets[targets.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
        }}>
        <Layers2 size={25} /><h2 id="login-title">Workshop</h2>
        <Field label="Пароль"><input autoFocus type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></Field>
        <ErrorMessage message={error} /><button className="primary full-width" disabled={busy}>{busy ? "Перевірка…" : "Увійти"}<ArrowRight size={16} /></button>
      </form>
    </div>}
  </div>;
}
