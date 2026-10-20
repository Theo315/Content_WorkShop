"""Opt-in deployment storage check. Never prints credentials or signed URLs.

Default: validate local configuration only, without creating an SDK client.
--cloud: write two disposable objects under one new temporary/deploy-check/ UUID,
exercise SDK and signed-HTTP operations, then delete only those exact objects.
This is not a browser CORS check or a Vercel runtime test.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import sys
from typing import Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Load .env.local/.env through the same mechanism as the application.
from backend import settings  # noqa: E402, F401

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

UPLOAD_BYTES = 6 * 1024 * 1024
CONTENT_TYPE = "application/octet-stream"


@dataclass(repr=False, frozen=True)
class CloudConfig:
    endpoint: str | None
    region: str
    bucket: str
    access_key: str
    secret_key: str


class ConfigurationError(Exception):
    """Contains field names and fixed validation messages, never field values."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class CheckError(Exception):
    pass


@dataclass
class CloudReport:
    completed: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    cleanup_errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.errors or self.cleanup_errors)


def configuration(environ: Mapping[str, str] | None = None) -> CloudConfig:
    env = os.environ if environ is None else environ
    problems = []
    for name in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        if not env.get(name, "").strip():
            problems.append(f"{name}: required")
    auth_fields = [("SESSION_SECRET", 32)]
    if env.get("APP_PUBLIC_ACCESS", "").strip().lower() not in ("true", "1"):
        auth_fields.append(("APP_PASSWORD", 16))
    for name, minimum in auth_fields:
        if len(env.get(name, "")) < minimum:
            problems.append(f"{name}: minimum {minimum} characters")

    endpoint = env.get("S3_ENDPOINT_URL", "").strip() or None
    region = env.get("S3_REGION", "auto").strip()
    if endpoint:
        try:
            parsed = urllib.parse.urlsplit(endpoint)
            valid = (parsed.scheme == "https" and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and parsed.path in ("", "/") and not parsed.query
                     and not parsed.fragment and parsed.port in (None, 443))
        except ValueError:
            valid = False
        if not valid:
            problems.append("S3_ENDPOINT_URL: HTTPS service endpoint required; no credentials, bucket path, query, or fragment")
    elif not region or region == "auto":
        problems.append("S3_REGION: explicit AWS region required when S3_ENDPOINT_URL is empty")
    if endpoint and not region:
        problems.append("S3_REGION: required (auto for R2)")
    if problems:
        raise ConfigurationError(problems)
    return CloudConfig(endpoint, region, env["S3_BUCKET"].strip(),
                       env["S3_ACCESS_KEY_ID"], env["S3_SECRET_ACCESS_KEY"])


def make_client(config: CloudConfig):
    return boto3.client(
        "s3", endpoint_url=config.endpoint, region_name=config.region,
        aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
        config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=45,
                      retries={"max_attempts": 1}, s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )


def safe_error_code(exc: BaseException) -> str:
    # Never stringify SDK/urllib exceptions: they can include signed URLs,
    # request headers, endpoint account IDs, or provider diagnostic payloads.
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "ProviderError")
        return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "ProviderError"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP_{exc.code}" if isinstance(exc.code, int) else "HTTPError"
    if isinstance(exc, (TimeoutError,)):
        return "Timeout"
    if isinstance(exc, (urllib.error.URLError, OSError, BotoCoreError)):
        return "NetworkOrSDKError"
    if isinstance(exc, CheckError):
        # Only fixed literals created inside this module are used here.
        return str(exc)
    if isinstance(exc, KeyboardInterrupt):
        return "Interrupted"
    return "UnexpectedError"


def _verify_head(response: dict):
    if response.get("ContentLength") != UPLOAD_BYTES or response.get("ContentType") != CONTENT_TYPE:
        raise CheckError("ObjectMetadataMismatch")


def run_cloud_check(config: CloudConfig) -> CloudReport:
    report = CloudReport()
    # No user-controlled key or cleanup prefix is accepted.
    prefix = f"temporary/deploy-check/{uuid.uuid4().hex}/"
    upload_key, copy_key = prefix + "upload.bin", prefix + "copy.bin"
    client = None
    operation = "client-init"
    try:
        client = make_client(config)
        operation = "sdk-put"
        client.put_object(Bucket=config.bucket, Key=upload_key,
                          Body=b"content-tools-deploy-check", ContentType=CONTENT_TYPE)
        report.completed.append(operation)

        operation = "sign-put"
        signed_url = client.generate_presigned_url(
            "put_object", Params={"Bucket": config.bucket, "Key": upload_key,
                                  "ContentType": CONTENT_TYPE, "ContentLength": UPLOAD_BYTES},
            ExpiresIn=600,
        )
        report.completed.append(operation)
        operation = "signed-http-put-6MiB"
        payload = b"d" * UPLOAD_BYTES
        request = urllib.request.Request(
            signed_url, data=payload, method="PUT",
            headers={"Content-Type": CONTENT_TYPE, "Content-Length": str(UPLOAD_BYTES)},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            if response.status not in (200, 201, 204):
                raise CheckError("SignedUploadRejected")
        report.completed.append(operation)

        operation = "head"
        _verify_head(client.head_object(Bucket=config.bucket, Key=upload_key))
        report.completed.append(operation)
        operation = "get"
        response = client.get_object(Bucket=config.bucket, Key=upload_key)
        body = response["Body"]
        try:
            if body.read(UPLOAD_BYTES + 1) != payload:
                raise CheckError("ObjectContentMismatch")
        finally:
            body.close()
        report.completed.append(operation)
        operation = "copy"
        client.copy_object(Bucket=config.bucket, Key=copy_key,
                           CopySource={"Bucket": config.bucket, "Key": upload_key})
        report.completed.append(operation)
        operation = "head-copy"
        _verify_head(client.head_object(Bucket=config.bucket, Key=copy_key))
        report.completed.append(operation)
        operation = "list"
        listing = client.list_objects_v2(Bucket=config.bucket, Prefix=prefix, MaxKeys=10)
        found = {item.get("Key") for item in listing.get("Contents", [])}
        if not {upload_key, copy_key}.issubset(found):
            raise CheckError("ObjectListingMismatch")
        report.completed.append(operation)
    except (Exception, KeyboardInterrupt) as exc:
        report.errors.append((operation, safe_error_code(exc)))
    finally:
        if client is not None:
            # Exact two keys created by this invocation, even after a partial
            # write. Never delete by a listed key or issue recursive deletion.
            for label, key in (("delete-upload", upload_key), ("delete-copy", copy_key)):
                try:
                    client.delete_object(Bucket=config.bucket, Key=key)
                    report.completed.append(label)
                except (Exception, KeyboardInterrupt) as exc:
                    report.cleanup_errors.append((label, safe_error_code(exc)))
            close = getattr(client, "close", None)
            if close:
                try:
                    close()
                except Exception as exc:
                    report.cleanup_errors.append(("close-client", safe_error_code(exc)))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", action="store_true",
                        help="Explicitly permit disposable cloud writes and a 6MiB signed HTTP PUT")
    args = parser.parse_args(argv)
    try:
        config = configuration()
    except ConfigurationError as exc:
        print("OFFLINE CONFIGURATION CHECK FAILED. No cloud requests were made.")
        for problem in exc.problems:
            print("ERROR " + problem)
        return 1
    if not args.cloud:
        print("OFFLINE CONFIGURATION CHECK PASSED. No cloud requests were made.")
        print("This does NOT validate credentials, bucket permissions, upload, CORS, or Vercel.")
        print("Use --cloud only to permit disposable writes under temporary/deploy-check/<uuid>/.")
        return 0

    print("CLOUD CHECK: disposable writes enabled under temporary/deploy-check/<uuid>/.")
    report = run_cloud_check(config)
    for operation in report.completed:
        print("OK " + operation)
    for operation, code in report.errors:
        print(f"ERROR {operation}: {code}")
    for operation, code in report.cleanup_errors:
        print(f"CLEANUP ERROR {operation}: {code}")
    if report.cleanup_errors:
        print("Cleanup was incomplete. Inspect only recent temporary/deploy-check/ objects in your bucket.")
    print("CLOUD CHECK PASSED." if report.ok else "CLOUD CHECK FAILED.")
    print("Python does not enforce browser CORS. Browser upload/preview and Vercel runtime remain unverified.")
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
