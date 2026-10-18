export const MIN_MASK_ZOOM = 0.1;
export const MAX_MASK_ZOOM = 16;
export const ZOOM_PRESETS = [0.1, 0.25, 0.5, 1, 2, 3, 4, 8, 16];

export function clampZoom(value: number): number {
  return Math.min(MAX_MASK_ZOOM, Math.max(MIN_MASK_ZOOM, value));
}

export function wheelZoom(current: number, deltaY: number, deltaMode = 0, pageHeight = 800): number {
  if (!Number.isFinite(deltaY) || deltaY === 0) return current;
  const pixels = deltaY * (deltaMode === 1 ? 16 : deltaMode === 2 ? pageHeight : 1);
  // Small trackpad movements stay smooth; a large wheel event cannot jump wildly.
  const step = Math.max(-240, Math.min(240, pixels));
  return clampZoom(Math.round(current * Math.exp(-step * 0.0018) * 10000) / 10000);
}

export function fitImageScale(width: number, height: number, viewportWidth: number, viewportHeight: number): number {
  // Leave space around the fitted image and for native scrollbars, on both axes.
  return Math.min(Math.max(1, viewportWidth - 40) / width, Math.max(1, viewportHeight - 40) / height);
}

type Frame = { left: number; top: number; width: number; height: number };
export function anchoredScroll(before: Frame, after: Frame, x: number, y: number, left: number, top: number) {
  const anchorX = Math.max(before.left, Math.min(before.left + before.width, x));
  const anchorY = Math.max(before.top, Math.min(before.top + before.height, y));
  return {
    left: left + after.left + (anchorX - before.left) / before.width * after.width - anchorX,
    top: top + after.top + (anchorY - before.top) / before.height * after.height - anchorY,
  };
}

export function bindWheelZoom(
  target: HTMLElement,
  current: () => number,
  apply: (zoom: number, event: WheelEvent) => void,
  blocked: () => boolean = () => false,
) {
  const wheel = (event: WheelEvent) => {
    if (!Number.isFinite(event.deltaY) || event.deltaY === 0) return;
    event.preventDefault();
    // Never move the image mid-stroke: that would join unrelated image pixels.
    if (blocked()) return;
    const next = wheelZoom(current(), event.deltaY, event.deltaMode, target.clientHeight);
    if (next !== current()) apply(next, event);
  };
  // React's delegated wheel event is passive; use a scoped non-passive listener.
  target.addEventListener("wheel", wheel, { passive: false });
  return () => target.removeEventListener("wheel", wheel);
}
