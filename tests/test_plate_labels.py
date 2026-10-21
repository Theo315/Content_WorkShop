"""Selectable plate labels never change filenames or bypass plate contour/sizing."""
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import png, settings, storage
from backend.app import app
from backend.models import PngRender, TemplateConfig


@pytest.mark.parametrize("config,expected", [
    ({"brand_name": "Asturcolor"}, "Asturcolor G44"),
    ({"brand_name": ""}, "G44"),
    ({"plate_text_mode": "code", "brand_name": "Ignored"}, "G44"),
    ({"plate_text_mode": "brand_code", "brand_name": "My Paint"}, "My Paint G44"),
    ({"plate_text_mode": "brand", "brand_name": "My Paint"}, "My Paint"),
    ({"plate_text_mode": "custom", "brand_name": "Ignored", "plate_text": "  Моя\n фарба\t 42  "}, "Моя фарба 42"),
    ({"plate_text_mode": "custom", "plate_text": "{code}"}, "{code}"),
    ({"plate_text_mode": "custom"}, ""),
    ({"plate_text_mode": "blank", "plate_text": "Ignored"}, ""),
])
def test_composition(config, expected):
    assert png.plate_text(config, "G44") == expected


@pytest.mark.parametrize("config", [
    {"plate_text_mode": "invalid"}, {"plate_text": "X" * 201},
])
def test_invalid_label_config_is_rejected(config):
    with pytest.raises(ValidationError):
        TemplateConfig(**config)


@pytest.mark.parametrize("mode,expected", [("brand_code", "My Paint G44"), ("code", "G44"),
                                         ("brand", "My Paint"), ("custom", "Моя фарба"), ("blank", "")])
def test_real_render_uses_selected_text_auto_size_and_contour(monkeypatch, tmp_path, mode, expected):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("S3_BUCKET", raising=False)
    captured = []
    original = png.draw_plate_text

    def record(image, plate, text, font, size, *args, **kwargs):
        output, warning = original(image, plate, text, font, size, *args, **kwargs)
        captured.append((text, size))
        assert plate.detected and plate.shape is not None
        assert np.array_equal(output[plate.shape == 0], image[plate.shape == 0])
        return output, warning

    monkeypatch.setattr(png, "draw_plate_text", record)
    result = png.render(PngRender(template_id="demo", config={
        "brand_name": "My Paint", "plate_text_mode": mode, "plate_text": "Моя фарба",
    }, color={"code": "G44", "hex": "#4D778A"}))
    assert captured == [(expected, None)]
    assert result["plate_text"] == expected and result["file"]["name"] == "G44.png"
    assert storage.read_bytes(result["file"]["token"]).startswith(b"\x89PNG")


def test_original_plate_toggle_skips_text_renderer(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(png, "draw_plate_text", lambda *a, **k: pytest.fail("Original plate must not be redrawn"))
    result = png.render(PngRender(template_id="demo", config={"plate_text_mode": "custom", "plate_text": "Ignored"},
                                 color={"code": "G44", "hex": "#4D778A"}, draw_plate=False))
    assert result["plate_text"] is None


def test_api_history_records_exact_plate_label_without_renaming_color(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    with TestClient(app) as client:
        response = client.post("/api/png/render", json={"template_id": "demo",
            "config": {"plate_text_mode": "custom", "plate_text": "CUSTOM LABEL"},
            "color": {"code": "G44", "hex": "#4D778A"}})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["plate_text"] == "CUSTOM LABEL" and result["file"]["name"] == "G44.png"
        record = client.get("/api/history/" + result["history_id"]).json()
        assert record["metadata"]["plate_text"] == "CUSTOM LABEL"
        assert record["metadata"]["color"]["code"] == "G44"
