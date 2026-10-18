import io
import time
import pytest
import pymupdf as fitz
from PIL import Image
from fastapi.testclient import TestClient
from backend.app import app
from backend import storage, settings, sds, preferences


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    with TestClient(app) as client:
        yield client


def png_bytes(color="#1E9E52", size=(100, 80)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def create_car(client, **overrides):
    image = storage.save_bytes(png_bytes(), "test.png", "image/png")
    body = dict(name="Test car", image_token=image["token"], config={"key_color": "#1E9E52", "brand_name": "Test"})
    response = client.post("/api/settings/templates", json={**body, **overrides})
    return response, image


def test_title_validation_and_restart(client):
    title = "Product {BRAND} ({CODE} {COLOR})"
    response = client.put("/api/settings/sds/title", json={"title_template": title})
    assert response.status_code == 200, response.text
    for invalid in ("No placeholders", "{UNKNOWN}", " "):
        assert client.put("/api/settings/sds/title", json={"title_template": invalid}).status_code == 422
    with TestClient(app) as second:
        assert second.get("/api/settings").json()["sds"]["title_template"] == title
        assert second.get("/api/sds/reference").json()["title_template"] == title


def test_pdf_default_is_durable_validated_used_and_reset(client, monkeypatch):
    source = sds.REFERENCE.read_bytes()
    # A harmless metadata marker proves generation uses the selected PDF, not the bundled one.
    with fitz.open(stream=source, filetype="pdf") as doc:
        doc.set_metadata({**doc.metadata, "subject": "saved-default-marker"})
        source = doc.tobytes()
    upload = storage.save_bytes(source, "my-reference.pdf", "application/pdf")
    response = client.put("/api/settings/sds/reference", json={"token": upload["token"]})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["custom"] and saved["file"]["name"] == "my-reference.pdf"
    storage.delete_file(upload["token"])
    with fitz.open() as doc:
        doc.new_page()
        invalid = storage.save_bytes(doc.tobytes(), "wrong.pdf", "application/pdf")
    assert client.put("/api/settings/sds/reference", json={"token": invalid["token"]}).status_code == 422
    assert client.get("/api/settings").json()["sds"]["custom"]
    from itsdangerous.timed import TimestampSigner
    future = int(time.time() + 2 * 86400)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: future)
    with TestClient(app) as second:
        refreshed = second.get("/api/sds/reference").json()
        assert second.get(refreshed["file"]["url"]).content == source
        result = second.post("/api/sds/generate", json={"item": {"code": "X", "brand": "Test", "color": "Green", "title": "Test product"}, "render_previews": False})
        assert result.status_code == 200, result.text
        with fitz.open(stream=second.get(result.json()["file"]["url"]).content, filetype="pdf") as output:
            assert output.metadata["subject"] == "saved-default-marker"
        removed = second.delete("/api/settings/sds/reference").json()
        assert removed["custom"] is False and removed["file"] is None and not removed["available"]
        assert second.get(refreshed["file"]["url"]).status_code == 404
        assert len(second.get("/api/history").json()["items"]) == 1


def test_car_survives_expired_upload_and_renders_without_key_input(client, monkeypatch):
    response, upload = create_car(client)
    assert response.status_code == 200, response.text
    car = response.json()
    storage.delete_file(upload["token"])
    from itsdangerous.timed import TimestampSigner
    future = int(time.time() + 2 * 86400)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: future)
    with TestClient(app) as second:
        templates = second.get("/api/png/templates").json()["templates"]
        saved = next(t for t in templates if t["id"] == car["id"])
        assert not saved["builtin"] and saved["config"]["key_color"] == "#1E9E52"
        assert second.get(saved["image"]["url"]).content[:8] == b"\x89PNG\r\n\x1a\n"
        mask = second.post("/api/png/mask", json={"template_id": car["id"], "backend": "chroma"})
        assert mask.status_code == 200, mask.text
        rendered = second.post("/api/png/render", json={"template_id": car["id"], "color": {"code": "AUTO", "hex": "#123456"}, "draw_plate": False, "config": {"brand_name": "Changed"}})
        assert rendered.status_code == 200, rendered.text
        assert second.put(f"/api/settings/templates/{car['id']}/key", json={"key_color": "#20A050"}).status_code == 200
        assert second.put(f"/api/settings/templates/{car['id']}/key", json={"key_color": "#ZZZZZZ"}).status_code == 422
        assert second.put(f"/api/settings/templates/{car['id']}/key", json={"key_color": None}).status_code == 422
        assert second.delete(f"/api/settings/templates/{car['id']}").status_code == 200
        assert all(t["id"] != car["id"] for t in second.get("/api/png/templates").json()["templates"])
        assert second.get(saved["image"]["url"]).status_code == 404
        assert second.post("/api/png/render", json={"template_id": car["id"], "color": {"code": "X", "hex": "#112233"}}).status_code == 404
        # Deleting a source template must not delete the historical output.
        assert second.get(rendered.json()["file"]["url"]).status_code == 200


def test_masks_dimensions_builtin_keys_and_auth(client, monkeypatch):
    invalid_mask = storage.save_bytes(png_bytes("white", (1, 1)), "mask.png", "image/png")
    response, _ = create_car(client, car_mask_token=invalid_mask["token"])
    assert response.status_code == 422
    assert len(client.get("/api/png/templates").json()["templates"]) == 4
    assert client.put("/api/settings/templates/kuga/key", json={"key_color": "#123456"}).status_code == 200
    builtin = client.get("/api/png/templates").json()["templates"][0]
    assert builtin["config"]["key_color"] == "#123456" and builtin["car_mask"]
    assert client.delete("/api/settings/templates/kuga").status_code == 200
    assert all(t["id"] != "kuga" for t in client.get("/api/png/templates").json()["templates"])
    assert client.delete("/api/settings/templates/../../bad").status_code != 200
    monkeypatch.setenv("APP_PASSWORD", "long-preferences-password")
    assert client.get("/api/settings").status_code == 401
    assert client.put("/api/settings/sds/title", json={"title_template": "{VALUE}"}).status_code == 401
    assert client.delete("/api/settings/sds/reference").status_code == 401
    assert client.post("/api/settings/templates", json={"name": "x", "image_token": "bad", "config": {}}).status_code == 401


def test_library_with_mask_needs_no_key(client):
    mask = storage.save_bytes(png_bytes("white"), "mask.png", "image/png")
    response, _ = create_car(client, car_mask_token=mask["token"], config={"brand_name": "Test"})
    assert response.status_code == 200, response.text
    car = response.json()
    assert car["car_mask"] and car["config"]["key_color"] is None
    result = client.post("/api/png/render", json={"template_id": car["id"], "color": {"code": "MASK", "hex": "#334455"}, "draw_plate": False})
    assert result.status_code == 200, result.text


def test_builtin_templates_can_be_deleted_restored_without_deleting_history(client):
    rendered = client.post("/api/png/render", json={"template_id": "demo", "color": {"code": "KEEP", "hex": "#456789"}})
    assert rendered.status_code == 200
    source = next(t for t in client.get("/api/png/templates").json()["templates"] if t["id"] == "demo")
    assert client.delete("/api/settings/templates/demo").json()["restorable"]
    catalog = client.get("/api/png/templates").json()
    assert all(t["id"] != "demo" for t in catalog["templates"])
    assert {"id": "demo", "name": "Демо-шаблон"} in catalog["deleted_templates"]
    assert client.post("/api/png/render", json={"template_id": "demo", "color": {"code": "X", "hex": "#123456"}}).status_code == 404
    assert client.post("/api/png/mask", json={"template_id": "demo", "backend": "chroma"}).status_code == 404
    assert client.put("/api/settings/templates/demo/key", json={"key_color": "#123456"}).status_code == 404
    assert client.get(source["image"]["url"]).status_code == 200  # packaged assets remain recoverable
    assert client.get(rendered.json()["file"]["url"]).status_code == 200
    assert client.post("/api/settings/templates/demo/restore").status_code == 200
    assert any(t["id"] == "demo" for t in client.get("/api/png/templates").json()["templates"])
    assert client.post("/api/settings/templates/not-existing/restore").status_code == 404


def test_builtin_pdf_removal_persists_and_prevents_implicit_fallback(client):
    response = client.delete("/api/settings/sds/reference")
    assert response.status_code == 200 and response.json()["file"] is None
    assert sds.REFERENCE.is_file()
    with TestClient(app) as second:
        assert not second.get("/api/sds/reference").json()["available"]
        assert second.get("/api/settings").json()["sds"]["pages"] == 0
        request = {"item": {"code": "X", "brand": "Test", "color": "White", "title": "Test product"}, "render_previews": False}
        blocked = second.post("/api/sds/generate", json=request)
        assert blocked.status_code == 422 and "PDF-шаблон видалено" in blocked.json()["detail"]
        restored = second.post("/api/settings/sds/reference/restore")
        assert restored.status_code == 200 and restored.json()["available"] and restored.json()["pages"] == 41
        assert not restored.json()["custom"]
        second.delete("/api/settings/sds/reference")
        upload = storage.save_bytes(sds.REFERENCE.read_bytes(), "new.pdf", "application/pdf")
        assert second.put("/api/settings/sds/reference", json={"token": upload["token"]}).json()["available"]
        assert second.post("/api/sds/generate", json=request).status_code == 200
