"""Copy-move inside one image: a patch of cells, a band or a colony pasted somewhere else in the same picture.

1. SIFT keypoints of the image are matched against each other; a keypoint whose best partner (other than itself)
   is much closer than its second-best is a candidate copy.
2. Copies share one displacement, so candidate pairs are grouped by displacement; a group of several keypoints
   moving together marks a cloned region.
3. The region is confirmed on the fine noise layer, after a sub-pixel search: honest tissue repeats its shapes
   (nuclei, bands) but never its camera noise; a clone tool copies both.

`find_mirrored` does the same for copies that were flipped (left-right or top-bottom) or turned 180 degrees before
they were pasted - the usual way to hide a cloned band or a patch of cells.  Keypoints of the image are matched
against keypoints of its mirror image; a mirrored copy keeps one mirror axis (and one offset along it), so pairs are
grouped by that axis instead of by a shift.  The fine noise layer then has to agree under the same mirror map.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass

import cv2
import numpy as np

from .imgutil import fit_scale, gray, periodicity

MIN_PAIRS = 5
RATIO = 0.8
REGION_NCC = 0.8
MIN_SIDE = 18          # px at working scale


@dataclass
class Clone:
    src: list[float]       # x, y, w, h in panel pixels
    dst: list[float]
    shift: tuple[float, float]
    blocks: int            # keypoint pairs supporting it
    ncc: float
    mode: str = "shift"    # shift | mirror-h (flipped left-right) | mirror-v (flipped top-bottom) | rot180


_sift = None


def _det():
    global _sift
    if _sift is None:
        _sift = cv2.SIFT_create(nfeatures=2500, contrastThreshold=0.01, edgeThreshold=15)
    return _sift


def _fine(g: np.ndarray) -> np.ndarray:
    """Fine texture: noise and speckle, a little smoothed so a copy resampled at another sub-pixel phase (every PDF
    figure is resampled) still agrees with its source."""
    f = g.astype(np.float32)
    return cv2.GaussianBlur(f, (0, 0), 0.7) - cv2.GaussianBlur(f, (0, 0), 2.0)


_MIRROR_CACHE: "OrderedDict[str, list[Clone]]" = OrderedDict()
_CACHE_SIZE = 4096          # results only (a few boxes each), so a long-running server stays small
_LOCK = threading.Lock()


def _key(img: np.ndarray, text_mask: np.ndarray | None) -> str:
    h = hashlib.blake2b(np.ascontiguousarray(img).tobytes(), digest_size=16)
    h.update(repr(img.shape).encode())
    if text_mask is not None:
        h.update(np.packbits(text_mask.astype(bool)).tobytes())
    return h.hexdigest()


def find(img: np.ndarray, text_mask: np.ndarray | None = None) -> list[Clone]:
    """Same-orientation copies.  The mirrored search runs in the same pass (it shares the keypoints) and its result
    is kept for `find_mirrored`."""
    same, mirrored = _find_both(img, text_mask)
    _remember(_key(img, text_mask), mirrored)
    return same


def find_mirrored(img: np.ndarray, text_mask: np.ndarray | None = None) -> list[Clone]:
    """Copies inside one image that were flipped left-right, flipped top-bottom or turned 180 degrees."""
    k = _key(img, text_mask)
    with _LOCK:
        hit = _MIRROR_CACHE.get(k)
        if hit is not None:
            _MIRROR_CACHE.move_to_end(k)
            return list(hit)
    _, mirrored = _find_both(img, text_mask)
    _remember(k, mirrored)
    return mirrored


def _remember(k: str, v: list[Clone]) -> None:
    with _LOCK:                         # the server runs scans in threads
        _MIRROR_CACHE[k] = list(v)
        _MIRROR_CACHE.move_to_end(k)
        while len(_MIRROR_CACHE) > _CACHE_SIZE:
            _MIRROR_CACHE.popitem(last=False)


def _find_both(img: np.ndarray, text_mask: np.ndarray | None = None) -> tuple[list[Clone], list[Clone]]:
    g0 = gray(img)
    h0, w0 = g0.shape
    if min(h0, w0) < 40:
        return [], []
    s = fit_scale(h0, w0, max_side=900, min_side=160)
    g = cv2.resize(g0, (int(w0 * s), int(h0 * s)), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC) \
        if s != 1 else g0
    h, w = g.shape
    sx, sy = w / w0, h / h0             # the true scale per axis after int() rounding
    kps, desc = _det().detectAndCompute(cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX), None)
    if desc is None or len(kps) < 12:
        return [], []
    pts = np.float32([k.pt for k in kps])
    if text_mask is not None:
        tm = cv2.resize(text_mask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        keep = ~tm[np.clip(pts[:, 1].astype(int), 0, h - 1), np.clip(pts[:, 0].astype(int), 0, w - 1)]
        pts, desc = pts[keep], desc[keep]
        if len(pts) < 12:
            return [], []
    d = desc / (desc.sum(axis=1, keepdims=True) + 1e-7)
    d = np.sqrt(d).astype(np.float32)
    matches = cv2.BFMatcher(cv2.NORM_L2).knnMatch(d, d, k=3)
    min_sep = max(10.0, 0.04 * min(h, w))
    pairs = []
    seen = set()
    for row in matches:
        cand = [m for m in row if m.trainIdx != m.queryIdx]
        if len(cand) < 2 or cand[0].distance >= RATIO * cand[1].distance:
            continue
        i, j = cand[0].queryIdx, cand[0].trainIdx
        v = pts[j] - pts[i]
        if np.hypot(*v) < min_sep:
            continue
        if v[0] < 0 or (v[0] == 0 and v[1] < 0):
            i, j, v = j, i, -v
        if (i, j) in seen:
            continue
        seen.add((i, j))
        pairs.append((i, j, v))
    fine = _fine(g)
    tmw = tm if text_mask is not None else None
    mirrored = _mirrored(g, fine, pts, d, pairs, min_sep, tmw, (sx, sy))
    if len(pairs) < MIN_PAIRS:
        return [], mirrored

    # group by displacement (4-px bins, neighbours merged)
    bins: dict[tuple[int, int], list[int]] = {}
    for k, (_, _, v) in enumerate(pairs):
        bins.setdefault((int(round(v[0] / 4)), int(round(v[1] / 4))), []).append(k)
    out: list[Clone] = []
    used: set[tuple[int, int]] = set()
    for key, _ in sorted(bins.items(), key=lambda kv: -len(kv[1])):
        if key in used:
            continue
        group = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nk = (key[0] + dx, key[1] + dy)
                if nk in bins and nk not in used:
                    group += bins[nk]
                    used.add(nk)
        if len(group) < MIN_PAIRS:
            continue
        vs = np.float32([pairs[k][2] for k in group])
        shift = np.median(vs, axis=0)
        consistent = [k for k, v in zip(group, vs) if np.hypot(*(v - shift)) < 3.5]
        if len(consistent) < MIN_PAIRS:
            continue
        sp = np.float32([pts[pairs[k][0]] for k in consistent])
        region = _copied_region(fine, shift, sp, g)
        if region is None:
            continue
        (x0, y0, bw, bh), (dx0, dy0), score = region
        if bw < MIN_SIDE or bh < MIN_SIDE or bw * bh > 0.5 * w * h or score < REGION_NCC:
            continue
        if any(abs(dx0 - x0 - c.shift[0] * sx) < 4 and abs(dy0 - y0 - c.shift[1] * sy) < 4 for c in out):
            continue
        out.append(Clone([x0 / sx, y0 / sy, bw / sx, bh / sy], [dx0 / sx, dy0 / sy, bw / sx, bh / sy],
                         ((dx0 - x0) / sx, (dy0 - y0) / sy), len(consistent), float(score)))
        if len(out) >= 4:
            break
    return out, mirrored


def _line_art(fine: np.ndarray, comp: np.ndarray, g: np.ndarray | None = None) -> bool:
    """Text, symbols or plot elements rather than a picture: most of the region has no texture (flat page or
    flat fill), or it is white page with dark marks."""
    a = np.abs(fine[comp])
    if float((a < 0.4).mean()) > 0.45 or float((a > 1.0).mean()) < 0.2:
        return True
    if g is not None:
        v = g[comp]
        return float((v > 230).mean()) > 0.4 and float((v < 80).mean()) > 0.03
    return False


def _local_ncc(a: np.ndarray, b: np.ndarray, k: int = 9) -> np.ndarray:
    ab = cv2.blur(a * b, (k, k))
    aa = cv2.blur(a * a, (k, k))
    bb = cv2.blur(b * b, (k, k))
    return ab / np.sqrt(aa * bb + 1e-6)


def _copied_region(fine: np.ndarray, shift: np.ndarray, src_pts: np.ndarray, g_ref: np.ndarray | None = None):
    """Where does the image agree with itself moved by `shift`?  Returns the source box, destination corner and the
    noise correlation inside the agreeing region - or None."""
    h, w = fine.shape
    best = None
    for ddx in (-1, 0, 1):              # integer refinement around the keypoint estimate
        for ddy in (-1, 0, 1):
            sx, sy = int(round(shift[0])) + ddx, int(round(shift[1])) + ddy
            M = np.float32([[1, 0, -sx], [0, 1, -sy]])
            moved = cv2.warpAffine(fine, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
            valid = np.zeros((h, w), bool)
            valid[max(0, -sy):min(h, h - sy), max(0, -sx):min(w, w - sx)] = True
            loc = _local_ncc(fine, moved)
            loc[~valid] = 0
            at = [loc[int(min(max(p[1], 0), h - 1)), int(min(max(p[0], 0), w - 1))] for p in src_pts]
            score = float(np.median(at))
            if best is None or score > best[0]:
                best = (score, sx, sy, loc)
    score, sx, sy, loc = best
    mask = (cv2.GaussianBlur(loc, (0, 0), 2) > 0.6).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n < 2:
        return None
    votes = np.zeros(n, int)
    for p in src_pts:
        votes[lab[int(min(max(p[1], 0), h - 1)), int(min(max(p[0], 0), w - 1))]] += 1
    votes[0] = 0
    c = int(np.argmax(votes))
    if votes[c] < 3:
        return None
    x0, y0, bw, bh = (int(v) for v in st[c, :4])
    comp = lab == c
    # the source and its copy must not be the same pixels (a periodic texture, or a flat area)
    if abs(sx) < bw * 0.5 and abs(sy) < bh * 0.5:
        return None
    a = fine[comp]
    ys, xs = np.where(comp)
    b = fine[np.clip(ys + sy, 0, h - 1), np.clip(xs + sx, 0, w - 1)]
    a = a - a.mean()
    b = b - b.mean()
    r = float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6))
    if np.std(fine[comp]) < 0.5:
        return None            # no texture to copy: flat background agrees with itself everywhere
    if _line_art(fine, comp, g_ref):
        return None
    if g_ref is not None and periodicity(g_ref[y0:y0 + bh, x0:x0 + bw]) > 0.3:
        return None            # a repeating pattern (grid, halftone, hatching) repeats by design            # repeated labels, axes or symbols in a chart are identical by design
    return (x0, y0, bw, bh), (x0 + sx, y0 + sy), r


# ------------------------------------------------------------------ mirrored and rotated copies

MIRROR_MODES = ("mirror-h", "mirror-v", "rot180")


def _mirrored(g: np.ndarray, fine: np.ndarray, pts: np.ndarray, d: np.ndarray, plain_pairs: list, min_sep: float,
              tm: np.ndarray | None, scale: tuple[float, float]) -> list[Clone]:
    """Regions repeated as a mirror image (left-right or top-bottom) or turned 180 degrees, in panel pixels."""
    h, w = g.shape
    sx, sy = scale
    # Labels, scale bars, arrows and frame lines drawn over a picture are often symmetric, and a symmetric drawing
    # matches its own mirror image edge for edge.  Only camera texture may vote: flat fills and the pixels next to
    # them are left out of the noise layer.
    nat = _natural(g)
    if tm is not None:
        nat &= ~tm
    fine = np.where(nat, fine, 0).astype(np.float32)
    groups: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {m: [] for m in MIRROR_MODES}
    # 180 degrees: SIFT is rotation-invariant, so the plain self-matches already pair a region with its turned copy
    for i, j, _ in plain_pairs:
        groups["rot180"].append((pts[i], pts[j]))
    # mirror images: match the image against its own left-right mirror.  A top-bottom mirror is a left-right mirror
    # turned 180 degrees, which SIFT also matches, so one extra keypoint pass serves both.
    gf = np.ascontiguousarray(g[:, ::-1])
    kf, df = _det().detectAndCompute(cv2.normalize(gf, None, 0, 255, cv2.NORM_MINMAX), None)
    if df is not None and len(kf) >= 12:
        ptsf = np.float32([k.pt for k in kf])
        ptsf[:, 0] = (w - 1) - ptsf[:, 0]            # back into the unflipped frame
        if tm is not None:
            keep = ~tm[np.clip(ptsf[:, 1].astype(int), 0, h - 1), np.clip(ptsf[:, 0].astype(int), 0, w - 1)]
            ptsf, df = ptsf[keep], df[keep]
        if len(ptsf) >= 12:
            dfr = np.sqrt(df / (df.sum(axis=1, keepdims=True) + 1e-7)).astype(np.float32)
            for row in cv2.BFMatcher(cv2.NORM_L2).knnMatch(d, dfr, k=4):
                # a symmetric spot matches its own mirror image in place: that is not a copy
                cand = [m for m in row if np.hypot(*(ptsf[m.trainIdx] - pts[m.queryIdx])) >= min_sep]
                if len(cand) < 2 or cand[0].distance >= RATIO * cand[1].distance:
                    continue
                p, q = pts[cand[0].queryIdx], ptsf[cand[0].trainIdx]
                groups["mirror-h"].append((p, q))
                groups["mirror-v"].append((p, q))
    cands: list[Clone] = []
    for mode in MIRROR_MODES:
        for found in _mirror_groups(groups[mode], mode, fine, g, min_sep, nat):
            (x0, y0, bw, bh), (dx0, dy0), score, n = found
            if bw < MIN_SIDE or bh < MIN_SIDE or bw * bh > 0.5 * w * h or score < REGION_NCC or n < MIN_PAIRS + 1:
                continue
            if _beside_own_axis(mode, (x0, y0, bw, bh), (dx0, dy0), min_sep) and (score < 0.95 or n < 12):
                continue                # unless the evidence is overwhelming
            src = [x0 / sx, y0 / sy, bw / sx, bh / sy]
            dst = [dx0 / sx, dy0 / sy, bw / sx, bh / sy]
            cands.append(Clone(src, dst, (dst[0] - src[0], dst[1] - src[1]), n, float(score), mode))
    out: list[Clone] = []
    # strongest first.  The region and its twin are one finding; and where two maps nearly coincide (inside a real
    # copy) a small patch agrees under both - that is the same copy seen again, not a second one
    for c in sorted(cands, key=lambda c: (-c.blocks, -c.src[2] * c.src[3])):
        if any(_contained(c.src, o.src) > 0.5 or _contained(c.src, o.dst) > 0.5 or _contained(c.dst, o.src) > 0.5
               or _contained(c.dst, o.dst) > 0.5 for o in out):
            continue
        out.append(c)
        if len(out) >= 4:
            break
    return out


def _natural(g: np.ndarray) -> np.ndarray:
    """Pixels that may hold camera texture: not inside a flat fill or a saturated stroke (drawn text, bars, boxes),
    nor right beside one."""
    f = g.astype(np.float32)
    m = cv2.blur(f, (5, 5))
    sd = np.sqrt(np.maximum(cv2.blur(f * f, (5, 5)) - m * m, 0))
    drawn = ((sd < 1.0) | (g >= 250) | (g <= 2)).astype(np.uint8)    # flat fills, white or black strokes
    return cv2.dilate(drawn, np.ones((7, 7), np.uint8)) == 0


def _beside_own_axis(mode: str, box, dst, min_sep: float) -> bool:
    """A region right next to its mirror twin, on one line with it, is one symmetric object (a band, a round cell, a
    scale-bar end) seen from both sides of its own axis - not a patch moved somewhere else."""
    x0, y0, bw, bh = box
    dx0, dy0 = dst
    if mode == "mirror-h":
        if abs(dy0 - y0) > 0.5 * bh:
            return False
        gap, extent = abs(dx0 - x0) - bw, bw
    elif mode == "mirror-v":
        if abs(dx0 - x0) > 0.5 * bw:
            return False
        gap, extent = abs(dy0 - y0) - bh, bh
    else:                               # a point-symmetric object: the two halves touch at its centre
        return max(abs(dx0 - x0) - bw, abs(dy0 - y0) - bh) < 3 * min_sep
    return gap < max(3 * min_sep, 0.5 * extent)


def _contained(a, b) -> float:
    """Share of the smaller box that lies inside the other."""
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / max(1e-6, min(a[2] * a[3], b[2] * b[3]))


def _iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / max(1e-6, a[2] * a[3] + b[2] * b[3] - inter)


def _mirror_key(p: np.ndarray, q: np.ndarray, mode: str) -> tuple[tuple[float, float], np.ndarray, np.ndarray]:
    """The two numbers a mirror map keeps constant for every point pair, with the pair put in a fixed order."""
    if mode == "mirror-h":              # x' = S - x, y' = y + dy
        if q[0] < p[0]:
            p, q = q, p
        return (float(p[0] + q[0]), float(q[1] - p[1])), p, q
    if mode == "mirror-v":              # x' = x + dx, y' = S - y
        if q[1] < p[1]:
            p, q = q, p
        return (float(q[0] - p[0]), float(p[1] + q[1])), p, q
    return (float(p[0] + q[0]), float(p[1] + q[1])), p, q      # rot180: x' = Sx - x, y' = Sy - y


def _mirror_groups(pairs: list, mode: str, fine: np.ndarray, g: np.ndarray, min_sep: float, nat: np.ndarray):
    if len(pairs) < MIN_PAIRS:
        return []
    keyed = [_mirror_key(np.asarray(p, np.float32), np.asarray(q, np.float32), mode) for p, q in pairs]
    bins: dict[tuple[int, int], list[int]] = {}
    for k, (key, _, _) in enumerate(keyed):
        bins.setdefault((int(round(key[0] / 4)), int(round(key[1] / 4))), []).append(k)
    used: set = set()
    found = []
    tried = 0
    for key, _ in sorted(bins.items(), key=lambda kv: -len(kv[1])):
        if key in used or tried >= 6:
            continue
        group = []
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                nk = (key[0] + ddx, key[1] + ddy)
                if nk in bins and nk not in used:
                    group += bins[nk]
                    used.add(nk)
        if len(group) < MIN_PAIRS:
            continue
        ks = np.float32([keyed[k][0] for k in group])
        par = np.median(ks, axis=0)
        consistent = [k for k, v in zip(group, ks) if np.hypot(*(v - par)) < 3.5]
        if len(consistent) < MIN_PAIRS:
            continue
        tried += 1
        src_pts = np.float32([keyed[k][1] for k in consistent] + [keyed[k][2] for k in consistent])
        region = _mapped_region(fine, mode, par, src_pts, g, min_sep, nat)
        if region is not None:
            found.append(region + (len(consistent),))
    return found


def _maps(mode: str, a: int, b: int, h: int, w: int):
    """Pixel maps of the mirror map: moved(x, y) = image(mapx, mapy)."""
    X, Y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    if mode == "mirror-h":
        return a - X, Y + b
    if mode == "mirror-v":
        return X + a, b - Y
    return a - X, b - Y


def _mapped_box(mode: str, a: int, b: int, box) -> tuple[int, int]:
    x0, y0, bw, bh = box
    if mode == "mirror-h":
        return a - (x0 + bw - 1), y0 + b
    if mode == "mirror-v":
        return x0 + a, b - (y0 + bh - 1)
    return a - (x0 + bw - 1), b - (y0 + bh - 1)


def _mapped_region(fine: np.ndarray, mode: str, par: np.ndarray, src_pts: np.ndarray, g_ref: np.ndarray,
                   min_sep: float, nat: np.ndarray):
    """Where does the image agree with its own mirror map?  Like `_copied_region`, for a flipped or turned copy."""
    h, w = fine.shape
    best = None
    for da in (-1, 0, 1):
        for db in (-1, 0, 1):
            a, b = int(round(par[0])) + da, int(round(par[1])) + db
            mx, my = _maps(mode, a, b, h, w)
            valid = (mx >= 0) & (mx <= w - 1) & (my >= 0) & (my <= h - 1)
            # pixels close to the mirror axis map onto themselves and agree trivially
            valid &= np.hypot(mx - np.arange(w, dtype=np.float32)[None, :],
                              my - np.arange(h, dtype=np.float32)[:, None]) >= min_sep
            if valid.sum() < 400:
                continue
            moved = cv2.remap(fine, mx, my, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            loc = _local_ncc(fine, moved)
            loc[~valid] = 0
            at = [loc[int(min(max(p[1], 0), h - 1)), int(min(max(p[0], 0), w - 1))] for p in src_pts]
            score = float(np.median(at))
            if best is None or score > best[0]:
                best = (score, a, b, loc, mx, my, valid)
    if best is None:
        return None
    score, a, b, loc, mx, my, valid = best
    mask = ((cv2.GaussianBlur(loc, (0, 0), 2) > 0.6) & valid).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n < 2:
        return None
    votes = np.zeros(n, int)
    for p in src_pts:
        votes[lab[int(min(max(p[1], 0), h - 1)), int(min(max(p[0], 0), w - 1))]] += 1
    votes[0] = 0
    c = int(np.argmax(votes))
    if votes[c] < 3:
        return None
    x0, y0, bw, bh = (int(v) for v in st[c, :4])
    comp = lab == c
    dx0, dy0 = _mapped_box(mode, a, b, (x0, y0, bw, bh))
    if _iou([x0, y0, bw, bh], [dx0, dy0, bw, bh]) > 0.1:
        return None            # a region straddling its own mirror axis: a symmetric shape, not a pasted copy
    # both sides must be camera texture for most of the region
    partner = nat[np.clip(my.astype(int), 0, h - 1), np.clip(mx.astype(int), 0, w - 1)]
    comp = comp & nat & partner
    if comp.sum() < 0.6 * st[c, 4] or comp.sum() < 200:
        return None
    if np.std(fine[comp]) < 0.5:
        return None
    a_ = fine[comp]
    b_ = fine[np.clip(my[comp].astype(int), 0, h - 1), np.clip(mx[comp].astype(int), 0, w - 1)]
    a_ = a_ - a_.mean()
    b_ = b_ - b_.mean()
    r = float((a_ * b_).sum() / (np.sqrt((a_ * a_).sum() * (b_ * b_).sum()) + 1e-6))
    if _line_art(fine, comp, g_ref):
        return None
    if periodicity(g_ref[y0:y0 + bh, x0:x0 + bw]) > 0.3:
        return None
    return (x0, y0, bw, bh), (dx0, dy0), r
