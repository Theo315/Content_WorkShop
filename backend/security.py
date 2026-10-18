import hashlib
import hmac
from fastapi import HTTPException, Request
from itsdangerous import URLSafeTimedSerializer, BadSignature
from . import settings

COOKIE = "content_tools_session"
SESSION_AGE = 12 * 3600
FILE_AGE = 24 * 3600


def signer(purpose: str):
    return URLSafeTimedSerializer(settings.secret(), salt=purpose)


def password_fingerprint():
    return hashlib.sha256(settings.password().encode()).hexdigest()


def authenticated(request: Request) -> bool:
    if not settings.password():
        return not settings.IS_VERCEL
    try:
        value = signer("session").loads(request.cookies.get(COOKIE, ""), max_age=SESSION_AGE)
        return hmac.compare_digest(value.get("password_hash", ""), password_fingerprint())
    except (BadSignature, RuntimeError):
        return False


def require_auth(request: Request):
    import os
    if settings.IS_VERCEL and (len(settings.password()) < 16 or len(os.getenv("SESSION_SECRET", "")) < 32):
        raise HTTPException(503, "Налаштуйте APP_PASSWORD (від 16 символів) і SESSION_SECRET.")
    if not authenticated(request):
        raise HTTPException(401, "Увійдіть у сервіс.")
    if settings.storage_mode() == "unconfigured":
        raise HTTPException(503, "Потрібно підключити S3/R2 сховище у змінних середовища.")


def read_file_token(token: str):
    try:
        value = signer("file").loads(token, max_age=FILE_AGE)
        key = value["key"]
        if not key.startswith(("temporary/", "assets/", "history/files/", "library/files/")) or ".." in key or "\\" in key:
            raise ValueError()
        return value
    except (BadSignature, KeyError, TypeError, ValueError):
        raise HTTPException(400, "Файл недоступний або посилання застаріло. Завантажте його ще раз.") from None
