"""Per-template configuration (`template_config.json`)."""

from __future__ import annotations

import dataclasses
import json
import pathlib

from .errors import UserError, require_file

DEFAULT_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


@dataclasses.dataclass
class TemplateConfig:
    brand_name: str = ""
    plate_box: tuple[int, int, int, int] | None = None  # x1, y1, x2, y2
    """Number-plate rectangle. `None` = the template has no plate, so no text is
    drawn and a batch can run with no config at all."""
    model_name: str | None = None
    """Car make/model for the output folder, e.g. "Ford Kuga". Stored per template so
    the folder name is not tied to the template's file name. CLI `--model` wins."""
    key_color: str | None = None
    """Chroma-key colour the template body is painted in (e.g. `#1E9E52`). When set,
    the mask is built by hue with no clicks at all."""
    font_path: str | None = None
    font_size: int = 34
    text_color: tuple[int, int, int] = (20, 20, 20)  # RGB
    plate_bg_color: tuple[int, int, int] | None = None  # RGB; None = sample from the template
    source: pathlib.Path | None = None

    @property
    def plate_size(self) -> tuple[int, int]:
        if self.plate_box is None:
            return (0, 0)
        x1, y1, x2, y2 = self.plate_box
        return (x2 - x1, y2 - y1)

    def resolved_font_path(self) -> str | None:
        """Config font, else the first available system fallback, else None."""
        if self.font_path:
            candidate = pathlib.Path(self.font_path)
            if not candidate.is_absolute() and self.source is not None:
                near_config = self.source.parent / candidate
                if near_config.exists():
                    return str(near_config)
            if candidate.exists():
                return str(candidate)
            raise UserError(
                f"шрифт із конфігу не знайдено: {self.font_path}\n"
                "Вкажіть існуючий .ttf у `font_path` або приберіть це поле "
                "(тоді буде взято системний шрифт)."
            )
        for fallback in DEFAULT_FONT_CANDIDATES:
            if pathlib.Path(fallback).exists():
                return fallback
        return None

    def validate_against(self, width: int, height: int) -> None:
        if self.plate_box is None:
            return
        x1, y1, x2, y2 = self.plate_box
        if x2 <= x1 or y2 <= y1:
            raise UserError(
                f"plate_box має бути [x1, y1, x2, y2] з x2>x1 та y2>y1, отримано {list(self.plate_box)}"
            )
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise UserError(
                f"plate_box {list(self.plate_box)} виходить за межі шаблону {width}x{height}"
            )


MASK_SIDECAR_SUFFIXES = ("_car_mask", "_marker_mask")
MASK_SIDECAR_KEYS = frozenset({"backend", "positives", "negatives", "rects", "strokes"})


def looks_like_mask_sidecar(raw: dict, path: pathlib.Path) -> bool:
    """`<template>_car_mask.json` remembers editor clicks — it is not a config.

    Both files sit next to the template and both are JSON, so the file dialog
    happily offers the wrong one; say so instead of reporting a missing field.
    """
    if "brand_name" in raw or "plate_box" in raw:
        return False
    if path.stem.endswith(MASK_SIDECAR_SUFFIXES):
        return True
    return len(MASK_SIDECAR_KEYS & raw.keys()) >= 3


def config_path_for_sidecar(path) -> pathlib.Path:
    """`<template>_car_mask.json` -> the config that belongs to that template.

    Prefers one that actually exists, in the same order the window autofills:
    `<template>_config.json`, then a shared `template_config.json`.
    """
    path = pathlib.Path(path)
    stem = path.stem
    for suffix in MASK_SIDECAR_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    own = path.with_name(f"{stem}_config.json")
    shared = path.with_name("template_config.json")
    for candidate in (own, shared):
        if candidate.exists():
            return candidate
    return own


def _as_int_tuple(value, name: str, length: int) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise UserError(f"`{name}` має бути списком з {length} цілих чисел, отримано {value!r}")
    try:
        return tuple(int(v) for v in value)
    except (TypeError, ValueError):
        raise UserError(f"`{name}` містить нечислове значення: {value!r}") from None


def _key_color(value) -> str | None:
    if not value:
        return None
    from .colors import normalize_hex

    try:
        return normalize_hex(str(value))
    except UserError:
        raise UserError(f"`key_color` має бути HEX, отримано {value!r}") from None


def load_template_config(path) -> TemplateConfig:
    config_path = require_file(path, "Файл конфігурації шаблону")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise UserError(f"конфіг не є валідним JSON ({config_path}): {exc.msg}, рядок {exc.lineno}") from None
    if not isinstance(raw, dict):
        raise UserError(f"конфіг має бути JSON-об'єктом: {config_path}")

    if looks_like_mask_sidecar(raw, config_path):
        expected = config_path_for_sidecar(config_path)
        hint = (
            f"Вкажіть {expected.name} — він лежить поруч із шаблоном."
            if expected.exists()
            else f"Конфігу {expected.name} ще немає: у вікні натисніть "
            '«1. Задати номер…» — програма створить його сама.'
        )
        raise UserError(
            f"{config_path.name} — це кліки маски (їх пише mask-editor), "
            f"а не конфіг шаблону.\n{hint}"
        )

    if "brand_name" not in raw:
        raise UserError(f"у конфігу {config_path} відсутнє обов'язкове поле `brand_name`")

    brand = str(raw["brand_name"]).strip()
    if not brand:
        raise UserError("`brand_name` не може бути порожнім")

    font_size = raw.get("font_size", 34)
    try:
        font_size = int(font_size)
    except (TypeError, ValueError):
        raise UserError(f"`font_size` має бути числом, отримано {font_size!r}") from None
    if font_size < 6:
        raise UserError(f"`font_size` замалий: {font_size}")

    plate_bg = raw.get("plate_bg_color", "auto")
    if isinstance(plate_bg, str):
        if plate_bg.strip().lower() != "auto":
            raise UserError(
                f'`plate_bg_color` має бути [r, g, b] або "auto", отримано {plate_bg!r}'
            )
        plate_bg = None
    else:
        plate_bg = _as_int_tuple(plate_bg, "plate_bg_color", 3)

    return TemplateConfig(
        brand_name=brand,
        model_name=(str(raw["model_name"]).strip() or None) if raw.get("model_name") else None,
        key_color=_key_color(raw.get("key_color")),
        plate_box=(
            _as_int_tuple(raw["plate_box"], "plate_box", 4)  # type: ignore[arg-type]
            if raw.get("plate_box")
            else None
        ),
        font_path=raw.get("font_path") or None,
        font_size=font_size,
        text_color=_as_int_tuple(raw.get("text_color", [20, 20, 20]), "text_color", 3),  # type: ignore[arg-type]
        plate_bg_color=plate_bg,  # type: ignore[arg-type]
        source=config_path,
    )
