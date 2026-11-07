import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import type { Template } from "./PngTool";

export default function TemplatePicker({ templates, selected, onChoose, disabled }: {
  templates: Template[];
  selected: string;
  onChoose: (id: string) => void;
  disabled: boolean;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [hover, setHover] = useState<Template>();
  const [preview, setPreview] = useState<Template>();
  const [layout, setLayout] = useState({ left: 0, top: 0, width: 0, maxHeight: 0, previewWidth: 0 });

  useEffect(() => {
    const delay = window.setTimeout(() => setPreview(open ? hover : undefined), 120);
    return () => window.clearTimeout(delay);
  }, [hover, open]);
  useEffect(() => {
    if (disabled && popup.current?.matches(":popover-open")) popup.current.hidePopover();
  }, [disabled]);

  function position() {
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect) return;
    const below = innerHeight - rect.bottom - 14;
    const above = rect.top - 14;
    const opensBelow = below >= 210 || below >= above;
    const height = Math.min(360, Math.max(0, opensBelow ? below : above));
    const width = Math.min(rect.width, innerWidth - 24);
    const left = Math.min(rect.left, innerWidth - width - 12);
    setLayout({
      left, width, maxHeight: height,
      top: opensBelow ? rect.bottom + 6 : rect.top - height - 6,
      previewWidth: Math.min(240, innerWidth - left - width - 24),
    });
  }

  useEffect(() => {
    if (!open) return;
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    return () => {
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
    };
  }, [open]);

  function choose(value: string) {
    popup.current?.hidePopover();
    onChoose(value);
    trigger.current?.focus();
  }

  return <div className="field template-picker">
    <span id={id + "-label"}>Машина</span>
    <button type="button" ref={trigger} className="template-picker-trigger" disabled={disabled}
      aria-labelledby={`${id}-label ${id}-value`} aria-haspopup="listbox" aria-expanded={open} aria-controls={id}
      onClick={() => {
        if (open) popup.current?.hidePopover();
        else { position(); popup.current?.showPopover(); }
      }}>
      <span id={id + "-value"}>{templates.find(t => t.id === selected)?.name ?? "Оберіть машину"}</span>
      <ChevronDown size={15} />
    </button>
    <div ref={popup} popover="auto" className="template-picker-popover"
      style={{ left: layout.left, top: layout.top, width: layout.width }}
      onToggle={event => {
        const expanded = event.newState === "open";
        setOpen(expanded);
        if (expanded) event.currentTarget.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus();
        else { setHover(undefined); setPreview(undefined); }
      }}>
      <div id={id} role="listbox" aria-labelledby={id + "-label"} className="template-picker-list"
        style={{ maxHeight: layout.maxHeight }}
        onPointerLeave={() => setHover(undefined)}
        onKeyDown={event => {
          const options = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button"));
          const index = options.indexOf(document.activeElement as HTMLButtonElement);
          const next = event.key === "ArrowDown" ? Math.min(options.length - 1, index + 1)
            : event.key === "ArrowUp" ? Math.max(0, index - 1)
            : event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : -1;
          if (next >= 0) { event.preventDefault(); options[next]?.focus(); }
        }}>
        <button type="button" role="option" aria-selected={!selected} onFocus={() => setHover(undefined)}
          onPointerEnter={() => setHover(undefined)} onClick={() => choose("")}>Оберіть машину</button>
        {templates.map(template => <button type="button" role="option" key={template.id}
          aria-selected={template.id === selected} onFocus={() => setHover(template)}
          onPointerEnter={() => setHover(template)} onClick={() => choose(template.id)}>
          <span>{template.name}</span>{template.id === selected && <Check size={14} />}
        </button>)}
      </div>
      {preview && layout.previewWidth >= 120 && <div className="template-picker-preview"
        style={{ width: layout.previewWidth }} key={preview.id} aria-hidden="true">
        <img src={preview.image.url} alt="" /><span>{preview.name}</span>
      </div>}
    </div>
  </div>;
}
