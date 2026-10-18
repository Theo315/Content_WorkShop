"""Where a rendered card is written.

The layout is a pattern, not hard-coded, so the operator decides the structure:

    {brand}/{model}/{code}   ->  Asturcolor/Ford Kuga/2DB.png   (default)
    {model}/{brand} {code}   ->  Ford Kuga/Asturcolor 2DB.png
    {code}                   ->  2DB.png

Placeholders: brand, model, code, hex, template. Each `/` becomes a folder level;
every segment is sanitised, so a stray slash inside a paint code or a brand name
cannot escape the output root.
"""

from __future__ import annotations

import pathlib
import re
import string

from .errors import UserError

DEFAULT_PATTERN = "{brand}/{model}/{code}"
PLACEHOLDERS = ("brand", "model", "code", "hex", "template")
PRESETS = (
    "{brand}/{model}/{code}",
    "{brand}/{code}",
    "{model}/{code}",
    "{code}",
)

_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def validate_pattern(pattern: str) -> str:
    """Check placeholders and shape once, before the batch starts."""
    pattern = (pattern or DEFAULT_PATTERN).strip().replace("\\", "/")
    if not pattern:
        raise UserError("шаблон шляху не може бути порожнім")
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:", pattern):
        raise UserError(
            f"шаблон шляху має бути відносним (без диска й початкового слеша): {pattern!r}"
        )

    names = {name for _, name, _, _ in string.Formatter().parse(pattern) if name}
    unknown = sorted(names - set(PLACEHOLDERS))
    if unknown:
        raise UserError(
            f"невідомі підстановки у шаблоні шляху: {', '.join(unknown)}. "
            f"Доступні: {', '.join('{' + p + '}' for p in PLACEHOLDERS)}"
        )
    if ".." in pattern.split("/"):
        raise UserError("шаблон шляху не може містити `..`")
    return pattern


def sanitize_segment(value: str, fallback: str = "_") -> str:
    """One folder or file name: no separators, no reserved device names."""
    cleaned = _UNSAFE.sub("-", str(value)).replace("/", "-").strip().rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    if cleaned.lower().split(".")[0] in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:120]


def render_pattern(
    pattern: str,
    *,
    brand: str,
    model: str,
    code: str,
    hex_color: str = "",
    template: str = "",
) -> pathlib.PurePosixPath:
    """Pattern + values -> a relative path (no suffix), every segment sanitised."""
    values = {
        "brand": brand,
        "model": model,
        "code": code,
        "hex": (hex_color or "").lstrip("#"),
        "template": template,
    }
    try:
        rendered = pattern.format(**values)
    except (KeyError, IndexError, ValueError) as exc:  # pragma: no cover - validated earlier
        raise UserError(f"не вдалося застосувати шаблон шляху {pattern!r}: {exc}") from None

    segments = [sanitize_segment(part) for part in rendered.replace("\\", "/").split("/") if part.strip()]
    if not segments:
        raise UserError(f"шаблон шляху {pattern!r} дав порожній шлях")
    return pathlib.PurePosixPath(*segments)


def resolve_output_path(
    root,
    pattern: str,
    *,
    brand: str,
    model: str,
    code: str,
    hex_color: str = "",
    template: str = "",
    overwrite: bool = False,
    suffix: str = ".png",
) -> tuple[pathlib.Path, str | None]:
    """Final file path. Without `overwrite`, adds `_v2`, `_v3`… instead of clobbering."""
    relative = render_pattern(
        pattern, brand=brand, model=model, code=code, hex_color=hex_color, template=template
    )
    base = pathlib.Path(root).expanduser() / pathlib.Path(*relative.parts)
    base = base.with_name(base.name + suffix)
    if overwrite or not base.exists():
        return base, None

    version = 2
    while True:
        candidate = base.with_name(f"{base.stem}_v{version}{suffix}")
        if not candidate.exists():
            return candidate, f"{base.name} вже існує, пишу {candidate.name}"
        version += 1
