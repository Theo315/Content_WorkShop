#!/usr/bin/env python3
"""Entry point.

    python recolor.py                     -> GUI (same as double-clicking HexPh.exe)
    python recolor.py mask-editor ...      -> CLI
    python recolor.py batch ...            -> CLI
"""

from __future__ import annotations

import os
import sys

if sys.version_info < (3, 11):  # pragma: no cover
    sys.exit("Потрібен Python 3.11 або новіший.")


def _ensure_streams(cli_mode: bool) -> None:
    """A windowed PyInstaller build has no stdout — the first `print` would crash.

    With arguments (CLI mode) reattach to the console that launched us so output is
    visible; otherwise fall back to a null sink and let the GUI capture its own log.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    if cli_mode and sys.platform == "win32":
        try:
            import ctypes

            if ctypes.windll.kernel32.AttachConsole(-1):
                console = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
                sys.stdout = sys.stderr = console
                return
        except Exception:  # pragma: no cover - no parent console
            pass
    sink = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or sink
    sys.stderr = sys.stderr or sink

try:
    from hexph.cli import main
except ImportError as exc:  # pragma: no cover - missing deps
    sys.exit(
        f"Не вдалося завантажити залежності ({exc}).\n"
        "Встановіть їх: pip install -r requirements.txt"
    )


def _hide_own_console() -> None:
    """Hide the console window when we were double-clicked into GUI mode.

    The build keeps the console subsystem so `HexPh.exe batch ...` behaves like a
    normal CLI tool (the shell waits for it and can capture output). The pid check
    makes sure we only hide a console that belongs to us, never the user's shell.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if not handle:
            return
        owner = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == os.getpid():
            ctypes.windll.user32.ShowWindow(handle, 0)  # SW_HIDE
    except Exception:  # pragma: no cover - console tricks are best-effort
        pass


def _run() -> int:
    cli_mode = len(sys.argv) > 1
    _ensure_streams(cli_mode)
    if cli_mode:
        return main()

    _hide_own_console()
    from hexph.gui import run

    return run()


if __name__ == "__main__":
    raise SystemExit(_run())
