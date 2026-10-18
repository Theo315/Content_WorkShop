"""CLI: `mask-editor` (once per template) and `batch` (the actual production run)."""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
import time

import numpy as np

from . import __version__, masking
from .colors import ColorSpec, parse_colors
from .config import TemplateConfig, load_template_config
from .errors import UserError, require_file
from .image_io import load_image, save_image
from .output_path import DEFAULT_PATTERN, resolve_output_path, validate_pattern
from .plate_text import (
    PlateGeometry,
    detect_plate,
    draw_plate_text,
    plate_guard_mask,
    sample_plate_background,
)
from .sanitize import collect_pngs, strip_files
from .timestamps import (
    DEFAULT_STEP,
    parse_start,
    parse_step_range,
    stamp_files,
    supports_creation_time,
)
from .recolor import (
    KEEP,
    MATCH,
    RecolorOptions,
    color_accuracy,
    combine_weights,
    prepare_weight,
    recolor_image,
    template_stats,
)

DELTA_E_LIMIT = 5.0  # acceptance criterion 1


class Log:
    """Console + `log.txt` in the output folder (ТЗ 4.5)."""

    def __init__(self, log_path: pathlib.Path | None = None):
        self.handle = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = log_path.open("a", encoding="utf-8")
            self.handle.write(f"\n===== запуск {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")

    def __call__(self, message: str) -> None:
        print(message)
        if self.handle:
            self.handle.write(message + "\n")
            self.handle.flush()

    def close(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None


def default_plate_margin(shape: tuple[int, int]) -> int:
    """~12 px on a 2048 px card — enough to cover the plate frame."""
    return max(4, round(0.006 * max(shape)))


def parse_points(raw: str | None, what: str) -> list[tuple[int, int]]:
    """`"900,740 1200,720"` (space or `;` separated) -> [(900, 740), (1200, 720)]."""
    if not raw:
        return []
    points = []
    for chunk in raw.replace(";", " ").replace("|", " ").split():
        try:
            x, y = (int(v) for v in chunk.split(","))
        except ValueError:
            raise UserError(f"{what}: очікується формат `x,y`, отримано {chunk!r}") from None
        points.append((x, y))
    return points


def parse_strokes(raw: str | None, what: str) -> list[list[tuple[int, int]]]:
    """`"x,y x,y | x,y x,y"` -> two polylines. `|` (or `;`) separates strokes."""
    if not raw:
        return []
    strokes = []
    for chunk in raw.replace(";", "|").split("|"):
        points = parse_points(chunk, what)
        if points:
            strokes.append(points)
    return strokes


def cmd_mask_editor(args) -> int:
    template = require_file(args.template, "Шаблон")

    key_color = args.key_color
    if key_color is None and args.config:  # take it from the template config if present
        key_color = load_template_config(args.config).key_color

    if args.clicks or (key_color and args.auto):
        if not args.only:
            raise UserError(
                "для неінтерактивного режиму вкажіть `--only car` або `--only marker`"
            )
        path, mask = masking.build_mask_headless(
            template,
            args.only,
            parse_points(args.clicks, "--clicks"),
            parse_points(args.neg_clicks, "--neg-clicks"),
            backend=args.backend,
            sam_checkpoint=args.sam_checkpoint,
            sam_model_type=args.sam_model_type,
            dilate=args.dilate,
            fill_holes=args.fill_holes,
            erase_strokes=parse_strokes(args.erase, "--erase"),
            paint_strokes=parse_strokes(args.paint, "--paint"),
            brush_radius=args.brush_radius,
            key_color=key_color,
            soft=args.soft,
        )
        print(f"[{args.only}] маску збережено: {path}  (площа {100 * (mask > 127).mean():.1f}%)")
        return 0

    stages = (args.only,) if args.only else masking.STAGES
    saved = masking.run_mask_editor(
        template,
        stages=stages,
        backend=args.backend,
        sam_checkpoint=args.sam_checkpoint,
        sam_model_type=args.sam_model_type,
        overwrite=args.overwrite,
        key_color=key_color,
        soft=args.soft,
    )
    print("\nГотово. Маски:")
    for kind, path in saved.items():
        print(f"  {kind}: {path}")
    return 0


GOOD_LIGHTNESS = (45.0, 68.0)  # median L where a template covers the whole colour range
GOOD_CHROMA = 15.0  # below this the body is as neutral as glass/tyres/chrome
GOOD_RETENTION = 0.65


def cmd_check_template(args) -> int:
    """Score a template before anyone invests time in masks and a full batch."""
    template_path = require_file(args.template, "Шаблон")
    image, _ = load_image(template_path, "Шаблон")
    height, width = image.shape[:2]
    car_mask = masking.load_mask(template_path, masking.CAR, (height, width))
    weight = prepare_weight(car_mask, 1.2)
    stats = template_stats(image, weight)

    print(f"Шаблон: {template_path.name} ({width}x{height})")
    print(f"  яскравість кузова (медіана L*): {stats.lightness:5.1f}   [добре: {GOOD_LIGHTNESS[0]:.0f}-{GOOD_LIGHTNESS[1]:.0f}]")
    print(f"  розкид світлотіні (p2..p98):    {stats.spread:5.1f}   [добре: > 30]")
    print(f"  насиченість кузова (C*):        {stats.chroma:5.1f}   [добре: > {GOOD_CHROMA:.0f}]")
    print("\n  Прогноз збереження об'єму для цільових кольорів:")
    probes = (("майже білий", 92.0), ("світлий", 80.0), ("середній", 58.0), ("темний", 30.0), ("майже чорний", 12.0))
    weak = []
    for label, target_l in probes:
        retained = stats.contrast_retained(target_l)
        mark = "ok" if retained >= GOOD_RETENTION else "ПЛАСКО"
        print(f"    L*={target_l:4.0f}  {label:13s} {100 * retained:3.0f}%  {mark}")
        if retained < GOOD_RETENTION:
            weak.append(label)

    problems = []
    if not GOOD_LIGHTNESS[0] <= stats.lightness <= GOOD_LIGHTNESS[1]:
        problems.append(
            f"кузов {'занадто темний' if stats.lightness < GOOD_LIGHTNESS[0] else 'занадто світлий'} "
            f"(L*={stats.lightness:.0f})"
        )
    if stats.chroma < GOOD_CHROMA:
        problems.append(
            f"кузов майже нейтральний (C*={stats.chroma:.0f}) — за кольором він майже не "
            "відрізняється від скла, шин, хрому й фону, тому маску доведеться домальовувати пензлем"
        )
    if stats.spread < 30:
        problems.append(f"мало світлотіні (розкид {stats.spread:.0f}) — результат виглядатиме пласким")

    print()
    if problems:
        print("  Слабкі місця:")
        for item in problems:
            print(f"    - {item}")
    if weak:
        print(f"  Погано підходить для: {', '.join(weak)}")
    if not problems and not weak:
        print("  Шаблон придатний для всього діапазону кольорів.")
    else:
        print(
            "\n  Рекомендація: генеруйте базовий шаблон у СЕРЕДНІЙ за яскравістю НАСИЧЕНИЙ колір\n"
            "  (L* 50-60, C* 20+) — напр. середній синій/бірюзовий/зелений. Такий кузов\n"
            "  кольорово відрізняється від скла й шин (легка маска) і має запас яскравості\n"
            "  і вгору, і вниз (об'єм зберігається на світлих і на темних кодах)."
        )
    return 0 if not problems else 1


def cmd_sanitize(args) -> int:
    """Strip metadata from already-generated photos."""
    from .sanitize import inspect

    files = collect_pngs(args.path, recursive=not args.no_recursive)
    if not files:
        print(f"PNG не знайдено: {args.path}")
        return 1

    if args.dry_run:
        dirty = [(path, inspect(path)) for path in files]
        dirty = [(path, found) for path, found in dirty if found]
        for path, found in dirty:
            details = ", ".join(f"{kind} ({size} B)" for kind, size in found)
            print(f"{path}: {details}")
        print(f"Перевірено {len(files)}, з метаданими {len(dirty)} (нічого не змінено)")
        return 0

    cleaned = strip_files(files)
    for path, kinds in cleaned:
        print(f"{path}: прибрано {', '.join(kinds)}")
    print(f"Перевірено {len(files)}, очищено {len(cleaned)}")
    return 0


def cmd_batch(args) -> int:
    template_path = require_file(args.template, "Шаблон")
    # no config at all = no number plate: recolour only, which is all a template
    # without a plate ever needs
    config = load_template_config(args.config) if args.config else TemplateConfig()
    if args.brand:
        config = dataclasses.replace(config, brand_name=args.brand.strip())
    colors = parse_colors(args.colors)

    image, alpha = load_image(template_path, "Шаблон")
    height, width = image.shape[:2]
    config.validate_against(width, height)

    car_mask = masking.load_mask(template_path, masking.CAR, (height, width))
    marker_mask = masking.load_mask(template_path, masking.MARKER, (height, width))

    options = RecolorOptions(lightness_mode=args.lightness, feather=args.feather)
    car_weight = prepare_weight(car_mask, options.feather)
    marker_weight = prepare_weight(marker_mask, options.feather)

    plate = None
    if config.plate_box is not None:
        plate = (
            PlateGeometry.from_box(config.plate_box)
            if args.no_plate_detect
            else detect_plate(image, config.plate_box, key_color=config.key_color)
        )

    # The plate is repainted with text anyway — never let the body colour touch it
    # (its frame sits right outside the plate and would otherwise turn body-coloured).
    # A chroma-key mask needs no such guard: the key colour already says what is paint,
    # and excluding the area would leave a ring of the template colour around the plate.
    keyed = bool(config.key_color) and args.plate_margin is None
    margin = 0 if keyed else (
        args.plate_margin if args.plate_margin is not None else default_plate_margin((height, width))
    )
    plate_guarded = 0.0
    if plate is not None and not keyed:
        guard = plate_guard_mask((height, width), plate, margin) > 0
        plate_guarded = float(
            (np.maximum(car_weight, marker_weight)[guard] > 0.01).mean() if guard.any() else 0.0
        )
        car_weight[guard] = 0.0
        marker_weight[guard] = 0.0

    # CLI wins over the per-template config, which wins over the file name
    model_name = args.model or config.model_name or template_path.stem
    stamp_start = parse_start(args.time_start) if args.time_start else None
    stamp_range = parse_step_range(args.time_step) if stamp_start else None

    pattern = validate_pattern(args.pattern)
    if "{brand}" in pattern and not config.brand_name:
        raise UserError(
            "у шаблоні шляху є {brand}, але бренд невідомий — "
            "додайте --brand НАЗВА або --config із `brand_name`"
        )
    root = pathlib.Path(args.output).expanduser()

    sample_path, _ = resolve_output_path(
        root,
        pattern,
        brand=config.brand_name,
        model=model_name,
        code=colors[0].code if colors else "CODE",
        hex_color=colors[0].hex if colors else "",
        template=template_path.stem,
        overwrite=True,
    )
    log_dir = sample_path.parent
    log_dir.mkdir(parents=True, exist_ok=True)

    font_path = config.resolved_font_path() if plate is not None else None
    plate_bg = (
        (config.plate_bg_color or sample_plate_background(image, plate, config.key_color))
        if plate is not None
        else (255, 255, 255)
    )

    selected = colors[:2] if args.preview else colors
    written: list[pathlib.Path] = []
    log = Log(log_dir / "log.txt")
    started = time.perf_counter()
    ok_count = 0
    errors: list[tuple[str, str]] = []
    warnings: list[str] = []

    try:
        log(f"HexPh {__version__} | шаблон: {template_path.name} ({width}x{height})")
        log(f"бренд: {config.brand_name or '—'} | марка авто: {model_name}")
        log(f"структура: {root}\\{pattern}.png")
        log(f"приклад:   {sample_path}")
        log(
            f"кольорів у списку: {len(colors)}"
            + (f" (режим --preview: беру перші {len(selected)})" if args.preview else "")
        )
        if "{code}" not in pattern and len(selected) > 1:
            log(
                "[WARN] у шаблоні шляху немає {code} — усі кольори пишуться в одне ім'я, "
                "додаватимуться суфікси _v2, _v3…"
            )
        if plate is None:
            log("режим яскравості: " + options.lightness_mode)
            log(
                "номер: не задано — текст не наноситься"
                + (" (конфіг не вказано)" if not args.config else " (у конфігу немає `plate_box`)")
            )
        else:
            log(
                f"режим яскравості: {options.lightness_mode} | "
                f"шрифт: {font_path or 'вбудований PIL'}"
            )
            log(
                f"номер: {'знайдено' if plate.detected else 'із конфігу'} "
                f"{plate.int_size[0]}x{plate.int_size[1]} px, нахил {plate.angle:+.1f}° | "
                f"фон {'з конфігу' if config.plate_bg_color else 'авто'} RGB{plate_bg} | "
                + (
                    "захист не потрібен (маска за ключовим кольором)"
                    if keyed
                    else f"захист +{margin} px"
                )
            )
        if config.key_color:
            log(f"ключовий колір шаблону: {config.key_color}")
        if plate is not None and config.font_path is None:
            log("[WARN] у конфігу немає `font_path` — використано системний шрифт")
        if plate_guarded > 0.05:
            log(
                f"[WARN] маска накривала {100 * plate_guarded:.0f}% зони номера — "
                "її вилучено із зони перефарбування"
            )

        for spec in selected:
            if not spec.ok:
                log(f"[ERROR] {spec.code} -> {spec.error}")
                errors.append((spec.code, spec.error or "невідома помилка"))
                continue
            try:
                path, note, warning = _render_one(
                    spec, image, alpha, [car_weight, marker_weight], car_weight, config,
                    plate, font_path, plate_bg, options, args,
                    root=root, pattern=pattern, model=model_name, template=template_path.stem,
                )
            except UserError as exc:
                log(f"[ERROR] {spec.code} -> {exc}")
                errors.append((spec.code, str(exc)))
                continue
            except Exception as exc:  # keep the batch alive (ТЗ 4.3 / 4.5)
                log(f"[ERROR] {spec.code} -> неочікувана помилка: {type(exc).__name__}: {exc}")
                errors.append((spec.code, f"{type(exc).__name__}: {exc}"))
                continue

            ok_count += 1
            written.append(path)
            log(f"[OK] {spec.code} -> {path}{note}")
            if warning:
                warnings.append(f"{spec.code}: {warning}")
                log(f"[WARN] {spec.code} -> {warning}")

        if written and not args.no_sanitize:
            cleaned = strip_files(written)
            if cleaned:
                kinds = sorted({kind for _, kinds in cleaned for kind in kinds})
                log(
                    f"очистка метаданих: {len(cleaned)} з {len(written)} файлів, "
                    f"прибрано {', '.join(kinds)}"
                )
            else:
                log(f"очистка метаданих: {len(written)} файлів, зайвого не було")

        if stamp_start is not None and written:
            stamped = stamp_files(written, stamp_start, args.time_step, args.time_seed)
            fields = (
                "дата створення + дата змінення"
                if supports_creation_time()
                else "дата змінення (дата створення — лише на Windows)"
            )
            log(
                f"час файлів ({fields}): {len(stamped)} шт, "
                f"з {stamped[0][1]:%Y-%m-%d %H:%M:%S} до {stamped[-1][1]:%Y-%m-%d %H:%M:%S} "
                f"(крок {stamp_range[0]:g}-{stamp_range[1]:g} хв)"
            )

        elapsed = time.perf_counter() - started
        per_image = elapsed / max(ok_count, 1)
        log("")
        log(f"Зведення: успішно {ok_count}, з помилками {len(errors)}, з попередженнями {len(warnings)}")
        log(f"Час: {elapsed:.1f} с загалом, {per_image:.2f} с на фото")
        if errors:
            log("Помилки:")
            for code, reason in errors:
                log(f"  {code}: {reason}")
        log(f"Лог: {log_dir / 'log.txt'}")
    finally:
        log.close()

    return 0 if not errors else 1


def _render_one(
    spec: ColorSpec,
    image: np.ndarray,
    alpha: np.ndarray | None,
    paint_weights: list[np.ndarray],
    car_weight: np.ndarray,
    config: TemplateConfig,
    plate: PlateGeometry | None,
    font_path: str | None,
    plate_bg: tuple[int, int, int],
    options: RecolorOptions,
    args,
    *,
    root: pathlib.Path,
    pattern: str,
    model: str,
    template: str,
) -> tuple[pathlib.Path, str, str | None]:
    """Recolour + plate text + save for one colour row."""
    recolored = recolor_image(image, paint_weights, spec.hex, spec.metallic, options)
    warning = None
    if plate is not None:
        recolored, warning = draw_plate_text(
            recolored,
            plate,
            f"{config.brand_name} {spec.code}".strip(),
            font_path,
            config.font_size,
            config.text_color,
            plate_bg,
        )

    path, note = resolve_output_path(
        root,
        pattern,
        brand=config.brand_name,
        model=model,
        code=spec.code,
        hex_color=spec.hex,
        template=template,
        overwrite=args.overwrite,
    )
    save_image(path, recolored, alpha)

    extras = []
    if note:
        extras.append(note)
    if spec.metallic:
        extras.append("metallic: потребує візуальної перевірки")
    delta_e = color_accuracy(recolored, car_weight, spec.hex)
    extras.append(f"ΔE={delta_e:.2f}")
    if delta_e >= DELTA_E_LIMIT:
        warning = (warning + "; " if warning else "") + (
            f"ΔE={delta_e:.2f} ≥ {DELTA_E_LIMIT:g}"
            + (" (очікувано для --lightness keep)" if options.lightness_mode == KEEP else "")
        )
    return path, f"  [{', '.join(extras)}]", warning


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recolor.py",
        description="Батч-рекольоринг авто для маркетплейс-фото (кузов + маркер + номер).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"HexPh {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    editor = sub.add_parser("mask-editor", help="інтерактивно побудувати маски для шаблону")
    editor.add_argument("--template", required=True, help="PNG шаблону")
    editor.add_argument("--only", choices=masking.STAGES, help="будувати лише одну маску")
    editor.add_argument(
        "--backend",
        choices=(
            masking.GRABCUT,
            masking.CHROMA,
            masking.FLOOD_GRADIENT,
            masking.FLOOD_FIXED,
            masking.SAM,
        ),
        help="бекенд сегментації (типово grabcut; chroma — якщо є key_color; "
        "sam — якщо вказано чекпоінт). У редакторі перемикається клавішею 't'",
    )
    editor.add_argument(
        "--key-color",
        help=f"ключовий колір кузова шаблону, напр. {masking.KEY_COLOR_HINT} — маска "
        "будується за відтінком без кліків (інакше береться з `key_color` у конфігу)",
    )
    editor.add_argument("--config", help="template_config.json — щоб узяти з нього `key_color`")
    editor.add_argument(
        "--auto",
        action="store_true",
        help="не відкривати вікно: побудувати маску за ключовим кольором і зберегти",
    )
    editor.add_argument(
        "--soft",
        action="store_true",
        help="зберегти напівпрозору маску (відблиски й відсвіти на підлозі теж "
        "перефарбовуватимуться, пропорційно до частки ключового кольору)",
    )
    editor.add_argument("--sam-checkpoint", help="шлях до чекпоінта SAM")
    editor.add_argument("--sam-model-type", choices=("vit_h", "vit_l", "vit_b"))
    editor.add_argument("--overwrite", action="store_true", help="не питати про перезапис масок")
    editor.add_argument(
        "--clicks",
        help="неінтерактивно: позитивні точки `x,y x,y ...` (потрібен --only). "
        "Без цього прапорця відкривається вікно редактора",
    )
    editor.add_argument("--neg-clicks", help="неінтерактивно: негативні точки `x,y x,y ...`")
    editor.add_argument(
        "--dilate", type=int, default=0, help="розширити (+) або звузити (-) готову маску, px"
    )
    editor.add_argument("--fill-holes", action="store_true", help="залити діри в масці")
    editor.add_argument(
        "--erase",
        help="неінтерактивний пензель-гумка: мазки `x,y x,y | x,y x,y` (`|` розділяє мазки)",
    )
    editor.add_argument("--paint", help="неінтерактивний пензель: домалювати мазки, той самий формат")
    editor.add_argument(
        "--brush-radius", type=int, help="радіус пензля в px (типово 1.2%% від сторони)"
    )
    editor.set_defaults(func=cmd_mask_editor)

    checker = sub.add_parser(
        "check-template", help="оцінити, чи придатний шаблон як база (потрібна маска кузова)"
    )
    checker.add_argument("--template", required=True, help="PNG шаблону")
    checker.set_defaults(func=cmd_check_template)

    cleaner = sub.add_parser(
        "sanitize", help="прибрати метадані з готових PNG (файл або тека)"
    )
    cleaner.add_argument("--path", required=True, help="PNG або тека з PNG")
    cleaner.add_argument(
        "--no-recursive", action="store_true", help="не заходити у вкладені теки"
    )
    cleaner.add_argument(
        "--dry-run", action="store_true", help="лише показати, що буде прибрано"
    )
    cleaner.set_defaults(func=cmd_sanitize)

    batch = sub.add_parser("batch", help="згенерувати фото для всіх кольорів зі списку")
    batch.add_argument("--template", required=True, help="PNG шаблону")
    batch.add_argument(
        "--config",
        help="template_config.json; без нього номер не наноситься, лише перефарбування",
    )
    batch.add_argument("--brand", help="бренд для тексту номера і шляху {brand} (замість конфігу)")
    batch.add_argument(
        "--no-sanitize",
        action="store_true",
        help="не чистити метадані у згенерованих файлах (типово чистяться)",
    )
    batch.add_argument(
        "--time-start",
        help="початкова дата створення і зміни файлів, напр. \"2026-09-11 09:00\" "
        "(без неї час файлів не чіпається)",
    )
    batch.add_argument(
        "--time-step",
        default=DEFAULT_STEP,
        help="крок між фото у хвилинах: діапазон `6-8` (випадково) або одне число",
    )
    batch.add_argument(
        "--time-seed", type=int, help="зерно генератора кроку — для відтворюваного результату"
    )
    batch.add_argument(
        "--colors",
        required=True,
        help="джерело кольорів: `.csv` із заголовком `code,hex[,metallic]` або будь-який "
        "текстовий файл-список, де кожен рядок — окреме фото: `1KGA #7FA9B5`",
    )
    batch.add_argument("--output", required=True, help="коренева папка виводу")
    batch.add_argument(
        "--model",
        help="марка/модель авто для папки (типово — `model_name` з конфігу, "
        "інакше ім'я файлу шаблону)",
    )
    batch.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help="структура шляху під --output. Підстановки: {brand} {model} {code} {hex} "
        "{template}; `/` — рівень папки. Приклади: `{brand}/{model}/{code}`, "
        "`{model}/{brand} {code}`, `{code}`",
    )
    batch.add_argument("--overwrite", action="store_true", help="форс перезапис існуючих файлів")
    batch.add_argument("--preview", action="store_true", help="тільки перші 2 кольори")
    batch.add_argument(
        "--lightness",
        choices=(MATCH, KEEP),
        default=MATCH,
        help="match — підганяє яскравість під HEX (ΔE<5, тіні зберігаються); "
        "keep — канал L взагалі не торкати (буквально ТЗ 4.2)",
    )
    batch.add_argument(
        "--feather", type=float, default=1.2, help="згладжування краю маски, px (0 = різкий край)"
    )
    batch.add_argument(
        "--plate-margin",
        type=int,
        default=None,
        help="на скільки px розширити зону номера, яку заборонено перефарбовувати "
        "(типово 0.6%% від довшої сторони)",
    )
    batch.add_argument(
        "--no-plate-detect",
        action="store_true",
        help="не шукати справжню геометрію номера, брати plate_box як є (без нахилу)",
    )
    batch.set_defaults(func=cmd_batch)

    return parser


def _tolerate_console_encoding() -> None:
    """Legacy Windows consoles (cp1251/cp866) cannot encode `Δ` — don't die on it.
    Keeps the console codepage (so Cyrillic stays readable); log.txt is always UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):  # pragma: no cover
            pass


def main(argv: list[str] | None = None) -> int:
    _tolerate_console_encoding()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UserError as exc:
        print(f"\nПомилка: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПерервано користувачем.", file=sys.stderr)
        return 130
