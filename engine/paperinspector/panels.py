"""Cut a figure into panels and guess what each panel shows.

Panels are separated by gutters: rows or columns that are (almost) one flat colour.  A recursive XY-cut finds them,
which handles the usual grids and the ragged layouts journals produce.  Each leaf is trimmed to its content and typed
(blot, micrograph, photo, chart, text) so later checks know what to compare with what.
"""
from __future__ import annotations

import string

import cv2
import numpy as np

from .models import Box, Figure, Panel

MIN_PANEL = 28            # px; smaller leaves are labels, arrows, scale bars
MIN_GAP = 3               # px of flat background between panels
MAX_DEPTH = 8


def segment(fig: Figure) -> list[Panel]:
    img = fig.image
    if img is None:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    flat = _flat_mask(img, gray)
    boxes: list[tuple[int, int, int, int]] = []
    H, W = gray.shape
    placed = [b for b in fig.image_boxes if b[2] * b[3] >= MIN_PANEL * MIN_PANEL]
    overlays = _overlays(placed)
    if len(placed) >= 2:
        # one raster per panel: the PDF already knows where each panel is.  Only a big composite raster is cut further.
        for bx, by, bw, bh in placed:
            x0, y0 = int(max(bx, 0)), int(max(by, 0))
            x1, y1 = int(min(bx + bw, W)), int(min(by + bh, H))
            if bw * bh >= 0.35 * W * H:
                _xycut(flat, x0, y0, x1 - x0, y1 - y0, 0, boxes)
            else:
                boxes.append(_trim(flat, x0, y0, x1 - x0, y1 - y0))
        boxes = [b for b in boxes if b is not None]
    else:
        _xycut(flat, 0, 0, W, H, 0, boxes)
    line_art = stats(img)["flat_light"] > 0.75     # a figure of plots and diagrams, not of pictures
    min_side = max(MIN_PANEL, int(0.035 * min(H, W)))   # labels and tick marks in a high-resolution figure are not panels
    panels: list[Panel] = []
    for (x, y, w, h) in sorted(boxes, key=lambda b: (round(b[1] / 40), b[0])):
        if w < min_side or h < min_side:
            continue
        crop = img[y:y + h, x:x + w]
        kind = classify(crop, line_art)
        tm = np.zeros((h, w), bool)
        for bx, by, bw, bh in fig.text_boxes:
            x0, y0 = int(max(bx - x, 0)), int(max(by - y, 0))
            x1, y1 = int(min(bx + bw - x, w)), int(min(by + bh - y, h))
            if x1 > x0 and y1 > y0:
                tm[y0:y1, x0:x1] = True
        p = Panel(id="", figure_id=fig.id, box=Box(x, y, w, h), kind=kind, image=crop,
                  text_mask=tm if tm.any() else None)
        om = _overlay_mask(overlays, x, y, w, h)
        if om is not None:
            p.overlay_mask = om   # type: ignore[attr-defined]
        panels.append(p)
    letters = _labels(len(panels))
    for p, l in zip(panels, letters):
        p.id = f"{fig.id}.{l}"
    fig.panels = panels
    return panels


def _overlays(placed: list) -> list:
    """Rasters placed on top of a bigger raster (a magnified inset, a colour key, a logo).  The rendered figure is a
    composite, so the inset's pixels also sit inside the panel cut from the raster beneath; that panel must not
    compare them, or the inset 'duplicates' its own pixels."""
    out = []
    for i, (x, y, w, h) in enumerate(placed):
        for j, (X, Y, W, H) in enumerate(placed):
            if i == j or W * H <= w * h:
                continue
            iw = min(x + w, X + W) - max(x, X)
            ih = min(y + h, Y + H) - max(y, Y)
            if iw > 0 and ih > 0 and iw * ih >= 0.8 * w * h:
                out.append((x, y, w, h))
                break
    return out


def _overlay_mask(overlays: list, x: int, y: int, w: int, h: int):
    """Where overlays cover this panel, unless the panel is the overlay itself."""
    m = None
    for ox, oy, ow, oh in overlays:
        ox, oy, ow, oh = ox - 2, oy - 2, ow + 4, oh + 4       # the inset's resampled edge
        x0, y0 = max(ox, x), max(oy, y)
        x1, y1 = min(ox + ow, x + w), min(oy + oh, y + h)
        if x1 <= x0 or y1 <= y0 or (x1 - x0) * (y1 - y0) >= 0.8 * w * h:
            continue
        if m is None:
            m = np.zeros((h, w), bool)
        m[int(y0 - y):int(np.ceil(y1 - y)), int(x0 - x):int(np.ceil(x1 - x))] = True
    return m


def _labels(n: int) -> list[str]:
    al = string.ascii_lowercase
    return [al[i] if i < 26 else al[i // 26 - 1] + al[i % 26] for i in range(n)]


def _flat_mask(img: np.ndarray, gray: np.ndarray) -> np.ndarray:
    """True where a pixel looks like page background: near-white (a blot's grey background is content, not gutter),
    or - for figures drawn on their own uniform dark or coloured ground - that ground colour."""
    white = gray >= 246
    border = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
    bg = float(np.median(border))
    if bg < 240 and border.std() < 4:
        return white | (np.abs(gray.astype(np.int16) - int(bg)) <= 4)
    return white


def _trim(flat: np.ndarray, x: int, y: int, w: int, h: int):
    sub = flat[y:y + h, x:x + w]
    rows = np.where(~sub.all(axis=1))[0]
    cols = np.where(~sub.all(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    return (x + int(cols[0]), y + int(rows[0]), int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1))


def _runs(profile: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """Runs of True at least min_len long, as (start, end)."""
    out, start = [], None
    for i, v in enumerate(profile):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_len:
                out.append((start, i))
            start = None
    if start is not None and len(profile) - start >= min_len:
        out.append((start, len(profile)))
    return out


def _xycut(flat: np.ndarray, x: int, y: int, w: int, h: int, depth: int, out: list) -> None:
    sub = flat[y:y + h, x:x + w]
    if sub.size == 0:
        return
    # trim flat margins
    rows = np.where(~sub.all(axis=1))[0]
    cols = np.where(~sub.all(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        return
    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    x, y, w, h = x + x0, y + y0, x1 - x0, y1 - y0
    sub = flat[y:y + h, x:x + w]
    if depth >= MAX_DEPTH or w < MIN_PANEL * 2 and h < MIN_PANEL * 2:
        out.append((x, y, w, h))
        return
    # a gutter row/column is ≥ 98.5% background (tolerates a stray dot)
    row_flat = sub.mean(axis=1) >= 0.985
    col_flat = sub.mean(axis=0) >= 0.985
    rgaps = [g for g in _runs(row_flat, MIN_GAP) if g[0] > 0 and g[1] < h]
    cgaps = [g for g in _runs(col_flat, MIN_GAP) if g[0] > 0 and g[1] < w]
    if not rgaps and not cgaps:
        out.append((x, y, w, h))
        return
    # cut along the axis with the widest gutter; that is the outermost structure of the layout
    rbest = max((g[1] - g[0] for g in rgaps), default=0)
    cbest = max((g[1] - g[0] for g in cgaps), default=0)
    if rbest >= cbest:
        edges = [0] + [v for g in rgaps for v in g] + [h]
        for a, b in zip(edges[::2], edges[1::2]):
            if b - a > 0:
                _xycut(flat, x, y + a, w, b - a, depth + 1, out)
    else:
        edges = [0] + [v for g in cgaps for v in g] + [w]
        for a, b in zip(edges[::2], edges[1::2]):
            if b - a > 0:
                _xycut(flat, x + a, y, b - a, h, depth + 1, out)


def stats(crop: np.ndarray) -> dict:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    sat = hsv[:, :, 1].astype(np.float32) / 255.0
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    small = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA) if min(gray.shape) > 8 else gray
    q = (crop // 32).reshape(-1, 3)
    uniq = len(np.unique(q[:: max(1, len(q) // 20000)], axis=0))
    ink = gray[gray < 235]
    if len(ink) > 50:
        hist = np.bincount(ink.ravel(), minlength=256).astype(np.float64)
        pr = hist[hist > 0] / hist.sum()
        tone_entropy = float(-(pr * np.log2(pr)).sum())
    else:
        tone_entropy = 0.0
    g32 = gray.astype(np.float32)
    m = cv2.blur(g32, (3, 3))
    local_sd = np.sqrt(np.maximum(cv2.blur(g32 * g32, (3, 3)) - m * m, 0))
    flat_light = float(((local_sd < 1.8) & (gray > 235)).mean())
    ink = gray < 200
    dark_ink = float((gray[ink] < 90).mean()) if ink.sum() > 30 else 0.0
    nonwhite = gray < 235
    mid_share = float(((gray > 90) & nonwhite).sum() / max(1, nonwhite.sum()))
    flat_any = _flat_share(gray)
    mid = (gray > 25) & (gray < 235)
    flat_color = float(((local_sd < 1.0) & mid).sum() / max(1, mid.sum())) if mid.sum() > 100 else 0.0
    long_axis = 0 if gray.shape[0] >= gray.shape[1] else 1
    along_var = float(g32.mean(axis=1 - long_axis).std())          # change along the long side
    cross_var = float(g32.std(axis=1 - long_axis).mean())          # change across it
    pure_white = float((gray >= 254).mean())
    return {
        "pure_white": pure_white,
        "flat_any": flat_any,
        "mid_share": mid_share,
        "flat_color": flat_color,
        "along_var": along_var,
        "cross_var": cross_var,
        "flat_light": flat_light,
        "dark_ink": dark_ink,
        "tone_entropy": tone_entropy,
        "mean": float(gray.mean()),
        "white": float((gray > 235).mean()),
        "dark": float((gray < 40).mean()),
        "sat": float(sat.mean()),
        "texture": float(np.abs(lap).mean()),
        "colors": uniq,
        "row_var": float(small.mean(axis=1).std()),
        "col_var": float(small.mean(axis=0).std()),
    }


def _flat_share(gray: np.ndarray, side: int = 160) -> float:
    """Share of non-black pixels with no texture at all, measured at a fixed coarse scale: upscaling (common in
    published blots) smooths noise away, so the panel is first brought down to ~160 px on its short side."""
    s = min(1.0, side / max(1, min(gray.shape)))
    g = gray if s >= 1 else cv2.resize(gray, (max(8, int(gray.shape[1] * s)), max(8, int(gray.shape[0] * s))),
                                       interpolation=cv2.INTER_AREA)
    g = g.astype(np.float32)
    m = cv2.blur(g, (3, 3))
    sd = np.sqrt(np.maximum(cv2.blur(g * g, (3, 3)) - m * m, 0))
    lit = g > 25
    return float(((sd < 0.5) & lit).sum() / max(1, lit.sum())) if lit.sum() > 100 else 0.0


def classify(crop: np.ndarray, line_art_figure: bool = False) -> str:
    h, w = crop.shape[:2]
    if h < MIN_PANEL or w < MIN_PANEL:
        return "text"
    s = stats(crop)
    if s["pure_white"] > 0.3 and s["dark_ink"] > 0.12 and s["sat"] < 0.4:
        return "chart" if min(h, w) >= 60 else "text"    # rendered page white with black strokes: axes, outlines
    if s["sat"] > 0.12 and (s["flat_color"] > 0.2 and s["white"] > 0.05 or s["flat_color"] > 0.35):
        return "chart"                                    # coloured flat fills: stacked bars, heatmaps, diagrams
    if s["tone_entropy"] < 4.5 and s["colors"] < 40 and s["flat_any"] > 0.2:
        return "chart"                                    # a handful of flat tones: rendered graphics, not a camera
    if s["flat_any"] > 0.47:
        # photographs, scans and micrographs carry noise almost everywhere; plots, diagrams and drawings are mostly
        # flat fills (black backgrounds of fluorescence images are not counted)
        return "chart" if min(h, w) >= 60 else "text"
    if s["white"] > 0.45 and s["sat"] < 0.12 and s["mid_share"] < 0.35:
        return "text"                 # black glyphs on white: a label row, not a blot
    if s["sat"] > 0.12 and s["flat_color"] > 0.6:
        return "chart"                # heatmaps and coloured diagrams: flat fills, no camera noise
    if (h > 4 * w or w > 4 * h) and s["cross_var"] < 3.0 and s["along_var"] > 10:
        return "chart"                # a colour bar or gradient legend
    # line art: flat white page and black strokes (blot bands on a white background are grey, not black)
    if s["dark_ink"] > 0.3 and (s["flat_light"] > 0.45 or line_art_figure and (s["flat_light"] > 0.1 or s["white"] > 0.4)):
        # most of the panel is flat, light background with no camera noise: line art (plots, diagrams, text)
        return "chart" if min(h, w) >= 60 else "text"
    if s["white"] > 0.6 and s["sat"] < 0.08 and s["tone_entropy"] > 5.0 and (w > h * 1.6 or s["texture"] < 6):
        return "blot"                 # a blot on a white background: continuous grey tones, not flat fills
    if s["white"] > 0.6 and s["colors"] < 40 and s["texture"] > 3:
        return "chart" if min(h, w) > 60 else "text"
    if s["white"] > 0.78:
        return "chart"
    if s["dark"] > 0.45 and s["mean"] < 70:
        return "micrograph"           # fluorescence: dark field with coloured signal
    if s["sat"] < 0.08:
        # grayscale: blots and gels are wide strips with horizontal band structure
        if w > h * 1.6 and s["row_var"] > s["col_var"] * 0.8 or w > h * 3:
            return "blot"
        return "micrograph"
    if s["sat"] > 0.18 and s["white"] < 0.25:
        return "micrograph"           # stained tissue
    return "photo"
