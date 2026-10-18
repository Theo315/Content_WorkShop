"""Rewrite the creation and "last written" times of the photos a batch produced.

Every file a batch writes lands within the same second, which makes a folder of
them read as one machine run. Stamping them from a start moment with a random
gap in a given range spreads them the way a real shoot would.

`os.utime` covers modified and accessed on every platform. Windows keeps a third
stamp — creation — that only `SetFileTime` can move, and Explorer sorts by it, so
it is set through the Win32 call below.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import pathlib
import random
import sys

from .errors import UserError

WINDOWS = sys.platform == "win32"

if WINDOWS:  # pragma: no cover - exercised on Windows only
    from ctypes import wintypes

    _EPOCH_AS_FILETIME = 116_444_736_000_000_000  # 1601-01-01 -> 1970-01-01, in 100 ns
    _FILE_WRITE_ATTRIBUTES = 0x0100
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.SetFileTime.restype = wintypes.BOOL
    _kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _as_filetime(moment: dt.datetime) -> _FILETIME:
        ticks = int(round(moment.timestamp() * 10_000_000)) + _EPOCH_AS_FILETIME
        return _FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)

    def _set_windows_times(path: pathlib.Path, moment: dt.datetime) -> None:
        """Creation + accessed + written in one call."""
        handle = _kernel32.CreateFileW(
            str(path), _FILE_WRITE_ATTRIBUTES, 0, None,
            _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if handle == _INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(), "CreateFileW", str(path))
        try:
            stamp = _as_filetime(moment)
            if not _kernel32.SetFileTime(
                handle, ctypes.byref(stamp), ctypes.byref(stamp), ctypes.byref(stamp)
            ):
                raise OSError(ctypes.get_last_error(), "SetFileTime", str(path))
        finally:
            _kernel32.CloseHandle(handle)


def supports_creation_time() -> bool:
    """Only Windows exposes a movable creation stamp."""
    return WINDOWS


def set_file_times(path, moment: dt.datetime) -> None:
    """Set created (Windows) and modified/accessed (everywhere) to `moment`."""
    path = pathlib.Path(path)
    epoch = moment.timestamp()
    try:
        os.utime(path, (epoch, epoch))
        if WINDOWS:
            _set_windows_times(path, moment)
    except OSError as exc:
        raise UserError(f"не вдалося змінити час файлу {path.name}: {exc}") from None


START_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
)
DEFAULT_STEP = "6-8"


def parse_start(value) -> dt.datetime:
    """Accept the formats a person actually types, not just ISO."""
    text = str(value).strip()
    if not text:
        raise UserError("не вказано початкову дату для часу файлів")
    for fmt in START_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise UserError(
        f"не розумію дату {text!r}.\n"
        "Формат: 2026-09-11 09:00 (або 11.09.2026 09:00, можна без часу)"
    )


def parse_step_range(value) -> tuple[float, float]:
    """`"6-8"` -> (6.0, 8.0) minutes. A single number means a fixed step."""
    text = str(value).strip().replace(",", ".")
    if not text:
        raise UserError("не вказано крок між фото (наприклад 6-8)")
    parts = [p.strip() for p in text.replace("–", "-").split("-") if p.strip()]
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        raise UserError(
            f"крок {text!r} має бути числом або діапазоном хвилин, наприклад 6-8"
        ) from None
    if len(numbers) == 1:
        low = high = numbers[0]
    elif len(numbers) == 2:
        low, high = numbers
    else:
        raise UserError(f"крок {text!r} має вигляд `6-8`, а не {len(numbers)} чисел")
    if low < 0 or high < 0:
        raise UserError("крок не може бути відʼємним")
    if low > high:
        low, high = high, low
    return (low, high)


def plan_times(
    count: int,
    start: dt.datetime,
    step_min: float,
    step_max: float,
    seed: int | None = None,
) -> list[dt.datetime]:
    """First photo gets `start`; each next one a random gap later.

    The gap is drawn per file, so the spacing varies the way a real series does
    instead of marching in a fixed rhythm.
    """
    rng = random.Random(seed)
    times: list[dt.datetime] = []
    moment = start
    for index in range(count):
        if index:
            moment += dt.timedelta(seconds=rng.uniform(step_min * 60.0, step_max * 60.0))
        times.append(moment)
    return times


def apply_times(paths, times) -> list[tuple[pathlib.Path, dt.datetime]]:
    """Stamp each path with its moment. -> what was actually stamped."""
    stamped: list[tuple[pathlib.Path, dt.datetime]] = []
    for path, moment in zip(paths, times):
        path = pathlib.Path(path)
        if not path.exists():
            continue
        set_file_times(path, moment)
        stamped.append((path, moment))
    return stamped


def stamp_files(
    paths,
    start,
    step: str = DEFAULT_STEP,
    seed: int | None = None,
) -> list[tuple[pathlib.Path, dt.datetime]]:
    """parse + plan + apply, in the order the paths were given."""
    paths = list(paths)
    step_min, step_max = parse_step_range(step)
    times = plan_times(len(paths), parse_start(start), step_min, step_max, seed)
    return apply_times(paths, times)
