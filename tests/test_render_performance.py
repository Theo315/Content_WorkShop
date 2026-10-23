"""Performance regressions: pixel equivalence and eliminating redundant S3 work."""
from threading import Barrier, Lock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import history, settings, storage
from backend.app import app
from backend.security import read_file_token
from vendor.hexph.hexph import recolor as core
from vendor.hexph.hexph.colors import bgr_to_lab, hex_to_lab, lab_to_bgr


def original_recolor(image, layers, target_hex, metallic, options):
    """Frozen pre-optimization algorithm as the pixel-level oracle."""
    lab, target = bgr_to_lab(image), hex_to_lab(target_hex)
    merged = image.astype(np.float32)
    for weight in layers:
        if not weight.any():
            continue
        solid = core._core_pixels(weight)
        painted = lab.copy()
        painted[:, :, 1], painted[:, :, 2] = target[1], target[2]
        if options.lightness_mode == "match":
            painted[:, :, 0], _ = core._match_lightness(lab[:, :, 0], solid, float(target[0]), options.min_contrast)
        if metallic:
            painted = core._metallic_flop(painted, painted[:, :, 0], solid, options)
        merged = merged + (lab_to_bgr(painted).astype(np.float32) - merged) * weight[:, :, None]
    return np.rint(np.clip(merged, 0, 255)).astype(np.uint8)


@pytest.mark.parametrize("shape", [(97, 113), (128, 192)])
@pytest.mark.parametrize("mode,metallic", [("match", False), ("keep", False), ("match", True), ("keep", True)])
@pytest.mark.parametrize("target", ["#FFFFFF", "#060606", "#7FA9B5"])
def test_roi_recolor_is_pixel_identical_including_overlap_and_feather(shape, mode, metallic, target):
    rng = np.random.default_rng(42)
    image = rng.integers(0, 256, (*shape, 3), dtype=np.uint8)
    first = np.zeros(shape, np.float32)
    second = first.copy()
    first[17:73, 19:61] = rng.random((56, 42))
    second[35:80, 42:91] = rng.random((45, 49)) * 0.7
    layers = [first, second, np.zeros(shape, np.float32)]
    options = core.RecolorOptions(lightness_mode=mode)
    expected = original_recolor(image, layers, target, metallic, options)
    actual = core.recolor_image(image, layers, target, metallic, options)
    assert np.array_equal(actual, expected)
    for weight in layers[:2]:
        expected_lab = np.median(bgr_to_lab(actual)[core._core_pixels(weight)], axis=0)
        assert np.array_equal(core.measure_lab(actual, weight), expected_lab)


def test_roi_never_converts_the_full_large_frame(monkeypatch):
    image = np.full((1024, 2048, 3), 128, np.uint8)
    weight = np.zeros(image.shape[:2], np.float32)
    weight[300:400, 811:951] = 1
    sizes = []
    original = core.bgr_to_lab
    def record(value):
        sizes.append(value.shape[:2])
        return original(value)
    monkeypatch.setattr(core, "bgr_to_lab", record)
    output = core.recolor_image(image, [weight], "#334455")
    core.measure_lab(output, weight)
    assert sizes and all(height == 100 and width <= 192 for height, width in sizes)


def test_final_output_uploads_overlap_and_history_never_copies_them(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-performance-bucket")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    barrier, lock = Barrier(2), Lock()
    writes = {}
    class Client:
        def generate_presigned_url(self, *a, **k):
            return "https://example.test/signed"
        def put_object(self, **kwargs):
            if kwargs["ContentType"] == "image/png":
                barrier.wait(timeout=5)  # fails if the two uploads are sequential
            with lock:
                writes[kwargs["Key"]] = kwargs["Body"]
        def copy_object(self, **kwargs):
            pytest.fail("PNG was redundantly copied after final upload")
    monkeypatch.setattr(storage, "s3", lambda: Client())
    pending = history.PendingResult()
    file, preview = pending.store((b"image", "A.png", "image/png"), (b"preview", "A-preview.png", "image/png"))
    result = history.record("png", "A", {"file": file, "preview": preview}, {}, pending=pending)
    assert pending.committed
    assert read_file_token(result["file"]["token"])["key"] == f"history/files/{pending.entry_id}/result/A.png"
    assert len(writes) == 3 and len([key for key in writes if key.startswith("temporary/")]) == 0


def test_failed_pending_output_is_cleaned_but_committed_history_is_kept(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    pending = history.PendingResult()
    pending.store((b"image", "A.png", "image/png"), (b"preview", "A-preview.png", "image/png"))
    keys = pending.keys.copy()
    assert all((tmp_path / key).exists() for key in keys)
    pending.cleanup()
    assert all(not (tmp_path / key).exists() for key in keys)
    other = history.PendingResult()
    file, preview = other.store((b"image", "A.png", "image/png"), (b"preview", "A-preview.png", "image/png"))
    history.record("png", "A", {"file": file, "preview": preview}, {}, pending=other)
    other.cleanup()
    assert all((tmp_path / key).exists() for key in other.keys)


def test_sequential_batch_reuses_one_validated_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    import uuid
    batch = {"batch_id": str(uuid.uuid4()), "batch_index": 0, "batch_size": 2}
    file = storage.save_bytes(b"image", "A.png", "image/png")
    history.record("png", "A", {"file": file}, {}, batch=batch)
    original, reads = history.read_json, []
    def record(key, **kwargs):
        reads.append(key)
        return original(key, **kwargs)
    monkeypatch.setattr(history, "read_json", record)
    history.record("png", "B", {"file": file}, {}, batch={**batch, "batch_index": 1})
    assert len(reads) == 1 and reads[0].startswith(history.BATCH_PREFIX)


def test_api_exposes_safe_stage_timings_and_no_temporary_output(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    with TestClient(app) as client:
        response = client.post("/api/png/render", json={"template_id": "demo", "color": {"code": "PERF", "hex": "#334455"}})
        assert response.status_code == 200, response.text
        timing = response.headers["Server-Timing"]
        assert all(name + ";dur=" in timing for name in ("read", "process", "encode", "upload", "history", "total"))
        assert "token" not in timing and "key" not in timing
        assert not list((tmp_path / "temporary").rglob("*.png"))
        result = response.json()
        assert client.get(result["file"]["url"]).status_code == 200


def test_api_failed_preview_upload_removes_only_its_new_output(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    original = storage.save_bytes
    kept = tmp_path / "history/files/existing/result.png"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"user-owned result")
    def failing_save(data, name, content_type, asset_key=None):
        if name.endswith("-preview.png"):
            raise RuntimeError("Simulated preview upload failure")
        return original(data, name, content_type, asset_key=asset_key)
    monkeypatch.setattr(storage, "save_bytes", failing_save)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/png/render", json={"template_id": "demo", "color": {"code": "FAIL", "hex": "#334455"}})
    assert response.status_code == 500
    assert list((tmp_path / "history/files").rglob("*.png")) == [kept]
    assert kept.read_bytes() == b"user-owned result"
    assert not list((tmp_path / "history/index").glob("*.json"))
