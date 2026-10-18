"""PNG load/save with alpha preservation (cv2.imread chokes on non-ASCII paths)."""

from __future__ import annotations

import pathlib

import cv2
import numpy as np

from .errors import UserError, require_file


def load_image(path, what: str = "Зображення") -> tuple[np.ndarray, np.ndarray | None]:
    """-> (bgr uint8, alpha uint8 or None)."""
    image_path = require_file(path, what)
    try:
        buffer = np.fromfile(str(image_path), dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_UNCHANGED)
    except Exception as exc:  # pragma: no cover - unreadable file
        raise UserError(f"не вдалося прочитати {what.lower()} {image_path}: {exc}") from None
    if image is None:
        raise UserError(f"{what} не є коректним зображенням: {image_path}")

    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), None
    if image.shape[2] == 4:
        return np.ascontiguousarray(image[:, :, :3]), np.ascontiguousarray(image[:, :, 3])
    if image.shape[2] == 3:
        return image, None
    raise UserError(f"непідтримувана кількість каналів ({image.shape[2]}) у {image_path}")


def save_image(path, bgr: np.ndarray, alpha: np.ndarray | None = None) -> pathlib.Path:
    out_path = pathlib.Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image = bgr if alpha is None else cv2.merge([*cv2.split(bgr), alpha])
    ok, buffer = cv2.imencode(out_path.suffix or ".png", image)
    if not ok:  # pragma: no cover - encoder failure
        raise UserError(f"не вдалося закодувати зображення для {out_path}")
    buffer.tofile(str(out_path))
    return out_path
