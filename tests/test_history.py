import pytest
import uuid
import json
from fastapi.testclient import TestClient
from backend.app import app
from backend import history, storage, settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    with TestClient(app) as client:
        yield client


def make_record(label="A", batch=None):
    file = storage.save_bytes(b"generated-png-content", f"{label}.png", "image/png")
    preview = storage.save_bytes(b"preview", "preview.png", "image/png")
    result = history.record("png", label, {"file": file, "preview": preview}, {"hex": "#112233"}, batch=batch)
    return result, file


def test_history_survives_temporary_deletion_and_new_session(client, monkeypatch):
    result, temporary = make_record()
    storage.delete_file(temporary["token"])
    # Original 24h file token is irrelevant: manifests contain descriptors, not tokens.
    monkeypatch.setattr(history, "now", lambda: __import__("time").time() + 2 * 86400)
    from itsdangerous.timed import TimestampSigner
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: int(history.now()))
    assert client.get(result["file"]["url"]).status_code == 400
    with TestClient(app) as second:
        listing = second.get("/api/history").json()
        assert len(listing["items"]) == 1
        row = listing["items"][0]
        assert row["id"] == result["history_id"] and row["kind"] == "png"
        assert second.get(row["file"]["url"]).content == b"generated-png-content"
        assert second.get(row["preview"]["url"]).content == b"preview"
        assert second.get("/api/history/" + row["id"]).status_code == 200
        assert second.request("DELETE", "/api/files", json={"token": row["file"]["token"]}).status_code == 400


def test_history_pagination_retention_and_auth(client, monkeypatch):
    base = history.now()
    ids = []
    for index in range(3):
        monkeypatch.setattr(history, "now", lambda i=index: base + i)
        ids.append(make_record(str(index))[0]["history_id"])
    page = client.get("/api/history?limit=2").json()
    assert [i["label"] for i in page["items"]] == ["2", "1"]
    more = client.get("/api/history", params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [i["label"] for i in more["items"]] == ["0"]
    assert more["next_cursor"] is None
    assert client.get("/api/history?limit=1000").status_code == 422
    assert client.get("/api/history?cursor=../../bad").status_code == 400
    assert client.get("/api/history/bad").status_code == 400
    monkeypatch.setattr(history, "now", lambda: base + 31 * 86400)
    assert client.get("/api/history").json()["items"] == []
    assert client.get("/api/history/" + ids[0]).status_code == 410
    monkeypatch.setenv("APP_PASSWORD", "long-history-password")
    assert client.get("/api/history").status_code == 401
    assert client.get("/api/history/" + ids[0]).status_code == 401


def test_generation_records_history_automatically(client):
    result = client.post("/api/png/render", json={"template_id": "demo", "color": {"code": "AUTO", "hex": "#557788"}})
    assert result.status_code == 200, result.text
    entries = client.get("/api/history").json()["items"]
    assert len(entries) == 1 and entries[0]["id"] == result.json()["history_id"]
    assert entries[0]["metadata"]["color"]["code"] == "AUTO"
    assert client.get(entries[0]["file"]["url"]).content[:8] == b"\x89PNG\r\n\x1a\n"


def test_batch_grouping_paginates_whole_lists_not_files(client, monkeypatch):
    base = history.now()
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    for index in range(51):
        monkeypatch.setattr(history, "now", lambda i=index: base + i)
        make_record(f"FIRST-{index}", dict(batch_id=first, batch_index=index, batch_size=51,
            zip_path=f"Folder/FIRST-{index}.png", last_written_at="2020-01-02T03:04:05+02:00"))
    monkeypatch.setattr(history, "now", lambda: base + 60)
    make_record("SECOND-1", dict(batch_id=second, batch_index=0, batch_size=2))
    make_record("SECOND-2", dict(batch_id=second, batch_index=1, batch_size=2))
    # Unbatched legacy records made at the exact same time are NOT combined.
    legacy_a = make_record("legacy-A")[0]["history_id"]
    legacy_b = make_record("legacy-B")[0]["history_id"]
    cursor, groups = None, []
    while True:
        page = client.get("/api/history/batches", params={"limit": 1, **({"cursor": cursor} if cursor else {})}).json()
        groups.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(groups) == 4
    assert {group["id"] for group in groups} == {first, second, legacy_a, legacy_b}
    first_row = next(group for group in groups if group["id"] == first)
    assert first_row["count"] == first_row["expected_count"] == 51 and not first_row["partial"]
    assert len(first_row["items"]) == 3 and first_row["total_size"] == 51 * len(b"generated-png-content")
    assert "FIRST-50" in first_row["search_text"]
    detail = client.get("/api/history/batches/" + first).json()
    assert len(detail["items"]) == 51 and detail["items"][50]["label"] == "FIRST-50"
    assert detail["items"][0]["zip_path"] == "Folder/FIRST-0.png"
    assert detail["items"][0]["last_written_at"] == "2020-01-02T03:04:05+02:00"
    assert client.get(detail["items"][50]["file"]["url"]).content == b"generated-png-content"


def test_partial_batch_metadata_refresh_retention_and_validation(client, monkeypatch):
    batch_id = str(uuid.uuid4())
    response = client.post("/api/png/render", json={"template_id": "demo", "color": {"code": "DATE", "hex": "#123456"},
        "batch_id": batch_id, "batch_index": 0, "batch_size": 2, "zip_path": "Model/DATE.png", "last_written_at": "2024-02-03T04:05:06Z"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["last_written_at"] == "2024-02-03T04:05:06+00:00"
    assert result["batch_id"] == batch_id and result["zip_path"] == "Model/DATE.png"
    group = client.get("/api/history/batches/" + batch_id).json()
    assert group["partial"] and group["count"] == 1 and group["expected_count"] == 2
    entry = group["items"][0]
    assert entry["metadata"]["last_written_at"] == result["last_written_at"]
    base = history.now()
    monkeypatch.setattr(history, "now", lambda: base + 2 * 86400)
    from itsdangerous.timed import TimestampSigner
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: int(history.now()))
    refreshed = client.get("/api/history/batches/" + batch_id).json()
    assert client.get(refreshed["items"][0]["file"]["url"]).status_code == 200
    assert client.get(result["file"]["url"]).status_code == 400
    monkeypatch.setattr(history, "now", lambda: base + 31 * 86400)
    assert client.get("/api/history/batches").json()["items"] == []
    assert client.get("/api/history/batches/" + batch_id).status_code == 410
    assert client.get("/api/history/batches/not-a-uuid").status_code == 400
    assert client.get("/api/history/batches?cursor=../../bad").status_code == 400
    assert client.get("/api/history/batches?limit=1000").status_code == 422
    monkeypatch.setenv("APP_PASSWORD", "long-history-password")
    assert client.get("/api/history/batches").status_code == 401
    assert client.get("/api/history/batches/" + batch_id).status_code == 401


@pytest.mark.parametrize("field,value", [
    ("last_written_at", "bad"), ("last_written_at", "2024-01-01T00:00:00"),
    ("last_written_at", "1979-01-01T00:00:00Z"), ("last_written_at", "2108-01-01T00:00:00Z"),
    ("zip_path", "../x.png"), ("zip_path", "/x.png"), ("zip_path", "C:\\x.png"),
    ("zip_path", "x//y.png"), ("batch_id", "not-a-uuid"), ("batch_index", 200), ("batch_size", 201),
])
def test_invalid_result_metadata_is_rejected_for_all_generators(field, value):
    from pydantic import ValidationError
    from backend.models import PngRender, SDSGenerate, Edit
    for model, payload in (
        (PngRender, {"template_id": "demo", "color": {"code": "X", "hex": "#123456"}}),
        (SDSGenerate, {"item": {"code": "X", "brand": "Test", "color": "White", "title": "Test"}}),
        (Edit, {"file_token": "file", "search_text": "Old", "replacement": "New"}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**payload, field: value})


def test_batch_listing_fast_path_reads_no_individual_manifests(client, monkeypatch):
    batch_id = str(uuid.uuid4())
    for index in range(6):
        make_record(f"CACHE-{index}", dict(batch_id=batch_id, batch_index=index, batch_size=8))
    def forbidden_read(_entry_id):
        raise AssertionError("Fast batch listing must not GET per-result manifests")
    monkeypatch.setattr(history, "read_manifest", forbidden_read)
    page = client.get("/api/history/batches").json()
    group = page["items"][0]
    assert group["count"] == 6 and group["partial"] and group["expected_count"] == 8
    assert len(group["items"]) == 3 and "CACHE-5" in group["search_text"]
    assert group["total_size"] == 6 * len(b"generated-png-content")
    assert client.get(group["items"][2]["file"]["url"]).content == b"generated-png-content"


def test_stale_concurrent_snapshot_falls_back_and_next_write_repairs(client, monkeypatch):
    batch_id = str(uuid.uuid4())
    make_record("FIRST", dict(batch_id=batch_id, batch_index=0, batch_size=3))
    first_keys = history.object_keys(history.BATCH_PREFIX + batch_id + "/")
    first_snapshot = history.read_json(first_keys[0])
    make_record("SECOND", dict(batch_id=batch_id, batch_index=1, batch_size=3))
    keys = history.object_keys(history.BATCH_PREFIX + batch_id + "/")
    # Simulate a simultaneous writer that had only seen the earlier membership.
    newest = history.latest_member_key(keys)
    storage.save_bytes(json.dumps(first_snapshot).encode(), "member.json", "application/json", asset_key=newest)
    original_read, calls = history.read_manifest, []
    def counted_read(entry_id):
        calls.append(entry_id)
        return original_read(entry_id)
    monkeypatch.setattr(history, "read_manifest", counted_read)
    group = client.get("/api/history/batches").json()["items"][0]
    assert group["count"] == 2 and group["partial"] and group["total_size"] == 2 * len(b"generated-png-content")
    assert "SECOND" in group["search_text"] and len(calls) == 2
    make_record("THIRD", dict(batch_id=batch_id, batch_index=2, batch_size=3))
    calls.clear()
    repaired = client.get("/api/history/batches").json()["items"][0]
    assert repaired["count"] == 3 and not repaired["partial"] and calls == []
    assert "THIRD" in repaired["search_text"]


def test_batch_id_cannot_be_reused_for_other_kind_or_list_size(client):
    batch_id = str(uuid.uuid4())
    make_record("FIRST", dict(batch_id=batch_id, batch_index=0, batch_size=2))
    file = storage.save_bytes(b"pdf-output", "other.pdf", "application/pdf")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as conflict:
        history.record("sds", "OTHER", {"file": file}, {}, batch=dict(batch_id=batch_id, batch_index=1, batch_size=2))
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as conflict:
        make_record("OTHER", dict(batch_id=batch_id, batch_index=1, batch_size=3))
    assert conflict.value.status_code == 409


def test_human_template_names_used_for_history_group_labels(client):
    response = client.post("/api/png/render", json={"template_id": "demo", "color": {"code": "HUMAN", "hex": "#123456"},
        "batch_id": str(uuid.uuid4()), "batch_size": 1, "batch_index": 0})
    assert response.status_code == 200
    group = client.get("/api/history/batches").json()["items"][0]
    assert group["label"] == "Фото · Демо-шаблон"
    assert group["items"][0]["metadata"]["template_name"] == "Демо-шаблон"


def test_long_sds_titles_do_not_remove_last_member_from_search_index():
    batch_id = str(uuid.uuid4())
    members = [dict(id=str(index), label=f"CODE-{index:03d}", file={"name": f"CODE-{index:03d}-product.pdf", "size": 100},
        kind="sds", created_at=1000 + index, expires_at=1000000 + index,
        metadata={"item": {"title": "Long product title " * 100, "brand": "Test"}}, batch_index=index, batch_size=200)
        for index in range(200)]
    summary = history.summarize(batch_id, members, preview_limit=3)
    assert len(summary["search_text"]) <= 100_000
    assert "CODE-199" in summary["search_text"] and "CODE-199-product.pdf" in summary["search_text"]
