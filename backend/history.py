"""Single-owner durable history. One immutable manifest per result; no shared index races."""
import json
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi import HTTPException
from botocore.exceptions import ClientError
from . import settings, storage, security

RETENTION_DAYS = 30
PREFIX = "history/index/"
BATCH_PREFIX = "history/batch-members/"
SNAPSHOT_MAX_BYTES = 512_000
ID = re.compile(r"^[0-9]{16}-[0-9a-f]{32}$")


def now():
    return time.time()


def archive_ref(ref, entry_id, role):
    value = security.read_file_token(ref["token"])
    key = f"history/files/{entry_id}/{role}/{value['name']}"
    if settings.storage_mode() == "s3":
        storage.s3().copy_object(Bucket=storage.bucket(), Key=key,
            CopySource={"Bucket": storage.bucket(), "Key": value["key"]})
    else:
        path = storage.local_root() / key
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(storage.local_root() / value["key"], path)
    return dict(key=key, name=value["name"], size=value["size"],
                content_type=value["content_type"], writable=False)


def record(kind, label, result, metadata, detail="", batch=None):
    created = now()
    batch = dict(batch or {})
    batch.setdefault("last_written_at", datetime.fromtimestamp(created, timezone.utc).isoformat())
    batch_id = batch.get("batch_id")
    if batch_id:
        # Membership objects are immutable. There is no counter or shared index
        # that concurrent requests could overwrite.
        existing = object_keys(BATCH_PREFIX + batch_id + "/")
        if existing:
            first_id = member_entry_id(existing[0])
            first = read_manifest(first_id)
            if first["kind"] != kind or first.get("batch_size") != batch.get("batch_size"):
                raise HTTPException(409, "Ідентифікатор пакета вже використаний для іншого списку.")
        if len(existing) >= 200:
            raise HTTPException(422, "Максимум 200 результатів у пакеті.")
    # Lexical S3 listing naturally returns newest entries first.
    entry_id = f"{9999999999999999 - int(created * 1000):016d}-{uuid.uuid4().hex}"
    file = archive_ref(result["file"], entry_id, "result")
    preview_ref = result.get("preview") or next(iter(result.get("previews", [])), None)
    preview = archive_ref(preview_ref, entry_id, "preview") if preview_ref else None
    manifest = dict(id=entry_id, created_at=created, expires_at=created + RETENTION_DAYS * 86400,
                    kind=kind, label=label, detail=detail, file=file, preview=preview,
                    metadata={**metadata, **batch}, warnings=result.get("warnings", []), **batch)
    # Commit the completed result only after all output files have been copied.
    payload = json.dumps(manifest, ensure_ascii=False).encode()
    storage.save_bytes(payload, "record.json", "application/json", asset_key=PREFIX + entry_id + ".json")
    if batch_id:
        snapshot = build_snapshot(batch_id, existing, manifest)
        snapshot_bytes = json.dumps(snapshot, ensure_ascii=False).encode()
        if len(snapshot_bytes) > SNAPSHOT_MAX_BYTES:
            raise HTTPException(500, "Пакет перевищив ліміт обсягу індексу історії.")
        storage.save_bytes(snapshot_bytes, "member.json", "application/json",
            asset_key=f"{BATCH_PREFIX}{batch_id}/{batch.get('batch_index', 0):03d}-{entry_id}.json")
    result["file"] = storage.make_ref(**file)
    if preview:
        if "preview" in result:
            result["preview"] = storage.make_ref(**preview)
        else:
            result["previews"][0] = storage.make_ref(**preview)
    result["history_id"] = entry_id
    result.update(batch)
    return result


def read_manifest(entry_id):
    if not ID.fullmatch(entry_id):
        raise HTTPException(400, "Некоректний запис історії.")
    value = read_json(PREFIX + entry_id + ".json", max_bytes=100_000)
    if value["expires_at"] <= now():
        raise HTTPException(410, "Строк зберігання результату завершився (30 днів).")
    return value


def read_json(key, max_bytes=SNAPSHOT_MAX_BYTES):
    if settings.storage_mode() == "s3":
        try:
            obj = storage.s3().get_object(Bucket=storage.bucket(), Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                raise HTTPException(404, "Запис історії не знайдено.") from None
            raise
        try:
            raw = obj["Body"].read(max_bytes + 1)
        finally:
            obj["Body"].close()
    else:
        path = storage.local_root() / key
        if not path.is_file():
            raise HTTPException(404, "Запис історії не знайдено.")
        if path.stat().st_size > max_bytes:
            raise HTTPException(500, "Пошкоджений запис історії.")
        raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise HTTPException(500, "Пошкоджений запис історії.")
    return json.loads(raw)


def present(value):
    return {**value, "file": storage.make_ref(**value["file"]),
            "preview": storage.make_ref(**value["preview"]) if value["preview"] else None}


def listing(cursor=None, limit=30):
    if cursor and not ID.fullmatch(cursor):
        raise HTTPException(400, "Некоректний курсор історії.")
    if settings.storage_mode() == "s3":
        args = dict(Bucket=storage.bucket(), Prefix=PREFIX, MaxKeys=limit + 1)
        if cursor:
            args["StartAfter"] = PREFIX + cursor + ".json"
        response = storage.s3().list_objects_v2(**args)
        ids = [o["Key"][len(PREFIX):-5] for o in response.get("Contents", [])]
    else:
        root = storage.local_root() / PREFIX
        ids = sorted(p.stem for p in root.glob("*.json") if not cursor or p.stem > cursor)[:limit + 1]
    items = []
    for entry_id in ids[:limit]:
        try:
            items.append(present(read_manifest(entry_id)))
        except HTTPException as exc:
            if exc.status_code not in (404, 410):
                raise
    return dict(items=items, next_cursor=ids[limit - 1] if len(ids) > limit else None,
                retention_days=RETENTION_DAYS)


def object_keys(prefix):
    """List keys, not every full manifest. S3 continuation pages are mandatory."""
    if settings.storage_mode() != "s3":
        root = storage.local_root()
        return sorted(path.relative_to(root).as_posix() for path in (root / prefix).rglob("*.json"))
    keys, continuation = [], None
    while True:
        args = dict(Bucket=storage.bucket(), Prefix=prefix, MaxKeys=1000)
        if continuation:
            args["ContinuationToken"] = continuation
        page = storage.s3().list_objects_v2(**args)
        keys.extend(item["Key"] for item in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        continuation = page["NextContinuationToken"]


def batch_ids(member_keys=None):
    """Legacy results are individual batches; timestamps never imply grouping."""
    all_ids = {key[len(PREFIX):-5] for key in object_keys(PREFIX)
               if ID.fullmatch(key[len(PREFIX):-5])}
    groups, assigned = {}, set()
    for key in object_keys(BATCH_PREFIX) if member_keys is None else member_keys:
        parts = key[len(BATCH_PREFIX):].split("/")
        if len(parts) != 2:
            continue
        batch_id, filename = parts
        entry_id = member_entry_id(key)
        if entry_id in all_ids:
            groups.setdefault(batch_id, []).append(entry_id)
            assigned.add(entry_id)
    groups.update({entry_id: [entry_id] for entry_id in all_ids - assigned})
    return groups


def member_entry_id(key):
    # Also accepts the original unprefixed marker filenames, for migration.
    return key.rsplit("/", 1)[-1][:-5][-49:]


def latest_member_key(keys):
    def order(key):
        filename = key.rsplit("/", 1)[-1]
        index = int(filename[:3]) if re.match(r"^[0-9]{3}-[0-9]{16}-", filename) else 0
        return index, -int(member_entry_id(key)[:16]), filename
    return max(keys, key=order)


def read_snapshot(keys):
    if not keys:
        return None
    try:
        value = read_json(latest_member_key(keys))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return None
    expected = {member_entry_id(key) for key in keys}
    # Concurrent writes may create competing snapshots. A stale one is never
    # authoritative: the exact membership check forces a correct fallback.
    if (set(value.get("member_ids", [])) != expected or not value.get("summary")
            or value["summary"].get("count") != len(expected)
            or not 1 <= len(expected) <= 200):
        return None
    return value


def search_value(value):
    # Bound EACH member, not the whole list: long SDS titles must not crowd
    # later color codes/files out of the search index (200 * 500 <= 100 KB chars).
    return " ".join(str(part) for part in (
        value["label"], value["file"]["name"], value.get("zip_path", ""),
        *value["metadata"].get("item", {}).values(),
        *value["metadata"].get("color", {}).values()))[:499]


def build_snapshot(batch_id, existing_keys, manifest):
    saved = read_snapshot(existing_keys)
    if saved:
        previous = saved["summary"]
        summary = summarize(batch_id, [*previous["items"], manifest], preview_limit=3)
        summary.update(count=previous["count"] + 1,
            expected_count=max(previous["expected_count"], manifest.get("batch_size", 1)),
            total_size=previous["total_size"] + manifest["file"]["size"],
            created_at=min(previous["created_at"], manifest["created_at"]),
            expires_at=min(previous["expires_at"], manifest["expires_at"]),
            search_text=(previous["search_text"] + "\n" + search_value(manifest))[:100_000])
        summary["partial"] = summary["count"] < summary["expected_count"]
    else:
        # Old empty markers and simultaneous writes use the immutable source
        # manifests. The next sequential write repairs the fast summary path.
        previous_values = read_members([member_entry_id(key) for key in existing_keys])
        summary = summarize(batch_id, [*previous_values, manifest], preview_limit=3)
    return {"member_ids": sorted({member_entry_id(key) for key in existing_keys} | {manifest["id"]}), "summary": summary}


def valid_batch_id(batch_id):
    if ID.fullmatch(batch_id):
        return
    try:
        if str(uuid.UUID(batch_id)) == batch_id:
            return
    except ValueError:
        pass
    raise HTTPException(400, "Некоректний ідентифікатор пакета.")


def read_members(entry_ids):
    def read(entry_id):
        try:
            return read_manifest(entry_id)
        except HTTPException as exc:
            if exc.status_code not in (404, 410):
                raise
            return None
    if settings.storage_mode() == "s3" and len(entry_ids) > 1:
        # No MuPDF/image operations here. The shared boto3 client is thread-safe.
        with ThreadPoolExecutor(max_workers=8) as pool:
            return [value for value in pool.map(read, entry_ids) if value]
    return [value for entry_id in entry_ids if (value := read(entry_id))]


def summarize(batch_id, values, preview_limit=None):
    if not values:
        raise HTTPException(410, "Пакет відсутній або строк зберігання завершився (30 днів).")
    values.sort(key=lambda value: (value.get("batch_index", 0), value["created_at"], value["id"]))
    first = values[0]
    kind = first["kind"]
    title = {"png": "Фото", "sds": "SDS", "edit": "Редагування PDF"}.get(kind, kind)
    model = (first["metadata"].get("template_name") or first["metadata"].get("template")) if kind == "png" else first["metadata"].get("item", {}).get("brand")
    label = f"{title} · {model}" if model and model != "custom" else title
    expected = max(value.get("batch_size", 1) for value in values)
    search_text = "\n".join(search_value(value) for value in values)[:100_000]
    return dict(id=batch_id, kind=kind, label=label, created_at=min(value["created_at"] for value in values),
        expires_at=min(value["expires_at"] for value in values), count=len(values), expected_count=expected,
        partial=len(values) < expected, search_text=search_text, total_size=sum(value["file"]["size"] for value in values),
        items=values if preview_limit is None else values[:preview_limit])


def present_batch(summary):
    return {**summary, "items": [present(value) for value in summary["items"]]}


def batch_summary(batch_id, entry_ids, preview_limit=None):
    return present_batch(summarize(batch_id, read_members(entry_ids), preview_limit))


def batch_entry(batch_id):
    valid_batch_id(batch_id)
    if ID.fullmatch(batch_id):
        # A legacy result id is always one standalone result, including direct links.
        return batch_summary(batch_id, [batch_id])
    keys = object_keys(BATCH_PREFIX + batch_id + "/")
    if not keys:
        raise HTTPException(404, "Пакет історії не знайдено.")
    return batch_summary(batch_id, [member_entry_id(key) for key in keys])


def batch_listing(cursor=None, limit=30):
    if cursor and not ID.fullmatch(cursor):
        raise HTTPException(400, "Некоректний курсор історії.")
    # Group order is the first successfully produced member, stable during generation.
    member_keys = object_keys(BATCH_PREFIX)
    keys_by_batch = {}
    for key in member_keys:
        parts = key[len(BATCH_PREFIX):].split("/")
        if len(parts) == 2:
            keys_by_batch.setdefault(parts[0], []).append(key)
    groups = sorted(((max(ids), batch_id, ids) for batch_id, ids in batch_ids(member_keys).items()), key=lambda group: group[0])
    items, page_cursor = [], None
    for key, batch_id, ids in groups:
        if cursor and key <= cursor:
            continue
        try:
            if not ID.fullmatch(batch_id):
                # One small object GET per batch on the normal sequential path,
                # never one manifest GET per generated file.
                snapshot = read_snapshot(keys_by_batch.get(batch_id, []))
                if snapshot and snapshot["summary"]["expires_at"] > now():
                    summary = present_batch(snapshot["summary"])
                else:
                    summary = batch_summary(batch_id, ids, preview_limit=3)
            else:
                summary = batch_summary(batch_id, ids, preview_limit=3)
        except HTTPException as exc:
            if exc.status_code in (404, 410):
                continue
            raise
        if len(items) == limit:
            return dict(items=items, next_cursor=page_cursor, retention_days=RETENTION_DAYS)
        items.append(summary)
        page_cursor = key
    return dict(items=items, next_cursor=None, retention_days=RETENTION_DAYS)
