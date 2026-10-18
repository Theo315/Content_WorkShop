from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")
ASSETS = ROOT / "assets"
MAX_UPLOAD = 25 * 1024 * 1024
MAX_PIXELS = 8_000_000
MAX_ITEMS = 200
IS_VERCEL = bool(os.getenv("VERCEL"))


def secret() -> str:
    value = os.getenv("SESSION_SECRET", "")
    if IS_VERCEL and len(value) < 32:
        raise RuntimeError("Налаштуйте SESSION_SECRET (щонайменше 32 символи).")
    return value or "local-development-only-not-for-production"


def password() -> str:
    return os.getenv("APP_PASSWORD", "")


def storage_mode() -> str:
    if all(os.getenv(key) for key in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")):
        return "s3"
    return "unconfigured" if IS_VERCEL else "local"
