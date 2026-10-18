"""Regressions for full plate text, automatic sizing and selected bounds."""
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from backend import png
from backend import storage
from backend.models import PngRender, TemplateConfig
from vendor.hexph.hexph.plate_text import (
    PlateGeometry, detect_plate, draw_plate_text, fit_font_size,
)

FONT = str(png.ASSETS / "fonts/NotoSans-Bold.ttf")


def ink_size(text, size):
    bbox = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
        (0, 0), text, font=ImageFont.truetype(FONT, size))
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


@pytest.mark.parametrize("box,text,upper", [
    ((109, 26), "Asturcolor 13", 34),
    ((109, 26), "Asturcolor 13", 300),
    ((109, 26), "Asturcolor 1234567890", None),
    ((188, 48), "Asturcolor " + "W" * 30, 300),
    ((220, 18), "ÁSTURCOLOR G44", None),
    ((450, 50), "Asturcolor 575", None),
    ((220, 60), "G44", 8),
])
def test_font_fits_both_axes_and_is_largest(box, text, upper):
    size, _ = fit_font_size(text, box, FONT, upper)
    pad = max(2, round(0.06 * box[1]))
    w, h = ink_size(text, size)
    assert w <= box[0] - 2 * pad
    assert h <= box[1] - 2 * pad
    maximum = upper if upper is not None else 4 * box[1]
    if size < maximum:
        w, h = ink_size(text, size + 1)
        assert w > box[0] - 2 * pad or h > box[1] - 2 * pad


@pytest.mark.parametrize("angle", [0, -7, 7])
def test_drawn_glyphs_fit_user_rectangle_even_if_detector_finds_larger_plate(angle):
    source = np.full((150, 300, 3), 240, np.uint8)
    geometry = PlateGeometry((150, 75), (180, 48), angle)
    bounds = (95, 59, 205, 91)
    output, warning = draw_plate_text(source, geometry, "Asturcolor 13", FONT, None,
                                     (20, 20, 20), (240, 240, 240), text_bounds=bounds)
    ys, xs = np.where(output.min(axis=2) < 120)
    assert xs.size > 0 and warning is None
    assert xs.min() > bounds[0] and xs.max() < bounds[2] - 1
    assert ys.min() > bounds[1] and ys.max() < bounds[3] - 1
    unchanged = np.ones(source.shape[:2], bool)
    unchanged[bounds[1]:bounds[3], bounds[0]:bounds[2]] = False
    assert np.array_equal(output[unchanged], source[unchanged])


def test_impossibly_long_text_scales_without_crop():
    source = np.full((80, 140, 3), 240, np.uint8)
    bounds = (20, 25, 120, 55)
    output, warning = draw_plate_text(source, bounds, "W" * 200, FONT, 300,
                                     (20, 20, 20), (240, 240, 240))
    assert warning and "без обрізання" in warning
    # Even the smallest-font fallback keeps its entire raster inside the inset.
    assert np.any(output[27:53, 22:118] != source[27:53, 22:118])
    assert np.array_equal(output[:, :22], source[:, :22])
    assert np.array_equal(output[:, 118:], source[:, 118:])


@pytest.mark.parametrize("template_id", list(png.TEMPLATES))
def test_real_template_text_stays_inside_selected_bounds(template_id):
    source, _ = png.image_from_bytes(png.template_paths(template_id)[0].read_bytes())
    config = png.TEMPLATES[template_id]
    bounds = tuple(config["plate_box"])
    geometry = detect_plate(source, bounds, key_color=config.get("key_color"))
    baseline, _ = draw_plate_text(source, geometry, "", FONT, None, (20, 20, 20),
                                 (240, 240, 240), text_bounds=bounds)
    rendered, warning = draw_plate_text(source, geometry, "Asturcolor 1234567890", FONT, None,
                                       (20, 20, 20), (240, 240, 240), text_bounds=bounds)
    ys, xs = np.where(np.any(rendered != baseline, axis=2))
    assert warning is None and xs.size > 0
    assert xs.min() >= bounds[0] and xs.max() < bounds[2]
    assert ys.min() >= bounds[1] and ys.max() < bounds[3]


def test_auto_size_is_default_and_manual_size_is_supported():
    assert TemplateConfig().auto_font_size
    assert not TemplateConfig(auto_font_size=False, font_size=8).auto_font_size


def test_render_pipeline_passes_auto_size_and_selected_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    seen = []
    original = png.draw_plate_text

    def record(*args, **kwargs):
        seen.append((args[4], kwargs["text_bounds"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(png, "draw_plate_text", record)
    for auto in (True, False):
        result = png.render(PngRender(template_id="demo", config={
            "auto_font_size": auto, "font_size": 300,
        }, color={"code": "1234567890", "hex": "#4D778A"}))
        assert not any("обрізання" in warning for warning in result["warnings"])
    assert seen == [(None, tuple(png.TEMPLATES["demo"]["plate_box"])),
                    (300, tuple(png.TEMPLATES["demo"]["plate_box"]))]


@pytest.mark.parametrize("bounds,detected", [((31, 33, 196, 74), True), ((1, 1, 14, 16), False)])
def test_png_pipeline_never_wipes_outside_plate_and_reports_failure(monkeypatch, tmp_path, bounds, detected):
    from pathlib import Path
    from tests.test_plate_contours import bmw_expected_silhouette
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    data = (Path(__file__).with_name("fixtures") / "bmw-plate.png").read_bytes()
    source, _ = png.image_from_bytes(data)
    allowed = bmw_expected_silhouette(source) > 0
    template = storage.save_bytes(data, "plate.png", "image/png")
    params = dict(template_token=template["token"], config={
        "brand_name": "Asturcolor", "plate_box": bounds, "key_color": "#1E9E52",
    }, color={"code": "252", "hex": "#B31218"}, feather=0)
    without_text = png.render(PngRender(**params, draw_plate=False))
    with_text = png.render(PngRender(**params))
    baseline, _ = png.image_from_bytes(storage.read_bytes(without_text["file"]["token"]))
    rendered, _ = png.image_from_bytes(storage.read_bytes(with_text["file"]["token"]))
    assert np.array_equal(rendered[~allowed], baseline[~allowed])
    if detected:
        assert np.any(rendered[allowed] != baseline[allowed])
        assert not any("Контур номера" in warning for warning in with_text["warnings"])
        geometry = detect_plate(source, bounds, key_color="#1E9E52")
        # The guard must also protect a valid plate during chroma-key recolouring.
        assert np.array_equal(baseline[geometry.shape > 0], source[geometry.shape > 0])
    else:
        assert np.array_equal(rendered, baseline)
        assert any("Контур номера" in warning for warning in with_text["warnings"])


def test_tinted_chroma_preset_plate_is_detected_and_guarded(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    source, _ = png.image_from_bytes(png.template_paths("kuga-key")[0].read_bytes())
    bounds = tuple(png.TEMPLATES["kuga-key"]["plate_box"])
    geometry = detect_plate(source, bounds, key_color="#1E9E52")
    assert geometry.detected and geometry.shape.any()
    result = png.render(PngRender(template_id="kuga-key", color={"code": "252", "hex": "#B31218"}, draw_plate=False))
    rendered, _ = png.image_from_bytes(storage.read_bytes(result["file"]["token"]))
    assert np.array_equal(rendered[geometry.shape > 0], source[geometry.shape > 0])
