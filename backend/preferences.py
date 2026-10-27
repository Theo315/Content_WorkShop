"""Single-owner persistent preferences and template library (disk or private S3)."""
import json
import hashlib
import logging
import os
import re
import uuid
from fastapi import HTTPException
from botocore.exceptions import ClientError
from vendor.hexph.hexph.colors import normalize_hex
from vendor.hexph.hexph.errors import UserError
from . import settings, storage, security, sds

log = logging.getLogger("content-tools")
KEY = "preferences/config.json"
CAR_ID = re.compile(r"^car-[0-9a-f]{32}$")


def load():
    if settings.storage_mode() == "s3":
        try:
            obj = storage.s3().get_object(Bucket=storage.bucket(), Key=KEY)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return {}
            raise
        try:
            raw = obj["Body"].read(256_001)
        finally:
            obj["Body"].close()
    else:
        path = storage.local_root() / KEY
        if not path.exists():
            return {}
        raw = path.read_bytes()
    if len(raw) > 256_000:
        raise HTTPException(500, "Файл налаштувань завеликий.")
    return json.loads(raw)


def save(value):
    raw = json.dumps(value, ensure_ascii=False).encode()
    if len(raw) > 256_000:
        raise UserError("Ліміт обсягу налаштувань вичерпано.")
    if settings.storage_mode() == "s3":
        storage.s3().put_object(Bucket=storage.bucket(), Key=KEY, Body=raw, ContentType="application/json")
    else:
        path = storage.local_root() / KEY
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def descriptor(ref):
    return {**security.read_file_token(ref["token"]), "writable": False}


def store_file(data, name, mime, folder):
    return descriptor(storage.save_bytes(data, name, mime,
        asset_key=f"library/files/{folder}/{storage.clean_name(name)}"))


def remove_files(values):
    for value in values:
        if not value:
            continue
        key = value["key"]
        if not key.startswith("library/files/") or ".." in key or "\\" in key:
            raise HTTPException(500, "Некоректний шлях бібліотеки.")
        if settings.storage_mode() == "s3":
            storage.s3().delete_object(Bucket=storage.bucket(), Key=key)
        else:
            (storage.local_root() / key).unlink(missing_ok=True)


def reference(value=None):
    value = load() if value is None else value
    custom = value.get("sds_reference")
    available = bool(custom) or not value.get("sds_reference_disabled", False)
    return dict(file=(storage.make_ref(**custom) if custom else storage.asset_ref(sds.REFERENCE)) if available else None,
        available=available, pages=value.get("sds_reference_pages", 41) if custom and available else 41 if available else 0, custom=bool(custom), title_template=value.get("sds_title_template", sds.DEFAULT_TITLE),
        description="Типовий SDS-еталон. Генератор змінює лише назви товарів.")


def default_pdf_bytes():
    value = load()
    custom = value.get("sds_reference")
    if not custom and value.get("sds_reference_disabled"):
        raise UserError("PDF-шаблон видалено. Встановіть PDF у налаштуваннях або завантажте його для цього пакета.")
    return storage.read_bytes(storage.make_ref(**custom)["token"]) if custom else None


def update_title(title):
    title = title.strip()
    sds.parse_items("template", "BRAND", title, "CODE COLOR")
    value = load()
    value["sds_title_template"] = title
    save(value)
    return reference(value)


def update_pdf(token=None):
    value = load()
    old = value.get("sds_reference")
    if token:
        data = storage.read_bytes(token)
        with sds.open_pdf(data) as doc:
            sds.preflight(doc)
            pages = doc.page_count
        source = security.read_file_token(token)
        value["sds_reference"] = store_file(data, source["name"], "application/pdf", uuid.uuid4().hex)
        value["sds_reference_pages"] = pages
        value.pop("sds_reference_disabled", None)
    else:
        value.pop("sds_reference", None)
        value.pop("sds_reference_pages", None)
        value["sds_reference_disabled"] = True
    save(value)
    remove_files([old])
    return reference(value)


def restore_pdf():
    value = load()
    old = value.pop("sds_reference", None)
    value.pop("sds_reference_pages", None)
    value.pop("sds_reference_disabled", None)
    save(value)
    remove_files([old])
    return reference(value)


def car(template_id, value=None):
    if not CAR_ID.fullmatch(template_id):
        raise UserError("Невідомий шаблон.")
    entry = (load() if value is None else value).get("cars", {}).get(template_id)
    if not entry:
        raise HTTPException(404, "Шаблон видалено або не знайдено.")
    return entry


def present_car(template_id, entry):
    return {**entry, "id": template_id, "builtin": False,
        **{role: storage.make_ref(**entry[role]) if entry.get(role) else None
           for role in ("image", "car_mask", "marker_mask")}}


def add_car(request, *, value=None, image_data=None):
    from . import png
    value = load() if value is None else value
    cars = value.setdefault("cars", {})
    if len(cars) >= 100:
        raise UserError("Максимум 100 власних шаблонів. Видаліть непотрібні.")
    name = request.name.strip()
    if not name:
        raise UserError("Вкажіть назву шаблону.")
    data = storage.read_bytes(request.image_token) if image_data is None else image_data
    image, _ = png.image_from_bytes(data)
    height, width = image.shape[:2]
    config = request.config.model_dump()
    config["key_color"] = normalize_hex(config["key_color"]) if config.get("key_color") else None
    if not config.get("model_name"):
        config["model_name"] = name
    masks = {}
    for role in ("car_mask", "marker_mask"):
        token = getattr(request, role + "_token")
        masks[role] = storage.read_bytes(token) if token else None
        if masks[role] is not None:
            mask = png.mask_from_bytes(masks[role], (height, width))
            if role == "car_mask" and not mask.any():
                raise UserError("Маска кузова порожня.")
    if not masks["car_mask"] and not config["key_color"]:
        raise UserError("Задайте ключовий колір кузова або завантажте його маску.")
    from vendor.hexph.hexph.config import TemplateConfig
    TemplateConfig(plate_box=config["plate_box"]).validate_against(width, height)
    template_id = "car-" + uuid.uuid4().hex
    entry = dict(name=name, width=width, height=height, config=config,
                 source_sha256=hashlib.sha256(data).hexdigest())
    # All writes use a new private folder. Roll back exact new objects when a
    # copy or preference commit fails; existing templates are never touched.
    planned = [{"key": f"library/files/{template_id}/{name}"}
               for name in ["template.png"] + [role + ".png" for role, raw in masks.items() if raw]]
    try:
        entry["image"] = store_file(data, "template.png", "image/png", template_id)
        for role, raw in masks.items():
            entry[role] = store_file(raw, role + ".png", "image/png", template_id) if raw else None
        cars[template_id] = entry
        save(value)
    except Exception:
        cars.pop(template_id, None)
        try:
            remove_files(planned)
        except Exception:
            log.exception("Failed to clean up uncommitted template objects")
        raise
    return present_car(template_id, entry)


def save_generated_reference(request, *, image_data=None):
    """Persist an uploaded source after a successful render, once per PNG.

    Stored masks/config are the first successful snapshot. Reusing a reference
    never overwrites settings the owner subsequently edited in the library.
    Call under the same compute lock as template mutations.
    """
    if not request.template_token:
        return None
    from pathlib import PurePosixPath
    from .models import SavedTemplate, TemplateConfig
    data = storage.read_bytes(request.template_token) if image_data is None else image_data
    checksum = hashlib.sha256(data).hexdigest()
    value = load()
    for template_id, entry in value.get("cars", {}).items():
        stored_checksum = entry.get("source_sha256")
        # Older libraries have no checksum. Only read same-size candidates, not
        # every PNG on every colour in a batch; remember this metadata migration.
        if stored_checksum is None and entry.get("image", {}).get("size") == len(data):
            candidate = storage.read_bytes(storage.make_ref(**entry["image"])["token"])
            stored_checksum = hashlib.sha256(candidate).hexdigest()
            entry["source_sha256"] = stored_checksum
            save(value)
        if stored_checksum == checksum:
            return {"template": present_car(template_id, entry), "created": False}
    config = request.config or TemplateConfig()
    source = security.read_file_token(request.template_token)
    name = (config.model_name or "").strip() or PurePosixPath(source["name"]).stem.strip() or "Власний PNG"
    saved = add_car(SavedTemplate(name=name[:180], image_token=request.template_token,
        car_mask_token=request.car_mask_token, marker_mask_token=request.marker_mask_token,
        config=config), value=value, image_data=data)
    return {"template": saved, "created": True}


def update_key(template_id, key_color):
    from . import png
    key_color = normalize_hex(key_color) if key_color else None
    value = load()
    if template_id in png.TEMPLATES:
        if template_id in value.get("hidden_builtin_templates", []):
            raise HTTPException(404, "Шаблон видалено. Спочатку відновіть його.")
        value.setdefault("builtin_keys", {})[template_id] = key_color
    else:
        entry = car(template_id, value)
        if not key_color and not entry.get("car_mask"):
            raise UserError("Без маски кузова потрібен ключовий колір.")
        entry["config"]["key_color"] = key_color
    save(value)
    return {"ok": True, "key_color": key_color}


def delete_car(template_id):
    from . import png
    value = load()
    if template_id in png.TEMPLATES:
        hidden = set(value.get("hidden_builtin_templates", []))
        hidden.add(template_id)
        value["hidden_builtin_templates"] = sorted(hidden)
        save(value)
        return {"ok": True, "restorable": True}
    entry = car(template_id, value)
    del value["cars"][template_id]
    save(value)
    remove_files([entry.get(role) for role in ("image", "car_mask", "marker_mask")])
    return {"ok": True}


def restore_car(template_id):
    from . import png
    if template_id not in png.TEMPLATES:
        raise HTTPException(404, "Відновлення доступне лише для вбудованих шаблонів.")
    value = load()
    value["hidden_builtin_templates"] = [key for key in value.get("hidden_builtin_templates", []) if key != template_id]
    save(value)
    return {"ok": True}
