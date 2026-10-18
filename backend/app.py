from contextlib import asynccontextmanager
from functools import wraps
import hmac
import logging
import threading
from urllib.parse import urlparse

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, Response, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from botocore.exceptions import BotoCoreError, ClientError
from vendor.hexph.hexph.errors import UserError
from . import models as m, settings, security, storage, sds, pdf_editor, png, history, preferences

log = logging.getLogger("content-tools")
# MuPDF is not thread-safe. Also bounds peak image memory per process.
compute_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    storage.prune_local()
    yield


app = FastAPI(title="Content Tools API", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
router = APIRouter(prefix="/api", dependencies=[Depends(security.require_auth)])


@app.middleware("http")
async def request_guards(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Запит з іншого сайту відхилено."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Запит з іншого сайту відхилено."}, status_code=403)
    limit = settings.MAX_UPLOAD if "/files/local/" in request.url.path else 256_000
    try:
        if int(request.headers.get("content-length", "0")) > limit:
            return JSONResponse({"detail": "Запит завеликий."}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "Некоректна довжина запиту."}, status_code=400)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(UserError)
@app.exception_handler(sds.SDSError)
async def user_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    errors = [".".join(str(p) for p in error["loc"][1:]) + ": " + error["msg"] for error in exc.errors()]
    return JSONResponse({"detail": "; ".join(errors)}, status_code=422)


@app.exception_handler(BotoCoreError)
@app.exception_handler(ClientError)
async def storage_error(request, exc):
    log.error("Object storage failure: %s", type(exc).__name__)
    return JSONResponse({"detail": "Сховище недоступне. Перевірте налаштування S3/R2 та CORS."}, status_code=503)


@app.exception_handler(Exception)
async def internal_error(request, exc):
    log.exception("Request failed: %s", request.url.path)
    return JSONResponse({"detail": "Помилка обробки. Спробуйте інший файл або перевірте серверний журнал."}, status_code=500)


@app.get("/api/status")
def status(request: Request):
    try:
        configured = not settings.IS_VERCEL or (len(settings.password()) >= 16 and len(settings.secret()) >= 32)
    except RuntimeError:
        configured = False
    return dict(authenticated=security.authenticated(request), auth_required=bool(settings.password()) or settings.IS_VERCEL,
                storage=settings.storage_mode(), ready=configured and settings.storage_mode() != "unconfigured",
                max_upload_bytes=settings.MAX_UPLOAD)


@app.post("/api/session")
def login(data: m.Login, response: Response):
    if not settings.password() and settings.IS_VERCEL:
        raise HTTPException(503, "Налаштуйте APP_PASSWORD та SESSION_SECRET.")
    if not hmac.compare_digest(data.password.encode(), settings.password().encode()):
        raise HTTPException(401, "Неправильний пароль.")
    value = security.signer("session").dumps({"password_hash": security.password_fingerprint()})
    response.set_cookie(security.COOKIE, value, max_age=security.SESSION_AGE, httponly=True,
                        secure=settings.IS_VERCEL, samesite="strict", path="/")
    return {"ok": True}


@app.delete("/api/session")
def logout(response: Response):
    response.delete_cookie(security.COOKIE, path="/")
    return {"ok": True}


@router.post("/files/upload")
def upload(data: m.Upload):
    return storage.new_upload(data.filename, data.size, data.content_type)


@router.put("/files/local/{token}")
async def local_upload(token: str, request: Request):
    if settings.storage_mode() != "local":
        raise HTTPException(404)
    value = security.read_file_token(token)
    if not value.get("writable") or not value["key"].startswith("temporary/"):
        raise HTTPException(403, "Цей файл не можна перезаписувати.")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > min(value["size"], settings.MAX_UPLOAD):
            raise HTTPException(413, "Файл завеликий.")
    if len(data) != value["size"]:
        raise HTTPException(400, "Файл завантажено не повністю.")
    path = storage.local_root() / value["key"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"ok": True}


@router.get("/files/local/{token}")
def local_file(token: str):
    if settings.storage_mode() != "local":
        raise HTTPException(404)
    value = security.read_file_token(token)
    data = storage.read_bytes(token, max_bytes=80 * 1024 * 1024)
    return Response(data, media_type=value["content_type"])


@router.delete("/files")
def delete_file(data: m.FileToken):
    storage.delete_file(data.token)
    return {"ok": True}


@router.post("/files/refresh")
def refresh_file(data: m.FileToken):
    value = security.read_file_token(data.token)
    return {"file": storage.make_ref(**value)}


@router.get("/png/templates")
def templates():
    return png.catalog()


@router.get("/settings")
def get_settings():
    return {"sds": preferences.reference(), **png.catalog()}


@router.put("/settings/sds/title")
def save_default_title(data: m.DefaultTitle):
    with compute_lock:
        return preferences.update_title(data.title_template)


@router.put("/settings/sds/reference")
def save_default_reference(data: m.FileToken):
    with compute_lock:
        return preferences.update_pdf(data.token)


@router.delete("/settings/sds/reference")
def reset_default_reference():
    with compute_lock:
        return preferences.update_pdf()


@router.post("/settings/sds/reference/restore")
def restore_default_reference():
    with compute_lock:
        return preferences.restore_pdf()


@router.post("/settings/templates")
def save_template(data: m.SavedTemplate):
    with compute_lock:
        return preferences.add_car(data)


@router.put("/settings/templates/{template_id}/key")
def save_template_key(template_id: str, data: m.TemplateKey):
    with compute_lock:
        return preferences.update_key(template_id, data.key_color)


@router.delete("/settings/templates/{template_id}")
def remove_template(template_id: str):
    with compute_lock:
        return preferences.delete_car(template_id)


@router.post("/settings/templates/{template_id}/restore")
def restore_template(template_id: str):
    with compute_lock:
        return preferences.restore_car(template_id)


@router.get("/history")
def list_history(cursor: str | None = Query(default=None, max_length=80), limit: int = Query(default=30, ge=1, le=50)):
    return history.listing(cursor, limit)


@router.get("/history/batches")
def list_history_batches(cursor: str | None = Query(default=None, max_length=80), limit: int = Query(default=30, ge=1, le=50)):
    return history.batch_listing(cursor, limit)


@router.get("/history/batches/{batch_id}")
def history_batch(batch_id: str):
    return history.batch_entry(batch_id)


@router.get("/history/{entry_id}")
def history_entry(entry_id: str):
    return history.present(history.read_manifest(entry_id))


@router.post("/png/colors")
def colors(data: m.Colors):
    return png.parse_colors(data.text, data.format)


@router.post("/png/render")
def render(data: m.PngRender):
    with compute_lock:
        # Reuse exactly the bytes rendered. Avoid a second S3 download for each
        # colour, and never save a different source if the upload gets replaced.
        source_data = storage.read_bytes(data.template_token) if data.template_token else None
        result = png.render(data, source_data=source_data) if source_data is not None else png.render(data)
        if data.template_token:
            try:
                result["auto_saved_template"] = preferences.save_generated_reference(data, image_data=source_data)
            except Exception as exc:
                # A library failure must not discard a successfully generated
                # photo or falsely tell the UI its reference has been saved.
                log.exception("Automatic PNG reference save failed")
                reason = str(exc) if isinstance(exc, UserError) else "Не вдалося записати референс у бібліотеку."
                warning = f"Референс не збережено автоматично: {reason} Фото згенеровано; повторіть генерацію або збережіть шаблон у налаштуваннях."
                result["reference_save_warning"] = warning
                result["warnings"].append(warning)
        del source_data
    return history.record("png", data.color.code, result,
        dict(color=data.color.model_dump(), template=data.template_id or "custom", template_name=result["template_name"], metrics=result["metrics"]),
        f"ΔE кузова {result['metrics']['body_delta_e']:.2f}", batch=data.result_metadata())


@router.post("/png/mask")
def mask(data: m.Mask):
    with compute_lock:
        return png.create_mask(data)


@router.get("/sds/reference")
def reference():
    return preferences.reference()


@router.post("/sds/parse")
def parse_sds(data: m.SDSParse):
    with compute_lock:
        return {"items": sds.parse_items(**data.model_dump())}


@router.post("/sds/generate")
def generate_sds(data: m.SDSGenerate):
    with compute_lock:
        reference_data = storage.read_bytes(data.reference_token) if data.reference_token else preferences.default_pdf_bytes()
        result, validation = sds.generate(data.item.title, reference_data)
        previews = [sds.render_page(result, index) for index in (0, 8, 23, 29)] if data.render_previews else []
    name = sds.filename(data.item.code, data.item.brand, data.item.color)
    file = storage.save_bytes(result, name, "application/pdf")
    preview_refs = [storage.save_bytes(image, f"{data.item.code}-p{index}.png", "image/png")
                    for image, index in zip(previews, (1, 9, 24, 30))]
    return history.record("sds", data.item.code or data.item.title,
        dict(file=file, previews=preview_refs, validation=validation),
        dict(item=data.item.model_dump(), validation=validation), f"{validation['pages']} стор. · SDS", batch=data.result_metadata())


@router.post("/sds/find")
def find_sds(data: m.Find):
    args = data.model_dump()
    content = storage.read_bytes(args.pop("file_token"))
    args["search"] = args.pop("search_text")
    with compute_lock:
        return {"matches": pdf_editor.find(content, **args)}


@router.post("/sds/edit")
def edit_sds(data: m.Edit):
    args = data.model_dump(exclude=set(m.BatchMetadata.model_fields))
    content = storage.read_bytes(args.pop("file_token"))
    args["search"] = args.pop("search_text")
    with compute_lock:
        result, replacements, pages, index = pdf_editor.edit(content, **args)
        preview = sds.render_page(result, index)
    source_name = security.read_file_token(data.file_token)["name"]
    file = storage.save_bytes(result, source_name.rsplit(".", 1)[0] + "-edited.pdf", "application/pdf")
    return history.record("edit", source_name,
        dict(file=file, preview=storage.save_bytes(preview, "edited-preview.png", "image/png"), replacements=replacements, pages=pages),
        dict(search=data.search_text, replacement=data.replacement, pages=data.pages_spec, replacements=replacements),
        f"{replacements} замін · {pages} стор.", batch=data.result_metadata())


app.include_router(router)
