#!/usr/bin/env python3
"""Generate a synthetic demo template + masks + config + CSV.

Not part of the production pipeline — it exists so the batch can be exercised
end to end (and by `tools/selfcheck.py`) without a real AI-generated template.

    python tools/make_demo_template.py demo
"""

from __future__ import annotations

import json
import pathlib
import sys

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hexph.colors import hex_to_bgr  # noqa: E402
from hexph.image_io import save_image  # noqa: E402
from hexph.plate_text import draw_plate_text  # noqa: E402

W, H = 1400, 1100
BODY_HEX = "#B9BEC4"
PLATE_BOX = (700, 990, 1150, 1040)

BODY = np.array(
    [(185, 690), (215, 545), (300, 470), (450, 395), (760, 385), (960, 425),
     (1160, 480), (1230, 550), (1240, 690)], np.int32
)
GLASS_FRONT = np.array([(470, 415), (735, 405), (735, 470), (452, 470)], np.int32)
GLASS_REAR = np.array([(755, 405), (935, 432), (985, 470), (755, 470)], np.int32)
WHEELS = ((395, 690, 84), (1055, 690, 84))
HEADLIGHT = ((1205, 520), (52, 26))
GRILLE = (1180, 560, 1245, 600)

MARKER_LEFT = (120, 780, 225, 1060)
MARKER_RIGHT = (255, 780, 360, 1060)
MARKER_CAP_H = 86

COLORS = [
    ("G41", "#7FA9B5", False), ("G42", "#9A8F6E", False), ("G43", "#5C5C5C", False),
    ("G44", "#EFEFEA", False), ("G45", "#1E2328", False), ("G46", "#8C9BA8", True),
    ("G47", "#B23A3A", False), ("G48", "#2E5C8A", False), ("G49", "#3F6B4A", True),
    ("G50", "#D9C27A", False), ("G51", "#6E4B8A", False), ("G52", "#C96A2B", False),
    ("G53", "#A8AEB3", True), ("G54", "#144B52", False), ("G55", "#F2D5C4", False),
    ("G56", "#404A55", True), ("G57", "#7C1F2E", False), ("G58", "#96A87F", False),
    ("G59", "#2B2B2B", False), ("G60", "#E0E4E8", True),
]


def _shading(shape: tuple[int, int]) -> np.ndarray:
    """Vertical highlight band + horizontal falloff — gives the body real L variance."""
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]].astype(np.float32)
    shade = 0.80 + 0.30 * np.exp(-((yy - 455.0) ** 2) / (2 * 95.0**2))
    shade += 0.08 * np.cos((xx / shape[1]) * np.pi)
    shade -= 0.18 * np.clip((yy - 620.0) / 120.0, 0, 1)  # shadow under the sills
    return np.clip(shade, 0.55, 1.16)[:, :, None]  # stays clear of pure white


def build(out_dir: pathlib.Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    bgr = np.full((H, W, 3), 255, np.uint8)
    alpha = np.zeros((H, W), np.uint8)

    body_area = np.zeros((H, W), np.uint8)
    cv2.fillPoly(body_area, [BODY], 255)
    for cx, cy, r in WHEELS:  # wheel arches belong to the body silhouette
        cv2.circle(body_area, (cx, cy), r + 16, 255, -1)

    shade = _shading((H, W))
    painted = np.clip(np.array(hex_to_bgr(BODY_HEX), np.float32) * shade, 0, 255).astype(np.uint8)
    bgr[body_area > 0] = painted[body_area > 0]
    alpha[body_area > 0] = 255

    car_mask = body_area.copy()

    def cut(draw, color):
        """Draw a non-repaintable part and punch it out of the car mask."""
        patch = np.zeros((H, W), np.uint8)
        draw(patch)
        bgr[patch > 0] = color
        alpha[patch > 0] = 255
        car_mask[cv2.dilate(patch, np.ones((3, 3), np.uint8)) > 0] = 0

    cut(lambda m: cv2.fillPoly(m, [GLASS_FRONT], 255), (66, 58, 52))
    cut(lambda m: cv2.fillPoly(m, [GLASS_REAR], 255), (72, 64, 58))
    cut(lambda m: [cv2.circle(m, (cx, cy), r, 255, -1) for cx, cy, r in WHEELS], (28, 28, 30))
    cut(lambda m: cv2.ellipse(m, HEADLIGHT[0], HEADLIGHT[1], 0, 0, 360, 255, -1), (205, 235, 245))
    cut(lambda m: cv2.rectangle(m, GRILLE[:2], GRILLE[2:], 255, -1), (40, 40, 42))
    for cx, cy, r in WHEELS:  # rims, on top of the tires
        cv2.circle(bgr, (cx, cy), 40, (150, 152, 155), -1)
        cv2.circle(bgr, (cx, cy), 14, (110, 112, 115), -1)

    # two touch-up markers: left = paint (recoloured cap), right = varnish (never touched)
    marker_mask = np.zeros((H, W), np.uint8)
    for index, (x1, y1, x2, y2) in enumerate((MARKER_LEFT, MARKER_RIGHT)):
        cv2.rectangle(bgr, (x1, y1), (x2, y2), (208, 210, 212), -1)
        cv2.rectangle(bgr, (x1, y1), (x2, y2), (150, 152, 155), 2)
        cv2.rectangle(alpha, (x1, y1), (x2, y2), 255, -1)
        cap = (x1 + 3, y1 + 3, x2 - 3, y1 + MARKER_CAP_H)
        cap_color = hex_to_bgr(BODY_HEX) if index == 0 else (235, 237, 240)
        cv2.rectangle(bgr, cap[:2], cap[2:], cap_color, -1)
        if index == 0:
            cv2.rectangle(marker_mask, cap[:2], cap[2:], 255, -1)

    # number plate with placeholder text
    px1, py1, px2, py2 = PLATE_BOX
    cv2.rectangle(bgr, (px1 - 8, py1 - 8), (px2 + 8, py2 + 8), (250, 250, 250), -1)
    cv2.rectangle(bgr, (px1 - 8, py1 - 8), (px2 + 8, py2 + 8), (170, 170, 170), 2)
    cv2.rectangle(alpha, (px1 - 8, py1 - 8), (px2 + 8, py2 + 8), 255, -1)
    bgr, _ = draw_plate_text(
        bgr, PLATE_BOX, "Asturcolor XXX", "C:/Windows/Fonts/arialbd.ttf"
        if pathlib.Path("C:/Windows/Fonts/arialbd.ttf").exists() else None,
        34, (20, 20, 20)
    )

    template = out_dir / "asturcolor_demo.png"
    save_image(template, bgr, alpha)
    save_image(out_dir / "asturcolor_demo_car_mask.png", cv2.cvtColor(car_mask, cv2.COLOR_GRAY2BGR))
    save_image(
        out_dir / "asturcolor_demo_marker_mask.png", cv2.cvtColor(marker_mask, cv2.COLOR_GRAY2BGR)
    )

    config_path = out_dir / "template_config.json"
    config_path.write_text(
        json.dumps(
            {
                "brand_name": "Asturcolor",
                "plate_box": list(PLATE_BOX),
                "font_path": "C:/Windows/Fonts/arialbd.ttf",
                "font_size": 34,
                "text_color": [20, 20, 20],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    csv_path = out_dir / "colors.csv"
    csv_path.write_text(
        "code,hex,metallic\n"
        + "".join(f"{code},{hex_value},{'true' if metallic else 'false'}\n" for code, hex_value, metallic in COLORS),
        encoding="utf-8",
    )

    return {"template": template, "config": config_path, "colors": csv_path}


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "demo")
    for name, path in build(target).items():
        print(f"{name}: {path}")
