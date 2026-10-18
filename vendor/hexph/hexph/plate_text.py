"""Number-plate text: wipe the placeholder, draw `{brand} {code}` centred.

Real cards render the plate at a slight angle (~2° on the Ford/Skoda templates).
Wiping an axis-aligned rectangle over it leaves visible slivers of the old plate
in the corners, so the plate quad is detected from the template and both the wipe
and the text follow it.
"""

from __future__ import annotations

import dataclasses
import functools
import math

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .colors import hex_to_lab
from .errors import UserError

MIN_FONT_SIZE = 1
PADDING_RATIO = 0.06  # keeps glyphs off the plate edge, scales with plate height
MAX_PLATE_ANGLE = 30.0  # also accept tilted plates on other vehicle templates


@dataclasses.dataclass(frozen=True)
class PlateGeometry:
    """Where the plate actually is: centre, size along/across the text, tilt."""

    center: tuple[float, float]
    size: tuple[float, float]
    angle: float = 0.0
    """Tilt of the plate's long axis in image coordinates (y down), degrees:
    negative = the right end sits higher. Feeds `cv2.boxPoints` directly; PIL
    needs the opposite sign because it rotates counter-clockwise."""
    detected: bool = False
    shape: np.ndarray | None = dataclasses.field(default=None, compare=False, repr=False)
    """Exact plate silhouette (uint8, full image size) when detection succeeded —
    the wipe follows it, so the rounded frame corners survive."""

    @classmethod
    def from_box(cls, box: tuple[int, int, int, int], detected: bool = False) -> "PlateGeometry":
        x1, y1, x2, y2 = box
        return cls(((x1 + x2) / 2.0, (y1 + y2) / 2.0), (float(x2 - x1), float(y2 - y1)), 0.0, detected)

    @property
    def int_size(self) -> tuple[int, int]:
        return (max(1, int(round(self.size[0]))), max(1, int(round(self.size[1]))))


DETECT_INSET = 2.0  # px kept away from the plate frame on every side


def detect_plate(
    image_bgr: np.ndarray, plate_box: tuple[int, int, int, int], search_ratio: float = 0.3,
    *, key_color: str | None = None,
) -> PlateGeometry:
    """Find the plate's light inner surface, without inventing its outline.

    Saturated bumper reflections are not plate pixels. Keep the external contour
    (including notches/rounded corners), filling only enclosed old glyph holes.
    Failure carries an empty mask: it must never become an opaque ROI rectangle.
    """
    height, width = image_bgr.shape[:2]
    x1, y1, x2, y2 = plate_box
    rejected = dataclasses.replace(PlateGeometry.from_box(plate_box),
                                   shape=np.zeros((height, width), np.uint8))
    pad = max(8, int(max(0.6, search_ratio) * (y2 - y1)) + 4)
    sx1, sy1 = max(0, x1 - pad), max(0, y1 - pad)
    sx2, sy2 = min(width, x2 + pad), min(height, y2 + pad)

    crop = image_bgr[sy1:sy2, sx1:sx2]
    if not crop.size:
        return rejected
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    saturation = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[:, :, 1]
    inner = gray[
        y1 - sy1 + (y2 - y1) // 4 : y2 - sy1 - (y2 - y1) // 4,
        x1 - sx1 + (x2 - x1) // 8 : x2 - sx1 - (x2 - x1) // 8
    ]
    if inner.size == 0:  # pragma: no cover - guarded by config validation
        return rejected
    level = float(np.percentile(inner, 70))
    box_area = (x2 - x1) * (y2 - y1)
    box_center = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
    roi = np.zeros(gray.shape, np.uint8)
    roi[y1 - sy1:y2 - sy1, x1 - sx1:x2 - sx1] = 255
    light = gray > max(level - 25.0, 80.0)
    # The original chroma preset has a tinted plate. This opt-in pass excludes
    # neutral pixels altogether, so it cannot attach a green reflection to a
    # normal white plate if the neutral pass failed.
    passes = [light & (saturation <= 50)]
    if key_color:
        passes.append(light & (saturation >= 60) & (saturation <= 150))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    for binary in passes:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary.astype(np.uint8))
        contours = []
        # A white body can surround the plate as a disconnected bright component.
        # RETR_EXTERNAL on the whole crop would hide the plate inside that hole.
        for label in range(1, count):
            if stats[label, cv2.CC_STAT_AREA] < 20:
                continue
            lx, ly, lw, lh = stats[label, :4]
            component = (labels[ly:ly + lh, lx:lx + lw] == label).astype(np.uint8)
            component_contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL,
                                                     cv2.CHAIN_APPROX_SIMPLE, offset=(int(lx), int(ly)))
            contours.extend((label, contour) for contour in component_contours)
        candidates = []
        for label, contour in contours:
            rect = cv2.minAreaRect(contour)
            (cx, cy), (rw, rh) = rect[0], rect[1]
            if rw < rh:
                rw, rh = rh, rw
            if rh <= 6 or not 2.5 <= rw / rh <= 12 or not 0.05 * box_area <= rw * rh <= 2.0 * box_area:
                continue
            edges = np.roll(cv2.boxPoints(rect), -1, axis=0) - cv2.boxPoints(rect)
            long_edge = max(edges, key=lambda e: float(np.hypot(*e)))
            if long_edge[0] < 0:
                long_edge = -long_edge
            angle = math.degrees(math.atan2(float(long_edge[1]), float(long_edge[0])))
            dx = abs(sx1 + cx - box_center[0]) / max(1, x2 - x1)
            dy = abs(sy1 + cy - box_center[1]) / max(1, y2 - y1)
            if abs(angle) > MAX_PLATE_ANGLE or dx > 0.5 or dy > 0.65:
                continue
            silhouette = np.zeros(gray.shape, np.uint8)
            cv2.drawContours(silhouette, [contour], -1, 255, cv2.FILLED)
            area = cv2.countNonZero(silhouette)
            # An enclosing white bumper panel is not the plate: filling its hole
            # would erase the actual plate/frame inside it. Small islands enclosed
            # by old glyphs (O/8/etc.) are fine; another large bright surface isn't.
            enclosed_labels, enclosed_counts = np.unique(labels[silhouette > 0], return_counts=True)
            foreign = (enclosed_labels != 0) & (enclosed_labels != label)
            enclosed_surface = False
            for other_label, other_area in zip(enclosed_labels[foreign], enclosed_counts[foreign]):
                _, _, other_w, other_h, _ = stats[other_label]
                other_aspect = max(other_w, other_h) / max(1, min(other_w, other_h))
                if other_area > max(40, 0.08 * area) or (
                    other_area >= 40 and min(other_w, other_h) >= 6 and other_aspect >= 2.5
                ):
                    enclosed_surface = True
                    break
            if enclosed_surface:
                continue
            overlap = cv2.countNonZero(cv2.bitwise_and(silhouette, roi)) / max(1, area)
            if area / (rw * rh) < 0.7 or overlap < 0.65:
                continue
            # A uniform white bumper/crop has no distinct plate boundary.
            ring = (cv2.dilate(silhouette, kernel, iterations=2) > 0) & (silhouette == 0)
            if not ring.any() or np.mean(gray[ring] < level - 15) < 0.35:
                continue
            score = overlap - 1.5 * dy - 0.5 * dx
            candidates.append((score, cx, cy, rw, rh, angle, silhouette))
        if candidates:
            _, cx, cy, rw, rh, angle, silhouette = max(candidates, key=lambda item: item[0])
            shape = np.zeros((height, width), np.uint8)
            shape[sy1:sy2, sx1:sx2] = cv2.erode(silhouette, kernel)
            return PlateGeometry((sx1 + cx, sy1 + cy),
                                 (rw - 2 * DETECT_INSET, rh - 2 * DETECT_INSET),
                                 angle, detected=True, shape=shape)
    return rejected


@functools.lru_cache(maxsize=64)
def _load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont:
    if font_path is None:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # pragma: no cover - very old Pillow
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(font_path, size)
    except OSError as exc:
        raise UserError(f"не вдалося завантажити шрифт {font_path}: {exc}") from None


def _text_box(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int, int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return left, top, right - left, bottom - top


def _padding(plate_height: int) -> int:
    return max(2, int(round(PADDING_RATIO * plate_height)))


def fit_font_size(
    text: str,
    box_size: tuple[int, int],
    font_path: str | None,
    font_size: int | None,
    *,
    angle: float = 0.0,
    rotated_size: tuple[float, float] | None = None,
) -> tuple[int, str | None]:
    """Largest font that fits both axes, not a fixed number of shrink attempts.

    None selects the size automatically; a numeric size is an upper limit.
    rotated_size also keeps tilted glyphs inside the user's selected rectangle.
    """
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    upper = max(MIN_FONT_SIZE, int(font_size) if font_size is not None else 4 * box_size[1])
    low, high, best = MIN_FONT_SIZE, upper, None
    while low <= high:
        size = (low + high) // 2
        _, _, width, height = _text_box(probe, text, _load_font(font_path, size))
        if _text_scale(width, height, box_size, angle, rotated_size) >= 1:
            best, low = size, size + 1
        else:
            high = size - 1
    if best is not None:
        return best, None if best >= 6 else "Дуже мала область номера: текст вписано, але він може бути важко читабельним."
    return MIN_FONT_SIZE, "Дуже довгий текст або мала область номера: текст додатково масштабовано без обрізання."


def _text_scale(
    width: int, height: int, box_size: tuple[int, int], angle: float,
    rotated_size: tuple[float, float] | None,
) -> float:
    pad = _padding(box_size[1])
    limits = [(max(1, box_size[0] - 2 * pad), width), (max(1, box_size[1] - 2 * pad), height)]
    if rotated_size is not None:
        radians = math.radians(angle)
        cos, sin = abs(math.cos(radians)), abs(math.sin(radians))
        limits.extend([(max(1, rotated_size[0] - 2 * pad), cos * width + sin * height),
                       (max(1, rotated_size[1] - 2 * pad), sin * width + cos * height)])
    return min([1.0] + [available / used for available, used in limits if used > 0])


def sample_plate_background(
    image_bgr: np.ndarray,
    geometry: PlateGeometry | tuple[int, int, int, int],
    key_color: str | None = None,
) -> tuple[int, int, int]:
    """Median RGB of the light pixels inside the plate — the wipe colour.

    Real plates are not pure white (studio lighting makes them ~[199,190,185] RGB);
    wiping with #FFFFFF would leave a glowing rectangle.
    """
    if isinstance(geometry, PlateGeometry):
        (cx, cy), (w, h) = geometry.center, geometry.size
        x1, y1 = int(cx - w / 4), int(cy - h / 4)
        x2, y2 = int(cx + w / 4), int(cy + h / 4)
    else:
        x1, y1, x2, y2 = geometry
    patch = image_bgr[max(0, y1) : y2, max(0, x1) : x2].reshape(-1, 3)
    if patch.size == 0:  # pragma: no cover - guarded by config validation
        return (255, 255, 255)
    lightness = patch.mean(axis=1)
    light = patch[lightness >= np.percentile(lightness, 60)]
    b, g, r = np.median(light, axis=0).astype(int)
    return _neutralise((int(r), int(g), int(b)), key_color)


MAX_PLATE_CHROMA = 12.0
KEYED_PLATE_CHROMA = 2.0  # a plate that picked up the key colour goes fully grey
KEY_HUE_TOLERANCE = 45.0  # how far off the key hue a sample may sit and still count


def _neutralise(rgb: tuple[int, int, int], key_color: str | None = None) -> tuple[int, int, int]:
    """Keep the plate's brightness but drop a strong colour cast.

    A number plate is never saturated. If the template's body colour bleeds into it
    (a chroma-key template is a bright green, and the plate picks that up), wiping
    with the sampled colour would leave a green plate on every card.

    With a known `key_color` the cap goes further: chroma sitting on the key hue is
    bleed by definition, so it is flattened to grey. Capping it at 12 instead leaves
    a plate that still reads visibly green next to a repainted bumper.
    """
    patch = np.array([[[rgb[2], rgb[1], rgb[0]]]], np.uint8)
    lab = cv2.cvtColor(patch.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    chroma = float(np.hypot(lab[0, 0, 1], lab[0, 0, 2]))
    limit = MAX_PLATE_CHROMA
    if key_color and _on_key_hue(lab[0, 0], key_color):
        limit = KEYED_PLATE_CHROMA
    if chroma <= limit:
        return rgb
    lab[0, 0, 1:] *= limit / chroma
    bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)[0, 0] * 255.0
    b, g, r = np.clip(np.rint(bgr), 0, 255).astype(int)
    return (int(r), int(g), int(b))


def _on_key_hue(lab_pixel: np.ndarray, key_color: str) -> bool:
    key = hex_to_lab(key_color)
    delta = math.atan2(float(lab_pixel[2]), float(lab_pixel[1])) - math.atan2(
        float(key[2]), float(key[1])
    )
    gap = abs(math.degrees(math.atan2(math.sin(delta), math.cos(delta))))
    return gap <= KEY_HUE_TOLERANCE


def draw_plate_text(
    image_bgr: np.ndarray,
    plate: PlateGeometry | tuple[int, int, int, int],
    text: str,
    font_path: str | None,
    font_size: int | None,
    text_color_rgb: tuple[int, int, int],
    bg_color_rgb: tuple[int, int, int] = (255, 255, 255),
    *,
    text_bounds: tuple[int, int, int, int] | None = None,
) -> tuple[np.ndarray, str | None]:
    """Wipe the plate with `bg_color_rgb`, then centre `text` inside it."""
    geometry = plate if isinstance(plate, PlateGeometry) else PlateGeometry.from_box(plate)
    if geometry.shape is not None and not geometry.shape.any():
        return image_bgr.copy(), "Контур номера не визначено надійно: текст не нанесено. Уточніть область номера."
    width, height = geometry.int_size
    rotated_size = None
    if text_bounds is not None:
        x1, y1, x2, y2 = text_bounds
        cx, cy = geometry.center
        rotated_size = (2 * min(cx - x1, x2 - cx), 2 * min(cy - y1, y2 - cy))
    size, warning = fit_font_size(text, (width, height), font_path, font_size,
                                  angle=geometry.angle, rotated_size=rotated_size)
    font = _load_font(font_path, size)

    tile = Image.new("RGBA", (width, height), (*tuple(bg_color_rgb), 255))
    draw = ImageDraw.Draw(tile)
    left, top, text_w, text_h = _text_box(draw, text, font)
    # Draw the complete glyph bitmap first. Even pathological text that cannot
    # fit at 1 px is scaled uniformly instead of being cropped by the plate tile.
    if text_w > 0 and text_h > 0:
        glyphs = Image.new("RGBA", (text_w, text_h))
        ImageDraw.Draw(glyphs).text((-left, -top), text, font=font, fill=tuple(text_color_rgb))
        scale = _text_scale(text_w, text_h, (width, height), geometry.angle, rotated_size)
        if scale < 1:
            glyphs = glyphs.resize((max(1, int(text_w * scale)), max(1, int(text_h * scale))), Image.Resampling.LANCZOS)
        tile.alpha_composite(glyphs, ((width - glyphs.width) // 2, (height - glyphs.height) // 2))
    if abs(geometry.angle) > 0.05:
        # PIL rotates counter-clockwise, image-coordinate tilt is clockwise-positive
        tile = tile.rotate(-geometry.angle, expand=True, resample=Image.BICUBIC)

    origin = (
        int(round(geometry.center[0] - tile.width / 2)),
        int(round(geometry.center[1] - tile.height / 2)),
    )
    result = _composite(image_bgr, tile, origin, geometry.shape)
    return result, warning


def _composite(
    image_bgr: np.ndarray,
    tile: Image.Image,
    origin: tuple[int, int],
    shape: np.ndarray | None,
) -> np.ndarray:
    """Alpha-blend `tile` at `origin`, clipped to the plate silhouette if known."""
    height, width = image_bgr.shape[:2]
    ox, oy = origin
    x1, y1 = max(0, ox), max(0, oy)
    x2, y2 = min(width, ox + tile.width), min(height, oy + tile.height)
    if x1 >= x2 or y1 >= y2:  # pragma: no cover - plate outside the frame
        return image_bgr

    patch = np.array(tile)[y1 - oy : y2 - oy, x1 - ox : x2 - ox]
    alpha = patch[:, :, 3].astype(np.float32) / 255.0
    if shape is not None:
        alpha = np.minimum(alpha, shape[y1:y2, x1:x2].astype(np.float32) / 255.0)

    result = image_bgr.copy()
    target = result[y1:y2, x1:x2].astype(np.float32)
    tile_bgr = patch[:, :, [2, 1, 0]].astype(np.float32)
    blended = target + (tile_bgr - target) * alpha[:, :, None]
    result[y1:y2, x1:x2] = np.rint(np.clip(blended, 0, 255)).astype(np.uint8)
    return result


def plate_guard_mask(
    shape: tuple[int, int], geometry: PlateGeometry, margin: int
) -> np.ndarray:
    """Binary mask of the plate (grown by `margin`) — never repaint this area."""
    mask = np.zeros(shape, np.uint8)
    if geometry.shape is not None:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * margin + 1, 2 * margin + 1))
        return cv2.dilate(geometry.shape, kernel)
    (cx, cy), (w, h) = geometry.center, geometry.size
    box = cv2.boxPoints(((cx, cy), (w + 2 * margin, h + 2 * margin), geometry.angle))
    cv2.fillConvexPoly(mask, np.round(box).astype(np.int32), 255)
    return mask


def plate_bounding_box(geometry: PlateGeometry, margin: int = 0) -> tuple[int, int, int, int]:
    """Axis-aligned bbox of the (possibly rotated) plate, grown by `margin`."""
    (cx, cy), (w, h) = geometry.center, geometry.size
    box = cv2.boxPoints(((cx, cy), (w + 2 * margin, h + 2 * margin), geometry.angle))
    x1, y1 = box.min(axis=0)
    x2, y2 = box.max(axis=0)
    return (int(math.floor(x1)), int(math.floor(y1)), int(math.ceil(x2)), int(math.ceil(y2)))
