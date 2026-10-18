"""Colour list parsing (CSV / plain list) and HEX <-> LAB conversion."""

from __future__ import annotations

import csv
import dataclasses
import pathlib
import re

import cv2
import numpy as np

from .errors import UserError, require_file

HEX_RE = re.compile(r"^#?(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")

_TRUE_WORDS = {"1", "true", "yes", "y", "t", "+", "metallic", "так", "да"}
_FALSE_WORDS = {"", "0", "false", "no", "n", "f", "-", "ні", "нет"}


@dataclasses.dataclass(frozen=True)
class ColorSpec:
    """One CSV row: paint code, target colour, metallic flag."""

    code: str
    hex: str
    metallic: bool = False
    error: str | None = None  # set instead of hex/metallic when the row is broken

    @property
    def ok(self) -> bool:
        return self.error is None


_FILENAME_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_code(raw: str) -> str:
    """`1GN00/B04` -> `1GN00`.

    Some exports glue two codes together with a slash; the first one is the real
    paint code. A slash would also turn into a directory separator on save.
    """
    code = (raw or "").strip().split("/")[0].strip()
    return _FILENAME_UNSAFE.sub("-", code).rstrip(". ")


def normalize_hex(raw: str) -> str:
    """`7fa9b5` / `#7FA9B5` / `#7AB` -> `#7FA9B5`. Raises UserError on garbage."""
    value = (raw or "").strip()
    if not HEX_RE.match(value):
        raise UserError(f"невалідний HEX: {raw!r} (очікується 6 hex-символів, напр. #7FA9B5)")
    digits = value.lstrip("#").upper()
    if len(digits) == 3:  # `#7AB` -> `#77AABB`, як у CSS
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits


def _parse_metallic(raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    raise UserError(f"невалідне значення metallic: {raw!r}")


def _build_spec(
    code_raw: str,
    hex_raw: str,
    metallic_raw: str,
    *,
    line_no: int,
    seen: dict[str, int],
) -> ColorSpec:
    """One row -> ColorSpec. Bad rows come back with `error` set, never raise.

    `line_no` names the row in messages — it is all a row with an empty code has.
    """
    code = normalize_code(code_raw)
    if not code:
        return ColorSpec(code=f"<рядок {line_no}>", hex="", error="порожній код кольору")
    if code in seen:
        return ColorSpec(code=code, hex="", error=f"дубль коду (вже є в рядку {seen[code]})")
    seen[code] = line_no
    try:
        return ColorSpec(
            code=code,
            hex=normalize_hex(hex_raw),
            metallic=_parse_metallic(metallic_raw),
        )
    except UserError as exc:
        return ColorSpec(code=code, hex="", error=str(exc))


def _sniff_dialect(sample: str) -> csv.Dialect | type[csv.Dialect]:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def parse_colors_csv(path) -> list[ColorSpec]:
    """Read `code,hex[,metallic]`. Header row is mandatory (see ТЗ 3.2).

    Structural problems (no header, missing columns, empty file) raise UserError.
    Per-row problems are returned as ColorSpec with `error` set, so the batch can
    log `[ERROR] code -> reason` and keep going.
    """
    csv_path = require_file(path, "CSV з кольорами")
    text = csv_path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise UserError(f"CSV порожній: {csv_path}")

    reader = csv.reader(text.splitlines(), dialect=_sniff_dialect(text[:2048]))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise UserError(f"CSV порожній: {csv_path}")

    header = [cell.strip().lower().lstrip("﻿") for cell in rows[0]]
    if "code" not in header or "hex" not in header:
        raise UserError(
            "CSV має починатися з заголовку `code,hex` (опційно `,metallic`). "
            f"Отримано: {','.join(rows[0]) or '<порожній рядок>'}"
        )

    i_code, i_hex = header.index("code"), header.index("hex")
    i_metallic = header.index("metallic") if "metallic" in header else None

    colors: list[ColorSpec] = []
    seen: dict[str, int] = {}
    for line_no, row in enumerate(rows[1:], start=2):
        cell = lambda i: row[i].strip() if i is not None and i < len(row) else ""  # noqa: E731
        colors.append(
            _build_spec(cell(i_code), cell(i_hex), cell(i_metallic), line_no=line_no, seen=seen)
        )

    if not colors:
        raise UserError(f"у CSV немає жодного рядка з кольором: {csv_path}")
    return colors


# --- plain list (`CODE #HEX` per line) ------------------------------------
# What the GUI text box produces and what a person naturally types or pastes out
# of a price list: one colour per line, any of `,` `;` tab `-` or spaces between
# the code and the HEX.

_LIST_SPLIT = re.compile(r"\s*[,;\t]\s*|\s+[-–—:=]\s+|\s+")
_LIST_HEADER = {"code", "код", "hex", "хекс", "колір", "color", "metallic", "метал", "металік"}


def _split_list_line(line: str) -> list[str]:
    parts = [part.strip(" \t-–—:=") for part in _LIST_SPLIT.split(line.strip())]
    return [part for part in parts if part]


def parse_colors_list(text: str, source: str = "список") -> list[ColorSpec]:
    """Parse `CODE #HEX [metallic]` lines — one output photo per line.

    Empty lines and `#`-less comments (`//`, `;;`) are skipped. Structural
    problems raise UserError; per-line problems come back as ColorSpec with
    `error` set, exactly like the CSV path, so the batch logs them and goes on.
    """
    colors: list[ColorSpec] = []
    seen: dict[str, int] = {}
    for line_no, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("//", ";;")):
            continue
        parts = _split_list_line(line)
        lowered = [part.lower().lstrip("﻿") for part in parts]
        if line_no == 1 and len(lowered) >= 2 and set(lowered) <= _LIST_HEADER:
            continue  # a `code,hex` header pasted along with the data
        if len(parts) == 1 or len(parts) > 3:
            colors.append(
                ColorSpec(
                    code=normalize_code(parts[0]) or f"<рядок {line_no}>",
                    hex="",
                    error=(
                        "у рядку лише одне значення"
                        if len(parts) == 1
                        else f"забагато значень у рядку ({len(parts)})"
                    )
                    + " — очікується `код HEX [metallic]`, напр. `1KGA #7FA9B5`",
                )
            )
            continue
        code_raw, hex_raw = parts[0], parts[1]
        if code_raw.startswith("#") and not hex_raw.startswith("#"):
            code_raw, hex_raw = hex_raw, code_raw  # HEX written first
        colors.append(
            _build_spec(
                code_raw,
                hex_raw,
                parts[2] if len(parts) > 2 else "",
                line_no=line_no,
                seen=seen,
            )
        )

    if not colors:
        raise UserError(
            f"{source}: немає жодного кольору. Формат — один колір на рядок: `1KGA #7FA9B5`"
        )
    return colors


def parse_colors(path) -> list[ColorSpec]:
    """Read a colour source by extension: `.csv` -> CSV with header, else a list."""
    source = pathlib.Path(path).expanduser()
    if source.suffix.lower() == ".csv":
        return parse_colors_csv(source)
    list_path = require_file(source, "Список кольорів")
    return parse_colors_list(list_path.read_text(encoding="utf-8-sig"), str(list_path))


def format_colors_list(colors: list[ColorSpec]) -> str:
    """ColorSpec list -> the text the GUI box shows (used when importing a CSV)."""
    lines = []
    for spec in colors:
        if not spec.ok:
            continue
        lines.append(f"{spec.code} {spec.hex}" + (" metallic" if spec.metallic else ""))
    return "\n".join(lines)


# --- colour space helpers -------------------------------------------------
# All LAB work is done in float32: L in [0, 100], a/b in [-127, 127].
# 8-bit LAB would quantize a/b to 1 unit ~= 1 ΔE, which eats the ΔE < 5 budget.


def hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
    value = normalize_hex(hex_color).lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return (b, g, r)


def hex_to_lab(hex_color: str) -> np.ndarray:
    """-> float32 array [L, a, b]."""
    bgr = np.array([[hex_to_bgr(hex_color)]], dtype=np.float32) / 255.0
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)


def bgr_to_lab(image_bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image_bgr.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)


def lab_to_bgr(image_lab: np.ndarray) -> np.ndarray:
    bgr = cv2.cvtColor(image_lab.astype(np.float32), cv2.COLOR_LAB2BGR)
    return np.clip(bgr * 255.0, 0, 255).astype(np.uint8)


def delta_e76(lab_a: np.ndarray, lab_b: np.ndarray) -> float:
    """CIE76 colour difference — the ΔE the acceptance criteria refer to."""
    return float(np.linalg.norm(np.asarray(lab_a, np.float64) - np.asarray(lab_b, np.float64)))
