"""Tkinter front-end: the whole pipeline without touching the command line.

Launched when the executable (or `recolor.py`) is started with no arguments.
Long jobs run in a worker thread; the OpenCV windows (mask editor, plate picker)
run modally on the main thread, which is what OpenCV's HighGUI expects.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, masking
from . import config as config_module
from .cli import main as cli_main
from .colors import format_colors_list, parse_colors_csv, parse_colors_list
from .errors import UserError
from .output_path import DEFAULT_PATTERN, PRESETS, render_pattern, validate_pattern
from .plate_picker import pick_plate_box, write_config
from .timestamps import DEFAULT_STEP, parse_start, parse_step_range

APP_NAME = "HexPh"
SETTINGS_PATH = pathlib.Path(
    os.environ.get("HEXPH_SETTINGS")  # overridable so tests never touch real settings
    or (pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home()) / APP_NAME / "settings.json")
)
COLORS_FILE = SETTINGS_PATH.parent / "colors.txt"  # what the text box is handed to the CLI as
PAD = 8
COLORS_HINT = "Один колір = одне фото. Формат рядка: `КОД HEX`, напр. `1KGA #7FA9B5`  (третім словом — `metallic`)"


class _QueueWriter:
    """Line-buffered stdout replacement that feeds the log widget."""

    def __init__(self, sink: queue.Queue):
        self.sink = sink
        self.buffer = ""

    def write(self, text: str) -> int:
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.sink.put(line)
        return len(text)

    def flush(self) -> None:
        if self.buffer:
            self.sink.put(self.buffer)
            self.buffer = ""

    def isatty(self) -> bool:
        return False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.messages: queue.Queue = queue.Queue()
        self.busy = False
        self.buttons: list[ttk.Button] = []
        self._autofilled_for: str | None = None

        root.title(f"{APP_NAME} {__version__} — батч-рекольоринг авто")
        root.minsize(820, 660)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)  # список кольорів
        root.rowconfigure(4, weight=2)  # журнал

        self.template = tk.StringVar()
        self.config_path = tk.StringVar()
        self.output = tk.StringVar()
        self.model = tk.StringVar()
        self.brand = tk.StringVar(value="Asturcolor")
        self.pattern = tk.StringVar(value=DEFAULT_PATTERN)
        self.key_color = tk.StringVar()
        self.lightness = tk.StringVar(value="match")
        self.overwrite = tk.BooleanVar(value=False)
        self.stamp_times = tk.BooleanVar(value=False)
        self.time_start = tk.StringVar(
            value=datetime.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        )
        self.time_step = tk.StringVar(value=DEFAULT_STEP)
        self.status = tk.StringVar(value="Готово")
        self.masks_state = tk.StringVar(value="")
        self.path_preview = tk.StringVar(value="")
        self.colors_state = tk.StringVar(value="")

        self._build_inputs()
        self._build_colors()
        self._build_actions()
        self._build_log()
        self._load_settings()

        sys.stdout = _QueueWriter(self.messages)  # noqa: SIM115 - lives for the app
        sys.stderr = sys.stdout
        self.template.trace_add("write", lambda *_: self._on_template_change())
        self._on_template_change()
        self.root.after(80, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- layout ------------------------------------------------------------
    def _build_inputs(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Вхідні дані", padding=PAD)
        frame.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD, 0))
        frame.columnconfigure(1, weight=1)

        self._row(frame, 0, "Шаблон (PNG)", self.template, self._browse_template)
        self._row(frame, 1, "Конфіг (JSON)", self.config_path, self._browse_config)
        self._row(frame, 2, "Папка виводу", self.output, self._browse_output, folder=True)

        ttk.Label(frame, textvariable=self.masks_state, foreground="#555").grid(
            row=3, column=1, columnspan=2, sticky="w", pady=(4, 0)
        )

        options = ttk.LabelFrame(self.root, text="Куди зберігати", padding=PAD)
        options.grid(row=2, column=0, sticky="ew", padx=PAD, pady=(PAD, 0))
        options.columnconfigure(5, weight=1)

        ttk.Label(options, text="Бренд").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.brand, width=16).grid(
            row=0, column=1, sticky="w", padx=(4, 16)
        )
        ttk.Label(options, text="Марка авто").grid(row=0, column=2, sticky="w")
        ttk.Entry(options, textvariable=self.model, width=22).grid(
            row=0, column=3, sticky="w", padx=(4, 16)
        )
        ttk.Label(options, text="Структура").grid(row=0, column=4, sticky="w")
        ttk.Combobox(options, textvariable=self.pattern, values=PRESETS, width=26).grid(
            row=0, column=5, sticky="w", padx=(4, 0)
        )

        ttk.Label(options, text="Ключовий колір").grid(row=1, column=4, sticky="w", pady=(6, 0))
        ttk.Entry(options, textvariable=self.key_color, width=12).grid(
            row=1, column=5, sticky="w", padx=(4, 0), pady=(6, 0)
        )
        ttk.Label(options, text="Яскравість").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(
            options, textvariable=self.lightness, values=("match", "keep"), width=8, state="readonly"
        ).grid(row=1, column=1, sticky="w", padx=(4, 16), pady=(6, 0))
        ttk.Checkbutton(options, text="Перезаписувати файли", variable=self.overwrite).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(
            options,
            text="Дата створення і зміни",
            variable=self.stamp_times,
            command=self._update_time_fields,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(options, text="з дати").grid(row=2, column=2, sticky="w", pady=(6, 0))
        self.time_start_entry = ttk.Entry(options, textvariable=self.time_start, width=18)
        self.time_start_entry.grid(row=2, column=3, sticky="w", padx=(4, 16), pady=(6, 0))
        ttk.Label(options, text="крок, хв").grid(row=2, column=4, sticky="w", pady=(6, 0))
        self.time_step_entry = ttk.Entry(options, textvariable=self.time_step, width=10)
        self.time_step_entry.grid(row=2, column=5, sticky="w", padx=(4, 0), pady=(6, 0))

        ttk.Label(options, textvariable=self.path_preview, foreground="#0a6").grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(6, 0)
        )
        self._update_time_fields()
        for var in (self.brand, self.model, self.pattern, self.output):
            var.trace_add("write", lambda *_: self._update_preview())

    def _update_time_fields(self) -> None:
        """The date/step entries only matter while the checkbox is on."""
        state = "normal" if self.stamp_times.get() else "disabled"
        self.time_start_entry.configure(state=state)
        self.time_step_entry.configure(state=state)

    def _row(self, parent, row: int, label: str, var: tk.StringVar, command, folder: bool = False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(parent, text="Огляд…" if not folder else "Папка…", command=command, width=10).grid(
            row=row, column=2, pady=2
        )

    def _build_colors(self) -> None:
        """The colour list itself — one `CODE #HEX` line per output photo."""
        frame = ttk.LabelFrame(self.root, text="Кольори (список)", padding=PAD)
        frame.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=(PAD, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text=COLORS_HINT, foreground="#555").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.colors_text = tk.Text(frame, height=8, wrap="none", undo=True, font=("Consolas", 10))
        self.colors_text.grid(row=1, column=0, sticky="nsew")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.colors_text.yview)
        bar.grid(row=1, column=1, sticky="ns")
        self.colors_text.configure(yscrollcommand=bar.set)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(buttons, text="Імпорт із CSV…", command=self._import_colors_csv, width=16).grid(
            row=0, column=0
        )
        ttk.Button(buttons, text="Очистити", command=self._clear_colors, width=12).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Label(buttons, textvariable=self.colors_state, foreground="#555").grid(
            row=0, column=2, sticky="w", padx=(12, 0)
        )
        buttons.columnconfigure(2, weight=1)

        # `<<Modified>>` fires once per change until the flag is cleared again
        self.colors_text.bind("<<Modified>>", self._on_colors_modified)
        self._update_colors_state()

    def _build_actions(self) -> None:
        frame = ttk.Frame(self.root, padding=(PAD, PAD, PAD, 0))
        frame.grid(row=3, column=0, sticky="ew")
        for index, (text, command) in enumerate(
            [
                ("1. Задати номер…", self.action_plate),
                ("2. Побудувати маски…", self.action_masks),
                ("3. Перевірка (2 кольори)", lambda: self.action_batch(preview=True)),
                ("4. Запустити батч", lambda: self.action_batch(preview=False)),
                ("Відкрити результат", self.action_open_output),
            ]
        ):
            button = ttk.Button(frame, text=text, command=command)
            button.grid(row=0, column=index, padx=(0, 6), sticky="ew")
            frame.columnconfigure(index, weight=1)
            self.buttons.append(button)

    def _build_log(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Журнал", padding=(PAD, 4))
        frame.grid(row=4, column=0, sticky="nsew", padx=PAD, pady=PAD)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log = tk.Text(frame, height=14, wrap="none", state="disabled", font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=bar.set)
        ttk.Label(self.root, textvariable=self.status, anchor="w", padding=(PAD, 0, PAD, PAD)).grid(
            row=5, column=0, sticky="ew"
        )

    # -- settings ----------------------------------------------------------
    def _load_settings(self) -> None:
        try:  # utf-8-sig: a hand-edited file may carry a BOM, which json.loads rejects
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        for key, var in self._vars().items():
            if key in data and data[key] is not None:
                var.set(data[key])
        text = data.get("colors_text")
        if not text and data.get("colors"):  # migrate the pre-list setting: the old CSV path
            text = self._csv_as_list(data["colors"])
        if text:
            self._set_colors_text(str(text))
        self._update_time_fields()  # the restored checkbox decides the entry state

    def _save_settings(self) -> None:
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: v.get() for k, v in self._vars().items()}
            payload["colors_text"] = self._colors_input()
            SETTINGS_PATH.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _vars(self) -> dict[str, tk.Variable]:
        return {
            "template": self.template,
            "config": self.config_path,
            "output": self.output,
            "model": self.model,
            "brand": self.brand,
            "pattern": self.pattern,
            "lightness": self.lightness,
            "overwrite": self.overwrite,
            "stamp_times": self.stamp_times,
            "time_start": self.time_start,
            "time_step": self.time_step,
        }

    # -- helpers -----------------------------------------------------------
    def _initial_dir(self) -> str:
        for value in (self.template.get(), self.output.get()):
            if value:
                path = pathlib.Path(value)
                return str(path.parent if path.suffix else path)
        return str(pathlib.Path.home())

    def _browse_template(self) -> None:
        path = filedialog.askopenfilename(
            title="Шаблон", initialdir=self._initial_dir(), filetypes=[("PNG", "*.png"), ("Усі", "*.*")]
        )
        if path:
            self.template.set(path)

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="template_config.json",
            initialdir=self._initial_dir(),
            filetypes=[("Конфіг шаблону", "*_config.json"), ("JSON", "*.json"), ("Усі", "*.*")],
        )
        if not path:
            return
        if self._reject_mask_sidecar(path):
            return
        self.config_path.set(path)

    def _reject_mask_sidecar(self, path) -> bool:
        """The editor writes `<template>_car_mask.json` next to the template.

        Picking it as the config used to fail with a confusing missing-field
        error, and running step 1 afterwards overwrote the clicks it holds.
        """
        path = pathlib.Path(path)
        if not path.stem.endswith(config_module.MASK_SIDECAR_SUFFIXES):
            return False
        expected = config_module.config_path_for_sidecar(path)
        messagebox.showerror(
            APP_NAME,
            f"{path.name} — це файл кліків маски, а не конфіг шаблону.\n\n"
            + (
                f"Виберіть {expected.name}."
                if expected.exists()
                else f"Конфігу {expected.name} ще немає — натисніть «1. Задати номер…», "
                "і він створиться."
            ),
        )
        return True

    # -- colour list --------------------------------------------------------
    def _colors_input(self) -> str:
        return self.colors_text.get("1.0", "end").strip("\n")

    def _set_colors_text(self, text: str) -> None:
        self.colors_text.delete("1.0", "end")
        self.colors_text.insert("1.0", text)
        self._update_colors_state()

    def _clear_colors(self) -> None:
        if self._colors_input().strip() and not messagebox.askyesno(APP_NAME, "Очистити список кольорів?"):
            return
        self._set_colors_text("")

    @staticmethod
    def _csv_as_list(path: str) -> str:
        try:
            return format_colors_list(parse_colors_csv(path))
        except (UserError, OSError, UnicodeDecodeError):
            return ""

    def _import_colors_csv(self) -> None:
        """Old CSVs still exist — pull one into the box instead of retyping it."""
        path = filedialog.askopenfilename(
            title="CSV з кольорами",
            initialdir=self._initial_dir(),
            filetypes=[("CSV", "*.csv"), ("Усі", "*.*")],
        )
        if not path:
            return
        try:
            colors = parse_colors_csv(path)
        except (UserError, OSError, UnicodeDecodeError) as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        text = format_colors_list(colors)
        if not text:
            messagebox.showwarning(APP_NAME, "У CSV немає жодного валідного рядка")
            return
        existing = self._colors_input().strip()
        if existing and messagebox.askyesno(APP_NAME, "Додати до наявного списку? (Ні — замінити)"):
            text = existing + "\n" + text
        self._set_colors_text(text)
        skipped = sum(1 for spec in colors if not spec.ok)
        self._log(f"Імпортовано з {path}: {len(colors) - skipped} кольорів"
                  + (f", пропущено битих рядків: {skipped}" if skipped else ""))

    def _on_colors_modified(self, _event=None) -> None:
        self.colors_text.edit_modified(False)
        self._update_colors_state()

    def _update_colors_state(self) -> None:
        """Live count + the first problem, so errors show up before the batch runs."""
        raw = self._colors_input()
        if not raw.strip():
            self.colors_state.set("список порожній")
            return
        try:
            colors = parse_colors_list(raw)
        except UserError as exc:
            self.colors_state.set(f"⚠ {exc}")
            return
        bad = [spec for spec in colors if not spec.ok]
        state = f"кольорів: {len(colors) - len(bad)}"
        if bad:
            state += f"  ⚠ з помилками: {len(bad)} — {bad[0].code}: {bad[0].error}"
        self.colors_state.set(state)

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(title="Папка виводу", initialdir=self._initial_dir())
        if path:
            self.output.set(path)

    def _update_preview(self) -> None:
        """Show the exact file that the next batch will write."""
        try:
            pattern = validate_pattern(self.pattern.get())
            relative = render_pattern(
                pattern,
                brand=self.brand.get().strip() or "Brand",
                model=self.model.get().strip() or "Model",
                code="2DB",
                hex_color="#9B9D9B",
                template=pathlib.Path(self.template.get() or "template").stem,
            )
        except UserError as exc:
            self.path_preview.set(f"⚠ {exc}")
            return
        root = self.output.get().strip() or "<папка виводу>"
        self.path_preview.set(f"Приклад: {pathlib.Path(root) / pathlib.Path(*relative.parts)}.png")

    def _load_config_names(self, template: pathlib.Path) -> None:
        """Brand/car make follow the selected template, not the previous one.

        Values come from the template config; without `model_name` the template's
        file name is used. Only refilled when the template selection changes, so a
        value typed by hand is not wiped while working with the same template.
        """
        if str(template) == self._autofilled_for:
            return
        self._autofilled_for = str(template)

        data = {}
        raw = self.config_path.get().strip()
        if raw:
            try:
                loaded = json.loads(pathlib.Path(raw).read_text(encoding="utf-8-sig"))
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                data = {}
        if data.get("brand_name"):
            self.brand.set(str(data["brand_name"]))
        self.model.set(str(data.get("model_name") or template.stem))
        self.key_color.set(str(data.get("key_color") or ""))

    def _on_template_change(self) -> None:
        """Autofill the config path and report whether the masks already exist."""
        raw = self.template.get().strip()
        if not raw:
            self.masks_state.set("")
            return
        template = pathlib.Path(raw)
        if not template.exists():
            self.masks_state.set("шаблон не знайдено")
            return

        if not self.config_path.get().strip():
            for candidate in (
                template.with_name(f"{template.stem}_config.json"),
                template.with_name("template_config.json"),
            ):
                if candidate.exists():
                    self.config_path.set(str(candidate))
                    break
        if not self.output.get().strip():
            self.output.set(str(template.parent / "out"))

        found = {kind: masking.find_mask(template, kind) for kind in masking.STAGES}
        labels = {masking.CAR: "кузов", masking.MARKER: "маркер"}
        parts = [f"{labels[k]}: {'✓' if v else '—'}" for k, v in found.items()]
        config = pathlib.Path(self.config_path.get()) if self.config_path.get().strip() else None
        if config is not None and config.stem.endswith(config_module.MASK_SIDECAR_SUFFIXES):
            parts.append("конфіг: ⚠ обрано кліки маски")
        elif config and config.exists():
            parts.append("конфіг: ✓")
        else:
            parts.append("конфіг: — (без номера)")
        self.masks_state.set("Маски — " + ",  ".join(parts))
        self._load_config_names(template)
        self._update_preview()

    def _require(self, **fields) -> dict[str, pathlib.Path] | None:
        resolved = {}
        for label, value in fields.items():
            raw = str(value).strip()
            if not raw:
                messagebox.showwarning(APP_NAME, f"Вкажіть: {label}")
                return None
            resolved[label] = pathlib.Path(raw)
        return resolved

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self.busy = busy
        for button in self.buttons:
            button.state(["disabled"] if busy else ["!disabled"])
        self.status.set(status or ("Працюю…" if busy else "Готово"))
        self.root.update_idletasks()

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                item = self.messages.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__done__":
                    self._set_busy(False, item[1])
                    self._on_template_change()
                else:
                    self._log(str(item))
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    # -- actions -----------------------------------------------------------
    def action_plate(self) -> None:
        if self.busy:
            return
        paths = self._require(**{"Шаблон (PNG)": self.template.get()})
        if not paths:
            return
        template = next(iter(paths.values()))
        chosen = self.config_path.get().strip()
        if chosen and pathlib.Path(chosen).stem.endswith(config_module.MASK_SIDECAR_SUFFIXES):
            self._log(f"{pathlib.Path(chosen).name} — це кліки маски, конфіг туди не пишу")
            chosen = ""
        config = chosen or str(template.with_name(f"{template.stem}_config.json"))
        self._set_busy(True, "Виділіть номерний знак у вікні OpenCV…")
        try:
            box = pick_plate_box(template)
            if box is None:
                self._log("Скасовано: номерний знак не вибрано")
                return
            path = write_config(
                config,
                template,
                self.brand.get().strip(),
                box,
                self.model.get().strip(),
                self.key_color.get().strip(),
            )
            self.config_path.set(str(path))
            self._log(f"Конфіг збережено: {path}  plate_box={list(box)}")
        except UserError as exc:
            messagebox.showerror(APP_NAME, str(exc))
        except Exception as exc:  # pragma: no cover - unexpected GUI failure
            messagebox.showerror(APP_NAME, f"{type(exc).__name__}: {exc}")
        finally:
            self._set_busy(False)
            self._on_template_change()

    def action_masks(self) -> None:
        if self.busy:
            return
        paths = self._require(**{"Шаблон (PNG)": self.template.get()})
        if not paths:
            return
        template = next(iter(paths.values()))
        key = self.key_color.get().strip() or None
        self._set_busy(True, "Редактор масок відкрито — працюйте у вікні OpenCV…")
        if key:
            self._log(f"Ключовий колір {key}: маска будується без кліків, ENTER — зберегти")
        else:
            self._log("Редактор масок: ЛКМ — кузов, ПКМ — скло/шини, b — пензель, ENTER — зберегти")
        try:
            saved = masking.run_mask_editor(template, key_color=key)
            for kind, path in saved.items():
                self._log(f"[{kind}] {path}")
        except UserError as exc:
            self._log(f"Помилка: {exc}")
            messagebox.showerror(APP_NAME, str(exc))
        except Exception as exc:  # pragma: no cover
            messagebox.showerror(APP_NAME, f"{type(exc).__name__}: {exc}")
        finally:
            self._set_busy(False)
            self._on_template_change()

    def action_batch(self, preview: bool) -> None:
        if self.busy:
            return
        required = {
            "Шаблон (PNG)": self.template.get(),
            "Папка виводу": self.output.get(),
        }
        # the config only carries the number plate: without it the batch still
        # recolours, it just draws no text
        chosen_config = self.config_path.get().strip()
        if chosen_config:
            required["Конфіг (JSON)"] = chosen_config
        paths = self._require(**required)
        if not paths:
            return
        colors_file = self._write_colors_file()
        if colors_file is None:
            return

        argv = [
            "batch",
            "--template", str(paths["Шаблон (PNG)"]),
            "--colors", str(colors_file),
            "--output", str(paths["Папка виводу"]),
            "--lightness", self.lightness.get(),
            "--pattern", self.pattern.get().strip() or DEFAULT_PATTERN,
        ]
        if "Конфіг (JSON)" in paths:
            argv += ["--config", str(paths["Конфіг (JSON)"])]
        else:
            self._log("Конфіг не вказано — номер не наноситься, лише перефарбування")
        if self.brand.get().strip():
            argv += ["--brand", self.brand.get().strip()]
        if self.model.get().strip():
            argv += ["--model", self.model.get().strip()]
        if self.stamp_times.get():
            try:
                start = parse_start(self.time_start.get())
                low, high = parse_step_range(self.time_step.get())
            except UserError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            argv += [
                "--time-start", start.strftime("%Y-%m-%d %H:%M:%S"),
                "--time-step", self.time_step.get().strip(),
            ]
            self._log(
                f"Дата створення і зміни файлів: з {start:%Y-%m-%d %H:%M:%S}, "
                f"крок {low:g}-{high:g} хв"
            )
        if self.overwrite.get():
            argv.append("--overwrite")
        if preview:
            argv.append("--preview")

        self._set_busy(True, "Перевірка на 2 кольорах…" if preview else "Батч виконується…")
        self._log("")
        self._log("> " + " ".join(argv))
        threading.Thread(target=self._batch_worker, args=(argv,), daemon=True).start()

    def _write_colors_file(self) -> pathlib.Path | None:
        """Hand the typed list to the CLI as a file (its `--colors` takes a path)."""
        text = self._colors_input().strip()
        if not text:
            messagebox.showwarning(APP_NAME, "Список кольорів порожній.\n\n" + COLORS_HINT)
            self.colors_text.focus_set()
            return None
        try:
            parse_colors_list(text)
        except UserError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return None
        try:
            COLORS_FILE.parent.mkdir(parents=True, exist_ok=True)
            COLORS_FILE.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Не вдалося зберегти список кольорів: {exc}")
            return None
        return COLORS_FILE

    def _batch_worker(self, argv: list[str]) -> None:
        try:
            code = cli_main(argv)
            summary = "Готово" if code == 0 else f"Завершено з помилками (код {code})"
        except Exception as exc:  # pragma: no cover - safety net
            self.messages.put(f"Помилка: {type(exc).__name__}: {exc}")
            summary = "Помилка"
        sys.stdout.flush()
        self.messages.put(("__done__", summary))

    def action_open_output(self) -> None:
        raw = self.output.get().strip()
        if not raw:
            messagebox.showwarning(APP_NAME, "Вкажіть: Папка виводу")
            return
        path = pathlib.Path(raw)
        # jump straight into the deepest folder the pattern actually creates
        try:
            relative = render_pattern(
                validate_pattern(self.pattern.get()),
                brand=self.brand.get().strip() or "Brand",
                model=self.model.get().strip() or "Model",
                code="x",
                template=pathlib.Path(self.template.get() or "t").stem,
            )
            for part in pathlib.Path(*relative.parts).parent.parts:
                if (path / part).is_dir():
                    path = path / part
        except UserError:
            pass
        if not path.exists():
            messagebox.showinfo(APP_NAME, f"Папки ще немає: {path}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - opening the user's own folder
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _on_close(self) -> None:
        self._save_settings()
        self.root.destroy()


def run() -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:  # pragma: no cover
        pass
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:  # pragma: no cover - non-Windows
        pass
    App(root)
    root.mainloop()
    return 0
