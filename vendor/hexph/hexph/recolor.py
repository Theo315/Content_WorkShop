"""LAB recoloring core: swap chroma inside the mask, keep the shading relief."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import cv2
import numpy as np

from .colors import bgr_to_lab, delta_e76, hex_to_lab, lab_to_bgr

KEEP = "keep"
MATCH = "match"


@dataclasses.dataclass
class RecolorOptions:
    """Tuning knobs for one batch run."""

    lightness_mode: str = MATCH
    """`match` re-centres L on the target lightness (keeps relief, hits ΔE < 5).
    `keep` leaves L exactly as in the template — literal ТЗ 4.2 behaviour."""

    feather: float = 1.2
    """Gaussian sigma (px) applied to mask edges so the repaint has no jaggies."""

    min_contrast: float = 0.55
    """Lower bound for L-range compression in `match` mode, so dark/light targets
    keep visible volume instead of flattening into a silhouette."""

    metallic_saturation: float = 0.12
    """Metallic flop: ±12 % chroma between shadows and highlights (mid-tones,
    i.e. what a pipette reads, stay on target)."""

    metallic_contrast: float = 1.08
    """Metallic flake: +8 % L contrast inside the mask."""


def prepare_weight(mask: np.ndarray, feather: float = 1.2) -> np.ndarray:
    """Binary mask -> float32 blend weight in [0, 1] with soft edges."""
    if mask.ndim == 3:
        mask = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY)
    weight = (mask.astype(np.float32) / 255.0).clip(0.0, 1.0)
    if feather > 0:
        weight = cv2.GaussianBlur(weight, (0, 0), sigmaX=float(feather), sigmaY=float(feather))
    return weight


def combine_weights(*weights: np.ndarray) -> np.ndarray:
    out = weights[0].copy()
    for extra in weights[1:]:
        out = np.maximum(out, extra)
    return out


def _core_pixels(weight: np.ndarray, threshold: float = 0.9) -> np.ndarray:
    """Indices of "solidly inside the mask" pixels — used for statistics."""
    core = weight >= threshold
    if not core.any():
        core = weight >= max(weight.max() * 0.5, 1e-3)
    return core


def measure_lab(image_bgr: np.ndarray, weight: np.ndarray, threshold: float = 0.9) -> np.ndarray:
    """Median LAB of the masked area — matches what a pipette on a panel reads."""
    lab = bgr_to_lab(image_bgr)
    core = _core_pixels(weight, threshold)
    return np.median(lab[core], axis=0).astype(np.float32)


def _match_lightness(
    lightness: np.ndarray,
    core: np.ndarray,
    target_l: float,
    min_contrast: float,
) -> tuple[np.ndarray, tuple[float, float]]:
    """Re-centre L on `target_l`, compressing the range only as much as needed.

    Highlights and shadows are scaled **independently**. One shared factor would be
    dictated by whichever side runs out of room first: repainting a black template
    (median L 13, speculars up to 67) in a light colour has no headroom above, and a
    shared factor would squash the shadows too, leaving a flat silhouette.
    """
    core_l = lightness[core]
    centre = float(np.median(core_l))
    deviation = lightness - centre

    high = float(np.percentile(core_l, 98) - centre)
    low = float(centre - np.percentile(core_l, 2))

    scale_up = 1.0 if high <= 1e-3 else float(
        np.clip((100.0 - target_l) / high, min_contrast, 1.0)
    )
    scale_down = 1.0 if low <= 1e-3 else float(np.clip(target_l / low, min_contrast, 1.0))

    scaled = np.where(deviation >= 0.0, deviation * scale_up, deviation * scale_down)
    return np.clip(target_l + scaled, 0.0, 100.0), (scale_up, scale_down)


def _metallic_flop(
    lab: np.ndarray,
    lightness: np.ndarray,
    core: np.ndarray,
    options: RecolorOptions,
) -> np.ndarray:
    """Fake the metallic look: more L contrast, chroma flop shadows -> highlights.

    Heuristic, not a physical model — flagged in the docs as "needs a visual check".
    """
    core_l = lightness[core]
    centre = float(np.median(core_l))
    span_high = max(float(np.percentile(core_l, 98) - centre), 1e-3)
    span_low = max(float(centre - np.percentile(core_l, 2)), 1e-3)
    deviation = lightness - centre

    highlight = np.clip(deviation / span_high, 0.0, 1.0)
    shadow = np.clip(-deviation / span_low, 0.0, 1.0)

    lab = lab.copy()
    lab[:, :, 0] = np.clip(centre + deviation * options.metallic_contrast, 0.0, 100.0)
    chroma_scale = 1.0 + options.metallic_saturation * (shadow - highlight)
    lab[:, :, 1] *= chroma_scale
    lab[:, :, 2] *= chroma_scale
    return lab


def recolor_image(
    image_bgr: np.ndarray,
    weights: np.ndarray | Sequence[np.ndarray],
    target_hex: str,
    metallic: bool = False,
    options: RecolorOptions | None = None,
) -> np.ndarray:
    """Repaint every weighted area to `target_hex`, preserving shadows/highlights.

    Pass the masks as separate weights (body, marker cap, …) rather than one
    merged mask: each region gets its own lightness statistics. A matte marker cap
    and a glossy body have different median L, and mixing them into one sample
    biases both away from the target.
    """
    options = options or RecolorOptions()
    if options.lightness_mode not in (MATCH, KEEP):  # pragma: no cover - argparse guards
        raise ValueError(f"unknown lightness mode: {options.lightness_mode}")
    layers = [weights] if isinstance(weights, np.ndarray) else list(weights)

    target = hex_to_lab(target_hex)
    lab = bgr_to_lab(image_bgr)
    # Composite in BGR, not LAB: outside the masks (weight == 0) the result stays
    # bit-identical to the template — no ±1 drift on glass/tires/lights.
    merged = image_bgr.astype(np.float32)

    for weight in layers:
        if not weight.any():
            continue
        core = _core_pixels(weight)
        painted = lab.copy()
        painted[:, :, 1] = target[1]
        painted[:, :, 2] = target[2]
        if options.lightness_mode == MATCH:
            painted[:, :, 0], _ = _match_lightness(
                lab[:, :, 0], core, float(target[0]), options.min_contrast
            )
        if metallic:
            painted = _metallic_flop(painted, painted[:, :, 0], core, options)
        blend = weight[:, :, None]
        merged = merged + (lab_to_bgr(painted).astype(np.float32) - merged) * blend

    return np.rint(np.clip(merged, 0, 255)).astype(np.uint8)


@dataclasses.dataclass
class TemplateStats:
    """How well a template can serve as a recolouring base."""

    lightness: float  # median L of the body
    spread: float  # p2..p98 of L — the shading range that carries the volume
    chroma: float  # C* of the body colour; neutral bodies clash with glass/tyres
    high: float
    low: float

    def contrast_retained(self, target_l: float, min_contrast: float = 0.55) -> float:
        """Share of the template's shading range that survives at this target L.

        Same maths as `_match_lightness`, so this predicts the real result without
        rendering: a dark template has no headroom above and flattens light colours.
        """
        if self.high + self.low <= 1e-6:
            return 0.0
        up = 1.0 if self.high <= 1e-3 else float(
            np.clip((100.0 - target_l) / self.high, min_contrast, 1.0)
        )
        down = 1.0 if self.low <= 1e-3 else float(np.clip(target_l / self.low, min_contrast, 1.0))
        return (self.high * up + self.low * down) / (self.high + self.low)


def template_stats(image_bgr: np.ndarray, weight: np.ndarray) -> TemplateStats:
    lab = bgr_to_lab(image_bgr)
    core = _core_pixels(weight)
    lightness = lab[:, :, 0][core]
    centre = float(np.median(lightness))
    body = np.median(lab[core], axis=0)
    return TemplateStats(
        lightness=centre,
        spread=float(np.percentile(lightness, 98) - np.percentile(lightness, 2)),
        chroma=float(np.hypot(body[1], body[2])),
        high=float(np.percentile(lightness, 98) - centre),
        low=float(centre - np.percentile(lightness, 2)),
    )


def color_accuracy(image_bgr: np.ndarray, weight: np.ndarray, target_hex: str) -> float:
    """ΔE76 between the repainted area and the requested HEX (acceptance crit. 1)."""
    return delta_e76(measure_lab(image_bgr, weight), hex_to_lab(target_hex))
