import io
from urllib.parse import urlparse, parse_qs
from botocore.response import StreamingBody
from botocore.stub import Stubber, ANY
import pytest
from backend import settings, storage
from backend import history, preferences
from backend.security import read_file_token


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "content-tools-test")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "testing-secret")
    monkeypatch.setenv("SESSION_SECRET", "testing-session-secret-at-least-32-characters")
    storage.s3.cache_clear()
    yield storage.s3()
    storage.s3.cache_clear()


def test_large_upload_uses_direct_signed_url(cloud):
    # No external network: exercise real boto3 signing, then assert scope/expiry.
    result = storage.new_upload("large.png", 6 * 1024 * 1024, "image/png")
    url = urlparse(result["upload_url"])
    args = parse_qs(url.query)
    assert url.netloc == "example.r2.cloudflarestorage.com"
    assert url.path.startswith("/content-tools-test/temporary/")
    assert args["X-Amz-Expires"] == ["600"]
    assert "content-length" in args["X-Amz-SignedHeaders"][0]
    assert "content-type" in args["X-Amz-SignedHeaders"][0]
    assert "testing-secret" not in result["upload_url"]
    assert result["method"] == "PUT"


@pytest.mark.parametrize("endpoint,region", [
    ("https://example.r2.cloudflarestorage.com", "auto"),
    ("https://s3.eu-central-003.backblazeb2.com", "eu-central-003"),
])
def test_provider_signing_uses_exact_endpoint_region_and_path_style(cloud, monkeypatch, endpoint, region):
    # Real local SDK signing; no network and no claim of live provider validation.
    monkeypatch.setenv("S3_ENDPOINT_URL", endpoint)
    monkeypatch.setenv("S3_REGION", region)
    storage.s3.cache_clear()
    result = storage.new_upload("custom.png", 6 * 1024 * 1024, "image/png")
    url = urlparse(result["upload_url"])
    args = parse_qs(url.query)
    assert url.netloc == urlparse(endpoint).netloc
    assert url.path.startswith("/content-tools-test/temporary/")
    assert f"/{region}/s3/aws4_request" in args["X-Amz-Credential"][0]
    assert args["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "content-type" in args["X-Amz-SignedHeaders"][0]
    assert "content-length" in args["X-Amz-SignedHeaders"][0]
    assert not any("checksum" in key.lower() for key in args)
    client_config = storage.s3().meta.config
    assert client_config.request_checksum_calculation == "when_required"
    assert client_config.response_checksum_validation == "when_required"


def test_cloud_preferences_and_persistent_library_contract(cloud):
    import json
    value = {"sds_title_template": "Product {VALUE}", "cars": {}}
    raw = json.dumps(value, ensure_ascii=False).encode()
    with Stubber(cloud) as stub:
        stub.add_client_error("get_object", "NoSuchKey", http_status_code=404,
            expected_params={"Bucket": "content-tools-test", "Key": preferences.KEY})
        assert preferences.load() == {}
        stub.add_response("put_object", {}, {"Bucket": "content-tools-test", "Key": preferences.KEY,
            "Body": raw, "ContentType": "application/json"})
        preferences.save(value)
        stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(raw), len(raw)), "ContentLength": len(raw)},
            {"Bucket": "content-tools-test", "Key": preferences.KEY})
        assert preferences.load() == value
        key = "library/files/test/template.png"
        stub.add_response("put_object", {}, {"Bucket": "content-tools-test", "Key": key,
            "Body": b"image-data", "ContentType": "image/png"})
        descriptor = preferences.store_file(b"image-data", "template.png", "image/png", "test")
        assert descriptor["key"] == key and not descriptor["writable"]
        stub.add_response("delete_object", {}, {"Bucket": "content-tools-test", "Key": key})
        preferences.remove_files([descriptor])
        stub.assert_no_pending_responses()


def test_cloud_generation_read_and_delete_contract(cloud):
    data = b"example-generated-file"
    with Stubber(cloud) as stub:
        stub.add_response("put_object", {}, {"Bucket": "content-tools-test", "Key": ANY, "Body": data, "ContentType": "image/png"})
        ref = storage.save_bytes(data, "result.png", "image/png")
        key = read_file_token(ref["token"])["key"]
        stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(data), len(data)), "ContentLength": len(data)}, {"Bucket": "content-tools-test", "Key": key})
        assert storage.read_bytes(ref["token"]) == data
        stub.add_response("delete_object", {}, {"Bucket": "content-tools-test", "Key": key})
        storage.delete_file(ref["token"])
        stub.assert_no_pending_responses()


def test_cloud_history_copy_manifest_and_listing(cloud, monkeypatch):
    import json
    monkeypatch.setattr(history, "now", lambda: 1_800_000_000)
    class FixedId:
        hex = "a" * 32
    monkeypatch.setattr(history.uuid, "uuid4", lambda: FixedId())
    entry_id = f"{9999999999999999 - 1_800_000_000_000:016d}-" + "a" * 32
    source = storage.make_ref("temporary/source/image.png", "image.png", 10, "image/png")
    key = f"history/files/{entry_id}/result/image.png"
    manifest_key = history.PREFIX + entry_id + ".json"
    with Stubber(cloud) as stub:
        stub.add_response("copy_object", {}, {"Bucket": "content-tools-test", "Key": key,
            "CopySource": {"Bucket": "content-tools-test", "Key": "temporary/source/image.png"}})
        stub.add_response("put_object", {}, {"Bucket": "content-tools-test", "Key": manifest_key,
            "Body": ANY, "ContentType": "application/json"})
        result = history.record("png", "A", {"file": source}, {})
        assert result["history_id"] == entry_id
        manifest = dict(id=entry_id, created_at=history.now(), expires_at=history.now() + 30 * 86400,
            kind="png", label="A", detail="", metadata={}, warnings=[], preview=None,
            file=dict(key=key, name="image.png", size=10, content_type="image/png", writable=False))
        raw = json.dumps(manifest).encode()
        stub.add_response("list_objects_v2", {"Contents": [{"Key": manifest_key}]},
            {"Bucket": "content-tools-test", "Prefix": history.PREFIX, "MaxKeys": 31})
        stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(raw), len(raw))},
            {"Bucket": "content-tools-test", "Key": manifest_key})
        page = history.listing()
        assert page["items"][0]["id"] == entry_id and page["next_cursor"] is None
        assert read_file_token(page["items"][0]["file"]["token"])["key"] == key
        stub.assert_no_pending_responses()


def test_cloud_batch_members_listing_follows_all_s3_pages(cloud):
    first = history.BATCH_PREFIX + "123e4567-e89b-42d3-a456-426614174000/first.json"
    second = history.BATCH_PREFIX + "123e4567-e89b-42d3-a456-426614174000/second.json"
    with Stubber(cloud) as stub:
        stub.add_response("list_objects_v2", {"Contents": [{"Key": first}], "IsTruncated": True, "NextContinuationToken": "next-page"},
            {"Bucket": "content-tools-test", "Prefix": history.BATCH_PREFIX, "MaxKeys": 1000})
        stub.add_response("list_objects_v2", {"Contents": [{"Key": second}], "IsTruncated": False},
            {"Bucket": "content-tools-test", "Prefix": history.BATCH_PREFIX, "MaxKeys": 1000, "ContinuationToken": "next-page"})
        assert history.object_keys(history.BATCH_PREFIX) == [first, second]
        stub.assert_no_pending_responses()
