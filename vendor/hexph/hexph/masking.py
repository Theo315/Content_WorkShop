"""Mask storage + the interactive `mask-editor` (click-driven segmentation).

Three segmentation backends:

* `GrabCutSegmenter` (default) — seeded GrabCut: builds colour models from the
  positive/negative clicks, so it holds together on real studio photos where a
  car body spans a huge lightness range across panels. Runs on a downscaled copy
  for speed, treats the image border as background, and keeps only the connected
  components that actually contain a positive click (kills stray blobs).
* `FloodSegmenter` — LAB flood-fill. Much faster and pixel-exact on flat,
  synthetic templates, but on a noisy photo the negative seeds tend to flood
  everything; kept as a fallback / for vector-like templates.
* `SamSegmenter` (optional) — Meta's Segment Anything, used when a checkpoint is
  supplied via `--sam-checkpoint` / `HEXPH_SAM_CHECKPOINT` and the
  `segment-anything` + `torch` packages are importable.

Every backend exposes `segment(positives, negatives)` plus a `KNOBS` table that
the editor wires to the `[ ]` and `- =` keys.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import queue
import threading
import time

import cv2
import numpy as np

from .colors import bgr_to_lab, hex_to_lab
from .errors import UserError
from .image_io import load_image, save_image

CAR = "car"
MARKER = "marker"
STAGES = (CAR, MARKER)

MASK_SUFFIX = {CAR: "_car_mask.png", MARKER: "_marker_mask.png"}
LEGACY_MASK_NAME = {CAR: "car_mask.png", MARKER: "marker_mask.png"}
STAGE_TITLE = {
    CAR: "CAR BODY  (positive: hood/doors/roof/bumper | negative: glass/tires/lights/grille)",
    MARKER: "LEFT MARKER cap  (rect mode 'x' is usually enough)",
}
STAGE_HINT_UA = {
    CAR: "кузов авто",
    MARKER: "кольорова частина лівого маркера",
}

MAX_DISPLAY_SIDE = 1000
WINDOW_MARGIN = (90, 130)  # room for the frame border, title bar and the taskbar
MAX_ZOOM = 12.0  # a 3000 px template on a 900 px screen needs real magnification
ZOOM_STEP = 1.25
PAN_KEYS = {ord("a"): (-1, 0), ord("d"): (1, 0), ord("w"): (0, -1), ord("s"): (0, 1)}


def wheel_delta(flags: int) -> int:
    """Scroll amount packed in the high word of the mouse flags.

    cv2.getMouseWheelDelta() is missing from some opencv-python builds, and the
    packing is the same on every backend, so decode it here.
    """
    delta = (flags >> 16) & 0xFFFF
    return delta - 0x10000 if delta > 0x7FFF else delta


def _screen_work_area() -> tuple[int, int]:
    """Usable desktop size. Falls back to a conservative guess."""
    try:  # Windows: ask for the work area, so the taskbar is already excluded
        import ctypes

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = _RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    try:
        import tkinter

        if tkinter._default_root is not None:  # reuse the GUI's root, never make a second
            root = tkinter._default_root
            return root.winfo_screenwidth(), root.winfo_screenheight()
        root = tkinter.Tk()
        root.withdraw()
        size = (root.winfo_screenwidth(), root.winfo_screenheight())
        root.destroy()
        return size
    except Exception:
        return 1600, 900


def hud_min_width() -> int:
    """Width the help panel needs; a portrait template is narrower than its own HUD."""
    widest = 0
    # the state row is the longest line the HUD ever draws, so measure it too
    sample = ("input=BRUSH r=000  backend=grabcut  seed_r=18 iters=5  "
              "fill_holes=off  dilate=+0  area=00.0%  zoom=0000%")
    for line in (*HELP_LINES, sample):
        (text_w, _), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
        widest = max(widest, text_w)
    return widest + 24


def fit_window_size(img_w: int, img_h: int, panel_h: int) -> tuple[int, int]:
    """Window size (w, h) that shows the whole template inside the screen."""
    screen_w, screen_h = _screen_work_area()
    max_w = max(480, screen_w - WINDOW_MARGIN[0])
    max_h = max(360, screen_h - WINDOW_MARGIN[1])
    scale = min(
        1.0,
        MAX_DISPLAY_SIDE / max(img_w, img_h),
        max_w / img_w,
        max(120, max_h - panel_h) / img_h,
    )
    width = min(max_w, max(int(img_w * scale), hud_min_width()))
    return int(width), int(img_h * scale) + panel_h

SEED_RADIUS = 4
NEGATIVE_BLOCK_RADIUS = 7


# --- mask files -----------------------------------------------------------


def mask_path(template_path, kind: str) -> pathlib.Path:
    template = pathlib.Path(template_path)
    return template.parent / f"{template.stem}{MASK_SUFFIX[kind]}"


def find_mask(template_path, kind: str) -> pathlib.Path | None:
    """Template-specific mask, falling back to a shared `car_mask.png`."""
    template = pathlib.Path(template_path)
    for candidate in (mask_path(template, kind), template.parent / LEGACY_MASK_NAME[kind]):
        if candidate.exists():
            return candidate
    return None


def load_mask(template_path, kind: str, shape: tuple[int, int]) -> np.ndarray:
    path = find_mask(template_path, kind)
    if path is None:
        raise UserError(
            f"маску ({STAGE_HINT_UA[kind]}) не знайдено біля шаблону: очікується "
            f"{mask_path(template_path, kind).name}\n"
            "Спочатку побудуйте маски: python recolor.py mask-editor --template <шаблон.png>"
        )
    mask, _ = load_image(path, f"Маска ({STAGE_HINT_UA[kind]})")
    mask = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY)
    if mask.shape[:2] != shape:
        raise UserError(
            f"розмір маски {path.name} ({mask.shape[1]}x{mask.shape[0]}) не збігається "
            f"з розміром шаблону ({shape[1]}x{shape[0]}). Перебудуйте маски для цього шаблону."
        )
    if not mask.any():
        raise UserError(f"маска {path.name} повністю чорна — нема зони перефарбування")
    return mask


def save_mask(path, mask: np.ndarray, soft: bool = False) -> pathlib.Path:
    """Binary by default; `soft` keeps the grey levels of a chroma-key matte."""
    out = mask if soft else np.where(mask > 127, 255, 0).astype(np.uint8)
    return save_image(path, cv2.cvtColor(out, cv2.COLOR_GRAY2BGR))


def sidecar_path(template_path, kind: str) -> pathlib.Path:
    """Where the clicks that produced a mask are remembered."""
    return mask_path(template_path, kind).with_suffix(".json")


def save_sidecar(
    template_path,
    kind: str,
    *,
    backend: str,
    positives=(),
    negatives=(),
    rects=(),
    strokes=(),
    dilate: int = 0,
    fill_holes: bool = False,
    brush_radius: int | None = None,
) -> pathlib.Path | None:
    """Store the clicks/strokes so reopening the editor does not start from zero."""
    path = sidecar_path(template_path, kind)
    data = {
        "backend": backend,
        "positives": [list(p) for p in positives],
        "negatives": [list(p) for p in negatives],
        "rects": [list(r) for r in rects],
        "strokes": [[name, [list(p) for p in pts], r] for name, pts, r in strokes],
        "dilate": dilate,
        "fill_holes": fill_holes,
        "brush_radius": brush_radius,
    }
    try:
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:  # pragma: no cover - read-only location
        return None
    return path


def load_sidecar(template_path, kind: str) -> dict | None:
    path = sidecar_path(template_path, kind)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# --- segmentation backends -----------------------------------------------


GRADIENT = "gradient"
FIXED = "fixed"
DEFAULT_TOLERANCE = {
    # (light tolerance, chroma tolerance)
    GRADIENT: (4, 3),  # per-step vs the neighbouring pixel: flows through shading, stops at edges
    FIXED: (40, 8),  # vs the seed pixel: robust for a strongly coloured body on white
}

GRABCUT = "grabcut"
FLOOD_GRADIENT = "flood-gradient"
FLOOD_FIXED = "flood-fixed"
CHROMA = "chroma"
SAM = "sam"
CYCLE_BACKENDS = (GRABCUT, CHROMA, FLOOD_GRADIENT, FLOOD_FIXED)  # SAM: reloading is slow
KEY_COLOR_HINT = "#1E9E52"  # saturated mid-tone green: best measured key colour


def _keep_components_with_seeds(mask: np.ndarray, seeds) -> np.ndarray:
    """Drop every blob that does not contain one of the clicked points."""
    count, labels = cv2.connectedComponents(mask)
    if count <= 1:
        return mask
    height, width = mask.shape[:2]
    wanted = {
        labels[y, x]
        for x, y in seeds
        if 0 <= x < width and 0 <= y < height and labels[y, x] != 0
    }
    if not wanted:
        return np.zeros_like(mask)
    return np.where(np.isin(labels, list(wanted)), 255, 0).astype(np.uint8)


def _keep_components_touching(weak: np.ndarray, seed: np.ndarray) -> np.ndarray:
    """Blobs of `weak` (bool) that contain at least one `seed` pixel — Canny-style."""
    count, labels = cv2.connectedComponents(weak.astype(np.uint8))
    if count <= 1:
        return np.zeros_like(weak)
    keep = np.zeros(count, bool)
    keep[np.unique(labels[seed])] = True
    keep[0] = False  # label 0 is everything the weak test rejected
    return keep[labels]


class GrabCutSegmenter:
    """Seeded GrabCut — the reliable choice on real product photos."""

    name = "GrabCut (OpenCV)"
    KNOBS = (("seed_r", "seed_radius", 4, 80, 2), ("iters", "iterations", 1, 12, 1))

    def __init__(self, image_bgr: np.ndarray, work_size: int = 768):
        self.shape = image_bgr.shape[:2]
        self.scale = min(1.0, work_size / max(self.shape))
        self.small = (
            cv2.resize(image_bgr, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
            if self.scale < 1.0
            else image_bgr
        )
        self.seed_radius = 18
        self.iterations = 5  # runs off the UI thread now, so spend the time on quality

    def segment(self, positives, negatives) -> np.ndarray:
        if not positives:
            return np.zeros(self.shape, np.uint8)
        state = np.full(self.small.shape[:2], cv2.GC_PR_BGD, np.uint8)

        # the frame is always background in a product shot — anchors the colour model
        edge = max(2, int(4 * self.scale))
        state[:edge, :] = state[-edge:, :] = cv2.GC_BGD
        state[:, :edge] = state[:, -edge:] = cv2.GC_BGD

        # Soft seeds: the disc only *suggests* fore/background and a small core is
        # the hard constraint. With fully hard discs a click on a thin part (roof
        # edge, mirror) forces a chunk of the background into the mask.
        radius = max(3, int(self.seed_radius * self.scale))
        core = max(1, radius // 3)
        scaled_pos = [(int(x * self.scale), int(y * self.scale)) for x, y in positives]
        for point in scaled_pos:
            cv2.circle(state, point, radius, cv2.GC_PR_FGD, -1)
            cv2.circle(state, point, core, cv2.GC_FGD, -1)
        for x, y in negatives:
            point = (int(x * self.scale), int(y * self.scale))
            cv2.circle(state, point, radius, cv2.GC_PR_BGD, -1)
            cv2.circle(state, point, core, cv2.GC_BGD, -1)

        cv2.grabCut(
            self.small,
            state,
            None,
            np.zeros((1, 65), np.float64),
            np.zeros((1, 65), np.float64),
            int(self.iterations),
            cv2.GC_INIT_WITH_MASK,
        )
        mask = np.where((state == cv2.GC_FGD) | (state == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
        mask = _keep_components_with_seeds(mask, scaled_pos)
        if self.scale < 1.0:
            mask = cv2.resize(mask, (self.shape[1], self.shape[0]), interpolation=cv2.INTER_LINEAR)
        return np.where(mask > 127, 255, 0).astype(np.uint8)


class ChromaKeySegmenter:
    """Chroma key: the body is painted in one distinctive colour, so select by hue.

    Made for templates generated in a key colour (a saturated mid-tone green such as
    `#1E9E52`). Selection is by hue proximity + minimum chroma, which follows the
    shading from deep shadow to specular highlight, needs no clicks, and takes ~10 ms
    instead of GrabCut's ~1.5 s. Measured on a keyed 2048 px card: IoU 0.96-0.97
    against a hand-built mask, and stable across hue tolerances of ±25°…±55°.

    `soft` returns a matte instead of a binary mask: pixels that only carry part of
    the key colour (bounce light on the floor, reflections in glass) get a
    proportional weight, so those follow the new colour instead of staying behind.

    Every edge in the template (the number plate, glass, black trim, the silhouette
    against the backdrop) is one or two pixels of key colour *blended* with what is
    behind it. Those keep the key hue but lose most of their chroma, so the
    `min_chroma` floor drops them and the batch leaves a green outline — most
    visible around the plate, where the blend sits against white. `_recover_fringe`
    takes them back by hysteresis.
    """

    name = "chroma key (hue)"
    KNOBS = (("hue_tol", "hue_tolerance", 5, 80, 5), ("min_chroma", "min_chroma", 2, 40, 1))
    MIN_COMPONENT_FRACTION = 2e-5  # drop keyed specks (<~85 px on a 2048² card)

    CHROMA_FRACTION = 0.4  # threshold as a share of the key's own chroma
    SHADOW_CHROMA_FRACTION = 0.2  # deep shadow keeps the hue but only ~a fifth of the chroma
    FRINGE_CHROMA_FRACTION = 0.06  # weak floor: a pixel carrying ≥6 % of the key
    FRINGE_HUE_FACTOR = 2.5  # hue drifts as the blend loses chroma — allow more of it
    FRINGE_RADIUS_FRACTION = 0.002  # reach from a solid pixel (~2 px @1254, 4 px @2048)

    def __init__(
        self,
        image_bgr: np.ndarray,
        key_color: str | None = None,
        hue_tolerance: int = 25,
        min_chroma: int | None = None,
        soft: bool = False,
        fringe_radius: int | None = None,
    ):
        self.shape = image_bgr.shape[:2]
        self.lab = bgr_to_lab(cv2.GaussianBlur(image_bgr, (0, 0), 1.0))
        self.hue_tolerance = hue_tolerance
        self.soft = soft
        self.key_color = key_color
        self._key = hex_to_lab(key_color) if key_color else None
        # A fixed threshold is wrong: it has to scale with how saturated the key is.
        # 40 % of the key chroma measured best (IoU 0.995 at recall 0.999 on a keyed card).
        self._auto_chroma = min_chroma is None
        self.min_chroma = min_chroma if min_chroma is not None else 8
        if self._key is not None and self._auto_chroma:
            self.min_chroma = self._chroma_floor(self._key)
        self.fringe_radius = (
            fringe_radius
            if fringe_radius is not None
            else max(2, int(round(self.FRINGE_RADIUS_FRACTION * max(self.shape))))
        )

    def _chroma_floor(self, key: np.ndarray) -> int:
        return int(np.clip(self.CHROMA_FRACTION * float(np.hypot(key[1], key[2])), 8, 40))

    def _recover_fringe(
        self, strong: np.ndarray, chroma: np.ndarray, hue_gap: np.ndarray, key: np.ndarray
    ) -> np.ndarray:
        """Grow the selection into the shadows and the anti-aliased rim.

        Two hysteresis passes, both anchored on the solid pixels — anything that never
        touches keyed paint (backdrop, chrome, grey packaging) stays out, so its sensor
        noise cannot creep in:

        1. *shadow*: same hue window, a fifth of the chroma. Deep shadow under a bumper
           is a band several pixels wide, so it is grown by connectivity, not by
           distance — every weak blob that contains a solid pixel joins the mask.
        2. *rim*: the 1-2 px anti-aliased blend at every edge. Its hue is unreliable and
           its chroma near zero, so it is allowed a very low floor and a wide hue
           window, but only within `fringe_radius` px of the region from step 1.
        """
        key_chroma = float(np.hypot(key[1], key[2]))
        shadow = (
            chroma >= max(self.SHADOW_CHROMA_FRACTION * key_chroma, 4.0)
        ) & (hue_gap <= self.hue_tolerance)
        grown = strong | _keep_components_touching(shadow, strong)

        if self.fringe_radius <= 0:
            return grown
        rim = (chroma >= max(self.FRINGE_CHROMA_FRACTION * key_chroma, 2.0)) & (
            hue_gap <= self.hue_tolerance * self.FRINGE_HUE_FACTOR
        )
        size = 2 * int(self.fringe_radius) + 1
        near = cv2.dilate(
            grown.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        )
        return grown | (rim & (near > 0))

    def _key_from_click(self, positives) -> np.ndarray:
        x, y = positives[0]
        patch = self.lab[max(0, y - 4) : y + 5, max(0, x - 4) : x + 5].reshape(-1, 3)
        return np.median(patch, axis=0).astype(np.float32)

    def key_lab(self, positives=()) -> np.ndarray | None:
        if self._key is not None:
            return self._key
        return self._key_from_click(positives) if positives else None

    def segment(self, positives, negatives) -> np.ndarray:
        key = self.key_lab(positives)
        if key is None:  # no key colour and no click to sample one
            return np.zeros(self.shape, np.uint8)
        if self._auto_chroma and self._key is None:  # key sampled from the click
            self.min_chroma = self._chroma_floor(key)

        a, b = self.lab[:, :, 1], self.lab[:, :, 2]
        chroma = np.hypot(a, b)
        delta = np.arctan2(b, a) - float(np.arctan2(key[2], key[1]))
        hue_gap = np.degrees(np.abs(np.arctan2(np.sin(delta), np.cos(delta))))

        strong = (chroma >= self.min_chroma) & (hue_gap <= self.hue_tolerance)
        hit = self._recover_fringe(strong, chroma, hue_gap, key)
        if self.soft:
            key_chroma = max(float(np.hypot(key[1], key[2])), 1e-3)
            matte = np.clip(chroma / key_chroma, 0.0, 1.0)
            # The hue penalty only applies to solid pixels: on a blend the hue is
            # unreliable by construction, and its chroma ratio already *is* the
            # unmixed coverage of the key colour.
            matte = np.where(
                strong,
                matte * np.clip(1.0 - hue_gap / max(self.hue_tolerance, 1e-3), 0.0, 1.0) ** 0.35,
                matte,
            )
            mask = np.where(hit, np.rint(matte * 255), 0).astype(np.uint8)
        else:
            mask = (hit.astype(np.uint8)) * 255

        for x, y in negatives:
            cv2.circle(mask, (int(x), int(y)), NEGATIVE_BLOCK_RADIUS * 3, 0, -1)

        binary = (mask > 0).astype(np.uint8) * 255
        if positives:
            binary = _keep_components_with_seeds(binary, positives)
        else:  # zero-click: keep every keyed area that is not a speck
            count, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
            floor = self.MIN_COMPONENT_FRACTION * self.shape[0] * self.shape[1]
            wanted = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= floor]
            binary = np.where(np.isin(labels, wanted), 255, 0).astype(np.uint8)
        mask[binary == 0] = 0
        return mask


class FloodSegmenter:
    """LAB flood-fill from click seeds. Pixel-exact on flat/synthetic templates.

    `gradient` compares each pixel with its neighbour, so it walks the whole
    shading gradient of a panel and stops at the hard edge of glass/tyre/background.
    `fixed` compares with the seed pixel — narrower, but immune to leaking through
    soft edges.
    """

    KNOBS = (("chroma_tol", "ab_tol", 1, 80, 1), ("light_tol", "l_tol", 1, 200, 1))

    def __init__(self, image_bgr: np.ndarray, mode: str = GRADIENT):
        self.lab = cv2.cvtColor(cv2.GaussianBlur(image_bgr, (0, 0), 1.0), cv2.COLOR_BGR2LAB)
        self.shape = image_bgr.shape[:2]
        self.mode = mode
        self.l_tol, self.ab_tol = DEFAULT_TOLERANCE[mode]

    @property
    def name(self) -> str:
        return f"flood-fill / {self.mode} (OpenCV)"

    def _grow(self, seeds) -> np.ndarray:
        h, w = self.shape
        acc = np.zeros((h, w), np.uint8)
        diff = (int(self.l_tol), int(self.ab_tol), int(self.ab_tol))
        flags = 8 | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
        if self.mode == FIXED:
            flags |= cv2.FLOODFILL_FIXED_RANGE
        for x, y in seeds:
            if not (0 <= x < w and 0 <= y < h):
                continue
            ff = np.zeros((h + 2, w + 2), np.uint8)
            cv2.floodFill(self.lab, ff, (int(x), int(y)), 0, diff, diff, flags)
            acc = np.maximum(acc, ff[1:-1, 1:-1])
        return acc

    def segment(self, positives, negatives) -> np.ndarray:
        if not positives:
            return np.zeros(self.shape, np.uint8)
        mask = self._grow(positives)
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        )
        if negatives:
            blocked = self._grow(negatives)
            for x, y in negatives:
                cv2.circle(blocked, (int(x), int(y)), NEGATIVE_BLOCK_RADIUS, 255, -1)
            mask[blocked > 0] = 0
        return cv2.morphologyEx(
            mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        )


class SamSegmenter:
    """Segment Anything (Meta, Apache-2.0). Used when a checkpoint is available."""

    KNOBS = ()

    def __init__(self, image_bgr: np.ndarray, checkpoint: str, model_type: str | None = None):
        try:
            import torch
            from segment_anything import SamPredictor, sam_model_registry
        except ImportError as exc:
            raise UserError(
                "для SAM потрібні пакети `torch` та `segment-anything`: "
                "pip install torch segment-anything\n"
                f"(деталі: {exc})"
            ) from None
        if not pathlib.Path(checkpoint).exists():
            raise UserError(f"чекпоінт SAM не знайдено: {checkpoint}")

        model_type = model_type or self._guess_model_type(checkpoint)
        if model_type not in sam_model_registry:
            raise UserError(
                f"невідомий тип моделі SAM: {model_type} (очікується vit_h / vit_l / vit_b)"
            )
        device = "cuda" if torch.cuda.is_available() else "cpu"
        sam = sam_model_registry[model_type](checkpoint=checkpoint).to(device)
        self.name = f"SAM {model_type} on {device}"
        self.shape = image_bgr.shape[:2]
        self.predictor = SamPredictor(sam)
        self.predictor.set_image(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))

    @staticmethod
    def _guess_model_type(checkpoint: str) -> str:
        stem = pathlib.Path(checkpoint).name.lower()
        for tag in ("vit_h", "vit_l", "vit_b"):
            if tag in stem or tag.replace("_", "") in stem:
                return tag
        return "vit_h"

    def segment(self, positives, negatives) -> np.ndarray:
        if not positives:
            return np.zeros(self.shape, np.uint8)
        coords = np.array([*positives, *negatives], dtype=np.float32)
        labels = np.array([1] * len(positives) + [0] * len(negatives), dtype=np.int32)
        masks, _, _ = self.predictor.predict(
            point_coords=coords, point_labels=labels, multimask_output=False
        )
        return (masks[0].astype(np.uint8)) * 255


def build_segmenter(
    backend: str,
    image_bgr: np.ndarray,
    sam_checkpoint: str | None = None,
    sam_model_type: str | None = None,
    key_color: str | None = None,
    soft: bool = False,
):
    if backend == SAM:
        checkpoint = sam_checkpoint or os.environ.get("HEXPH_SAM_CHECKPOINT")
        if not checkpoint:
            raise UserError(
                "для бекенду sam потрібен чекпоінт: --sam-checkpoint <файл.pth> "
                "або змінна оточення HEXPH_SAM_CHECKPOINT"
            )
        return SamSegmenter(image_bgr, checkpoint, sam_model_type)
    if backend == FLOOD_GRADIENT:
        return FloodSegmenter(image_bgr, GRADIENT)
    if backend == FLOOD_FIXED:
        return FloodSegmenter(image_bgr, FIXED)
    if backend == CHROMA:
        return ChromaKeySegmenter(image_bgr, key_color, soft=soft)
    if backend == GRABCUT:
        return GrabCutSegmenter(image_bgr)
    raise UserError(f"невідомий бекенд сегментації: {backend}")


def default_backend(sam_checkpoint: str | None = None, key_color: str | None = None) -> str:
    """Key colour -> chroma key; SAM checkpoint -> SAM; otherwise seeded GrabCut."""
    if key_color:
        return CHROMA
    return SAM if (sam_checkpoint or os.environ.get("HEXPH_SAM_CHECKPOINT")) else GRABCUT


# --- interactive editor ---------------------------------------------------

HELP_LINES = (
    "LMB add positive   RMB add negative   u undo   r reset   x rect mode",
    "b BRUSH (drag LMB paint / RMB erase)  , . brush size     f fill holes",
    "t switch backend   [ ] knob A  - = knob B                g grow  h shrink",
    "ENTER save         n keep existing    ESC cancel",
    "wheel or z / Z zoom   MMB drag or W A S D pan   0 reset zoom and window",
)
LEAK_THRESHOLD = 0.55  # mask covering more than this share of the frame = tolerance too high


class _StageEditor:
    """One mask (car or marker): click points, watch the overlay, accept."""

    def __init__(
        self,
        image_bgr: np.ndarray,
        segmenter,
        kind: str,
        initial: np.ndarray | None,
        backend: str = GRABCUT,
        key_color: str | None = None,
        soft: bool = False,
    ):
        self.image = image_bgr
        self.segmenter = segmenter
        self.backend = backend
        self.key_color = key_color
        self.soft = soft
        self.kind = kind
        self.height, self.width = image_bgr.shape[:2]
        # constant: title + help + state + status (see _draw_hud)
        self._panel_h = 20 * (3 + len(HELP_LINES)) + 16
        # the frame is rendered at exactly the window size, so a template taller
        # than the screen still shows its ENTER/ESC hints instead of hiding them
        # under the taskbar, and dragging the window edge rescales the view
        self._win_size = fit_window_size(self.width, self.height, self._panel_h)
        self.scale = 1.0
        self.fit_scale = 1.0
        self.zoom = 1.0  # 1.0 = whole template visible; higher crops into it
        self.origin = (0, 0)  # top-left of the visible crop, in image pixels
        self.crop_size = (self.width, self.height)
        self.base = image_bgr
        self.view_size = (self.width, self.height)
        self._view_x = 0
        self._pan_from: tuple[int, int] | None = None

        self.positives: list[tuple[int, int]] = []
        self.negatives: list[tuple[int, int]] = []
        self.history: list[str] = []
        self.rects: list[tuple[int, int, int, int]] = []
        self.rect_mode = kind == MARKER
        self.drag_start: tuple[int, int] | None = None
        self.drag_now: tuple[int, int] | None = None

        self.fill_holes = False
        self.dilate = 0

        # manual brush on top of the segmenter — the escape hatch for templates the
        # colour models cannot split (a black car: paint, glass and tyres all match)
        self.brush_mode = False
        self.brush_radius = max(6, int(0.012 * max(self.height, self.width)))
        self.brush_add = np.zeros((self.height, self.width), np.uint8)
        self.brush_erase = np.zeros((self.height, self.width), np.uint8)
        self.strokes: list[tuple[str, list[tuple[int, int]], int]] = []
        self.cursor: tuple[int, int] | None = None

        self.initial = initial
        self.segmented = initial.copy() if initial is not None else np.zeros(
            (self.height, self.width), np.uint8
        )
        self.mask = self.segmented.copy()
        self.status = "loaded existing mask" if initial is not None else ""

        # display-resolution copy of the mask: the overlay is built from this, so a
        # frame never touches the full-resolution array (set_view_size fills it in)
        self.mask_small = self.mask

        # frame cache — the wait-loop spins at ~50 Hz and must not rebuild the
        # overlay every time (that alone used to burn ~17 ms per frame)
        self._canvas: np.ndarray | None = None
        self._hud_lines: tuple[str, ...] | None = None
        self.frame_version = 0  # bumped on every real change; the loop redraws on it
        self._dirty = True
        self._cursor_backup: tuple[tuple[int, int, int, int], np.ndarray] | None = None
        self._cursor_at: tuple[int, int] | None = None
        self.dragging = False

        # background segmentation
        self._results: queue.Queue = queue.Queue()
        self._seg_lock = threading.Lock()
        self._token = 0
        self.computing = False
        self._pending_global = False
        self._pending_stroke_region: tuple[int, int, int, int] | None = None
        self._last_brush_compose = 0.0

        self.set_view_size(*self._win_size, force=True)

    # -- geometry
    @property
    def window_size(self) -> tuple[int, int]:
        return self._win_size

    def set_view_size(self, win_w: int, win_h: int, force: bool = False) -> bool:
        """Fit the template into a window of this size. -> True if anything changed.

        Called on every window resize: the frame is always rendered at the exact
        window size, so highgui never stretches it and clicks keep landing where
        the cursor is.
        """
        win_w = max(320, int(win_w))
        win_h = max(self._panel_h + 120, int(win_h))
        if not force and (win_w, win_h) == self._win_size:
            return False
        self._win_size = (win_w, win_h)
        self._rebuild_view()
        return True

    def _rebuild_view(self) -> None:
        """Recompute the scale, the visible crop and the scaled preview of it.

        Only the visible crop is ever resized, so zooming into a 3000 px template
        costs no more than showing all of it.
        """
        win_w, win_h = self._win_size
        avail_w = max(1, win_w)
        avail_h = max(1, win_h - self._panel_h)
        self.fit_scale = min(1.0, avail_w / self.width, avail_h / self.height)
        self.zoom = float(np.clip(self.zoom, 1.0, MAX_ZOOM))
        self.scale = self.fit_scale * self.zoom

        crop_w = min(self.width, max(1, int(avail_w / self.scale)))
        crop_h = min(self.height, max(1, int(avail_h / self.scale)))
        self.crop_size = (crop_w, crop_h)
        ox, oy = self.origin
        self.origin = (
            int(np.clip(ox, 0, self.width - crop_w)),
            int(np.clip(oy, 0, self.height - crop_h)),
        )
        ox, oy = self.origin

        view_w = max(1, min(avail_w, int(round(crop_w * self.scale))))
        view_h = max(1, min(avail_h, int(round(crop_h * self.scale))))
        self.view_size = (view_w, view_h)

        crop = self.image[oy : oy + crop_h, ox : ox + crop_w]
        if (view_w, view_h) == (crop_w, crop_h):
            self.base = crop.copy()
        else:
            interp = cv2.INTER_AREA if self.scale < 1.0 else cv2.INTER_LINEAR
            self.base = cv2.resize(crop, (view_w, view_h), interpolation=interp)
        # a portrait template in a wide window: centre it instead of leaving one
        # big black bar on the right
        self._view_x = max(0, (win_w - view_w) // 2)
        if getattr(self, "mask", None) is not None:
            self.mask_small = self._downscale(self.mask)
        self._canvas = None  # geometry changed: the cached frame no longer fits
        self._hud_lines = None
        self._cursor_backup = None
        self._cursor_at = None
        self.mark_dirty()

    def set_zoom(self, zoom: float, anchor: tuple[int, int] | None = None) -> bool:
        """Zoom, keeping the image point under `anchor` (view coords) where it is."""
        zoom = float(np.clip(zoom, 1.0, MAX_ZOOM))
        if abs(zoom - self.zoom) < 1e-6:
            return False
        held = self.to_image(*anchor) if anchor else None
        self.zoom = zoom
        self._rebuild_view()
        if held is not None:
            self.origin = (
                int(round(held[0] - anchor[0] / self.scale)),
                int(round(held[1] - anchor[1] / self.scale)),
            )
            self._rebuild_view()
        self.status = f"zoom {self.scale * 100:.0f}%" if self.zoom > 1.0 else "zoom: fit"
        return True

    def pan(self, dx_view: int, dy_view: int) -> None:
        """Shift the visible crop by a screen-space delta."""
        if self.crop_size == (self.width, self.height):
            return
        ox, oy = self.origin
        self.origin = (
            int(round(ox + dx_view / self.scale)),
            int(round(oy + dy_view / self.scale)),
        )
        self._rebuild_view()

    def reset_zoom(self) -> None:
        self.zoom = 1.0
        self.origin = (0, 0)
        self._rebuild_view()

    def zoom_anchor(self) -> tuple[int, int]:
        """Keyboard zoom pivots on the cursor, or on the middle of the view."""
        if self.cursor is not None:
            return self.cursor
        return (self.view_size[0] // 2, self.view_size[1] // 2)

    def pan_step(self) -> int:
        return max(20, self.view_size[0] // 6)

    def to_image(self, x: int, y: int) -> tuple[int, int]:
        ox, oy = self.origin
        return (
            int(np.clip(round(ox + x / self.scale), 0, self.width - 1)),
            int(np.clip(round(oy + y / self.scale), 0, self.height - 1)),
        )

    @property
    def works_without_clicks(self) -> bool:
        """Chroma key with a configured key colour needs no seeds at all."""
        getter = getattr(self.segmenter, "key_lab", None)
        return getter is not None and getter() is not None

    def mark_dirty(self) -> None:
        """Ask for one frame rebuild (HUD text, mode switches, …)."""
        self._dirty = True

    def _downscale(self, mask: np.ndarray) -> np.ndarray:
        ox, oy = self.origin
        crop_w, crop_h = self.crop_size
        whole = (ox, oy) == (0, 0) and (crop_w, crop_h) == (self.width, self.height)
        if whole and self.view_size == (self.width, self.height):
            return mask  # 1:1 on the whole template: the overlay reads the mask itself
        return cv2.resize(
            mask[oy : oy + crop_h, ox : ox + crop_w],
            self.view_size,
            interpolation=cv2.INTER_NEAREST,
        )

    # -- mask computation
    def resegment(self) -> None:
        """Kick off segmentation. Heavy backends run in a worker thread.

        GrabCut needs ~1 s on a 2048 px card; doing that inside the mouse callback
        froze the window on every click. The old mask stays on screen (with a
        `computing…` note) until the new one arrives, and a newer click makes any
        in-flight result stale.
        """
        if not self.positives and not self.rects and not self.works_without_clicks:
            self._token += 1
            self.computing = False
            self.segmented = self.initial.copy() if self.initial is not None else np.zeros(
                (self.height, self.width), np.uint8
            )
            self.compose()
            return

        self._token += 1
        job = copy.copy(self.segmenter)  # snapshot the knob values for this run
        args = (self._token, job, list(self.positives), list(self.negatives))
        self.computing = True
        self._dirty = True
        threading.Thread(target=self._segment_worker, args=args, daemon=True).start()

    def _segment_worker(self, token: int, job, positives, negatives) -> None:
        with self._seg_lock:  # one at a time; rapid clicks collapse to the last one
            if token != self._token:
                return
            try:
                result = job.segment(positives, negatives)
            except Exception as exc:  # pragma: no cover - surfaced in the HUD
                result = exc
        self._results.put((token, result))

    def collect_segmentation(self) -> None:
        """Apply the newest finished segmentation, drop stale ones."""
        newest = None
        while True:
            try:
                newest = self._results.get_nowait()
            except queue.Empty:
                break
        if newest is None:
            return
        token, result = newest
        if token != self._token:
            return
        self.computing = False
        self._dirty = True
        if isinstance(result, Exception):
            self.status = f"segmentation failed: {result}"
            return
        for x1, y1, x2, y2 in self.rects:
            cv2.rectangle(result, (x1, y1), (x2, y2), 255, -1)
        for x, y in self.negatives:
            cv2.circle(result, (x, y), NEGATIVE_BLOCK_RADIUS, 0, -1)
        self.segmented = result
        self.compose()

    def wait_for_segmentation(self, timeout: float = 60.0) -> None:
        """Block until nothing is in flight — for scripts and tests."""
        deadline = time.monotonic() + timeout
        while self.computing and time.monotonic() < deadline:
            self.collect_segmentation()
            if self.computing:
                time.sleep(0.01)

    def compose(self, region: tuple[int, int, int, int] | None = None) -> None:
        """Brush strokes + hole filling + grow/shrink. No re-segmentation.

        `region` (y0, y1, x0, x1) limits the work to the area a brush stroke just
        touched; the global operations are deferred until the stroke ends.
        """
        if region is None or (self._pending_global and not self.dragging):
            mask = np.maximum(self.segmented, self.brush_add)
            mask[self.brush_erase > 0] = 0
            if self.fill_holes:
                mask = _fill_holes(mask)
            if self.dilate:
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
                op = cv2.dilate if self.dilate > 0 else cv2.erode
                mask = op(mask, kernel, iterations=abs(self.dilate))
            self.mask = mask
            self.mask_small = self._downscale(mask)
            self._pending_global = False
        else:
            y0, y1, x0, x1 = region
            patch = np.maximum(self.segmented[y0:y1, x0:x1], self.brush_add[y0:y1, x0:x1])
            patch[self.brush_erase[y0:y1, x0:x1] > 0] = 0
            self.mask[y0:y1, x0:x1] = patch
            self._refresh_small(region)
            if self.fill_holes or self.dilate:
                self._pending_global = True  # exact result once the stroke ends

        share = float((self.mask_small > 127).mean())  # display copy: 4x cheaper
        if self.positives and share > LEAK_THRESHOLD:
            self.status = "LEAK? mask covers most of the frame - lower knobs, 't', or brush it"
        elif self.positives and share < 1e-4 and not self.computing:
            self.status = "mask empty - negative clicks may have eaten it; undo (u)"
        elif self.status.startswith(("LEAK", "mask empty")):
            self.status = ""
        self._dirty = True

    def _refresh_small(self, region: tuple[int, int, int, int]) -> None:
        if self.mask_small is self.mask:  # 1:1 view: the overlay already sees it
            return
        y0, y1, x0, x1 = region
        ox, oy = self.origin
        crop_w, crop_h = self.crop_size
        x0, x1 = max(x0, ox), min(x1, ox + crop_w)  # the stroke may run off-screen
        y0, y1 = max(y0, oy), min(y1, oy + crop_h)
        if x1 <= x0 or y1 <= y0:
            return
        view_w, view_h = self.view_size
        sx0 = int((x0 - ox) * view_w / crop_w)
        sx1 = min(view_w, int((x1 - ox) * view_w / crop_w) + 1)
        sy0 = int((y0 - oy) * view_h / crop_h)
        sy1 = min(view_h, int((y1 - oy) * view_h / crop_h) + 1)
        if sy1 <= sy0 or sx1 <= sx0:
            return
        self.mask_small[sy0:sy1, sx0:sx1] = cv2.resize(
            self.mask[y0:y1, x0:x1], (sx1 - sx0, sy1 - sy0), interpolation=cv2.INTER_NEAREST
        )

    # kept for callers/tests that do not care about the split
    recompute = resegment

    def _apply_stroke(self, kind: str, points, radius: int) -> None:
        """Paint one stroke, clearing the opposite layer under it.

        The two brush layers are flat, so without this the erase layer would win
        forever: compose() zeroes every erased pixel last, and painting over an
        erased area would do nothing. Clearing the opposite layer makes the later
        stroke the one that counts, which is what a brush is expected to do.
        """
        target, other = (
            (self.brush_add, self.brush_erase)
            if kind == "add"
            else (self.brush_erase, self.brush_add)
        )
        _draw_stroke(target, points, radius)
        _draw_stroke(other, points, radius, 0)

    def _rebuild_strokes(self) -> None:
        self.brush_add[:] = 0
        self.brush_erase[:] = 0
        for kind, points, radius in self.strokes:
            self._apply_stroke(kind, points, radius)

    def restore(self, data: dict) -> None:
        """Reload clicks/strokes from a sidecar; the saved mask stays until a change."""
        as_points = lambda seq: [(int(p[0]), int(p[1])) for p in seq or ()]  # noqa: E731
        self.positives = as_points(data.get("positives"))
        self.negatives = as_points(data.get("negatives"))
        self.rects = [tuple(int(v) for v in r) for r in data.get("rects") or ()]
        self.strokes = [
            (str(kind), as_points(points), int(radius))
            for kind, points, radius in data.get("strokes") or ()
        ]
        self.dilate = int(data.get("dilate", 0) or 0)
        self.fill_holes = bool(data.get("fill_holes", False))
        self.brush_radius = int(data.get("brush_radius", self.brush_radius) or self.brush_radius)
        self.history = (
            ["pos"] * len(self.positives)
            + ["neg"] * len(self.negatives)
            + ["rect"] * len(self.rects)
            + ["stroke"] * len(self.strokes)
        )
        self._rebuild_strokes()
        self.status = f"restored {len(self.positives)}+{len(self.negatives)} clicks"
        self.mark_dirty()

    # -- rendering
    def render(self) -> np.ndarray:
        """Cached: rebuilds the frame only when something actually changed."""
        self.collect_segmentation()
        if self._dirty or self._canvas is None:
            self._build_frame()
            self._dirty = False
            self._cursor_backup = None
            self._cursor_at = None
            self.frame_version += 1

        want_cursor = self.brush_mode and self.cursor is not None
        if want_cursor and self._cursor_at != self.cursor:
            # dirty-rect: restore what was under the old circle, stamp the new one.
            # Copying the whole frame per mouse move cost 3.4 MB of memcpy each time.
            self._restore_under_cursor()
            self._stamp_cursor()
            self._cursor_at = self.cursor
            self.frame_version += 1
        elif not want_cursor and self._cursor_backup is not None:
            self._restore_under_cursor()
            self._cursor_at = None
            self.frame_version += 1
        return self._canvas

    def _restore_under_cursor(self) -> None:
        if self._cursor_backup is None:
            return
        (y0, y1, x0, x1), patch = self._cursor_backup
        self._canvas[y0:y1, x0:x1] = patch
        self._cursor_backup = None

    def _stamp_cursor(self) -> None:
        radius = max(2, int(self.brush_radius * self.scale))
        cx, cy = self.cursor[0] + self._view_x, self.cursor[1] + self._panel_h
        margin = radius + 3
        y0, y1 = max(0, cy - margin), min(self._canvas.shape[0], cy + margin)
        x0, x1 = max(0, cx - margin), min(self._canvas.shape[1], cx + margin)
        if y1 <= y0 or x1 <= x0:
            return
        self._cursor_backup = ((y0, y1, x0, x1), self._canvas[y0:y1, x0:x1].copy())
        cv2.circle(self._canvas, (cx, cy), radius, (0, 0, 0), 2)
        cv2.circle(self._canvas, (cx, cy), radius, (255, 255, 255), 1)

    def _build_frame(self) -> np.ndarray:
        # one persistent canvas holds HUD + image, so a frame costs no allocation
        if self._canvas is None:
            win_w, win_h = self._win_size
            self._canvas = np.zeros((win_h, win_w, 3), np.uint8)
        img_h, img_w = self.base.shape[:2]
        vx = self._view_x
        view = self._canvas[self._panel_h : self._panel_h + img_h, vx : vx + img_w]
        np.copyto(view, self.base)

        painted = self.mask_small > 127
        view[painted] = (
            0.45 * np.array((60, 220, 60), np.float32) + 0.55 * view[painted]
        ).astype(np.uint8)
        if not self.dragging:  # the outline is cosmetic; skip it while painting
            view[cv2.Canny(self.mask_small, 50, 150) > 0] = (0, 0, 0)

        for x, y in self.positives:
            cv2.circle(view, self._to_view(x, y), SEED_RADIUS, (255, 255, 255), -1)
            cv2.circle(view, self._to_view(x, y), SEED_RADIUS, (0, 160, 0), 1)
        for x, y in self.negatives:
            cv2.circle(view, self._to_view(x, y), SEED_RADIUS, (255, 255, 255), -1)
            cv2.circle(view, self._to_view(x, y), SEED_RADIUS, (0, 0, 220), 2)
        for x1, y1, x2, y2 in self.rects:
            cv2.rectangle(view, self._to_view(x1, y1), self._to_view(x2, y2), (0, 200, 255), 1)
        if self.drag_start and self.drag_now:
            cv2.rectangle(view, self.drag_start, self.drag_now, (0, 200, 255), 1)
        self._draw_hud()
        return self._canvas

    def _to_view(self, x: int, y: int) -> tuple[int, int]:
        ox, oy = self.origin
        return (int(round((x - ox) * self.scale)), int(round((y - oy) * self.scale)))

    def _draw_hud(self) -> None:
        coverage = 100.0 * float((self.mask_small > 127).mean())
        mode = (
            f"BRUSH r={self.brush_radius}"
            if self.brush_mode
            else ("RECT" if self.rect_mode else "POINTS")
        )
        state = (
            f"input={mode}  backend={self.backend}  "
            + "".join(
                f"{label}={getattr(self.segmenter, attr)} "
                for label, attr, *_ in getattr(self.segmenter, "KNOBS", ())
            )
            + f" fill_holes={'on' if self.fill_holes else 'off'}  "
            f"dilate={self.dilate:+d}  area={coverage:.1f}%  "
            f"zoom={self.scale * 100:.0f}%"
        )
        # the status row is always reserved: a panel that grows and shrinks would
        # shift the image under the cursor and make clicks land in the wrong place
        status = "computing..." if self.computing else self.status
        lines = (STAGE_TITLE[self.kind], *HELP_LINES, state, status)
        if lines == self._hud_lines:  # text unchanged -> the panel is still valid
            return
        self._hud_lines = lines

        pad, line_h = 8, 20
        panel = self._canvas[: self._panel_h]
        panel[:] = 0
        for i, line in enumerate(lines):
            if i == 0:
                color = (255, 255, 255)
            elif line == "computing...":
                color = (80, 200, 255)
            else:
                color = (200, 200, 200)
            cv2.putText(
                panel,
                line,
                (pad, pad + line_h * i + 14),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                color,
                1,
                cv2.LINE_AA,
            )

    @property
    def panel_height(self) -> int:
        return self._panel_h

    # -- input
    def on_mouse(self, event: int, x: int, y: int, flags: int, _param) -> None:
        if event == cv2.EVENT_MOUSEWHEEL:
            # the Win32 backend reports screen coordinates for wheel events, so
            # anchor on the last position a move event gave us instead
            delta = wheel_delta(flags)
            if delta:
                step = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
                self.set_zoom(self.zoom * step, anchor=self.cursor)
            return

        y -= self.panel_height  # HUD sits above the image
        x -= self._view_x  # and the image may be centred in a wider window
        if event == cv2.EVENT_MBUTTONDOWN:
            self._pan_from = (x, y)
            self.status = "panning"
            return
        if event == cv2.EVENT_MBUTTONUP:
            self._pan_from = None
            return
        if self._pan_from is not None and event == cv2.EVENT_MOUSEMOVE:
            if not flags & cv2.EVENT_FLAG_MBUTTON:  # released outside the window
                self._pan_from = None
            else:
                self.pan(self._pan_from[0] - x, self._pan_from[1] - y)
                self._pan_from = (x, y)
                return
        if y < 0:
            return
        self.cursor = (x, y)
        if self.brush_mode:
            self._on_mouse_brush(event, x, y, flags)
            return
        if self.rect_mode:
            self._on_mouse_rect(event, x, y)
            return

        if event == cv2.EVENT_LBUTTONDOWN:
            self.positives.append(self.to_image(x, y))
            self.history.append("pos")
            self.resegment()
        elif event == cv2.EVENT_RBUTTONDOWN:
            self.negatives.append(self.to_image(x, y))
            self.history.append("neg")
            self.resegment()

    def _stroke_region(self, points, radius: int) -> tuple[int, int, int, int]:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        margin = radius + 2
        return (
            max(0, min(ys) - margin),
            min(self.height, max(ys) + margin),
            max(0, min(xs) - margin),
            min(self.width, max(xs) + margin),
        )

    def _merge_pending(self, region: tuple[int, int, int, int]) -> None:
        """Grow the not-yet-composed region to cover `region` as well."""
        previous = self._pending_stroke_region
        if previous is None:
            self._pending_stroke_region = region
            return
        self._pending_stroke_region = (
            min(previous[0], region[0]),
            max(previous[1], region[1]),
            min(previous[2], region[2]),
            max(previous[3], region[3]),
        )

    def _on_mouse_brush(self, event: int, x: int, y: int, flags: int) -> None:
        point = self.to_image(x, y)
        if event in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_RBUTTONDOWN):
            kind = "add" if event == cv2.EVENT_LBUTTONDOWN else "erase"
            self.strokes.append((kind, [point], self.brush_radius))
            self.history.append("stroke")
            self._apply_stroke(kind, [point], self.brush_radius)
            self.dragging = True
            self.compose(self._stroke_region([point], self.brush_radius))
        elif event == cv2.EVENT_MOUSEMOVE and self.strokes and self.dragging:
            kind, points, radius = self.strokes[-1]
            expected = cv2.EVENT_FLAG_LBUTTON if kind == "add" else cv2.EVENT_FLAG_RBUTTON
            if not flags & expected:  # button released outside the window
                self._end_stroke()
                return
            segment = [points[-1], point]
            self._apply_stroke(kind, segment, radius)
            points.append(point)
            # drawing is cheap, recomposing is not: coalesce moves into ~40 fps.
            # every skipped move still has to reach compose() eventually, so the
            # regions accumulate - dropping them left holes along a fast drag.
            self._merge_pending(self._stroke_region(segment, radius))
            now = time.monotonic()
            if now - self._last_brush_compose >= 0.025:
                self._last_brush_compose = now
                region = self._pending_stroke_region
                self._pending_stroke_region = None
                self.compose(region)
        elif event in (cv2.EVENT_LBUTTONUP, cv2.EVENT_RBUTTONUP):
            self._end_stroke()

    def end_stroke(self) -> None:
        """Public: the key loop ends a drag when it switches tool."""
        self._end_stroke()

    def _end_stroke(self) -> None:
        """Finish a brush drag: apply whatever was coalesced, then the global steps."""
        if not self.dragging:
            return
        self.dragging = False
        region = getattr(self, "_pending_stroke_region", None)
        self._pending_stroke_region = None
        if region is not None:
            self.compose(region)
        if self._pending_global:
            self.compose()  # exact fill_holes / dilate over the whole mask
        else:
            self.mask_small = self._downscale(self.mask)  # drop the rounding drift
        self._dirty = True

    def _on_mouse_rect(self, event: int, x: int, y: int) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            self.drag_start = (x, y)
            self.drag_now = (x, y)
            self._dirty = True
        elif event == cv2.EVENT_MOUSEMOVE and self.drag_start:
            self.drag_now = (x, y)
            self._dirty = True  # rubber band follows the cursor
        elif event == cv2.EVENT_LBUTTONUP and self.drag_start:
            x1, y1 = self.to_image(*self.drag_start)
            x2, y2 = self.to_image(x, y)
            self.drag_start = self.drag_now = None
            self._dirty = True
            if abs(x2 - x1) > 2 and abs(y2 - y1) > 2:
                self.rects.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
                self.history.append("rect")
                self.resegment()
        elif event == cv2.EVENT_RBUTTONDOWN:
            self.negatives.append(self.to_image(x, y))
            self.history.append("neg")
            self.resegment()

    def undo(self) -> None:
        if not self.history:
            return
        self.dragging = False
        self._pending_stroke_region = None
        last = self.history.pop()
        if last == "stroke":
            self.strokes.pop()
            self._rebuild_strokes()
            self.compose()
            return
        {"pos": self.positives, "neg": self.negatives, "rect": self.rects}[last].pop()
        self.resegment()

    def reset(self) -> None:
        self.positives.clear()
        self.negatives.clear()
        self.rects.clear()
        self.history.clear()
        self.strokes.clear()
        self.brush_add[:] = 0
        self.brush_erase[:] = 0
        self.dragging = False
        self._pending_stroke_region = None
        self.dilate = 0
        self.fill_holes = False
        self.resegment()


def _draw_stroke(target: np.ndarray, points, radius: int, value: int = 255) -> None:
    for point in points:
        cv2.circle(target, point, radius, value, -1)
    for start, end in zip(points, points[1:]):
        cv2.line(target, start, end, value, 2 * radius)


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    filled = mask.copy()
    contours, _ = cv2.findContours(filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(filled, contours, -1, 255, -1)
    return filled


def build_mask_headless(
    template_path,
    kind: str,
    positives,
    negatives=(),
    backend: str | None = None,
    sam_checkpoint: str | None = None,
    sam_model_type: str | None = None,
    dilate: int = 0,
    fill_holes: bool = False,
    erase_strokes=(),
    paint_strokes=(),
    brush_radius: int | None = None,
    key_color: str | None = None,
    soft: bool = False,
) -> tuple[pathlib.Path, np.ndarray]:
    """Same segmentation as the editor, but from a scripted click list (no GUI).

    Lets a template be re-masked reproducibly and makes the pipeline testable.
    `erase_strokes` / `paint_strokes` are the scripted equivalent of the brush —
    the only practical way to mask a near-black template.
    """
    image, _ = load_image(template_path, "Шаблон")
    backend = backend or default_backend(sam_checkpoint, key_color)
    segmenter = build_segmenter(backend, image, sam_checkpoint, sam_model_type, key_color, soft)
    mask = segmenter.segment(list(positives), list(negatives))

    radius = brush_radius or max(6, int(0.012 * max(image.shape[:2])))
    for stroke in paint_strokes:
        _draw_stroke(mask, list(stroke), radius)
    if erase_strokes:
        erase = np.zeros(image.shape[:2], np.uint8)
        for stroke in erase_strokes:
            _draw_stroke(erase, list(stroke), radius)
        mask[erase > 0] = 0

    if fill_holes:
        mask = _fill_holes(mask)
    if dilate:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        op = cv2.dilate if dilate > 0 else cv2.erode
        mask = op(mask, kernel, iterations=abs(dilate))
    if not (mask > 127).any():
        raise UserError(
            f"маска ({STAGE_HINT_UA[kind]}) вийшла порожня — перевірте координати кліків"
        )
    saved = save_mask(mask_path(template_path, kind), mask, soft=soft)
    save_sidecar(  # so the editor opens with these clicks instead of a blank slate
        template_path,
        kind,
        backend=backend,
        positives=positives,
        negatives=negatives,
        strokes=[("erase", list(s), radius) for s in erase_strokes]
        + [("add", list(s), radius) for s in paint_strokes],
        dilate=dilate,
        fill_holes=fill_holes,
        brush_radius=radius,
    )
    return saved, mask


def _check_gui_available() -> None:
    build = cv2.getBuildInformation()
    if "GUI:" in build and "NO" in build.split("GUI:")[1].split("\n")[1]:  # pragma: no cover
        raise UserError(
            "ця збірка OpenCV без GUI — mask-editor не запуститься.\n"
            "Встановіть `opencv-python` (не `opencv-python-headless`)."
        )


def run_mask_editor(
    template_path,
    stages=STAGES,
    backend: str | None = None,
    sam_checkpoint: str | None = None,
    sam_model_type: str | None = None,
    overwrite: bool = False,
    key_color: str | None = None,
    soft: bool = False,
) -> dict[str, pathlib.Path]:
    """Build and save masks interactively. Returns {stage: saved path}."""
    _check_gui_available()
    image, _ = load_image(template_path, "Шаблон")
    backend = backend or default_backend(sam_checkpoint, key_color)
    segmenter = build_segmenter(backend, image, sam_checkpoint, sam_model_type, key_color, soft)
    print(f"Сегментація: {segmenter.name}")
    if backend == CHROMA:
        print(
            f"Ключовий колір: {key_color or 'візьму з першого кліку'} — "
            "маска будується без кліків, ENTER щоб зберегти"
        )

    saved: dict[str, pathlib.Path] = {}
    for kind in stages:
        existing_path = find_mask(template_path, kind)
        initial = None
        if existing_path is not None:
            previous, _ = load_image(existing_path, "Маска")
            previous = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
            if previous.shape[:2] == image.shape[:2]:
                initial = np.where(previous > 127, 255, 0).astype(np.uint8)
                print(f"[{kind}] знайдено готову маску {existing_path.name} "
                      "— 'n' щоб залишити як є, або клікайте заново")

        editor = _StageEditor(image, segmenter, kind, initial, backend, key_color, soft)
        if initial is None and editor.works_without_clicks:
            editor.resegment()  # chroma key: the mask is ready before the first click
        if initial is not None:
            remembered = load_sidecar(template_path, kind)
            if remembered:
                editor.restore(remembered)
                print(
                    f"[{kind}] відновлено {len(editor.positives)} позитивних, "
                    f"{len(editor.negatives)} негативних точок і {len(editor.strokes)} мазків "
                    "— можна доправити, а не починати заново"
                )

        window = f"HexPh mask-editor - {kind}"
        # NORMAL, not AUTOSIZE: AUTOSIZE locks the window to the frame size, so a
        # tall template opened a window the screen could not hold and the user
        # could neither resize it nor reach the bottom of the image
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window, *editor.window_size)
        try:
            cv2.moveWindow(window, 30, 20)
        except cv2.error:  # pragma: no cover - backend without window placement
            pass
        cv2.setMouseCallback(window, editor.on_mouse)
        try:
            result = _stage_loop(window, editor, kind)
        finally:
            cv2.destroyWindow(window)
            cv2.waitKey(1)
        backend, segmenter = editor.backend, editor.segmenter  # carry a 't' switch over

        if result is None:
            done = ", ".join(f"{k} -> {v.name}" for k, v in saved.items())
            raise UserError(
                "редактор масок скасовано користувачем"
                + (f" (уже збережено: {done})" if saved else " — нічого не збережено")
            )
        if result == "keep":
            saved[kind] = existing_path  # type: ignore[assignment]
            print(f"[{kind}] залишено без змін: {existing_path.name}")
            continue

        target = mask_path(template_path, kind)
        if target.exists() and not overwrite and existing_path != target:
            print(f"[{kind}] перезаписую {target.name}")
        saved[kind] = save_mask(target, editor.mask, soft=editor.soft and editor.backend == CHROMA)
        save_sidecar(  # remember the clicks for next time
            template_path,
            kind,
            backend=editor.backend,
            positives=editor.positives,
            negatives=editor.negatives,
            rects=editor.rects,
            strokes=editor.strokes,
            dilate=editor.dilate,
            fill_holes=editor.fill_holes,
            brush_radius=editor.brush_radius,
        )
        print(f"[{kind}] маску збережено: {saved[kind]}")

    return saved


def _sync_window_size(window: str, editor: _StageEditor) -> bool:
    """Re-fit the view after the user dragged the window edge."""
    try:
        _, _, win_w, win_h = cv2.getWindowImageRect(window)
    except cv2.error:  # pragma: no cover - backend without window geometry
        return False
    if win_w <= 0 or win_h <= 0:
        return False
    return editor.set_view_size(win_w, win_h)


def _stage_loop(window: str, editor: _StageEditor, kind: str) -> str | None:
    """-> 'save' | 'keep' | None (cancelled)."""
    shown_version = -1
    ticks = 0
    while True:
        ticks += 1
        if ticks % 5 == 0 and _sync_window_size(window, editor):
            shown_version = -1
        frame = editor.render()
        if editor.frame_version != shown_version:  # nothing changed -> no redraw
            cv2.imshow(window, frame)
            shown_version = editor.frame_version
        key = cv2.waitKey(20) & 0xFF

        if key == 255:  # no key this tick — keep the loop cheap
            if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                return None
            continue

        if key in (27, ord("q")):
            return None
        if key in (13, 10):  # ENTER only: "s" now pans down (WASD)
            if editor.computing:
                editor.status = "waiting for segmentation..."
                editor.wait_for_segmentation()
            if not (editor.mask > 127).any():
                editor.status = "mask is empty - add positive clicks first"
                editor.mark_dirty()
                continue
            return "save"
        if key == ord("n"):
            if editor.initial is None:
                editor.status = "no existing mask to keep"
                editor.mark_dirty()
                continue
            return "keep"

        if key == ord("u"):
            editor.undo()
        elif key == ord("r"):
            editor.reset()
        elif key == ord("x"):
            editor.rect_mode = not editor.rect_mode
            editor.brush_mode = False
            editor.drag_start = editor.drag_now = None
            editor.end_stroke()
            editor.mark_dirty()
        elif key == ord("b"):
            editor.brush_mode = not editor.brush_mode
            editor.rect_mode = False
            editor.end_stroke()
            editor.status = "brush: drag LMB to paint, RMB to erase" if editor.brush_mode else ""
            editor.mark_dirty()
        elif key in (ord(","), ord(".")) and editor.brush_mode:
            step = max(2, editor.brush_radius // 4)
            editor.brush_radius = int(
                np.clip(editor.brush_radius + (-step if key == ord(",") else step), 2, 400)
            )
            editor.mark_dirty()
        elif key == ord("0"):
            fitted = fit_window_size(editor.width, editor.height, editor.panel_height)
            cv2.resizeWindow(window, *fitted)
            editor.set_view_size(*fitted, force=True)
            editor.reset_zoom()
            editor.status = "window fitted to screen, zoom reset"
            shown_version = -1
        elif key in (ord("z"), ord("Z")):
            step = ZOOM_STEP if key == ord("z") else 1.0 / ZOOM_STEP
            editor.set_zoom(editor.zoom * step, anchor=editor.zoom_anchor())
            shown_version = -1
        elif key in PAN_KEYS:
            dx, dy = PAN_KEYS[key]
            editor.pan(dx * editor.pan_step(), dy * editor.pan_step())
            shown_version = -1
        elif key == ord("f"):
            editor.fill_holes = not editor.fill_holes
            editor.compose()
        elif key == ord("t") and editor.backend in CYCLE_BACKENDS:
            order = CYCLE_BACKENDS
            editor.backend = order[(order.index(editor.backend) + 1) % len(order)]
            editor.segmenter = build_segmenter(
                editor.backend, editor.image, key_color=editor.key_color, soft=editor.soft
            )
            editor.status = f"backend: {editor.segmenter.name}"
            editor.resegment()
        elif key == ord("g"):
            editor.dilate = min(editor.dilate + 1, 12)
            editor.compose()
        elif key == ord("h"):
            editor.dilate = max(editor.dilate - 1, -12)
            editor.compose()
        elif key in (ord("["), ord("]"), ord("-"), ord("=")):
            knobs = getattr(editor.segmenter, "KNOBS", ())
            index = 0 if key in (ord("["), ord("]")) else 1
            if index < len(knobs):
                label, attr, low, high, step = knobs[index]
                delta = -step if key in (ord("["), ord("-")) else step
                value = int(np.clip(getattr(editor.segmenter, attr) + delta, low, high))
                setattr(editor.segmenter, attr, value)
                editor.status = f"{label} = {value}"
                editor.resegment()
        else:
            editor.status = ""
            editor.mark_dirty()

        if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:  # window closed by [x]
            return None
