"""Pick the number-plate rectangle by dragging on the template (no pixel maths).

Used by the GUI so a new template can be configured without hand-editing JSON.
"""

from __future__ import annotations

import json
import pathlib

import cv2
import numpy as np

from .errors import UserError
from .image_io import load_image
from .masking import fit_window_size
from .plate_text import detect_plate

MAX_DISPLAY_SIDE = 1000
HELP = (
    "Drag a rectangle over the NUMBER PLATE (the white area with the code).",
    "ENTER accept    r reset    ESC cancel    0 fit window",
)
PANEL_H = 20 * (len(HELP) + 1) + 16


def pick_plate_box(template_path) -> tuple[int, int, int, int] | None:
    """Show the template, let the user drag over the plate. -> box or None."""
    image, _ = load_image(template_path, "Шаблон")
    height, width = image.shape[:2]
    fitted = fit_window_size(width, height, PANEL_H)
    state = {"start": None, "now": None, "box": None, "panel": PANEL_H,
             "scale": 1.0, "base": image, "size": None}

    def refit(win_w: int, win_h: int) -> None:
        """Rescale the preview to the current window, so nothing is cut off."""
        win_w, win_h = max(320, int(win_w)), max(PANEL_H + 120, int(win_h))
        if state["size"] == (win_w, win_h):
            return
        state["size"] = (win_w, win_h)
        scale = min(1.0, win_w / width, (win_h - PANEL_H) / height)
        state["scale"] = scale
        state["base"] = (
            cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if scale < 1.0
            else image.copy()
        )

    refit(*fitted)

    def to_image(x: int, y: int) -> tuple[int, int]:
        scale = state["scale"]
        return (
            int(np.clip(round(x / scale), 0, width - 1)),
            int(np.clip(round(y / scale), 0, height - 1)),
        )

    def on_mouse(event, x, y, flags, _param):
        y -= state["panel"]
        if y < 0:
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            state["start"] = (x, y)
            state["now"] = (x, y)
            state["box"] = None
        elif event == cv2.EVENT_MOUSEMOVE and state["start"]:
            state["now"] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and state["start"]:
            x1, y1 = to_image(*state["start"])
            x2, y2 = to_image(x, y)
            state["start"] = state["now"] = None
            if abs(x2 - x1) > 8 and abs(y2 - y1) > 4:
                state["box"] = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    window = "HexPh - plate box"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)  # AUTOSIZE cannot be resized by the user
    cv2.resizeWindow(window, *fitted)
    try:
        cv2.moveWindow(window, 30, 20)
    except cv2.error:  # pragma: no cover - backend without window placement
        pass
    cv2.setMouseCallback(window, on_mouse)
    try:
        while True:
            try:
                _, _, win_w, win_h = cv2.getWindowImageRect(window)
                if win_w > 0 and win_h > 0:
                    refit(win_w, win_h)
            except cv2.error:  # pragma: no cover
                pass
            scale = state["scale"]
            view = state["base"].copy()
            if state["box"]:
                x1, y1, x2, y2 = state["box"]
                cv2.rectangle(
                    view,
                    (int(x1 * scale), int(y1 * scale)),
                    (int(x2 * scale), int(y2 * scale)),
                    (0, 200, 255),
                    2,
                )
            elif state["start"] and state["now"]:
                cv2.rectangle(view, state["start"], state["now"], (0, 200, 255), 1)

            lines = list(HELP)
            lines.append(
                f"box = {list(state['box'])}" if state["box"] else "no box selected yet"
            )
            panel = np.zeros((PANEL_H, view.shape[1], 3), np.uint8)
            for i, line in enumerate(lines):
                cv2.putText(
                    panel,
                    line,
                    (8, 22 + 20 * i),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (255, 255, 255) if i == 0 else (200, 200, 200),
                    1,
                    cv2.LINE_AA,
                )
            state["panel"] = PANEL_H
            win_w, win_h = state["size"]
            canvas = np.zeros((win_h, win_w, 3), np.uint8)  # pad: no highgui stretching
            canvas[:PANEL_H, : panel.shape[1]] = panel
            canvas[PANEL_H : PANEL_H + view.shape[0], : view.shape[1]] = view
            cv2.imshow(window, canvas)

            key = cv2.waitKey(20) & 0xFF
            if key in (27, ord("q")):
                return None
            if key in (13, 10) and state["box"]:
                return state["box"]
            if key == ord("r"):
                state["box"] = None
            if key == ord("0"):
                cv2.resizeWindow(window, *fitted)
                refit(*fitted)
            if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                return None
    finally:
        cv2.destroyWindow(window)
        cv2.waitKey(1)


FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialn.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def write_config(
    config_path,
    template_path,
    brand_name: str,
    plate_box: tuple[int, int, int, int],
    model_name: str = "",
    key_color: str = "",
) -> pathlib.Path:
    """Create/update `template_config.json`, guessing font and size from the plate."""
    path = pathlib.Path(config_path)
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            existing = {}

    image, _ = load_image(template_path, "Шаблон")
    geometry = detect_plate(image, tuple(plate_box), key_color=key_color or existing.get("key_color"))
    box_height = geometry.int_size[1] if geometry.detected else plate_box[3] - plate_box[1]

    font = existing.get("font_path")
    if not font or not pathlib.Path(font).exists():
        font = next((f for f in FONT_CANDIDATES if pathlib.Path(f).exists()), None)

    config = {
        "brand_name": brand_name or existing.get("brand_name") or "Brand",
        "model_name": model_name.strip()
        or existing.get("model_name")
        or pathlib.Path(template_path).stem,
        "plate_box": [int(v) for v in plate_box],
        "font_path": font,
        "font_size": max(8, int(round(0.8 * box_height))),
        "text_color": existing.get("text_color", [20, 20, 20]),
        "plate_bg_color": existing.get("plate_bg_color", "auto"),
    }
    key = key_color.strip() or existing.get("key_color")
    if key:
        config["key_color"] = key
    if config["font_path"] is None:
        config.pop("font_path")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if not path.exists():  # pragma: no cover - unwritable location
        raise UserError(f"не вдалося записати конфіг: {path}")
    return path
