import io
import json
from pathlib import Path
import numpy as np
import pymupdf as fitz
from PIL import Image
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend import sds, pdf_editor, png, storage, settings
from backend.models import PngRender


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("APP_PUBLIC_ACCESS", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    with TestClient(app) as client:
        yield client


def upload(client, data, name, content_type):
    response = client.post("/api/files/upload", json=dict(filename=name, size=len(data), content_type=content_type))
    assert response.status_code == 200, response.text
    plan = response.json()
    response = client.put(plan["upload_url"], content=data, headers=plan["headers"])
    assert response.status_code == 200, response.text
    refreshed = client.post("/api/files/refresh", json={"token": plan["file"]["token"]})
    assert refreshed.status_code == 200, refreshed.text
    assert client.get(refreshed.json()["file"]["url"]).content == data
    return plan["file"]


def test_sds_generation_preserves_all_pages_and_titles(client):
    parsed = client.post("/api/sds/parse", json={"mode": "template", "brand": "Seat", "title_template": sds.DEFAULT_TITLE,
                                               "values_text": "LB9A CANDY WHITE"})
    assert parsed.status_code == 200, parsed.text
    item = parsed.json()["items"][0]
    response = client.post("/api/sds/generate", json={"item": item, "render_previews": True})
    assert response.status_code == 200, response.text
    value = response.json()
    archived = client.get("/api/history/" + value["history_id"]).json()
    assert archived["kind"] == "sds" and archived["metadata"]["item"]["code"] == "LB9A"
    data = client.get(value["file"]["url"]).content
    assert value["validation"] == dict(pages=41, paint_title_regions_exact=24, clear_varnish_regions_exact=19,
                                        dates_preserved=True, protected_text_preserved=True)
    with fitz.open(stream=data, filetype="pdf") as doc, fitz.open(sds.REFERENCE) as reference:
        assert len(doc) == 41
        assert sds.title_regions(doc) == [sds.normalized(item["title"])] * 24
        # Varnish pages are also pixel-identical, not merely text-equivalent.
        for index in (23, 29, 40):
            assert doc[index].get_pixmap().samples == reference[index].get_pixmap().samples
    qa = Path("tmp/qa")
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "sds.pdf").write_bytes(data)
    for index, preview in zip((1, 9, 24, 30), value["previews"]):
        (qa / f"sds-p{index}.png").write_bytes(client.get(preview["url"]).content)
    # Regression: Noto's tall ascender boxes must not make search collect text
    # from the line above or cause a replacement to erase that line.
    matches = pdf_editor.find(data, "Seat", "1")
    assert len(matches) == 2
    edited, replacements, pages, _ = pdf_editor.edit(data, "Seat", "Ford", "1")
    assert replacements == 2 and pages == 41
    with fitz.open(stream=edited, filetype="pdf") as doc:
        assert "Seat" not in doc[0].get_text()
        assert "Ford" in doc[0].get_text()
        assert "Seat" in doc[1].get_text()


def test_png_real_template_keeps_alpha_and_unmasked_pixels(client):
    response = client.post("/api/png/render", json={"template_id": "kuga", "color": {"code": "436", "hex": "#4D778A"}, "draw_plate": False})
    assert response.status_code == 200, response.text
    value = response.json()
    output = client.get(value["file"]["url"]).content
    painted, alpha = png.image_from_bytes(output)
    source, source_alpha = png.image_from_bytes(png.template_paths("kuga")[0].read_bytes())
    mask1 = png.mask_from_bytes(png.template_paths("kuga")[1].read_bytes(), source.shape[:2])
    mask2 = png.mask_from_bytes(png.template_paths("kuga")[2].read_bytes(), source.shape[:2])
    import cv2
    outside = cv2.dilate(np.maximum(mask1, mask2), np.ones((21, 21), np.uint8)) == 0
    assert np.array_equal(painted[outside], source[outside])
    assert np.array_equal(alpha, source_alpha)
    assert painted.shape == source.shape == (2048, 2048, 3)
    assert value["metrics"]["body_delta_e"] < 5
    qa = Path("tmp/qa")
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "kuga.png").write_bytes(output)
    (qa / "kuga-preview.png").write_bytes(client.get(value["preview"]["url"]).content)


def test_png_uploaded_template_mask_and_plate(client):
    catalog = client.get("/api/png/templates").json()["templates"]
    demo = next(t for t in catalog if t["id"] == "demo")
    source = png.template_paths("demo")[0].read_bytes()
    ref = upload(client, source, "custom.png", "image/png")
    result = client.post("/api/png/render", json={"template_token": ref["token"],
                        "car_mask_token": demo["car_mask"]["token"], "marker_mask_token": demo["marker_mask"]["token"],
                        "config": demo["config"], "color": {"code": "G44", "hex": "#72A2BB"}, "draw_plate": True})
    assert result.status_code == 200, result.text
    assert result.json()["file"]["name"] == "G44.png"
    assert client.get(result.json()["file"]["url"]).content[:8] == b"\x89PNG\r\n\x1a\n"


def test_colors_exact_and_invalid_input(client):
    response = client.post("/api/png/colors", json={"text": "code,hex,metallic\nA,#7AB,true\nA,#ffffff,false\nB,wrong,false", "format": "csv"})
    colors = response.json()["colors"]
    assert colors[0]["hex"] == "#77AABB" and colors[0]["metallic"]
    assert colors[1]["error"] and colors[2]["error"]
    assert client.post("/api/png/render", json={"template_id": "../../x", "color": {"code": "A", "hex": "#ffffff"}}).status_code == 422
    assert client.post("/api/png/render", json={"template_id": "demo", "color": {"code": "A", "hex": "nohex"}}).status_code == 422
    response = client.post("/api/sds/parse", json={"mode": "exact", "exact_text": "LKR | Citroen | ROJO BABYLONE | Exact title"})
    assert response.json()["items"][0]["title"] == "Exact title"
    assert client.post("/api/sds/parse", json={"mode": "template", "brand": "Seat", "title_template": "{BAD}", "values_text": "A White"}).status_code == 422


def test_editor_preserves_unrelated_words_and_handles_selection(client):
    with fitz.open() as doc:
        for _ in range(2):
            page = doc.new_page()
            page.insert_text((72, 72), "Original Product", fontsize=12)
            page.insert_text((72, 110), "Keep this safety text unchanged.", fontsize=10)
        source = doc.tobytes()
    ref = upload(client, source, "source.pdf", "application/pdf")
    request = dict(file_token=ref["token"], search_text="Original Product", pages_spec="2", exact_case=True, replace_all=True)
    assert client.post("/api/sds/find", json=request).json()["matches"][0]["page"] == 2
    response = client.post("/api/sds/edit", json={**request, "replacement": "New Product"})
    assert response.status_code == 200, response.text
    edited = client.get(response.json()["file"]["url"]).content
    archived = client.get("/api/history/" + response.json()["history_id"]).json()
    assert archived["kind"] == "edit" and archived["metadata"]["replacements"] == 1
    with fitz.open(stream=edited, filetype="pdf") as doc:
        assert len(doc) == 2
        assert "Original Product" in doc[0].get_text()
        assert "New Product" in doc[1].get_text()
        assert "Original Product" not in doc[1].get_text()
        assert all("Keep this safety text unchanged." in page.get_text() for page in doc)
    assert client.post("/api/sds/find", json={**request, "pages_spec": "0,3"}).status_code == 422


def test_auth_storage_tampering_and_fail_closed(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "a-very-long-test-password")
    assert client.get("/api/png/templates").status_code == 401
    assert client.post("/api/session", json={"password": "wrong"}).status_code == 401
    response = client.post("/api/session", json={"password": "a-very-long-test-password"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/api/png/templates").status_code == 200
    assert client.get("/api/files/local/forged-token").status_code == 400
    assert client.post("/api/files/refresh", json={"token": "forged-token"}).status_code == 400
    assert client.post("/api/files/upload", json={"filename": "x", "content_type": "image/png", "size": settings.MAX_UPLOAD + 1}).status_code == 422
    assert client.post("/api/session", json={"password": "x"}, headers={"Origin": "https://attacker.example"}).status_code == 403
    client.delete("/api/session")
    assert client.get("/api/png/templates").status_code == 401
    monkeypatch.setattr(settings, "IS_VERCEL", True)
    monkeypatch.delenv("APP_PASSWORD")
    assert not client.get("/api/status").json()["ready"]


def test_chroma_mask_and_invalid_dimensions(client):
    response = client.post("/api/png/mask", json={"template_id": "kuga-key", "backend": "chroma", "key_color": "#1E9E52", "kind": "car"})
    assert response.status_code == 200, response.text
    data = client.get(response.json()["file"]["url"]).content
    assert np.array(Image.open(io.BytesIO(data))).any()
    small = io.BytesIO()
    Image.new("L", (10, 10), 255).save(small, format="PNG")
    ref = upload(client, small.getvalue(), "small-mask.png", "image/png")
    response = client.post("/api/png/render", json={"template_id": "demo", "car_mask_token": ref["token"], "color": {"code": "A", "hex": "#ff0000"}})
    assert response.status_code == 422
