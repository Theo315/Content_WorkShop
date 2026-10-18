import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal, flushSync } from "react-dom";
import type { PointerEvent as ReactPointerEvent } from "react";
import { Brush, Eraser, Minus, Plus, RotateCcw, X } from "lucide-react";
import { anchoredScroll, bindWheelZoom, clampZoom, fitImageScale, MIN_MASK_ZOOM, MAX_MASK_ZOOM, ZOOM_PRESETS } from "./mask-zoom";

export default function MaskEditor({
  source,
  initial,
  mode,
  onClose,
  onSave,
}: {
  source: string;
  initial?: string;
  mode: "car" | "marker" | "plate";
  onClose: () => void;
  onSave: (value: string | number[]) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const mask = useRef(document.createElement("canvas"));
  const image = useRef<HTMLImageElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const imageFrame = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState([1, 1]);
  const [brush, setBrush] = useState(24);
  const [erase, setErase] = useState(false);
  const [zoom, setZoom] = useState(1);
  const zoomValue = useRef(zoom);
  zoomValue.current = zoom;
  const [fitScale, setFitScale] = useState(1);
  const [viewportSize, setViewportSize] = useState([0, 0]);
  const [box, setBox] = useState<number[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [maskReady, setMaskReady] = useState(false);
  const dialog = useRef<HTMLElement>(null);
  const last = useRef<number[] | null>(null);
  const start = useRef<number[] | null>(null);

  function changeZoom(value: number, clientX?: number, clientY?: number) {
    const target = viewport.current;
    const frame = imageFrame.current;
    const next = clampZoom(value);
    if (!target || !frame || next === zoomValue.current) return;
    const before = frame.getBoundingClientRect();
    const view = target.getBoundingClientRect();
    const x = clientX ?? view.left + target.clientWidth / 2;
    const y = clientY ?? view.top + target.clientHeight / 2;
    // Measure the committed image size immediately, including rapid wheel events.
    flushSync(() => setZoom(next));
    const scroll = anchoredScroll(before, frame.getBoundingClientRect(), x, y, target.scrollLeft, target.scrollTop);
    target.scrollLeft = scroll.left;
    target.scrollTop = scroll.top;
  }
  function centerImage() {
    const target = viewport.current;
    if (!target) return;
    target.scrollLeft = (target.scrollWidth - target.clientWidth) / 2;
    target.scrollTop = (target.scrollHeight - target.clientHeight) / 2;
  }
  function fitView() {
    changeZoom(1);
    centerImage();
  }
  useLayoutEffect(() => {
    const target = viewport.current;
    if (!target || size[0] === 1) return;
    const update = () => {
      const bounds = target.getBoundingClientRect();
      setFitScale(fitImageScale(size[0], size[1], bounds.width, bounds.height));
      setViewportSize(previous => previous[0] === bounds.width && previous[1] === bounds.height
        ? previous : [bounds.width, bounds.height]);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(target, { box: "border-box" });
    return () => observer.disconnect();
  }, [size]);
  useLayoutEffect(() => {
    if (zoomValue.current === 1) centerImage();
  }, [fitScale, viewportSize, size]);
  useEffect(() => {
    const target = viewport.current;
    if (!target) return;
    return bindWheelZoom(target, () => zoomValue.current,
      (next, event) => changeZoom(next, event.clientX, event.clientY), () => last.current !== null);
  }, []);

  function redraw() {
    const target = canvas.current;
    if (!target) return;
    const ctx = target.getContext("2d")!;
    ctx.clearRect(0, 0, target.width, target.height);
    if (mode === "plate") return;
    const pixels = mask.current
      .getContext("2d")!
      .getImageData(0, 0, target.width, target.height);
    for (let i = 0; i < pixels.data.length; i += 4) {
      const alpha = pixels.data[i];
      pixels.data[i] = 100;
      pixels.data[i + 1] = 135;
      pixels.data[i + 2] = 183;
      pixels.data[i + 3] = alpha * 0.5;
    }
    ctx.putImageData(pixels, 0, 0);
  }
  useEffect(() => {
    if (size[0] === 1) return;
    const ctx = mask.current.getContext("2d")!;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, ...(size as [number, number]));
    setLoadError("");
    setMaskReady(false);
    if (initial) {
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => {
        ctx.drawImage(img, 0, 0, ...(size as [number, number]));
        redraw();
        setMaskReady(true);
      };
      img.onerror = () =>
        setLoadError(
          "Не вдалося завантажити маску. Закрийте редактор і перевірте доступ до файлу.",
        );
      img.src = initial;
    } else {
      redraw();
      setMaskReady(true);
    }
  }, [size, initial]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const root = document.getElementById("root");
    const wasInert = root?.inert ?? false;
    const previousOverflow = document.body.style.overflow;
    if (root) root.inert = true;
    document.body.style.overflow = "hidden";
    dialog.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const listener = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab") {
        const targets = Array.from(
          dialog.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled), select, input:not(:disabled)",
          ) ?? [],
        );
        const first = targets[0];
        const last = targets[targets.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    window.addEventListener("keydown", listener);
    return () => {
      window.removeEventListener("keydown", listener);
      if (root) root.inert = wasInert;
      document.body.style.overflow = previousOverflow;
      previous?.focus();
    };
  }, []);
  function point(e: ReactPointerEvent<HTMLCanvasElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    return [
      Math.max(
        0,
        Math.min(size[0], ((e.clientX - r.left) * size[0]) / r.width),
      ),
      Math.max(
        0,
        Math.min(size[1], ((e.clientY - r.top) * size[1]) / r.height),
      ),
    ];
  }
  function draw(p: number[]) {
    if (!last.current) return;
    if (mode === "plate") {
      const a = start.current!;
      const next = [
        Math.round(Math.min(a[0], p[0])),
        Math.round(Math.min(a[1], p[1])),
        Math.round(Math.max(a[0], p[0])),
        Math.round(Math.max(a[1], p[1])),
      ];
      setBox(next);
      const ctx = canvas.current!.getContext("2d")!;
      ctx.clearRect(0, 0, ...(size as [number, number]));
      ctx.fillStyle = "#6487b744";
      ctx.strokeStyle = "#799bc8";
      ctx.lineWidth = Math.max(2, size[0] / 400);
      ctx.fillRect(next[0], next[1], next[2] - next[0], next[3] - next[1]);
      ctx.strokeRect(next[0], next[1], next[2] - next[0], next[3] - next[1]);
    } else {
      const ctx = mask.current.getContext("2d")!;
      ctx.strokeStyle = erase ? "#000" : "#fff";
      ctx.lineWidth = brush;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(last.current[0], last.current[1]);
      ctx.lineTo(p[0] + 0.01, p[1]);
      ctx.stroke();
      redraw();
    }
    last.current = p;
  }
  return createPortal(
    <div className="modal-backdrop">
      <section
        ref={dialog}
        className="editor-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="editor-title"
      >
        <header>
          <div>
            <span className="eyebrow">НАЛАШТУВАННЯ ШАБЛОНУ</span>
            <h2 id="editor-title">
              {mode === "plate"
                ? "Область номерного знака"
                : mode === "car"
                  ? "Маска кузова"
                  : "Маска маркера"}
            </h2>
          </div>
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Закрити редактор"
          >
            <X />
          </button>
        </header>
        <div className="editor-toolbar">
          {mode !== "plate" && (
            <>
              <button
                className={!erase ? "selected" : ""}
                onClick={() => setErase(false)}
              >
                <Brush size={16} />
                Пензель
              </button>
              <button
                className={erase ? "selected" : ""}
                onClick={() => setErase(true)}
              >
                <Eraser size={16} />
                Гумка
              </button>
              <label>
                Розмір{" "}
                <input
                  aria-label="Розмір пензля"
                  type="range"
                  min="2"
                  max="180"
                  value={brush}
                  onChange={(e) => setBrush(Number(e.target.value))}
                />
                {brush}px
              </label>
            </>
          )}
          <label>
            Масштаб{" "}
            <select
              value={zoom}
              aria-label="Масштаб фото"
              onChange={(e) => Number(e.target.value) === 1 ? fitView() : changeZoom(Number(e.target.value))}
            >
              {!ZOOM_PRESETS.includes(zoom) && <option value={zoom}>{Math.round(zoom * 100)}%</option>}
              {ZOOM_PRESETS.map(value => <option key={value} value={value}>
                {value === 1 ? "Вписати" : `${Math.round(value * 100)}%`}
              </option>)}
            </select>
          </label>
          <button aria-label="Віддалити фото" disabled={zoom <= MIN_MASK_ZOOM} onClick={() => changeZoom(zoom / 1.2)}><Minus size={15} /></button>
          <button aria-label="Наблизити фото" disabled={zoom >= MAX_MASK_ZOOM} onClick={() => changeZoom(zoom * 1.2)}><Plus size={15} /></button>
          <button onClick={fitView}>Вписати</button>
          <button
            onClick={() => {
              mask.current.getContext("2d")!.fillStyle = "#000";
              mask.current
                .getContext("2d")!
                .fillRect(0, 0, ...(size as [number, number]));
              setBox(null);
              redraw();
            }}
          >
            <RotateCcw size={15} />
            Очистити
          </button>
        </div>
        <p className="editor-help">
          {mode === "plate"
            ? "Обведіть номер прямокутником. Координати зберігаються у розмірі оригіналу. Колесо миші — зум під курсором."
            : "Зафарбуйте область кузова. Синій шар — маска; гумка прибирає зайве. Колесо миші — зум під курсором."}
        </p>
        <div ref={viewport} className="editor-viewport">
          {/* Surround the image with scrollable space so cursor anchoring also
              works when the image is smaller than the viewport. */}
          <div className="editor-stage" style={{
            width: size[0] * fitScale * zoom + 2 * viewportSize[0],
            height: size[1] * fitScale * zoom + 2 * viewportSize[1],
            padding: `${viewportSize[1]}px ${viewportSize[0]}px`,
          }}>
          <div ref={imageFrame} className="editor-image" style={{ width: size[0] * fitScale * zoom, height: size[1] * fitScale * zoom }}>
            <img
              ref={image}
              src={source}
              alt="Шаблон для редагування"
              draggable={false}
              onLoad={() => {
                const s = [
                  image.current!.naturalWidth,
                  image.current!.naturalHeight,
                ];
                mask.current.width = s[0];
                mask.current.height = s[1];
                setSize(s);
              }}
            />
            <canvas
              ref={canvas}
              width={size[0]}
              height={size[1]}
              onPointerDown={(e) => {
                e.currentTarget.setPointerCapture(e.pointerId);
                start.current = point(e);
                last.current = point(e);
                draw(point(e));
              }}
              onPointerMove={(e) => {
                if (last.current) draw(point(e));
              }}
              onPointerUp={() => {
                last.current = null;
              }}
              onPointerCancel={() => {
                last.current = null;
              }}
            />
          </div>
          </div>
        </div>
        {loadError && (
          <p className="error-message" role="alert">
            {loadError}
          </p>
        )}
        <footer>
          <span className="muted">
            {size[0]} × {size[1]} px{box && ` · ${box.join(", ")}`}
          </span>
          <div className="button-row">
            <button onClick={onClose}>Скасувати</button>
            <button
              className="primary"
              disabled={
                !maskReady ||
                !!loadError ||
                size[0] === 1 ||
                (mode === "plate" &&
                  (!box || box[0] === box[2] || box[1] === box[3]))
              }
              onClick={() => {
                onSave(
                  mode === "plate" ? box! : mask.current.toDataURL("image/png"),
                );
                onClose();
              }}
            >
              Застосувати
            </button>
          </div>
        </footer>
      </section>
    </div>, document.body
  );
}
