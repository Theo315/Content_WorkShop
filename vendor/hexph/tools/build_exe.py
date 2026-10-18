#!/usr/bin/env python3
"""Build `dist/HexPh.exe` (single file).

    pip install pyinstaller
    python tools/build_exe.py

Console subsystem on purpose: `HexPh.exe batch ...` then behaves like a normal CLI
tool (the shell waits for it, output can be piped). On a double-click the app hides
its own console window and shows the GUI instead — see `_hide_own_console`.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# these never get imported by HexPh but PyInstaller likes to drag them in
EXCLUDES = (
    "matplotlib",
    "scipy",
    "pandas",
    "pytest",
    "IPython",
    "notebook",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "torch",
    "sqlite3",
    "test",
    "unittest",
)


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        "HexPh",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "build"),
        "--paths",
        str(ROOT),
        "--collect-submodules",
        "hexph",
    ]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    command.append(str(ROOT / "recolor.py"))

    print(" ".join(command))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode == 0:
        exe = ROOT / "dist" / ("HexPh.exe" if sys.platform == "win32" else "HexPh")
        size = exe.stat().st_size / 1024 / 1024 if exe.exists() else 0
        print(f"\nГотово: {exe}  ({size:.0f} МБ)")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
