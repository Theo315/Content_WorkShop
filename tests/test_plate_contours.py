"""Plate replacement must never turn bumper pixels into a white rectangle.

The BMW fixture is an unmodified crop of the user's failing source PNG:
source box (485, 545, 730, 650). Only the crop is retained, not generated output.
"""
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from vendor.hexph.hexph.plate_text import detect_plate, draw_plate_text


ROOT = Path(__file__).resolve().parents[1]
FONT = str(ROOT / "assets/fonts/NotoSans-Bold.ttf")
BMW_CROP = Path(__file__).with_name("fixtures") / "bmw-plate.png"


def rounded_plate_scene():
    """Known silhouette independent of the implementation's detected mask."""
    image = Image.new("RGB", (440, 230), (38, 170, 55))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((109, 93, 315, 147), radius=10, fill=(25, 25, 25))
    draw.rounded_rectangle((112, 96, 312, 144), radius=8, fill=(224, 224, 224))
    silhouette = Image.new("L", image.size)
    ImageDraw.Draw(silhouette).rounded_rectangle((112, 96, 312, 144), radius=8, fill=255)
    return np.array(image)[:, :, ::-1].copy(), np.array(silhouette)


def render(source, geometry, bounds):
    return draw_plate_text(
        source, geometry, "Asturcolor 252", FONT, None,
        (20, 20, 20), (241, 241, 241), text_bounds=bounds,
    )


def assert_unchanged_outside(source, output, allowed):
    assert np.array_equal(output[allowed == 0], source[allowed == 0])


def test_saturated_bumper_highlight_cannot_join_rounded_plate():
    source, allowed = rounded_plate_scene()
    # Bright enough to pass the previous gray-only threshold. The old global
    # hull bridged the white plate and this green reflection above its corner.
    cv2.fillConvexPoly(source, np.array([(301, 90), (337, 83), (331, 118), (313, 120)]),
                       (140, 255, 190))
    bounds = (108, 92, 318, 149)
    geometry = detect_plate(source, bounds)
    assert geometry.detected and geometry.shape is not None
    assert np.count_nonzero(geometry.shape) > 1000
    assert not np.any((geometry.shape > 0) & (allowed == 0))
    output, warning = render(source, geometry, bounds)
    assert warning is None
    assert np.any(output != source)
    assert_unchanged_outside(source, output, allowed)
    # This body pixel is inside the former expanded hull, not the real plate.
    assert np.array_equal(output[94, 320], source[94, 320])


def test_dark_placeholder_glyph_holes_are_wiped_without_growing_outline():
    source, allowed = rounded_plate_scene()
    cv2.putText(source, "OLD", (164, 129), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (20, 20, 20), 2, cv2.LINE_8)
    dark_glyphs = np.all(source[104:136, 150:275] == 20, axis=2)
    assert dark_glyphs.any()
    bounds = (108, 92, 318, 149)
    geometry = detect_plate(source, bounds)
    assert geometry.detected and geometry.shape is not None
    output, warning = draw_plate_text(source, geometry, "", FONT, None,
                                     (20, 20, 20), (241, 241, 241), text_bounds=bounds)
    assert warning is None
    assert np.all(output[104:136, 150:275][dark_glyphs] == 241)
    assert_unchanged_outside(source, output, allowed)


def test_outline_notch_is_not_filled_by_convex_hull():
    source, allowed = rounded_plate_scene()
    # A visible body-colored interruption touching the plate boundary is not an
    # interior glyph hole. External-contour fill must retain this concavity.
    source[96:110, 204:218] = (55, 170, 38)
    allowed[96:110, 204:218] = 0
    bounds = (108, 92, 318, 149)
    geometry = detect_plate(source, bounds)
    assert geometry.detected and geometry.shape is not None
    output, warning = render(source, geometry, bounds)
    assert warning is None
    assert_unchanged_outside(source, output, allowed)


@pytest.mark.parametrize("background", [(0, 0, 0), (80, 220, 70)])
def test_failed_detection_is_noop_not_unmasked_rectangle(background):
    source = np.full((120, 280, 3), background, np.uint8)
    bounds = (60, 40, 220, 80)
    geometry = detect_plate(source, bounds)
    assert not geometry.detected
    assert geometry.shape is not None and not geometry.shape.any()
    output, warning = render(source, geometry, bounds)
    assert warning
    assert np.array_equal(output, source)


def test_largest_neighbouring_bright_blob_is_not_selected():
    source, allowed = rounded_plate_scene()
    # Broad neutral bumper strip overlaps the search crop but is above the
    # configured number. It is larger than the true plate in the crop.
    source[58:91, 65:365] = 245
    bounds = (82, 72, 342, 154)
    geometry = detect_plate(source, bounds)
    output, warning = render(source, geometry, bounds)
    assert_unchanged_outside(source, output, allowed)
    if geometry.detected:
        assert warning is None
        assert geometry.shape is not None
        assert not np.any((geometry.shape > 0) & (allowed == 0))
        assert geometry.center[1] > 100
    else:
        # Rejecting an ambiguous search is safe; whitening the other blob is not.
        assert warning and np.array_equal(output, source)


@pytest.mark.parametrize("bounds", [
    (92, 75, 331, 163),       # Oversized selection includes body above/below.
    (130, 92, 350, 150),     # Selection shifted sideways across the plate edge.
    (162, 106, 278, 130),    # Partial, very tight selection inside the plate.
])
def test_oversized_or_misaligned_selection_never_whitens_body(bounds):
    source, allowed = rounded_plate_scene()
    geometry = detect_plate(source, bounds)
    output, warning = render(source, geometry, bounds)
    assert_unchanged_outside(source, output, allowed)
    if not geometry.detected:
        assert warning and np.array_equal(output, source)


def bmw_expected_silhouette(source):
    """Independent neutral plate boundary, without closing or a convex hull."""
    gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    saturation = cv2.cvtColor(source, cv2.COLOR_BGR2HSV)[:, :, 1]
    neutral = ((gray > 185) & (saturation < 45)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(neutral)
    assert count > 1
    component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    contours, _ = cv2.findContours((labels == component).astype(np.uint8),
                                  cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask = np.zeros(source.shape[:2], np.uint8)
    cv2.drawContours(mask, contours, -1, 255, cv2.FILLED)
    return mask


def test_bmw_plate_wipe_preserves_original_bumper_contours():
    with Image.open(BMW_CROP) as image:
        source = np.array(image.convert("RGB"))[:, :, ::-1].copy()
    allowed = bmw_expected_silhouette(source)
    # Full-image selection [516,578,681,619], translated by crop origin.
    bounds = (31, 33, 196, 74)
    geometry = detect_plate(source, bounds)
    assert geometry.detected and geometry.shape is not None
    assert not np.any((geometry.shape > 0) & (allowed == 0))
    output, warning = render(source, geometry, bounds)
    assert warning is None
    assert np.any(output != source)
    assert_unchanged_outside(source, output, allowed)
    # The old grayscale hull painted this high-chroma top-right wedge white.
    previous_bad_region = np.zeros(source.shape[:2], bool)
    previous_bad_region[27:34, 183:207] = True
    outside = previous_bad_region & (allowed == 0)
    assert outside.any()
    assert np.array_equal(output[outside], source[outside])


@pytest.mark.parametrize("body_brightness", [175, 240, 255])
def test_white_or_gray_body_is_separate_from_plate_by_dark_frame(body_brightness):
    source, allowed = rounded_plate_scene()
    source[np.all(source == (55, 170, 38), axis=2)] = body_brightness
    bounds = (108, 92, 318, 149)
    geometry = detect_plate(source, bounds)
    assert geometry.detected and geometry.shape is not None
    assert not np.any((geometry.shape > 0) & (allowed == 0))
    output, warning = render(source, geometry, bounds)
    assert warning is None and np.any(output != source)
    assert_unchanged_outside(source, output, allowed)


@pytest.mark.parametrize("angle", [-20, -7, 7, 20])
def test_tilted_rounded_plate_follows_its_actual_outline(angle):
    source, allowed = rounded_plate_scene()
    matrix = cv2.getRotationMatrix2D((212, 120), angle, 1)
    source = cv2.warpAffine(source, matrix, (440, 230), flags=cv2.INTER_NEAREST,
                            borderValue=(55, 170, 38))
    allowed = cv2.warpAffine(allowed, matrix, (440, 230), flags=cv2.INTER_NEAREST)
    bounds = (102, 79, 325, 162)
    geometry = detect_plate(source, bounds)
    assert geometry.detected and geometry.shape is not None
    assert abs(geometry.angle) == pytest.approx(abs(angle), abs=0.8)
    assert not np.any((geometry.shape > 0) & (allowed == 0))
    output, warning = render(source, geometry, bounds)
    assert warning is None and np.any(output != source)
    assert_unchanged_outside(source, output, allowed)


def test_uniform_gray_has_no_distinct_plate_and_is_not_whitened():
    source = np.full((120, 280, 3), 220, np.uint8)
    bounds = (60, 40, 220, 80)
    geometry = detect_plate(source, bounds, key_color="#1E9E52")
    assert not geometry.detected
    assert geometry.shape is not None and not geometry.shape.any()
    output, warning = render(source, geometry, bounds)
    assert warning and np.array_equal(output, source)


def test_perspective_trapezoid_keeps_all_paint_inside_original_surface():
    source = np.full((230, 440, 3), (55, 170, 38), np.uint8)
    frame = np.array([(105, 87), (319, 108), (311, 157), (111, 145)], np.int32)
    inner = np.array([(111, 94), (313, 114), (305, 151), (118, 139)], np.int32)
    cv2.fillConvexPoly(source, frame, (25, 25, 25))
    cv2.fillConvexPoly(source, inner, (224, 224, 224))
    allowed = np.zeros(source.shape[:2], np.uint8)
    cv2.fillConvexPoly(allowed, inner, 255)
    bounds = (106, 90, 318, 157)
    geometry = detect_plate(source, bounds)
    assert geometry.detected and geometry.shape is not None
    assert not np.any((geometry.shape > 0) & (allowed == 0))
    output, warning = render(source, geometry, bounds)
    assert warning is None and np.any(output != source)
    assert_unchanged_outside(source, output, allowed)


def test_blue_eu_strip_is_not_wiped_or_drawn_over():
    source, allowed = rounded_plate_scene()
    blue = allowed.astype(bool)
    blue[:, 130:] = False
    source[blue] = (190, 65, 12)
    bounds = (108, 92, 318, 149)
    geometry = detect_plate(source, bounds)
    assert geometry.detected and geometry.shape is not None
    assert not np.any((geometry.shape > 0) & blue)
    output, warning = render(source, geometry, bounds)
    assert warning is None and np.any(output != source)
    assert np.array_equal(output[blue], source[blue])
    assert_unchanged_outside(source, output, allowed)


def test_oversized_roi_does_not_treat_enclosing_white_bumper_panel_as_plate():
    plate_scene, allowed = rounded_plate_scene()
    panel = Image.new("RGB", (440, 230), (50, 50, 50))
    ImageDraw.Draw(panel).rounded_rectangle((65, 70, 365, 170), radius=16,
                                           fill=(240, 240, 240))
    source = np.array(panel)[:, :, ::-1].copy()
    frame_or_plate = np.any(plate_scene != (55, 170, 38), axis=2)
    source[frame_or_plate] = plate_scene[frame_or_plate]
    bounds = (60, 60, 370, 180)
    geometry = detect_plate(source, bounds)
    output, warning = render(source, geometry, bounds)
    assert_unchanged_outside(source, output, allowed)
    if geometry.detected:
        assert geometry.shape is not None
        assert not np.any((geometry.shape > 0) & (allowed == 0))
    else:
        assert warning and np.array_equal(output, source)


def test_small_nested_plate_cannot_make_large_white_panel_a_valid_plate():
    source = np.full((340, 800, 3), 50, np.uint8)
    cv2.rectangle(source, (65, 70), (735, 270), (240, 240, 240), -1)
    cv2.rectangle(source, (297, 142), (503, 198), (25, 25, 25), -1)
    cv2.rectangle(source, (300, 145), (500, 195), (224, 224, 224), -1)
    allowed = np.zeros(source.shape[:2], np.uint8)
    allowed[145:196, 300:501] = 255
    bounds = (60, 60, 740, 280)
    geometry = detect_plate(source, bounds)
    output, warning = render(source, geometry, bounds)
    assert_unchanged_outside(source, output, allowed)
    if not geometry.detected:
        assert warning and np.array_equal(output, source)


@pytest.mark.parametrize("bounds", [
    (31, 33, 196, 74),
    (13, 13, 216, 91),
    (94, 17, 226, 69),
    (155, 20, 221, 39),
])
def test_bmw_chroma_key_does_not_allow_green_reflections_as_plate(bounds):
    with Image.open(BMW_CROP) as image:
        source = np.array(image.convert("RGB"))[:, :, ::-1].copy()
    allowed = bmw_expected_silhouette(source)
    geometry = detect_plate(source, bounds, key_color="#1E9E52")
    output, warning = render(source, geometry, bounds)
    assert_unchanged_outside(source, output, allowed)
    if geometry.detected:
        assert geometry.shape is not None
        assert not np.any((geometry.shape > 0) & (allowed == 0))
        saturation = cv2.cvtColor(source, cv2.COLOR_BGR2HSV)[:, :, 1]
        assert not np.any((geometry.shape > 0) & (saturation > 60))
    else:
        assert warning and np.array_equal(output, source)
