"""Single-image forensics: splice seams in blots, flat painted patches, and the viewing modes a sleuth uses.

Sholto David's most-used trick is to crank brightness and contrast until the background shows its seams; the
`view()` modes do that and more, so a person can look at any panel the way the detectors do.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .imgutil import dog, gray


@dataclass
class Seam:
    axis: str          # "x": a vertical seam at column `pos`; "y": horizontal
    pos: float
    step: float        # background brightness jump across the seam (grey levels)
    consistency: float  # share of rows (or columns) that agree on the jump
    z: float


def seams(img: np.ndarray, text_mask: np.ndarray | None = None, axes: tuple[str, ...] = ("x",)) -> list[Seam]:
    """Straight vertical or horizontal lines where the background jumps: two images butted together (a spliced blot,
    a lane pasted in).  Honest blots have smooth backgrounds; a splice shows a step that runs the full height."""
    g = gray(img).astype(np.float32)
    out: list[Seam] = []
    for axis in axes:
        a = g if axis == "x" else g.T
        tm = None if text_mask is None else (text_mask if axis == "x" else text_mask.T)
        h, w = a.shape
        if w < 30 or h < 16:
            continue
        # step between the 2 columns left and the 2 columns right of each boundary
        left = (a[:, 0:w - 3] + a[:, 1:w - 2]) / 2
        right = (a[:, 2:w - 1] + a[:, 3:w]) / 2
        step = right - left                         # (h, w-3), boundary between col i+1 and i+2
        if tm is not None:
            bad = tm[:, 1:w - 2] | tm[:, 2:w - 1]
            step = np.where(bad, np.nan, step)
        # trimmed mean, not median: grey levels are integers and a median of them is too coarse to rank seams
        lo, hi = np.nanpercentile(step, (20, 80), axis=0)
        clipped = np.where((step >= lo) & (step <= hi), step, np.nan)
        med = np.nanmean(clipped, axis=0)
        sign = np.sign(med)
        agree = np.nanmean((np.sign(step) == sign[None, :]) & (np.abs(step) > 1.0), axis=0)
        robust = np.nanmedian(np.abs(med - np.nanmedian(med))) * 1.4826 + 0.15
        z = np.abs(med - np.nanmedian(med)) / robust
        for i in np.argsort(-z)[:3]:
            pos = i + 1.5
            if pos < 10 or pos > w - 10:
                continue
            if z[i] >= 5.5 and agree[i] >= 0.65 and 2.0 <= abs(med[i]) <= 40:   # bigger jumps are object edges
                # a band edge is local; a seam is everywhere - require the jump in both halves of the strip
                top = np.nanmean(clipped[: h // 2, i])
                bot = np.nanmean(clipped[h // 2:, i])
                if np.sign(top) == np.sign(bot) == sign[i] and min(abs(top), abs(bot)) > 1.0:
                    if any(o.axis == axis and abs(o.pos - pos) <= 4 for o in out):
                        continue        # the same seam seen one column over
                    out.append(Seam(axis, float(pos), float(med[i]), float(agree[i]), float(z[i])))
    return out


@dataclass
class Flat:
    box: list[float]
    area: float
    value: float
    ring_noise: float


def flat_patches(img: np.ndarray, text_mask: np.ndarray | None = None) -> list[Flat]:
    """Patches with no camera noise at all inside an otherwise noisy image: a brush stroke of one colour, a pasted
    rectangle of solid background.  Clipped black or white that the image has everywhere is not counted."""
    g = gray(img)
    h, w = g.shape
    if min(h, w) < 30:
        return []
    f = g.astype(np.float32)
    mean = cv2.blur(f, (5, 5))
    var = cv2.blur(f * f, (5, 5)) - mean * mean
    flat = (var < 0.3).astype(np.uint8)
    if text_mask is not None:
        flat[text_mask] = 0
    flat = cv2.morphologyEx(flat, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    noise = np.abs(dog(g, 0.7, 2.0))
    if np.median(noise) < 0.8:
        return []          # a clean (rendered, drawn) image: flatness means nothing here
    n, lab, st, _ = cv2.connectedComponentsWithStats(flat, 8)
    out = []
    min_area = max(60, 0.002 * h * w)
    for i in range(1, n):
        x, y, bw, bh, area = st[i]
        if area < min_area or area > 0.3 * h * w:
            continue
        comp = lab == i
        val = float(np.median(g[comp]))
        if val <= 2 or val >= 253:
            elsewhere = ((g <= 2) | (g >= 253)).mean()
            if elsewhere > 0.02 or x == 0 or y == 0 or x + bw == w or y + bh == h:
                continue        # clipping, not painting
        ring = cv2.dilate(comp.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool) & ~comp
        rn = float(np.median(noise[ring])) if ring.any() else 0.0
        if rn < 1.0:
            continue
        out.append(Flat([float(x), float(y), float(bw), float(bh)], float(area), val, rn))
    return out


VIEW_MODES = ("original", "levels", "reveal", "noise", "edges", "false")


def view(img: np.ndarray, mode: str) -> np.ndarray:
    """Re-render a panel for inspection.

    levels  - stretch the darkest and brightest 0.5% to full range (what "brightness/contrast" does by hand)
    reveal  - strong local contrast (CLAHE) plus gamma: makes pasted rectangles and painted strokes visible
    noise   - the camera-noise layer alone, amplified: copies share it, paint has none, splices change it
    edges   - straight lines and hard rectangle corners
    false   - false colour of brightness, separates near-identical greys
    """
    if mode == "original":
        return img
    g = gray(img)
    if mode == "levels":
        lo, hi = np.percentile(img, (0.5, 99.5))
        return np.clip((img.astype(np.float32) - lo) * 255.0 / max(hi - lo, 1), 0, 255).astype(np.uint8)
    if mode == "reveal":
        clahe = cv2.createCLAHE(clipLimit=6.0, tileGridSize=(8, 8))
        e = clahe.apply(g)
        e = (255 * (e / 255.0) ** 0.6).astype(np.uint8)
        return cv2.cvtColor(e, cv2.COLOR_GRAY2BGR)
    if mode == "noise":
        r = dog(g, 0.6, 2.0)
        r = np.clip(128 + r * 18, 0, 255).astype(np.uint8)
        return cv2.cvtColor(r, cv2.COLOR_GRAY2BGR)
    if mode == "edges":
        e = cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 20, 60)
        return cv2.cvtColor(255 - e, cv2.COLOR_GRAY2BGR)
    if mode == "false":
        eq = cv2.equalizeHist(g)
        return cv2.applyColorMap(eq, cv2.COLORMAP_TURBO)
    raise ValueError(f"unknown view mode {mode}")
