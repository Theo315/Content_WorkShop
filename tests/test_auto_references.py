"""Uploaded references become durable only after a successful PNG render."""

import copy
import io
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, PngImagePlugin

from backend.app import app
from backend import preferences, settings, storage
from backend.security import read_file_token


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    with TestClient(app) as client:
        yield client


def encode(image):
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


def source_bytes():
    image = Image.new("RGBA", (96, 72), (230, 230, 230, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 16, 88, 60), fill="#1E9E52")
    draw.rectangle((5, 8, 14, 64), fill="#1E9E52")
    draw.rectangle((35, 46, 73, 57), fill="#D8D8D8")
    image.putpixel((0, 0), (0, 0, 0, 0))
    return encode(image)


def mask_bytes(role):
    image = Image.new("L", (96, 72), 0)
    ImageDraw.Draw(image).rectangle(
        (20, 16, 88, 60) if role == "car" else (5, 8, 14, 64), fill=255
    )
    return encode(image)


def upload(client, data, name):
    response = client.post(
        "/api/files/upload",
        json={"filename": name, "size": len(data), "content_type": "image/png"},
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    uploaded = client.put(plan["upload_url"], content=data, headers=plan["headers"])
    assert uploaded.status_code == 200, uploaded.text
    return plan["file"]


def custom_request(client, name="Reference.png", model_name="  My reference  "):
    source = upload(client, source_bytes(), name)
    car = upload(client, mask_bytes("car"), "body-mask.png")
    marker = upload(client, mask_bytes("marker"), "marker-mask.png")
    request = {
        "template_token": source["token"],
        "car_mask_token": car["token"],
        "marker_mask_token": marker["token"],
        "config": {
            "brand_name": "Asturcolor",
            "model_name": model_name,
            "key_color": "#1E9E52",
            "plate_box": [35, 46, 73, 57],
            "font_size": 22,
            "auto_font_size": False,
            "text_color": [10, 20, 30],
            "plate_bg_color": "auto",
        },
        "color": {"code": "FIRST", "hex": "#B52330"},
        "draw_plate": False,
        "feather": 0,
    }
    return request, (source, car, marker)


def render(client, request):
    response = client.post("/api/png/render", json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    output = client.get(result["file"]["url"])
    assert output.status_code == 200
    with Image.open(io.BytesIO(output.content)) as image:
        assert image.format == "PNG" and image.size == (96, 72)
        assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 0
    assert client.get("/api/history/" + result["history_id"]).status_code == 200
    return result


def cars(client):
    response = client.get("/api/png/templates")
    assert response.status_code == 200, response.text
    return [template for template in response.json()["templates"] if not template["builtin"]]


def library_files():
    return set((storage.local_root() / "library/files").rglob("*.png"))


def test_upload_and_auto_mask_do_not_save_reference_before_render(client):
    request, _ = custom_request(client)
    assert cars(client) == []
    response = client.post(
        "/api/png/mask",
        json={
            "template_token": request["template_token"],
            "backend": "chroma",
            "key_color": "#1E9E52",
            "kind": "car",
        },
    )
    assert response.status_code == 200, response.text
    assert cars(client) == []
    assert preferences.load().get("cars", {}) == {}
    assert library_files() == set()


def test_failed_render_does_not_save_reference(client):
    request, _ = custom_request(client)
    request["car_mask_token"] = upload(
        client, encode(Image.new("L", (1, 1), 255)), "wrong-size.png"
    )["token"]
    response = client.post("/api/png/render", json=request)
    assert response.status_code == 422, response.text
    assert cars(client) == []
    assert library_files() == set()
    assert client.get("/api/history").json()["items"] == []


def test_first_render_saves_original_masks_config_and_survives_expired_uploads(client, monkeypatch):
    request, uploads = custom_request(client)
    result = render(client, request)
    assert result["auto_saved_template"]["created"] is True
    saved = result["auto_saved_template"]["template"]
    assert saved["id"].startswith("car-") and not saved["builtin"]
    assert saved["name"] == "My reference"
    assert saved["width"] == 96 and saved["height"] == 72
    for key, value in request["config"].items():
        assert saved["config"][key] == value
    assert len(cars(client)) == 1
    for role, expected in (
        ("image", source_bytes()),
        ("car_mask", mask_bytes("car")),
        ("marker_mask", mask_bytes("marker")),
    ):
        assert client.get(saved[role]["url"]).content == expected
        descriptor = preferences.car(saved["id"])[role]
        assert descriptor["key"].startswith("library/files/")
    assert client.get(saved["image"]["url"]).content != client.get(result["file"]["url"]).content

    for uploaded in uploads:
        storage.delete_file(uploaded["token"])
    from itsdangerous.timed import TimestampSigner

    future = int(time.time() + 2 * 86400)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: future)
    with TestClient(app) as second:
        refreshed = next(template for template in cars(second) if template["id"] == saved["id"])
        assert second.get(refreshed["image"]["url"]).content == source_bytes()
        rerendered = render(
            second,
            {
                "template_id": saved["id"],
                "color": {"code": "SAVED", "hex": "#123456"},
                "draw_plate": False,
                "feather": 0,
            },
        )
        assert "auto_saved_template" not in rerendered
        assert len(cars(second)) == 1


def test_repeated_batch_and_reupload_reuse_reference_without_overwriting(client):
    request, _ = custom_request(client)
    batch_id = str(uuid.uuid4())
    request.update(batch_id=batch_id, batch_index=0, batch_size=2)
    first = render(client, request)
    saved = first["auto_saved_template"]["template"]
    original_entry = copy.deepcopy(preferences.car(saved["id"]))
    original_files = library_files()

    second_request = copy.deepcopy(request)
    second_request.update(batch_index=1, color={"code": "SECOND", "hex": "#112233"})
    second_request["config"]["model_name"] = "Must not replace original name"
    second_request["config"]["brand_name"] = "Must not replace original settings"
    second = render(client, second_request)
    assert second["auto_saved_template"]["created"] is False
    assert second["auto_saved_template"]["template"]["id"] == saved["id"]

    reupload = upload(client, source_bytes(), "another-name.png")
    third_request = copy.deepcopy(second_request)
    for key in ("batch_id", "batch_index", "batch_size"):
        third_request.pop(key)
    third_request["template_token"] = reupload["token"]
    third_request["color"]["code"] = "REUPLOAD"
    third = render(client, third_request)
    assert third["auto_saved_template"]["created"] is False
    assert third["auto_saved_template"]["template"]["id"] == saved["id"]
    assert len(cars(client)) == 1
    assert preferences.car(saved["id"]) == original_entry
    assert library_files() == original_files


def test_different_png_bytes_are_not_deduplicated_by_filename_or_pixels(client):
    request, _ = custom_request(client, name="Same name.png")
    first = render(client, request)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("source", "different original PNG bytes")
    with Image.open(io.BytesIO(source_bytes())) as image:
        changed = io.BytesIO()
        image.save(changed, format="PNG", pnginfo=metadata)
    different_source = changed.getvalue()
    assert different_source != source_bytes()
    request["template_token"] = upload(client, different_source, "Same name.png")["token"]
    second = render(client, request)
    first_saved = first["auto_saved_template"]["template"]
    second_saved = second["auto_saved_template"]["template"]
    assert second["auto_saved_template"]["created"] is True
    assert second_saved["id"] != first_saved["id"]
    assert client.get(second_saved["image"]["url"]).content == different_source
    assert len(cars(client)) == 2


def test_key_only_reference_can_be_reused_without_temporary_upload(client):
    request, uploads = custom_request(client)
    request.pop("car_mask_token")
    request.pop("marker_mask_token")
    result = render(client, request)
    saved = result["auto_saved_template"]["template"]
    assert saved["car_mask"] is None and saved["marker_mask"] is None
    assert saved["config"]["key_color"] == "#1E9E52"
    storage.delete_file(uploads[0]["token"])
    rerendered = render(
        client,
        {"template_id": saved["id"], "color": {"code": "CHROMA", "hex": "#112233"}, "draw_plate": False},
    )
    assert "auto_saved_template" not in rerendered


@pytest.mark.parametrize("model_name", [None, "", "   "])
def test_reference_name_falls_back_to_uploaded_filename(client, model_name):
    request, _ = custom_request(client, name="My uploaded reference.png", model_name=model_name)
    result = render(client, request)
    assert result["auto_saved_template"]["template"]["name"] == "My uploaded reference"


def test_manual_saved_reference_is_reused_without_changes(client):
    request, uploads = custom_request(client)
    manual_config = {"brand_name": "Manual brand", "model_name": "Manual model", "key_color": "#1E9E52"}
    response = client.post(
        "/api/settings/templates",
        json={
            "name": "Manually configured reference",
            "image_token": uploads[0]["token"],
            "car_mask_token": uploads[1]["token"],
            "config": manual_config,
        },
    )
    assert response.status_code == 200, response.text
    manual = response.json()
    old_entry = copy.deepcopy(preferences.car(manual["id"]))
    old_files = library_files()

    result = render(client, request)
    assert result["auto_saved_template"]["created"] is False
    assert result["auto_saved_template"]["template"]["id"] == manual["id"]
    assert preferences.car(manual["id"]) == old_entry
    assert library_files() == old_files
    assert len(cars(client)) == 1


def test_legacy_manual_reference_reuses_entry_and_migrates_checksum_once(client, monkeypatch):
    request, uploads = custom_request(client)
    response = client.post(
        "/api/settings/templates",
        json={
            "name": "Legacy reference",
            "image_token": uploads[0]["token"],
            "car_mask_token": uploads[1]["token"],
            "config": {"brand_name": "Legacy brand", "key_color": "#1E9E52"},
        },
    )
    assert response.status_code == 200, response.text
    template_id = response.json()["id"]
    value = preferences.load()
    value["cars"][template_id].pop("source_sha256", None)
    preferences.save(value)
    original_entry = copy.deepcopy(value["cars"][template_id])
    library_key = original_entry["image"]["key"]
    original_read = storage.read_bytes
    library_reads = []

    def track_read(token, *args, **kwargs):
        if read_file_token(token)["key"] == library_key:
            library_reads.append(library_key)
        return original_read(token, *args, **kwargs)

    monkeypatch.setattr(storage, "read_bytes", track_read)
    first = render(client, request)
    assert first["auto_saved_template"]["created"] is False
    assert first["auto_saved_template"]["template"]["id"] == template_id
    assert len(library_reads) == 1
    migrated_entry = preferences.car(template_id)
    assert migrated_entry["source_sha256"]
    assert {key: val for key, val in migrated_entry.items() if key != "source_sha256"} == original_entry
    library_reads.clear()
    second = render(client, request)
    assert second["auto_saved_template"]["created"] is False
    assert library_reads == []
    assert len(cars(client)) == 1


def test_builtin_render_does_not_autosave_reference(client):
    response = client.post(
        "/api/png/render",
        json={"template_id": "demo", "color": {"code": "BUILTIN", "hex": "#123456"}, "draw_plate": False},
    )
    assert response.status_code == 200, response.text
    assert "auto_saved_template" not in response.json()
    assert cars(client) == []


def test_full_library_warns_without_losing_generated_image_or_history(client):
    request, _ = custom_request(client)
    other_image = upload(client, encode(Image.new("RGB", (96, 72), "#1E9E52")), "other.png")
    response = client.post(
        "/api/settings/templates",
        json={"name": "Existing", "image_token": other_image["token"], "config": {"key_color": "#1E9E52"}},
    )
    assert response.status_code == 200, response.text
    value = preferences.load()
    existing = copy.deepcopy(next(iter(value["cars"].values())))
    value["cars"] = {"car-" + f"{index:032x}": copy.deepcopy(existing) for index in range(100)}
    preferences.save(value)
    old_files = library_files()

    result = render(client, request)
    assert "auto_saved_template" not in result
    assert result["reference_save_warning"]
    assert result["reference_save_warning"] in result["warnings"]
    assert len(cars(client)) == 100
    assert preferences.load() == value
    assert library_files() == old_files


def test_library_persistence_failure_rolls_back_without_losing_generated_image(client, monkeypatch):
    request, _ = custom_request(client)

    def fail_save(value):
        raise OSError("Simulated full disk while saving reference")

    monkeypatch.setattr(preferences, "save", fail_save)
    result = render(client, request)
    assert "auto_saved_template" not in result
    assert result["reference_save_warning"]
    assert result["reference_save_warning"] in result["warnings"]
    assert preferences.load().get("cars", {}) == {}
    assert library_files() == set()


def test_partial_library_copy_failure_removes_new_objects_but_keeps_render(client, monkeypatch):
    request, _ = custom_request(client)
    original_store = preferences.store_file

    def fail_after_marker_copy(data, name, mime, folder):
        copied = original_store(data, name, mime, folder)
        if name == "marker_mask.png":
            raise OSError("Simulated storage failure after writing marker mask")
        return copied

    monkeypatch.setattr(preferences, "store_file", fail_after_marker_copy)
    result = render(client, request)
    assert "auto_saved_template" not in result
    assert result["reference_save_warning"] in result["warnings"]
    assert preferences.load().get("cars", {}) == {}
    assert library_files() == set()
