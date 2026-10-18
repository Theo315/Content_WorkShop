"""Print exact-origin B2 CORS policy; only --apply permits a bucket update.

Uses a separate, short-lived STANDARD B2 application key with readBuckets and
writeBuckets, not the application's S3 key or a master key. S3 PutBucketCors
replaces the existing S3 CORS rules. Revoke the setup key afterwards.

References:
https://www.backblaze.com/apidocs/s3-put-bucket-cors
https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import getpass
import json
import os
import re
import sys
from typing import Mapping
from urllib.parse import urlsplit
import warnings

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


@dataclass(frozen=True)
class CorsConfig:
    bucket: str
    endpoint: str
    region: str
    origins: tuple[str, ...]

    def policy(self) -> dict:
        return {"CORSRules": [{
            "AllowedOrigins": list(self.origins),
            "AllowedMethods": ["GET", "PUT", "HEAD"],
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
            "MaxAgeSeconds": 3600,
        }]}


@dataclass(frozen=True, repr=False)
class SetupCredentials:
    key_id: str
    secret: str


@dataclass
class ApplyReport:
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class SetupError(Exception):
    """Fixed local messages only; never include user input or secret values."""


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
        labels = host.split(".")
        valid_host = (0 < len(host) <= 253 and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels))
        valid = (parsed.scheme == "https" and valid_host
                 and parsed.username is None and parsed.password is None
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and "*" not in value and not any(char.isspace() for char in value)
                 and (port is None or 1 <= port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise SetupError("--origin must be an exact HTTPS origin, without credentials, wildcard, path, query, or fragment")
    return "https://" + host + (f":{port}" if port not in (None, 443) else "")


def create_config(*, bucket: str, endpoint: str, region: str, origins: list[str]) -> CorsConfig:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{4,61}[A-Za-z0-9]", bucket):
        raise SetupError("--bucket must be a B2 bucket name, 6-63 letters, numbers, or hyphens")
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]{3}", region):
        raise SetupError("--region must be the explicit B2 region, not auto")
    expected = f"https://s3.{region}.backblazeb2.com"
    if endpoint not in (expected, expected + "/"):
        raise SetupError("--endpoint must be https://s3.<region>.backblazeb2.com and match --region")
    if not origins:
        raise SetupError("At least one --origin is required")
    normalized = tuple(dict.fromkeys(_origin(value) for value in origins))
    # B2 imposes a 1000-byte rule limit. Keep enough room for provider metadata.
    rule_bytes = sum(len(value.encode("utf-8")) for value in normalized) + 150
    if rule_bytes > 900:
        raise SetupError("Too many origins for one B2 CORS rule; use fewer exact origins")
    return CorsConfig(bucket, expected, region, normalized)


def setup_credentials(environ: Mapping[str, str] | None = None) -> SetupCredentials:
    env = os.environ if environ is None else environ
    key_id, secret = env.get("B2_SETUP_KEY_ID", ""), env.get("B2_SETUP_SECRET", "")
    if bool(key_id) != bool(secret):
        raise SetupError("Set both B2_SETUP_KEY_ID and B2_SETUP_SECRET, or neither for masked prompts")
    if not key_id:
        try:
            # getpass otherwise falls back to echoing input when no TTY exists.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key_id = getpass.getpass("B2 STANDARD setup key ID (masked; not master): ")
                secret = getpass.getpass("B2 STANDARD setup application key (masked): ")
        except (getpass.GetPassWarning, EOFError):
            raise SetupError("Masked input unavailable; use dedicated B2_SETUP_KEY_ID and B2_SETUP_SECRET environment variables") from None
    if not key_id.strip() or not secret.strip():
        raise SetupError("Both dedicated setup credentials are required")
    return SetupCredentials(key_id, secret)


def make_client(config: CorsConfig, credentials: SetupCredentials):
    return boto3.client(
        "s3", endpoint_url=config.endpoint, region_name=config.region,
        aws_access_key_id=credentials.key_id, aws_secret_access_key=credentials.secret,
        config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=45,
                      retries={"max_attempts": 1}, s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )


def _safe_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "ProviderError")
        return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "ProviderError"
    if isinstance(exc, (BotoCoreError, OSError)):
        return "NetworkOrSDKError"
    if isinstance(exc, KeyboardInterrupt):
        return "Interrupted"
    return "UnexpectedError"


def _normalized_rules(policy: dict) -> list[dict]:
    rules = []
    for rule in policy.get("CORSRules", []):
        rules.append({
            "AllowedOrigins": sorted(rule.get("AllowedOrigins", [])),
            "AllowedMethods": sorted(rule.get("AllowedMethods", [])),
            "AllowedHeaders": sorted(value.lower() for value in rule.get("AllowedHeaders", [])),
            "ExposeHeaders": sorted(value.lower() for value in rule.get("ExposeHeaders", [])),
            "MaxAgeSeconds": rule.get("MaxAgeSeconds"),
        })
    return rules


def apply_cors(config: CorsConfig, credentials: SetupCredentials) -> ApplyReport:
    report, client, operation = ApplyReport(), None, "client-init"
    try:
        client = make_client(config, credentials)
        operation = "put-bucket-cors"
        client.put_bucket_cors(Bucket=config.bucket, CORSConfiguration=config.policy())
        operation = "verify-bucket-cors"
        actual = client.get_bucket_cors(Bucket=config.bucket)
        if _normalized_rules(actual) != _normalized_rules(config.policy()):
            report.errors.append((operation, "PolicyVerificationMismatch"))
    except (Exception, KeyboardInterrupt) as exc:
        report.errors.append((operation, _safe_code(exc)))
    finally:
        if client is not None:
            try:
                client.close()
            except (Exception, KeyboardInterrupt) as exc:
                report.errors.append(("close-client", _safe_code(exc)))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--origin", required=True, action="append", dest="origins",
                        help="Exact HTTPS origin without trailing slash; repeat for additional domains")
    parser.add_argument("--apply", action="store_true",
                        help="Explicitly REPLACE S3 CORS rules on the one named bucket")
    args = parser.parse_args(argv)
    try:
        config = create_config(bucket=args.bucket, endpoint=args.endpoint,
                               region=args.region, origins=args.origins)
    except SetupError as exc:
        print("ERROR " + str(exc), file=sys.stderr)
        return 1
    if not args.apply:
        # Machine-readable preview; do not read keys, create SDK clients, or send requests.
        print(json.dumps(config.policy(), indent=2))
        print("OFFLINE POLICY PREVIEW ONLY. No credentials read and no cloud requests made.", file=sys.stderr)
        return 0

    print(f"WARNING: --apply REPLACES ALL existing S3 CORS rules on bucket {config.bucket}.", file=sys.stderr)
    print("Only CORS is changed; bucket privacy, object data, and lifecycle are not changed.", file=sys.stderr)
    print("Use a short-lived STANDARD setup key with readBuckets/writeBuckets. Revoke it afterwards.", file=sys.stderr)
    try:
        credentials = setup_credentials()
    except (SetupError, KeyboardInterrupt) as exc:
        message = str(exc) if isinstance(exc, SetupError) else "Interrupted"
        print("ERROR " + message, file=sys.stderr)
        return 1
    report = apply_cors(config, credentials)
    for operation, code in report.errors:
        print(f"ERROR {operation}: {code}", file=sys.stderr)
    if not report.ok:
        print("CORS may have changed. Inspect the named bucket before retrying. Existing Native API CORS presets require separate owner handling.", file=sys.stderr)
        return 2
    print("S3 CORS updated and read back successfully on the named bucket. Revoke the setup key now.")
    print("Browser upload/preview CORS still requires an actual browser test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
