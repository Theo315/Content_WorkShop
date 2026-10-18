"""User-facing errors: printed as a readable message, never as a traceback."""

from __future__ import annotations


class UserError(Exception):
    """Wrong input, missing file, broken config — anything the user can fix."""


def require_file(path, what: str):
    """Return `path` if it points at an existing file, else raise UserError."""
    import pathlib

    p = pathlib.Path(path)
    if not p.exists():
        raise UserError(f"{what} не знайдено: {p}")
    if not p.is_file():
        raise UserError(f"{what} не є файлом: {p}")
    return p
