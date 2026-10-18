"""Private S3/R2 objects in production, equivalent disk adapter in development."""
import hashlib
import io
import mimetypes
import os
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException
from . import settings
from .security import signer, read_file_token, FILE_AGE


def local_root():
    root = Path(os.getenv("LOCAL_STORAGE_DIR", str(settings.ROOT / "tmp/storage"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


@lru_cache(maxsize=1)
def s3():
    return boto3.client(
        "s3", endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
        region_name=os.getenv("S3_REGION", "auto"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=60,
                      retries={"max_attempts": 2}, s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required"),
    )


def bucket():
    return os.environ["S3_BUCKET"]


def clean_name(name: str):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:180]
    return name or "file"


def make_ref(key, name, size, content_type, writable=False):
    data = dict(key=key, name=clean_name(name), size=size, content_type=content_type, writable=writable)
    token = signer("file").dumps(data)
    if settings.storage_mode() == "s3":
        url = s3().generate_presigned_url("get_object", Params={"Bucket": bucket(), "Key": key}, ExpiresIn=3600)
    else:
        url = "/api/files/local/" + quote(token)
    return {"token": token, "name": data["name"], "url": url, "size": size}


def new_upload(name, size, content_type):
    key = f"temporary/{uuid.uuid4().hex}/{clean_name(name)}"
    ref = make_ref(key, name, size, content_type, writable=True)
    headers = {"Content-Type": content_type}
    if settings.storage_mode() == "s3":
        url = s3().generate_presigned_url("put_object", Params={"Bucket": bucket(), "Key": key,
                    "ContentType": content_type, "ContentLength": size}, ExpiresIn=600)
    else:
        url = ref["url"]
    return {"upload_url": url, "method": "PUT", "headers": headers, "file": ref}


def save_bytes(data: bytes, name: str, content_type: str, asset_key=None):
    key = asset_key or f"temporary/{uuid.uuid4().hex}/{clean_name(name)}"
    if settings.storage_mode() == "s3":
        s3().put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type)
    else:
        target = local_root() / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return make_ref(key, name, len(data), content_type)


def asset_ref(path: Path):
    data = path.read_bytes()
    key = f"assets/{hashlib.sha256(data).hexdigest()[:24]}/{path.name}"
    exists = False
    if settings.storage_mode() == "s3":
        try:
            s3().head_object(Bucket=bucket(), Key=key)
            exists = True
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise
    else:
        exists = (local_root() / key).exists()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not exists:
        return save_bytes(data, path.name, content_type, asset_key=key)
    return make_ref(key, path.name, len(data), content_type)


def read_bytes(token, max_bytes=settings.MAX_UPLOAD):
    value = read_file_token(token)
    if value["size"] > max_bytes:
        raise HTTPException(413, "Файл перевищує дозволений розмір.")
    if settings.storage_mode() == "s3":
        try:
            obj = s3().get_object(Bucket=bucket(), Key=value["key"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                raise HTTPException(404, "Файл не знайдено. Завантажте його повторно.") from None
            raise
        try:
            if obj["ContentLength"] > max_bytes:
                raise HTTPException(413, "Файл завеликий.")
            data = obj["Body"].read(max_bytes + 1)
        finally:
            obj["Body"].close()
    else:
        path = local_root() / value["key"]
        if not path.is_file():
            raise HTTPException(404, "Файл не знайдено. Завантажте його повторно.")
        if path.stat().st_size > max_bytes:
            raise HTTPException(413, "Файл завеликий.")
        data = path.read_bytes()
    if len(data) > max_bytes or len(data) != value["size"]:
        raise HTTPException(400, "Розмір файлу не відповідає завантаженню.")
    return data


def delete_file(token):
    value = read_file_token(token)
    if not value["key"].startswith("temporary/"):
        raise HTTPException(400, "Шаблони та архівні результати не видаляються через цей endpoint.")
    if settings.storage_mode() == "s3":
        s3().delete_object(Bucket=bucket(), Key=value["key"])
    else:
        (local_root() / value["key"]).unlink(missing_ok=True)


def prune_local():
    if settings.storage_mode() != "local":
        return
    # Only this application's generated temporary objects; never the workspace.
    root = local_root() / "temporary"
    for path in root.glob("*/*"):
        if path.is_file() and time.time() - path.stat().st_mtime > FILE_AGE:
            path.unlink(missing_ok=True)
    # History owns only these generated files, not uploaded source documents.
    history_root = local_root() / "history"
    for path in history_root.rglob("*"):
        if path.is_file() and time.time() - path.stat().st_mtime > 30 * 86400:
            path.unlink(missing_ok=True)
