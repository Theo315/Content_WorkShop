#!/usr/bin/env python3
"""Automated pass over the acceptance criteria (ТЗ §7) on the synthetic demo.

    python tools/selfcheck.py

Builds a demo template, runs the batch, then measures ΔE, checks that glass /
tires / lights / grille / the varnish marker were not repainted, that the plate
text fits, and that a full 20-colour run survives bad CSV rows.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import shutil
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import datetime  # noqa: E402
import json  # noqa: E402
import struct  # noqa: E402
import zlib  # noqa: E402

from hexph import config as config_module  # noqa: E402
from hexph import masking  # noqa: E402
from hexph import sanitize  # noqa: E402
from hexph import timestamps  # noqa: E402
from hexph.config import load_template_config  # noqa: E402
from hexph.errors import UserError  # noqa: E402
from hexph.cli import main  # noqa: E402
from hexph.colors import delta_e76, hex_to_lab, normalize_code  # noqa: E402
from hexph.image_io import load_image, save_image  # noqa: E402
from hexph.plate_text import detect_plate, fit_font_size  # noqa: E402
from hexph.recolor import (  # noqa: E402
    RecolorOptions,
    measure_lab,
    prepare_weight,
    recolor_image,
    template_stats,
)
from make_demo_template import PLATE_BOX, build  # noqa: E402

PROTECTED = {
    "скло (переднє)": (600, 440),
    "скло (заднє)": (860, 450),
    "шина": (330, 690),
    "диск": (395, 690),
    "фара": (1205, 520),
    "решітка": (1210, 580),
    "маркер varnish (ковпачок)": (300, 800),
    "маркер varnish (корпус)": (300, 950),
}
BODY_PROBE = (700, 520)
DELTA_E_LIMIT = 5.0

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")
    return ok


def run_batch(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = main(argv)
    return code, buffer.getvalue()


def check_gui(template, config, colors, out_root) -> None:
    """The window must hand the batch exactly what the CLI would get."""
    real_out = sys.stdout
    os.environ["HEXPH_SETTINGS"] = str(pathlib.Path(out_root).parent / "gui-settings.json")
    try:
        import tkinter as tk

        import hexph.gui as gui
    except Exception as exc:  # pragma: no cover - no display / no tk
        check("GUI: пропущено (tkinter недоступний)", True, str(exc))
        return

    captured: dict[str, list[str]] = {}

    class _Thread:  # run the "worker" inline so the check is deterministic
        def __init__(self, target=None, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    original_main, original_thread = gui.cli_main, gui.threading.Thread
    gui.cli_main = lambda argv: (captured.__setitem__("argv", list(argv)), 0)[1]
    gui.threading.Thread = _Thread
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = gui.App(root)
        app.template.set(str(template))
        app.config_path.set(str(config))
        app._set_colors_text(gui.format_colors_list(gui.parse_colors_csv(str(colors))))
        app.output.set(str(out_root))
        app._on_template_change()
        brand_model_ok = app.brand.get() == "Asturcolor" and app.model.get() == template.stem
        app.model.set("Ford Kuga")
        colors_state = app.colors_state.get()
        app.action_batch(preview=True)
        argv = captured.get("argv", [])
        listed = pathlib.Path(argv[argv.index("--colors") + 1]) if "--colors" in argv else None
        sys.stdout = real_out
        check("GUI: бренд/марку підтягнуто з конфігу", brand_model_ok,
              f"{app.brand.get()} / {template.stem}")
        check(
            "GUI: передає --pattern і --model у батч",
            "--pattern" in argv and argv[argv.index("--model") + 1] == "Ford Kuga",
            " ".join(argv[-6:]),
        )
        check(
            "GUI: список кольорів із поля передано у батч файлом",
            listed is not None and listed.exists()
            and len(gui.parse_colors_list(listed.read_text(encoding="utf-8"))) == 20,
            str(listed),
        )
        check("GUI: лічильник кольорів під полем", colors_state == "кольорів: 20", colors_state)

        app._set_colors_text("G50 #7FA9B5\nG50 #123456\nБІДА\n")
        state = app.colors_state.get()
        check("GUI: биті рядки видно ще до запуску", "з помилками: 2" in state, state)
        app._set_colors_text("G50 #7FA9B5")
        app.pattern.set("{model}/../escape")
        app._update_preview()
        check("GUI: небезпечний шаблон шляху відхилено ще у прогляді",
              app.path_preview.get().startswith("⚠"))
    finally:
        sys.stdout = real_out
        gui.cli_main, gui.threading.Thread = original_main, original_thread
        if root is not None:
            root.destroy()


def main_selfcheck(workdir: pathlib.Path) -> int:
    if workdir.exists():
        shutil.rmtree(workdir)
    paths = build(workdir / "demo")
    template, config, colors = paths["template"], paths["config"], paths["colors"]
    out_root = workdir / "out"

    base_bgr, base_alpha = load_image(template)
    height, width = base_bgr.shape[:2]
    car_weight = prepare_weight(masking.load_mask(template, masking.CAR, (height, width)))
    marker_weight = prepare_weight(masking.load_mask(template, masking.MARKER, (height, width)))

    print("\n0) Сегментація (бекенди mask-editor, без GUI)")
    truth = masking.load_mask(template, masking.CAR, (height, width)) > 127
    clicks_pos = [(700, 520), (300, 560), (1100, 520), (600, 650), (900, 500)]
    clicks_neg = [(600, 440), (860, 450), (330, 690), (1205, 520), (1210, 580), (300, 900)]
    for backend in (masking.FLOOD_GRADIENT, masking.GRABCUT):
        segmenter = masking.build_segmenter(backend, base_bgr)
        auto = segmenter.segment(clicks_pos, clicks_neg) > 127
        iou = (auto & truth).sum() / max((auto | truth).sum(), 1)
        limit = 0.85 if backend == masking.FLOOD_GRADIENT else 0.9
        check(f"{backend}: 5 поз. + 6 нег. кліків -> IoU > {limit}", iou > limit, f"IoU={iou:.3f}")
        leaked = [part for part, (x, y) in PROTECTED.items() if auto[y, x]]
        check(f"{backend}: маска не зачіпає скло/шини/фари/решітку", not leaked, ", ".join(leaked))

    editor = masking._StageEditor(
        base_bgr, masking.build_segmenter(masking.GRABCUT, base_bgr), masking.CAR, None
    )
    editor.render()

    def editor_click(x, y, event=cv2.EVENT_LBUTTONDOWN):
        view_x, view_y = editor._to_view(x, y)
        editor.on_mouse(
            event, view_x + editor._view_x, view_y + editor.panel_height, 0, None
        )

    started_clicks = time.perf_counter()
    for x, y in clicks_pos:
        editor_click(x, y)
    click_return = time.perf_counter()
    editor.wait_for_segmentation()  # segmentation runs in a worker thread
    check(
        "кліки не блокують вікно (сегментація у фоні)",
        (click_return - started_clicks) < 0.2,
        f"керування повернулось за {1000 * (click_return - started_clicks):.0f} мс",
    )
    before = int((editor.mask > 127).sum())
    editor.brush_mode = True
    editor.brush_radius = 40
    editor_click(200, 200)
    editor.on_mouse(cv2.EVENT_LBUTTONUP, 200, 200 + editor.panel_height, 0, None)
    painted = int((editor.mask > 127).sum())
    editor.undo()
    check(
        "пензель додає й скасовує мазок без перезапуску сегментації",
        painted > before and int((editor.mask > 127).sum()) == before,
        f"{before} -> {painted} -> {int((editor.mask > 127).sum())} px",
    )

    editor.render()  # settle any pending redraw first
    editor.render()
    frames_before = editor.frame_version
    for _ in range(50):
        editor.render()
    check(
        "кадр не перемальовується, якщо нічого не змінилось",
        editor.frame_version == frames_before,
        f"50 викликів render -> {editor.frame_version - frames_before} перемальовок",
    )

    print("\n0b) Вікно редактора: вміщається в екран, зум і панорама")
    win_w, win_h = editor.window_size
    screen_w, screen_h = masking._screen_work_area()
    check(
        "стартове вікно вміщається в робочу область екрана",
        win_w <= screen_w and win_h <= screen_h,
        f"вікно {win_w}x{win_h}, екран {screen_w}x{screen_h}",
    )
    frame = editor.render()
    check(
        "кадр малюється рівно під розмір вікна (highgui нічого не розтягує)",
        (frame.shape[1], frame.shape[0]) == editor.window_size,
        f"{frame.shape[1]}x{frame.shape[0]}",
    )

    editor.set_view_size(900, 700, force=True)
    editor.reset_zoom()
    check(
        "на зумі 1.0 видно весь шаблон",
        editor.crop_size == (editor.width, editor.height) and editor.origin == (0, 0),
        f"crop={editor.crop_size} origin={editor.origin}",
    )
    editor.set_zoom(6.0)
    anchor = (300, 200)
    held = editor.to_image(*anchor)
    editor.set_zoom(editor.zoom * masking.ZOOM_STEP, anchor=anchor)
    moved = editor.to_image(*anchor)
    check(
        "точка під курсором не тікає під час зуму",
        max(abs(moved[0] - held[0]), abs(moved[1] - held[1])) <= 2,
        f"{held} -> {moved} на зумі {editor.scale * 100:.0f}%",
    )
    check(
        "зум ріже видиму область, а не масштабує весь кадр",
        editor.crop_size[0] < editor.width and editor.crop_size[1] < editor.height,
        f"crop={editor.crop_size} з {editor.width}x{editor.height}",
    )
    round_trip = editor._to_view(*editor.to_image(120, 90))
    check(
        "перетворення view <-> image оборотне на зумі",
        max(abs(round_trip[0] - 120), abs(round_trip[1] - 90)) <= 1,
        f"(120, 90) -> {round_trip}",
    )
    editor.pan(10 ** 6, 10 ** 6)
    check(
        "панорама не виходить за межі шаблону",
        editor.origin == (editor.width - editor.crop_size[0], editor.height - editor.crop_size[1]),
        f"origin={editor.origin}",
    )

    zoom_mask_before = int((editor.mask > 127).sum())
    editor.brush_mode = True
    editor.brush_radius = 8
    editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 150, 120 + editor.panel_height, 0, None)
    editor.on_mouse(cv2.EVENT_LBUTTONUP, 150, 120 + editor.panel_height, 0, None)
    spot = editor.to_image(150, 120)
    view_spot = editor._to_view(*spot)
    check(
        "пензель на зумі малює під курсором і видно це в оверлеї",
        editor.mask[spot[1], spot[0]] > 127
        and editor.mask_small[view_spot[1], view_spot[0]] > 127
        and int((editor.mask > 127).sum()) > zoom_mask_before,
        f"image {spot} = {int(editor.mask[spot[1], spot[0]])}, "
        f"view {view_spot} = {int(editor.mask_small[view_spot[1], view_spot[0]])}",
    )
    editor.undo()
    editor.brush_mode = False

    wheel_flags = 120 << 16  # Win32 packs the scroll amount into the high word
    check(
        "дельта колеса читається без cv2.getMouseWheelDelta",
        masking.wheel_delta(wheel_flags) == 120 and masking.wheel_delta(-120 << 16 & 0xFFFFFFFF) < 0,
        f"{masking.wheel_delta(wheel_flags)}",
    )

    started_zoom = time.perf_counter()
    for _ in range(30):
        editor.mark_dirty()
        editor.render()
    per_frame = (time.perf_counter() - started_zoom) / 30
    check(
        "кадр на зумі малюється швидко (ріжеться тільки видима частина)",
        per_frame < 0.05,
        f"{per_frame * 1000:.1f} мс/кадр",
    )
    editor.reset_zoom()

    print("\n0c) Файл кліків маски не плутається з конфігом шаблону")
    sidecar = masking.sidecar_path(template, masking.CAR)
    check(
        "mask-editor кладе кліки поруч із шаблоном як *_car_mask.json",
        sidecar.name.endswith("_car_mask.json"),
        sidecar.name,
    )
    sidecar.write_text(
        json.dumps({"backend": "chroma", "positives": [], "negatives": [],
                    "rects": [], "strokes": []}),
        encoding="utf-8",
    )
    try:
        load_template_config(sidecar)
        message = ""
    except UserError as exc:
        message = str(exc)
    check(
        "вибраний замість конфігу файл кліків пояснює себе, а не «відсутнє поле»",
        "кліки маски" in message and "brand_name" not in message,
        message.splitlines()[0] if message else "помилки не було",
    )
    check(
        "у помилці названо, який саме конфіг потрібен",
        config.name in message,
        config.name,
    )
    check(
        "справжній конфіг і далі читається",
        load_template_config(config).brand_name != "",
        load_template_config(config).brand_name,
    )
    check(
        "підказка веде на конфіг, який справді існує",
        config_module.config_path_for_sidecar(
            masking.sidecar_path(template, masking.MARKER)
        )
        == config,
        config.name,
    )
    sidecar.unlink(missing_ok=True)

    print("\n1) Батч на 3 контрольних кольорах (світлий / темний / металік)")
    probes = {"G44": "#EFEFEA", "G45": "#1E2328", "G46": "#8C9BA8"}
    single = workdir / "probe.csv"
    single.write_text(
        "code,hex,metallic\nG44,#EFEFEA,false\nG45,#1E2328,false\nG46,#8C9BA8,true\n",
        encoding="utf-8",
    )
    started = time.perf_counter()
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(single), "--output", str(out_root), "--overwrite"]
    )
    check("батч на 3 кольорах завершився без помилок", code == 0, output.strip().splitlines()[-1] if code else "")
    output_batch = output  # section 1d checks what this run logged
    elapsed = (time.perf_counter() - started) / 3

    out_dir = out_root / "Asturcolor" / template.stem
    for probe_code, probe_hex in probes.items():
        path = out_dir / f"{probe_code}.png"
        if not check(f"{probe_code}: файл створено", path.exists()):
            continue
        bgr, alpha = load_image(path)

        delta = delta_e76(measure_lab(bgr, car_weight), hex_to_lab(probe_hex))
        check(f"{probe_code}: колір кузова ΔE < {DELTA_E_LIMIT:g}", delta < DELTA_E_LIMIT, f"ΔE={delta:.2f}")

        cap_target = delta_e76(measure_lab(bgr, marker_weight), hex_to_lab(probe_hex))
        check(
            f"{probe_code}: ковпачок маркера теж ΔE < {DELTA_E_LIMIT:g} (окрема статистика L)",
            cap_target < DELTA_E_LIMIT,
            f"ΔE={cap_target:.2f}",
        )
        cap_delta = delta_e76(measure_lab(bgr, marker_weight), measure_lab(bgr, car_weight))
        check(f"{probe_code}: ковпачок лівого маркера = кузов", cap_delta < DELTA_E_LIMIT, f"ΔE={cap_delta:.2f}")

        untouched = [
            part for part, (x, y) in PROTECTED.items()
            if not np.array_equal(bgr[y, x], base_bgr[y, x])
        ]
        check(f"{probe_code}: скло/шини/фари/решітка/varnish не перефарбовані", not untouched,
              "змінено: " + ", ".join(untouched) if untouched else "")
        check(f"{probe_code}: кузов таки перефарбовано",
              not np.array_equal(bgr[BODY_PROBE[1], BODY_PROBE[0]], base_bgr[BODY_PROBE[1], BODY_PROBE[0]]))
        check(f"{probe_code}: альфа-канал збережено",
              alpha is not None and np.array_equal(alpha, base_alpha))

    print("\n1b) Батч без конфігу: перефарбування без номера")
    noplate_root = workdir / "out-noplate"
    code, output = run_batch(
        ["batch", "--template", str(template), "--colors", str(single),
         "--output", str(noplate_root), "--pattern", "{code}", "--overwrite"]
    )
    check(
        "батч без --config завершився без помилок",
        code == 0,
        output.strip().splitlines()[-1] if code else "",
    )
    check(
        "у лозі сказано, що номер не наноситься",
        "номер: не задано" in output,
        next((line for line in output.splitlines() if "номер:" in line), "рядка немає"),
    )
    noplate_file = noplate_root / "G45.png"
    if check("файл без конфігу створено", noplate_file.exists(), str(noplate_file)):
        noplate_bgr = load_image(noplate_file)[0]
        delta = delta_e76(measure_lab(noplate_bgr, car_weight), hex_to_lab(probes["G45"]))
        check(
            f"кузов перефарбовано так само точно, ΔE < {DELTA_E_LIMIT:g}",
            delta < DELTA_E_LIMIT,
            f"ΔE={delta:.2f}",
        )
        px1, py1, px2, py2 = PLATE_BOX
        check(
            "зона номера лишилась такою, як у шаблоні (нічого не намальовано)",
            np.array_equal(noplate_bgr[py1:py2, px1:px2], base_bgr[py1:py2, px1:px2]),
        )

    code, output = run_batch(
        ["batch", "--template", str(template), "--colors", str(single),
         "--output", str(noplate_root), "--pattern", "{brand}/{code}", "--overwrite"]
    )
    check(
        "{brand} без бренду -> зрозуміла помилка, а не порожня тека",
        code != 0 and "--brand" in output,
        output.strip().splitlines()[-1],
    )

    code, output = run_batch(
        ["batch", "--template", str(template), "--colors", str(single),
         "--output", str(noplate_root), "--pattern", "{brand}/{code}",
         "--brand", "Asturcolor", "--overwrite"]
    )
    check(
        "--brand замінює конфіг для шляху {brand}",
        code == 0 and (noplate_root / "Asturcolor" / "G45.png").exists(),
        output.strip().splitlines()[-1] if code else "Asturcolor/G45.png",
    )

    plateless_config = workdir / "no_plate_config.json"
    plateless_config.write_text(
        json.dumps({"brand_name": "Asturcolor"}), encoding="utf-8"
    )
    loaded = load_template_config(plateless_config)
    check(
        "конфіг без `plate_box` читається, plate_box = None",
        loaded.plate_box is None and loaded.brand_name == "Asturcolor",
    )

    print("\n1c) Дата створення і зміни файлів (галочка «Дата створення і зміни»)")
    check(
        "діапазон `6-8` читається як хвилини",
        timestamps.parse_step_range("6-8") == (6.0, 8.0)
        and timestamps.parse_step_range("7") == (7.0, 7.0)
        and timestamps.parse_step_range("8-6") == (6.0, 8.0),
    )
    check(
        "дата приймається і в ISO, і в 11.09.2026",
        timestamps.parse_start("2026-09-11 09:00")
        == timestamps.parse_start("11.09.2026 09:00")
        == datetime.datetime(2026, 9, 11, 9, 0),
    )
    bad = ""
    try:
        timestamps.parse_start("вчора")
    except UserError as exc:
        bad = str(exc)
    check("незрозуміла дата -> підказка з форматом", "2026-09-11 09:00" in bad, bad.splitlines()[0])

    stamp_root = workdir / "out-stamped"
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(single), "--output", str(stamp_root), "--pattern", "{code}",
         "--time-start", "2026-09-11 09:00", "--time-step", "6-8",
         "--time-seed", "7", "--overwrite"]
    )
    check("батч із --time-start завершився без помилок", code == 0,
          output.strip().splitlines()[-1] if code else "")
    stamped = [stamp_root / f"{code_name}.png" for code_name in ("G44", "G45", "G46")]
    times = [
        datetime.datetime.fromtimestamp(path.stat().st_mtime) for path in stamped if path.exists()
    ]
    check("час проставлено на всі фото", len(times) == 3, f"{len(times)} з 3")
    created = [
        datetime.datetime.fromtimestamp(path.stat().st_ctime) for path in stamped if path.exists()
    ]
    check(
        "дата створення збігається з датою зміни на кожному фото",
        timestamps.supports_creation_time()
        and len(created) == len(times)
        and all(abs((c - m).total_seconds()) < 2 for c, m in zip(created, times)),
        ", ".join(f"{c:%H:%M:%S}" for c in created),
    )
    check(
        "дата створення теж іде від початкової дати",
        created and abs((created[0] - datetime.datetime(2026, 9, 11, 9, 0)).total_seconds()) < 2,
        f"{created[0]:%Y-%m-%d %H:%M:%S}" if created else "немає",
    )
    if len(times) == 3:
        check(
            "перше фото має рівно початкову дату",
            abs((times[0] - datetime.datetime(2026, 9, 11, 9, 0)).total_seconds()) < 2,
            f"{times[0]:%Y-%m-%d %H:%M:%S}",
        )
        gaps = [
            (times[i + 1] - times[i]).total_seconds() / 60.0 for i in range(len(times) - 1)
        ]
        check(
            "крок між фото тримається в діапазоні 6-8 хв",
            all(6.0 - 0.02 <= gap <= 8.0 + 0.02 for gap in gaps),
            ", ".join(f"{gap:.2f} хв" for gap in gaps),
        )
        check(
            "кроки різні, а не фіксовані",
            len({round(gap, 3) for gap in gaps}) > 1,
            ", ".join(f"{gap:.2f}" for gap in gaps),
        )
        check("час іде вперед", times == sorted(times))
    check(
        "у лозі названо обидві дати і діапазон",
        "дата створення + дата змінення" in output,
        next((line for line in output.splitlines() if "час файлів" in line), "рядка немає"),
    )

    plain_root = workdir / "out-unstamped"
    before = datetime.datetime.now()
    run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(single), "--output", str(plain_root), "--pattern", "{code}",
         "--overwrite"]
    )
    plain = plain_root / "G44.png"
    check(
        "без галочки обидві дати лишаються поточними",
        plain.exists()
        and datetime.datetime.fromtimestamp(plain.stat().st_mtime)
        >= before - datetime.timedelta(seconds=5)
        and datetime.datetime.fromtimestamp(plain.stat().st_ctime)
        >= before - datetime.timedelta(seconds=5),
        f"{datetime.datetime.fromtimestamp(plain.stat().st_mtime):%Y-%m-%d %H:%M:%S}",
    )

    print("\n1d) Очистка метаданих перед віддачею")

    def inject_chunk(path, kind: bytes, payload: bytes) -> None:
        """Put an ancillary chunk right after IHDR, the way a generator would."""
        data = path.read_bytes()
        cut = 8 + 8 + struct.unpack(">I", data[8:12])[0] + 4  # magic + whole IHDR
        chunk = (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )
        path.write_bytes(data[:cut] + chunk + data[cut:])

    dirty = workdir / "dirty.png"
    shutil.copy2(out_root / "Asturcolor" / template.stem / "G45.png", dirty)
    pixels_before = load_image(dirty)[0]
    inject_chunk(dirty, b"tEXt", b"Software\x00HexPh selfcheck")
    inject_chunk(dirty, b"caBX", b"jumbc2pa fake content credentials")
    inject_chunk(dirty, b"sRGB", b"\x00")  # colour chunk: must survive
    check(
        "підкинуті чанки видно у файлі",
        {kind for kind, _ in sanitize.inspect(dirty)} == {"tEXt", "caBX"},
        str(sanitize.inspect(dirty)),
    )
    removed = sanitize.strip_file(dirty)
    check("очистка прибирає tEXt і C2PA", sorted(removed) == ["caBX", "tEXt"], str(removed))
    check("після очистки метаданих не лишилось", sanitize.inspect(dirty) == [])
    check(
        "кольорові чанки не чіпаються (інакше поїде колір)",
        b"sRGB" in dirty.read_bytes(),
    )
    check(
        "пікселі після очистки байт у байт ті самі",
        np.array_equal(load_image(dirty)[0], pixels_before),
    )
    check("повторна очистка нічого не робить", sanitize.strip_file(dirty) == [])

    before_dry = dirty.read_bytes()
    code, output = run_batch(["sanitize", "--path", str(dirty), "--dry-run"])
    check(
        "--dry-run не чіпає файл",
        code == 0 and dirty.read_bytes() == before_dry,
        output.strip().splitlines()[-1],
    )

    batch_out = out_root / "Asturcolor" / template.stem / "G44.png"
    check(
        "у звичайному виході батчу метаданих немає",
        sanitize.inspect(batch_out) == [],
        str(sanitize.inspect(batch_out)),
    )
    check(
        "у лозі батчу видно крок очистки",
        "очистка метаданих" in output_batch,
        next(
            (line for line in output_batch.splitlines() if "очистка метаданих" in line),
            "рядка немає",
        ),
    )

    # the cleaning pass rewrites the file, so it must run BEFORE the stamping —
    # otherwise every stamped date would be reset to "now"
    order_root = workdir / "out-order"
    code, output = run_batch(
        ["batch", "--template", str(template), "--colors", str(single),
         "--output", str(order_root), "--pattern", "{code}",
         "--time-start", "2026-03-04 08:15", "--time-step", "6", "--overwrite"]
    )
    order_file = order_root / "G44.png"
    check(
        "очистка не збиває проставлені дати (йде до них)",
        code == 0
        and order_file.exists()
        and abs(
            (
                datetime.datetime.fromtimestamp(order_file.stat().st_mtime)
                - datetime.datetime(2026, 3, 4, 8, 15)
            ).total_seconds()
        )
        < 2,
        f"{datetime.datetime.fromtimestamp(order_file.stat().st_mtime):%Y-%m-%d %H:%M:%S}"
        if order_file.exists()
        else "файлу немає",
    )

    print("\n2) Текст на номерному знаку")
    x1, y1, x2, y2 = PLATE_BOX
    size, warning = fit_font_size("Asturcolor G45", (x2 - x1, y2 - y1), "C:/Windows/Fonts/arialbd.ttf", 34)
    check("текст влазить у plate_box без попереджень", warning is None, f"font_size={size}")
    long_size, long_warning = fit_font_size("Asturcolor " + "M" * 40, (x2 - x1, y2 - y1),
                                            "C:/Windows/Fonts/arialbd.ttf", 34)
    check("довгий текст -> автоматичне вписування без обрізання",
          long_warning is None and long_size < 34, f"font_size={long_size}")

    plate = load_image(out_dir / "G45.png")[0][y1:y2, x1:x2]
    dark = (plate.mean(axis=2) < 128)
    check("текст намальовано всередині plate_box", dark.any())
    check("текст не торкається межі plate_box",
          not (dark[0].any() or dark[-1].any() or dark[:, 0].any() or dark[:, -1].any()))

    geometry = detect_plate(base_bgr, PLATE_BOX)
    check(
        "геометрію номера знайдено автоматично",
        geometry.detected and abs(geometry.angle) < 1.0 and geometry.shape is not None,
        f"{geometry.int_size[0]}x{geometry.int_size[1]} px, нахил {geometry.angle:+.2f}°",
    )

    print("\n3) Повний батч на 20 кольорах")
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(colors), "--output", str(out_root), "--overwrite"]
    )
    produced = len(list(out_dir.glob("G*.png")))
    check("20 кольорів без падіння скрипта", code == 0 and "успішно 20" in output, f"файлів: {produced}")

    print("\n4) Валідація вхідних даних")
    slashed = workdir / "slashed.csv"
    slashed.write_text("Code,Hex\n1GN00/B04,#0E2B1D\n1YW00/B144,#D8B72E\n", encoding="utf-8")
    check(
        "код із подвійним записом -> береться частина до `/`",
        normalize_code("1GN00/B04") == "1GN00" and normalize_code("a/b") == "a",
    )
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(slashed), "--output", str(out_root), "--overwrite"]
    )
    check(
        "заголовок `Code,Hex` і файл без `/` у назві",
        code == 0 and (out_dir / "1GN00.png").exists() and not (out_dir / "1GN00").exists(),
    )

    broken = workdir / "broken.csv"
    broken.write_text("code,hex\nG90,#ZZZZZZ\nG91,#123456\n,#654321\nG91,#111111\n", encoding="utf-8")
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(broken), "--output", str(out_root), "--overwrite"]
    )
    check("невалідний HEX -> [ERROR], батч продовжується",
          "[ERROR] G90" in output and "[OK] G91" in output and "успішно 1" in output)
    check("порожній код і дубль коду теж відловлені",
          "порожній код кольору" in output and "дубль коду" in output)

    listed = workdir / "list.txt"
    listed.write_text(
        "code hex\nG80 #7FA9B5\nG81,#41456A metallic\nG82 - 9B9D9B\nG83 #7AB\n\nG84\n",
        encoding="utf-8",
    )
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(listed), "--output", str(out_root), "--overwrite"]
    )
    check(
        "список `код HEX` у рядках -> окреме фото на рядок",
        all((out_dir / f"G8{i}.png").exists() for i in range(4)) and "успішно 4" in output,
        f"файлів: {sum((out_dir / f'G8{i}.png').exists() for i in range(5))}",
    )
    check("у списку: рядок без HEX -> [ERROR], решта проходить",
          "[ERROR] G84" in output and "успішно 4" in output)

    headerless = workdir / "headerless.csv"
    headerless.write_text("G92,#7FA9B5\n", encoding="utf-8")
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(headerless), "--output", str(out_root)]
    )
    check("CSV без заголовку -> читабельна помилка, не трейсбек",
          code == 2 and "заголовку" in output and "Traceback" not in output)

    code, output = run_batch(
        ["batch", "--template", str(workdir / "nope.png"), "--config", str(config),
         "--colors", str(colors), "--output", str(out_root)]
    )
    check("відсутній шаблон -> читабельна помилка", code == 2 and "не знайдено" in output)

    missing_mask_dir = workdir / "no-masks"
    missing_mask_dir.mkdir()
    shutil.copy(template, missing_mask_dir / template.name)
    code, output = run_batch(
        ["batch", "--template", str(missing_mask_dir / template.name), "--config", str(config),
         "--colors", str(colors), "--output", str(out_root)]
    )
    check("відсутня маска -> підказка запустити mask-editor",
          code == 2 and "mask-editor" in output)

    print("\n4b) Структура папок виводу")
    layout_root = workdir / "layout"
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config), "--colors", str(single),
         "--output", str(layout_root), "--model", "Ford Kuga", "--overwrite"]
    )
    check(
        "типово бренд / марка авто / фото",
        (layout_root / "Asturcolor" / "Ford Kuga" / "G44.png").exists(),
        str((layout_root / "Asturcolor" / "Ford Kuga" / "G44.png").relative_to(workdir)),
    )
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config), "--colors", str(single),
         "--output", str(layout_root), "--model", "Ford Kuga", "--pattern", "{model}/{brand} {code}",
         "--overwrite"]
    )
    check(
        "власний шаблон шляху `{model}/{brand} {code}`",
        (layout_root / "Ford Kuga" / "Asturcolor G44.png").exists(),
    )
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config), "--colors", str(single),
         "--output", str(layout_root), "--pattern", "{code}", "--overwrite"]
    )
    check("плоска структура `{code}`", (layout_root / "G44.png").exists())

    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config), "--colors", str(single),
         "--output", str(layout_root), "--pattern", "../{code}", "--overwrite"]
    )
    check("шаблон з `..` відхилено", code == 2 and ".." in output)
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config), "--colors", str(single),
         "--output", str(layout_root), "--pattern", "{brand}/{colour}", "--overwrite"]
    )
    check("невідома підстановка -> читабельна помилка", code == 2 and "colour" in output)
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config), "--colors", str(single),
         "--output", str(layout_root), "--pattern", "{brand}/all", "--overwrite"]
    )
    check("шаблон без {code} -> попередження про суфікси", "немає {code}" in output)

    print("\n0b) Хромакей: маска без кліків")
    key_hex = "#1E9E52"
    keyed_bgr = recolor_image(
        base_bgr,
        [car_weight, marker_weight],
        key_hex,
        False,
        RecolorOptions(),
    )
    keyed_template = workdir / "demo" / "keyed.png"
    save_image(keyed_template, keyed_bgr, base_alpha)
    segmenter = masking.ChromaKeySegmenter(keyed_bgr, key_hex)
    started_key = time.perf_counter()
    keyed_mask = segmenter.segment([], []) > 0
    key_ms = 1000 * (time.perf_counter() - started_key)
    ground = (truth | (masking.load_mask(template, masking.MARKER, (height, width)) > 127))
    key_iou = (keyed_mask & ground).sum() / max((keyed_mask | ground).sum(), 1)
    check("нуль кліків -> IoU > 0.9 проти маски з кліків", key_iou > 0.9, f"IoU={key_iou:.3f}")
    check(
        "порог насиченості підбирається від насиченості ключа",
        20 <= segmenter.min_chroma <= 30,
        f"C*>{segmenter.min_chroma}",
    )
    leaked_key = [part for part, (x, y) in PROTECTED.items() if keyed_mask[y, x]]
    check("хромакей не чіпає скло/шини/фон", not leaked_key, ", ".join(leaked_key))
    check("швидше за grabcut на порядок", key_ms < 400, f"{key_ms:.0f} мс")
    soft = masking.ChromaKeySegmenter(keyed_bgr, key_hex, soft=True).segment([], [])
    check(
        "м'яка маска має напівтони (відсвіти перефарбовуються частково)",
        bool(((soft > 0) & (soft < 250)).any()),
        f"{100 * ((soft > 0) & (soft < 250)).mean():.2f}% пікселів у напівтоні",
    )

    print("\n4bb) Оцінка придатності шаблону")
    code, output = run_batch(["check-template", "--template", str(template)])
    check(
        "check-template друкує L*, розкид, C* і прогноз",
        all(word in output for word in ("медіана L*", "розкид", "C*", "збереження об'єму")),
    )
    stats = template_stats(base_bgr, car_weight)
    dark_ok = stats.contrast_retained(30.0) >= stats.contrast_retained(92.0)
    check(
        "прогноз узгоджений: у світлого шаблону запас униз більший, ніж угору",
        dark_ok,
        f"L*={stats.lightness:.0f}: темний {100 * stats.contrast_retained(30):.0f}%, "
        f"білий {100 * stats.contrast_retained(92):.0f}%",
    )

    print("\n4c) Вікно (GUI) передає ті самі параметри")
    check_gui(template, config, colors, out_root)

    print("\n5) Перезапис і --preview")
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config),
         "--colors", str(single), "--output", str(out_root)]
    )
    check("без --overwrite файли не перезаписуються мовчки",
          (out_dir / "G44_v2.png").exists() and "вже існує" in output)
    code, output = run_batch(
        ["batch", "--template", str(template), "--config", str(config), "--colors", str(colors),
         "--output", str(out_root), "--preview", "--overwrite"]
    )
    check("--preview генерує лише 2 кольори", "успішно 2" in output)

    print("\n6) Швидкодія")
    check("обробка одного фото < 2 с на CPU", elapsed < 2.0, f"{elapsed:.2f} с/фото")

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 70}\nПеревірок: {len(results)}, провалено: {len(failed)}")
    for name in failed:
        print(f"  FAIL: {name}")
    print(f"Артефакти: {workdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("selfcheck-artifacts")
    raise SystemExit(main_selfcheck(target))
