"""HTTP adapter around the original HexPh algorithms; no desktop dependencies."""
from dataclasses import asdict
import io
import json
import tempfile
from time import perf_counter
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from vendor.hexph.hexph.colors import parse_colors_list, parse_colors_csv, normalize_hex, normalize_code
from vendor.hexph.hexph.config import TemplateConfig
from vendor.hexph.hexph.errors import UserError
from vendor.hexph.hexph.masking import ChromaKeySegmenter, FloodSegmenter, GrabCutSegmenter
from vendor.hexph.hexph.recolor import RecolorOptions, prepare_weight, recolor_image, color_accuracy
from vendor.hexph.hexph.plate_text import detect_plate, draw_plate_text, sample_plate_background, plate_guard_mask
from .settings import ASSETS, MAX_PIXELS, MAX_ITEMS
from . import storage, preferences

cv2.setNumThreads(1)
TEMPLATES = {
    "kuga": {"stem": "kuga_575", "name": "Ford Kuga", "plate_box": [843, 1020, 1064, 1075], "font_size": 44},
    "kuga-key": {"stem": "keytpl_575", "name": "Ford Kuga · chroma", "plate_box": [843, 1020, 1064, 1075], "font_size": 44, "key_color": "#1E9E52"},
    "kodiaq": {"stem": "kodiaq_c9x", "name": "Škoda Kodiaq", "plate_box": [801, 1030, 1004, 1100], "font_size": 38},
    "demo": {"stem": "asturcolor_demo", "name": "Демо-шаблон", "plate_box": [700, 990, 1150, 1040], "font_size": 34},
}


def template_paths(template_id):
    if template_id not in TEMPLATES:
        raise UserError("Невідомий шаблон.")
    stem = TEMPLATES[template_id]["stem"]
    return [ASSETS / "templates" / f"{stem}{suffix}.png" for suffix in ("", "_car_mask", "_marker_mask")]


def template_config(template_id, saved=None):
    saved = preferences.load() if saved is None else saved
    if template_id not in TEMPLATES:
        return dict(preferences.car(template_id, saved)["config"])
    if template_id in saved.get("hidden_builtin_templates", []):
        from fastapi import HTTPException
        raise HTTPException(404, "Шаблон видалено або не знайдено.")
    value = TEMPLATES[template_id]
    return dict(brand_name="Asturcolor", model_name=value["name"], plate_box=value["plate_box"],
                font_size=value["font_size"], key_color=saved.get("builtin_keys", {}).get(template_id, value.get("key_color")), text_color=[20, 20, 20], plate_bg_color="auto")


def catalog():
    saved = preferences.load()
    out = []
    for key, entry in TEMPLATES.items():
        if key in saved.get("hidden_builtin_templates", []):
            continue
        path, car, marker = template_paths(key)
        with Image.open(path) as image:
            width, height = image.size
        out.append(dict(id=key, name=entry["name"], builtin=True, width=width, height=height, image=storage.asset_ref(path),
                        car_mask=storage.asset_ref(car), marker_mask=storage.asset_ref(marker), config=template_config(key, saved)))
    out.extend(preferences.present_car(key, entry) for key, entry in saved.get("cars", {}).items())
    return {"templates": out, "deleted_templates": [dict(id=key, name=TEMPLATES[key]["name"])
        for key in saved.get("hidden_builtin_templates", []) if key in TEMPLATES]}


def image_from_bytes(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.width * image.height > MAX_PIXELS:
                raise UserError(f"Потрібен PNG до {MAX_PIXELS // 1_000_000} мегапікселів.")
            if image.mode not in ("RGB", "RGBA", "L", "LA", "P", "1"):
                raise UserError("Потрібен 8-бітний PNG (RGB/RGBA), не 16-бітний.")
            image.verify()
        raw = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise UserError("Пошкоджений або непідтримуваний PNG.") from None
    if raw is None:
        raise UserError("Не вдалося декодувати PNG.")
    if raw.ndim == 2:
        return cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR), None
    return np.ascontiguousarray(raw[:, :, :3]), np.ascontiguousarray(raw[:, :, 3]) if raw.shape[2] == 4 else None


def mask_from_bytes(data, shape):
    image, alpha = image_from_bytes(data)
    if image.shape[:2] != shape:
        raise UserError("Розміри маски повинні збігатися з шаблоном.")
    mask = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if alpha is not None:
        mask = np.rint(mask.astype(np.float32) * alpha / 255).astype(np.uint8)
    return mask


def encode(image, alpha=None):
    if alpha is not None:
        image = np.dstack((image, alpha))
    ok, result = cv2.imencode(".png", image)
    if not ok:
        raise UserError("Не вдалося зберегти PNG.")
    return result.tobytes()


def parse_colors(text, format="list"):
    if format == "csv":
        with tempfile.TemporaryDirectory(prefix="hex-colors-") as folder:
            path = Path(folder) / "colors.csv"
            path.write_text(text, encoding="utf-8")
            colors = parse_colors_csv(path)
    else:
        colors = parse_colors_list(text)
    if len(colors) > MAX_ITEMS:
        raise UserError(f"Максимум {MAX_ITEMS} кольорів за пакет.")
    return {"colors": [asdict(spec) for spec in colors]}


def template_data(template_id=None, template_token=None, saved=None):
    if template_token:
        return storage.read_bytes(template_token)
    if template_id:
        if template_id not in TEMPLATES:
            entry = preferences.car(template_id, saved)
            return storage.read_bytes(storage.make_ref(**entry["image"])["token"])
        saved = preferences.load() if saved is None else saved
        if template_id in saved.get("hidden_builtin_templates", []):
            from fastapi import HTTPException
            raise HTTPException(404, "Шаблон видалено або не знайдено.")
        return template_paths(template_id)[0].read_bytes()
    raise UserError("Виберіть або завантажте PNG-шаблон.")


def render(request, *, source_data=None, output_store=None, timings=None):
    started = perf_counter()
    saved = preferences.load()
    image, alpha = image_from_bytes(source_data if source_data is not None
                                   else template_data(request.template_id, request.template_token, saved))
    height, width = image.shape[:2]
    custom = bool(request.template_token)
    raw_config = (template_config(request.template_id, saved) if request.template_id and not custom else {})
    if request.config is not None:
        raw_config.update(request.config.model_dump(exclude_unset=True))
    config = TemplateConfig(
        brand_name=raw_config.get("brand_name", ""), model_name=raw_config.get("model_name"),
        plate_box=tuple(raw_config["plate_box"]) if raw_config.get("plate_box") else None,
        key_color=normalize_hex(raw_config["key_color"]) if raw_config.get("key_color") else None,
        font_path=str(ASSETS / "fonts/NotoSans-Bold.ttf"), font_size=raw_config.get("font_size", 34),
        text_color=tuple(raw_config.get("text_color", [20, 20, 20])),
        plate_bg_color=tuple(raw_config["plate_bg_color"]) if isinstance(raw_config.get("plate_bg_color"), (list, tuple)) else None,
    )
    config.validate_against(width, height)
    paths = template_paths(request.template_id) if request.template_id in TEMPLATES and not custom else None
    entry = preferences.car(request.template_id, saved) if request.template_id and not custom and not paths else None
    def mask_data(role, token, index):
        if token:
            return storage.read_bytes(token)
        if paths:
            return paths[index].read_bytes()
        if entry and entry.get(role):
            return storage.read_bytes(storage.make_ref(**entry[role])["token"])
        return None
    car_data = mask_data("car_mask", request.car_mask_token, 1)
    marker_data = mask_data("marker_mask", request.marker_mask_token, 2)
    loaded = perf_counter()
    marker = mask_from_bytes(marker_data, (height, width)) if marker_data else np.zeros((height, width), np.uint8)
    if car_data:
        car = mask_from_bytes(car_data, (height, width))
    elif config.key_color:
        car = ChromaKeySegmenter(image, config.key_color).segment([], [])
        car[marker > 127] = 0
    else:
        raise UserError("Завантажте/намалюйте маску кузова або вкажіть ключовий колір chroma.")
    if not car.any():
        raise UserError("Маска кузова порожня.")
    options = RecolorOptions(lightness_mode=request.lightness_mode, feather=request.feather)
    weights = [prepare_weight(car, options.feather), prepare_weight(marker, options.feather)]
    plate = detect_plate(image, config.plate_box, key_color=config.key_color) if config.plate_box is not None else None
    if plate is not None and plate.detected:
        # Protect the detected interior even with chroma-key. Do not protect a
        # rectangular patch of the surrounding green bumper.
        margin = 0 if config.key_color else max(4, round(0.006 * max(height, width)))
        guard = plate_guard_mask((height, width), plate, margin) > 0
        for weight in weights:
            weight[guard] = 0
    color = request.color
    code, target = normalize_code(color.code), normalize_hex(color.hex)
    if not code:
        raise UserError("Код кольору порожній.")
    output = recolor_image(image, weights, target, color.metallic, options)
    warnings = []
    label = plate_text(raw_config, code) if request.draw_plate else None
    if request.draw_plate and plate is not None:
        background = config.plate_bg_color or sample_plate_background(image, plate, config.key_color)
        output, warning = draw_plate_text(output, plate, label, config.font_path,
                                          None if raw_config.get("auto_font_size", True) else config.font_size,
                                          config.text_color, background, text_bounds=config.plate_box)
        if warning:
            warnings.append(warning)
    elif request.draw_plate:
        warnings.append("Область номера не задана: текст не нанесено.")
    metrics = dict(body_delta_e=round(color_accuracy(output, weights[0], target), 3),
                   marker_delta_e=round(color_accuracy(output, weights[1], target), 3) if weights[1].any() else None)
    if color.metallic:
        warnings.append("Metallic — візуальна імітація; перевірте результат.")
    if metrics["body_delta_e"] >= 5:
        warnings.append(f"Відхилення кольору ΔE={metrics['body_delta_e']:.2f}." + (" Очікувано для keep." if request.lightness_mode == "keep" else ""))
    processed = perf_counter()
    file_data = (encode(output, alpha), f"{code}.png", "image/png")
    scale = min(1, 900 / max(width, height))
    preview_img = cv2.resize(output, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    preview_alpha = cv2.resize(alpha, (preview_img.shape[1], preview_img.shape[0]), interpolation=cv2.INTER_AREA) if alpha is not None else None
    preview_data = (encode(preview_img, preview_alpha), f"{code}-preview.png", "image/png")
    encoded = perf_counter()
    if output_store:
        file, preview = output_store(file_data, preview_data)
    else:
        file, preview = storage.save_bytes(*file_data), storage.save_bytes(*preview_data)
    if timings is not None:
        timings.update(read=loaded - started, process=processed - loaded, encode=encoded - processed,
                       upload=perf_counter() - encoded)
    template_name = (TEMPLATES[request.template_id]["name"] if request.template_id in TEMPLATES and not custom
        else entry["name"] if entry else raw_config.get("model_name") or "Власний PNG")
    return dict(file=file, preview=preview, metrics=metrics, warnings=warnings, template_name=template_name,
                plate_text=label)


def plate_text(config, code):
    """Compose only the plate label; colour codes and output names stay intact."""
    mode = config.get("plate_text_mode", "brand_code")
    brand = config.get("brand_name", "")
    if mode == "code":
        text = code
    elif mode == "brand":
        text = brand
    elif mode == "custom":
        text = config.get("plate_text", "")
    elif mode == "blank":
        text = ""
    else:
        text = f"{brand} {code}"
    return " ".join(text.split())


def create_mask(request):
    image, _ = image_from_bytes(template_data(request.template_id, request.template_token))
    h, w = image.shape[:2]
    for x, y in request.positives + request.negatives:
        if not 0 <= x < w or not 0 <= y < h:
            raise UserError("Точка маски поза межами зображення.")
    if request.backend == "chroma":
        key_color = request.key_color or (template_config(request.template_id).get("key_color") if request.template_id and not request.template_token else None)
        if not key_color:
            raise UserError("Вкажіть ключовий HEX.")
        segmenter = ChromaKeySegmenter(image, normalize_hex(key_color))
    elif request.backend == "grabcut":
        segmenter = GrabCutSegmenter(image)
    else:
        segmenter = FloodSegmenter(image)
        segmenter.ab_tol = request.tolerance
    if request.backend != "chroma" and not request.positives:
        raise UserError("Позначте хоча б одну точку області фарбування.")
    mask = segmenter.segment(request.positives, request.negatives)
    if request.rects:
        region = np.zeros((h, w), np.uint8)
        for x1, y1, x2, y2 in request.rects:
            if not 0 <= x1 < x2 <= w or not 0 <= y1 < y2 <= h:
                raise UserError("Прямокутник маски поза межами зображення.")
            region[y1:y2, x1:x2] = 255
        mask = np.minimum(mask, region)
    if not mask.any():
        raise UserError("Маска порожня. Змініть точки/ключовий колір.")
    return {"file": storage.save_bytes(encode(mask), f"{request.kind}_mask.png", "image/png")}
