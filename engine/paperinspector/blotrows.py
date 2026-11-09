"""The same row of bands shown under two blots: a reused loading control (actin, tubulin, GAPDH) or any band strip
pasted under another label.

Whole-panel matching misses this case.  A blot panel is often a stack of strips - the target protein above, the
loading control below - and when only the control strip is reused, the rest of the two panels differs, so the panels
as a whole do not match.  Here every blot is cut into its rows of bands and the rows are compared one by one:

1. A row is a run of image lines that contain dark bands, padded with a little background above and below.
2. Each row is searched for inside every other row by template matching, at the same size and at the size implied
   by the band heights, plain and mirrored.  Negatives (light bands on dark) are inverted first.
3. A match must agree twice: band intensities (rank-based, so any brightness or contrast curve is allowed) and the
   fine background noise, in both halves of the strip.  Rows from two different blots share band shapes, never
   their noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from .imgdup import is_grey
from .imgutil import gray
from .models import Panel

ROW_KINDS = {"blot", "micrograph", "other"}
MIN_W = 40                 # px at matching scale
INT_MIN = 0.85             # band intensities (rank correlation) after alignment
FINE_MIN = 0.5             # fine background noise after alignment (unrelated blots: below 0.2)
HALF_MIN = 0.4             # ...in each half of the strip separately
TEXTURE_MIN = 0.25         # share of strip pixels with visible noise; smoothed strips cannot be confirmed
MAX_ROWS = 8
MIN_CALM = 80              # background pixels compared; fewer cannot confirm a copy


@dataclass
class Row:
    panel: Panel
    index: int                 # row number inside the panel, top to bottom
    y0: int                    # padded strip, panel pixels
    y1: int
    core: tuple[int, int]      # the band rows themselves
    g: np.ndarray = field(repr=False)          # strip, float32, dark bands on light (negatives already inverted)
    mask: np.ndarray = field(repr=False)       # True where pixels may be compared (no printed text)
    inverted: bool = False


@dataclass
class RowMatch:
    a: Row
    b: Row
    flip: bool                 # b shows a mirrored left-right
    scale: float               # b's size relative to a
    box_a: list[float]         # panel pixels
    box_b: list[float]
    ncc_int: float
    ncc_fine: float
    halves: float              # the weaker half of the strip

    @property
    def inverted(self) -> bool:
        return self.a.inverted != self.b.inverted


def _fine(g: np.ndarray) -> np.ndarray:
    return cv2.GaussianBlur(g, (0, 0), 0.7) - cv2.GaussianBlur(g, (0, 0), 2.0)


def rows(panel: Panel) -> list[Row]:
    """Rows of bands in a grey blot panel, top to bottom."""
    img = panel.image
    if img is None or panel.kind not in ROW_KINDS or not is_grey(img):
        return []
    g = gray(img).astype(np.float32)
    h, w = g.shape
    if h < 10 or w < MIN_W:
        return []
    inverted = float(np.median(g)) < 110
    if inverted:
        g = 255 - g
    if float(np.median(g)) < 150:
        return []                     # not a blot: no light background around the bands
    if panel.kind != "blot" and not inverted:
        return []                     # a grey micrograph is not a stack of band rows
    tm = panel.text_mask if panel.text_mask is not None and panel.text_mask.shape == (h, w) else None
    bg = float(np.percentile(g, 80))
    dark = np.clip(bg - g, 0, None)
    if tm is not None:
        dark[tm] = 0
    prof = np.percentile(dark, 90, axis=1)
    thr = max(12.0, 0.3 * float(prof.max()))
    if prof.max() < 25:
        return []                     # no bands
    on = prof > thr
    runs: list[list[int]] = []
    for y in np.where(on)[0]:
        if runs and y - runs[-1][1] <= 3:
            runs[-1][1] = int(y)
        else:
            runs.append([int(y), int(y)])
    out: list[Row] = []
    for y0, y1 in runs:
        ch = y1 - y0 + 1
        if ch < 3:
            continue
        pad = max(3, int(0.6 * ch))
        a, b = max(0, y0 - pad), min(h, y1 + 1 + pad)
        strip = g[a:b]
        m = np.ones(strip.shape, bool) if tm is None else ~tm[a:b]
        out.append(Row(panel, len(out), a, b, (y0, y1 + 1), strip, m, inverted))
        if len(out) >= MAX_ROWS:
            break
    return out


def _rank(x: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Replace values by their rank among the compared pixels: any monotonic contrast change drops out."""
    v = x[m]
    r = np.empty(len(v), np.float32)
    r[np.argsort(v, kind="stable")] = np.arange(len(v), dtype=np.float32)
    return r


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


def _calm(g: np.ndarray) -> np.ndarray:
    """Background pixels, away from the bands.  Similar bands look alike in any blot, edges and centres alike; only
    the noise of the background between and around the bands tells one exposure from another."""
    s = cv2.GaussianBlur(g, (0, 0), 2.0)
    bg = float(np.percentile(s, 80))
    gx, gy = cv2.Sobel(s, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(s, cv2.CV_32F, 0, 1, ksize=3)
    near_band = cv2.dilate((s < bg - 12).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    return (np.hypot(gx, gy) < 6.0) & ~near_band


def _no_streaks(fine: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Remove line patterns (one value per column or per row: sensor column offsets, scanner streaks).  Those repeat
    in every picture from one imager; the noise a copy shares with its source is two-dimensional."""
    f = np.where(m, fine, 0).astype(np.float32)
    n_col = np.maximum(m.sum(axis=0, keepdims=True), 1)
    f = np.where(m, f - f.sum(axis=0, keepdims=True) / n_col, 0)
    n_row = np.maximum(m.sum(axis=1, keepdims=True), 1)
    return np.where(m, f - f.sum(axis=1, keepdims=True) / n_row, 0)


def _textured(fine: np.ndarray, m: np.ndarray) -> bool:
    return m.sum() >= MIN_CALM and float((np.abs(fine[m]) > 0.5).mean()) >= TEXTURE_MIN


def _resize(x: np.ndarray, s: float, nearest: bool = False) -> np.ndarray:
    if abs(s - 1) < 1e-3:
        return x
    h, w = x.shape
    size = (max(4, int(round(w * s))), max(4, int(round(h * s))))
    if nearest:
        return cv2.resize(x.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST) > 0
    return cv2.resize(x, size, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)


def _peaks(res: np.ndarray, k: int = 3, sep: int = 6) -> list[tuple[int, int, float]]:
    r = res.copy()
    out = []
    for _ in range(k):
        _, v, _, (x, y) = cv2.minMaxLoc(r)
        if not np.isfinite(v):
            break
        out.append((x, y, float(v)))
        r[max(0, y - sep):y + sep + 1, max(0, x - sep):x + sep + 1] = -2
    return out


def compare(a: Row, b: Row) -> Optional[RowMatch]:
    """Is row `b` (or part of it) the same strip as row `a` (or part of it)?"""
    ha, hb = a.core[1] - a.core[0], b.core[1] - b.core[0]
    scales = [1.0]
    r = hb / max(1, ha)
    if 0.6 < r < 1.7 and abs(r - 1) > 0.06:
        # band heights give the size change only roughly; a few nearby sizes keep a long strip aligned end to end
        scales += [r * k for k in (0.97, 0.985, 1.0, 1.015, 1.03)]
    best: Optional[RowMatch] = None
    for s in scales:
        # bring the larger copy down to the smaller one: shrinking keeps the shared noise, enlarging invents it
        sa, sb = (1.0, 1.0 / s) if s > 1 else (s, 1.0)
        A, Am = _resize(a.g, sa), _resize(a.mask, sa, True)
        B, Bm = _resize(b.g, sb), _resize(b.mask, sb, True)
        for flip in (False, True):
            Af, Afm = (A[:, ::-1], Am[:, ::-1]) if flip else (A, Am)
            m = _match(Af, Afm, B, Bm, (a.core[0] - a.y0) * sa, (a.core[1] - a.y0) * sa,
                       (b.core[0] - b.y0) * sb, (b.core[1] - b.y0) * sb)
            if m is None:
                continue
            (ax, ay, w, h), (bx, by), ri, rf, half = m
            if flip:
                ax = Af.shape[1] - ax - w
            box_a = [ax / sa, a.y0 + ay / sa, w / sa, h / sa]
            box_b = [bx / sb, b.y0 + by / sb, w / sb, h / sb]
            cand = RowMatch(a, b, flip, s, [round(v, 1) for v in box_a], [round(v, 1) for v in box_b], ri, rf, half)
            if best is None or (rf, ri) > (best.ncc_fine, best.ncc_int):
                best = cand
    return best


def _match(A, Am, B, Bm, a_c0, a_c1, b_c0, b_c1):
    """Template-match the narrower strip inside the wider one.  Returns ((x, y, w, h) in A, (x, y) in B, intensity
    correlation, noise correlation, weaker half) or None."""
    swap = A.shape[1] > B.shape[1]
    if swap:
        A, Am, B, Bm, a_c0, a_c1, b_c0, b_c1 = B, Bm, A, Am, b_c0, b_c1, a_c0, a_c1
    # the template: the band rows of the narrower strip with as much background above and below as the other
    # strip leaves room for (the background carries the noise that confirms a copy)
    c0, c1 = int(a_c0), int(np.ceil(a_c1))
    room = max(0, (B.shape[0] - 2 - (c1 - c0)) // 2)
    t0, t1 = max(0, c0 - room), min(A.shape[0], c1 + room)
    T, Tm = A[t0:t1], Am[t0:t1]
    if T.shape[0] > B.shape[0] or T.shape[0] < 4:
        return None
    windows = [(0, T.shape[1])]
    if T.shape[1] >= 3 * MIN_W:         # the two strips may each hold lanes the other lacks
        k = int(T.shape[1] * 0.65)
        windows += [(0, k), (T.shape[1] - k, T.shape[1])]
    b_core = B[max(0, int(b_c0)):max(int(b_c0) + 1, int(np.ceil(b_c1)))].mean(axis=0, keepdims=True).astype(np.float32)
    lazy: dict = {}

    def layers():
        if not lazy:
            lazy.update(fa=_fine(A), fb=_fine(B), ca=_calm(A), cb=_calm(B))
        return lazy["fa"], lazy["fb"], lazy["ca"], lazy["cb"]
    best = None
    for x0, x1 in windows:
        Tw, Twm = T[:, x0:x1], Tm[:, x0:x1]
        if Tw.shape[1] < MIN_W or Tw.shape[1] < 2.5 * (a_c1 - a_c0) or Tw.shape[1] > B.shape[1]:
            continue
        # quick screen on the lane pattern (darkness per column) before the full 2-D search
        pt = Tw[max(0, c0 - t0):c1 - t0].mean(axis=0, keepdims=True).astype(np.float32)
        pb = B.mean(axis=0, keepdims=True).astype(np.float32) if b_core is None else b_core
        if pt.shape[1] <= pb.shape[1] and float(cv2.matchTemplate(pb, pt, cv2.TM_CCOEFF_NORMED).max()) < 0.75:
            continue
        res = cv2.matchTemplate(B, Tw, cv2.TM_CCOEFF_NORMED)
        for px, py, v in _peaks(res):
            if v < 0.6:
                continue
            Bw, Bwm = B[py:py + Tw.shape[0], px:px + Tw.shape[1]], Bm[py:py + Tw.shape[0], px:px + Tw.shape[1]]
            m = Twm & Bwm
            m[:3], m[-3:], m[:, :3], m[:, -3:] = False, False, False, False      # blur edges of the strip
            if m.sum() < 200:
                continue
            fa_all, fb_all, ca_all, cb_all = layers()
            fa = fa_all[t0:t1, x0:x1]
            fb = fb_all[py:py + Tw.shape[0], px:px + Tw.shape[1]]
            mc = m & ca_all[t0:t1, x0:x1] & cb_all[py:py + Tw.shape[0], px:px + Tw.shape[1]]
            if mc.sum() < MIN_CALM or not (_textured(fa, mc) and _textured(fb, mc)):
                continue
            fa, fb = _no_streaks(fa, mc), _no_streaks(fb, mc)
            rf = _ncc(fa[mc], fb[mc])
            if rf < FINE_MIN or (best is not None and rf <= best[3]):
                continue
            mid = Tw.shape[1] // 2
            left, right = mc.copy(), mc.copy()
            left[:, mid:] = False
            right[:, :mid] = False
            if left.sum() < MIN_CALM // 3 or right.sum() < MIN_CALM // 3:
                continue
            half = min(_ncc(fa[left], fb[left]), _ncc(fa[right], fb[right]))
            if half < HALF_MIN:
                continue
            # last (it is the slow one): band intensities agree up to any brightness or contrast curve
            ri = _ncc(_rank(Tw, m), _rank(Bw, m))
            if ri < INT_MIN:
                continue
            best = ((x0, t0, Tw.shape[1], Tw.shape[0]), (px, py), ri, rf, half)
    if best is None:
        return None
    (ax, ay, w, h), (bx, by), ri, rf, half = best
    if swap:
        return (bx, by, w, h), (ax, ay), ri, rf, half
    return (ax, ay, w, h), (bx, by), ri, rf, half


def _same_place(m: RowMatch) -> bool:
    """Two panels cut from overlapping parts of one figure show the same rendered pixels: not a reuse."""
    pa, pb = m.a.panel, m.b.panel
    if pa.figure_id != pb.figure_id or m.flip or abs(m.scale - 1) > 0.02:
        return False
    ax, ay = pa.box.x + m.box_a[0], pa.box.y + m.box_a[1]
    bx, by = pb.box.x + m.box_b[0], pb.box.y + m.box_b[1]
    return abs(ax - bx) <= 3 and abs(ay - by) <= 3


def find(panels: list[Panel], skip: Optional[set] = None) -> list[RowMatch]:
    """Band rows repeated across blot panels (and across rows of one panel).  `skip` holds frozensets of panel ids
    already reported as copies of each other.  At most one match per pair of panels: the strongest."""
    skip = skip or set()
    allrows = [r for p in panels for r in rows(p)]
    best: dict[tuple[str, str], RowMatch] = {}
    for i in range(len(allrows)):
        for j in range(i + 1, len(allrows)):
            a, b = allrows[i], allrows[j]
            same_panel = a.panel is b.panel
            if same_panel and (a.y1 > b.y0 and b.y1 > a.y0):
                continue                # overlapping strips of one panel
            key = (a.panel.id, b.panel.id)
            if not same_panel and frozenset(key) in skip:
                continue
            m = compare(a, b)
            if m is None or _same_place(m):
                continue
            if key not in best or (m.ncc_fine, m.ncc_int) > (best[key].ncc_fine, best[key].ncc_int):
                best[key] = m
    return list(best.values())
