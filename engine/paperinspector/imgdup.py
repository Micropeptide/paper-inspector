"""Find the same picture used twice: across panels, figures and papers.

1. SIFT keypoints for every image panel, plus the mirrored panel (SIFT already tolerates rotation and scale, not
   mirroring).
2. One nearest-neighbour index over every descriptor of every panel votes for candidate panel pairs, so a paper with
   300 panels costs one search, not 45,000 comparisons.
3. Each candidate is verified: RANSAC fits a similarity (or a mild affine, for stretched copies), the copy is warped
   onto the original, and the band-passed pixels must correlate.  Band-pass keeps camera noise and speckle and drops
   brightness edits, so a re-contrasted copy still matches while two honest images of the same tissue do not.

`find_inverted` looks for the one edit the band-pass cannot undo: a copy shown as a negative (light bands on dark,
a common way to present blots and gels).  Grey panels with a light background are compared with the negatives of
grey panels with a dark background, through the same voting and verification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from .imgutil import decompose, dog, fit_scale, gray, hull_area, ncc, periodicity
from .models import Panel

IMAGE_KINDS = {"blot", "micrograph", "photo", "other"}
MIN_KP = 8
VOTES = 5
MAX_PARTNERS = 12
NCC_DOG_MIN = 0.42
NCC_INT_MIN = 0.55


@dataclass
class Feat:
    key: str                  # unique across papers: "<paper>/<panel id>"
    paper: int
    panel: Panel
    g: np.ndarray             # grayscale panel, native pixels
    tmask: Optional[np.ndarray]   # PDF text over the panel (ignored when comparing pixels)
    kp: np.ndarray            # (N,2) keypoints in panel pixels
    desc: np.ndarray
    kpf: np.ndarray           # same for the mirrored panel (coordinates in the mirrored frame)
    descf: np.ndarray


@dataclass
class Match:
    a: Feat
    b: Feat
    flip: bool                 # B is a mirror image of A
    M: np.ndarray              # 2x3: (mirrored) A pixels -> B pixels
    inliers: int
    ncc_dog: float
    ncc_int: float
    frac_a: float              # share of A that appears in B
    frac_b: float
    box_a: list[float]         # overlap, in A's (unmirrored) pixels
    box_b: list[float]
    geo: dict = field(default_factory=dict)


_sift = None


def _detector():
    global _sift
    if _sift is None:
        _sift = cv2.SIFT_create(nfeatures=1000, contrastThreshold=0.015, edgeThreshold=12)
    return _sift


def _root(desc: Optional[np.ndarray]) -> np.ndarray:
    if desc is None or len(desc) == 0:
        return np.zeros((0, 128), np.float32)
    d = desc / (desc.sum(axis=1, keepdims=True) + 1e-7)
    return np.sqrt(d).astype(np.float32)


def _kp(g: np.ndarray, s: float):
    h, w = g.shape
    gs = cv2.resize(g, (max(8, int(w * s)), max(8, int(h * s))),
                    interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    gs = cv2.normalize(gs, None, 0, 255, cv2.NORM_MINMAX)
    kps, desc = _detector().detectAndCompute(gs, None)
    # the true scale per axis: the max(8, int(...)) rounding differs from s on thin strips
    pts = np.array([k.pt for k in kps], np.float32).reshape(-1, 2) / np.float32([gs.shape[1] / w, gs.shape[0] / h])
    return pts, _root(desc)


def features(panel: Panel, paper: int = 0) -> Optional[Feat]:
    if panel.image is None or panel.kind not in IMAGE_KINDS:
        return None
    g = gray(panel.image)
    h, w = g.shape
    if min(h, w) < 24:
        return None
    s = fit_scale(h, w)
    kp, desc = _kp(g, s)
    if len(kp) < MIN_KP:
        return None
    kpf, descf = _kp(np.ascontiguousarray(g[:, ::-1]), s)
    tm = panel.text_mask
    om = getattr(panel, "overlay_mask", None)       # an inset raster placed on top: its own panel, not ours
    if om is not None and om.shape == (h, w):
        tm = om if tm is None else (tm | om)
    if tm is not None:
        keep = ~tm[np.clip(kp[:, 1].astype(int), 0, h - 1), np.clip(kp[:, 0].astype(int), 0, w - 1)]
        kp, desc = kp[keep], desc[keep]
        tmf = tm[:, ::-1]
        keep = ~tmf[np.clip(kpf[:, 1].astype(int), 0, h - 1), np.clip(kpf[:, 0].astype(int), 0, w - 1)]
        kpf, descf = kpf[keep], descf[keep]
        if len(kp) < MIN_KP:
            return None                 # all its features are on labels: nothing left to compare
    return Feat(f"{paper}/{panel.id}", paper, panel, g, tm, kp, desc, kpf, descf)


def candidates(feats: list[Feat]) -> dict[tuple[int, int, bool], int]:
    """Vote for panel pairs that share many distinctive descriptors."""
    if sum(1 for f in feats if len(f.desc)) < 2:
        return {}                       # FLANN on an empty index crashes the process, not just the call
    owner = np.concatenate([np.full(len(f.desc), i) for i, f in enumerate(feats)])
    base = np.vstack([f.desc for f in feats])
    index = cv2.flann_Index(base, dict(algorithm=1, trees=4))
    k = min(8, len(base))
    votes: dict[tuple[int, int, bool], int] = {}
    for flip in (False, True):
        q = np.vstack([f.descf if flip else f.desc for f in feats])
        qown = np.concatenate([np.full(len(f.descf if flip else f.desc), i) for i, f in enumerate(feats)])
        if len(q) == 0:
            continue
        idx, dist = index.knnSearch(q, k, params=dict(checks=48))
        dist = np.sqrt(np.maximum(dist, 0))
        nown = owner[idx]
        for r in range(len(q)):
            me = qown[r]
            row_own, row_d = nown[r], dist[r]
            first = -1
            for c in range(k):
                o = row_own[c]
                if o == me:
                    continue
                if first < 0:
                    first = c
                    continue
                if o != row_own[first]:
                    if row_d[first] < 0.8 * row_d[c]:
                        j = int(row_own[first])
                        key = (min(me, j), max(me, j), flip)
                        votes[key] = votes.get(key, 0) + 1
                    break
            else:
                if first >= 0 and row_d[first] < 0.25:
                    j = int(row_own[first])
                    key = (min(me, j), max(me, j), flip)
                    votes[key] = votes.get(key, 0) + 1
    # keep the strongest partners per panel
    keep: dict[tuple[int, int, bool], int] = {}
    per: dict[int, int] = {}
    for key, v in sorted(votes.items(), key=lambda kv: -kv[1]):
        if v < VOTES:
            break
        i, j, _ = key
        if per.get(i, 0) >= MAX_PARTNERS or per.get(j, 0) >= MAX_PARTNERS:
            continue                    # a hub panel (a shared scale bar, a common background) is verified 12 times, not n
        per[i] = per.get(i, 0) + 1
        per[j] = per.get(j, 0) + 1
        keep[key] = v
    return keep


def _pairs(src_d, dst_d, ratio=0.8):
    if len(src_d) < 2 or len(dst_d) < 2:
        return []
    bf = cv2.BFMatcher(cv2.NORM_L2)
    fw = bf.knnMatch(src_d, dst_d, k=2)
    bw = bf.match(dst_d, src_d)
    back = {m.queryIdx: m.trainIdx for m in bw}
    out = []
    for m in fw:
        if len(m) == 2 and m[0].distance < ratio * m[1].distance and back.get(m[0].trainIdx) == m[0].queryIdx:
            out.append((m[0].queryIdx, m[0].trainIdx))
    return out


def verify(fa: Feat, fb: Feat, flip: bool) -> Optional[Match]:
    src_kp, src_d = (fa.kpf, fa.descf) if flip else (fa.kp, fa.desc)
    pairs = _pairs(src_d, fb.desc)
    if len(pairs) < 8:
        return None
    src = np.float32([src_kp[i] for i, _ in pairs])
    dst = np.float32([fb.kp[j] for _, j in pairs])
    thr = max(2.5, 0.012 * max(fb.g.shape))
    Mp, inl_p = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=thr,
                                            maxIters=5000, confidence=0.995)
    Mf, inl_f = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=thr,
                                     maxIters=5000, confidence=0.995)
    n_p = int(inl_p.sum()) if inl_p is not None else 0
    n_f = int(inl_f.sum()) if inl_f is not None else 0
    M, inl = Mp, inl_p
    if Mf is not None and n_f > max(1.25 * n_p, n_p + 6):
        geo_f = decompose(Mf)
        if geo_f["aniso"] < 1.35 and abs(Mf[0, 1] * Mf[1, 0]) < 0.5:
            M, inl = Mf, inl_f
    small = min(min(fa.g.shape), min(fb.g.shape)) < 80
    if M is None or inl is None or int(inl.sum()) < (20 if small else 9):
        return None                     # a tiny panel matches anything simple on a handful of points
    geo = decompose(M)
    if not 0.2 < geo["scale"] < 5.0:
        return None
    m = inl.ravel().astype(bool)
    ga = np.ascontiguousarray(fa.g[:, ::-1]) if flip else fa.g
    gb = fb.g
    ta = fa.tmask[:, ::-1] if (flip and fa.tmask is not None) else fa.tmask
    tb = fb.tmask
    area_a, area_b = ga.shape[0] * ga.shape[1], gb.shape[0] * gb.shape[1]
    if hull_area(src[m]) < 0.012 * area_a or hull_area(dst[m]) < 0.012 * area_b:
        return None             # matches bunched on one small feature (a label, a scale bar)

    # warp the higher-resolution view into the lower-resolution frame
    if geo["scale"] >= 1.0:          # B shows A enlarged: bring B down to A
        Minv = cv2.invertAffineTransform(M)
        src_img, dst_img, Mw, sw, st, dt = gb, ga, Minv, 1.0 / geo["scale"], tb, ta
    else:
        src_img, dst_img, Mw, sw, st, dt = ga, gb, M, geo["scale"], ta, tb
    if sw < 0.75:
        src_img = cv2.GaussianBlur(src_img, (0, 0), 0.5 / sw)
    h, w = dst_img.shape
    warped = cv2.warpAffine(src_img, Mw, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
    valid = cv2.warpAffine(np.full(src_img.shape, 255, np.uint8), Mw, (w, h), flags=cv2.INTER_NEAREST) > 0
    valid = cv2.erode(valid.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    if st is not None:
        valid &= cv2.warpAffine(st.astype(np.uint8), Mw, (w, h), flags=cv2.INTER_NEAREST) == 0
    if dt is not None:
        valid &= ~dt
    valid = cv2.erode(valid.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    if valid.sum() < 400:
        return None
    textured = (cv2.GaussianBlur(np.abs(dog(dst_img)), (0, 0), 3) > 0.6) | (cv2.GaussianBlur(np.abs(dog(warped)), (0, 0), 3) > 0.6)
    mk = valid & textured
    if mk.sum() < 300:
        mk = valid
    # real pictures have texture everywhere; a bar, a box or a filled shape only at its edges
    coverage = float((np.abs(dog(dst_img))[valid] > 1.0).mean())
    if coverage < 0.2:
        return None
    ys_, xs_ = np.where(valid)
    if max(periodicity(dst_img[ys_.min():ys_.max() + 1, xs_.min():xs_.max() + 1]), periodicity(src_img)) > 0.3:
        return None                     # a halftone, dithered or hatched fill matches itself however it is moved
    n_dog = ncc(dog(dst_img), dog(warped), mk)
    n_int = ncc(dst_img, warped, mk)
    if n_dog < NCC_DOG_MIN or n_int < NCC_INT_MIN:
        return None

    ys, xs = np.where(valid)
    box_dst = [float(xs.min()), float(ys.min()), float(xs.max() - xs.min() + 1), float(ys.max() - ys.min() + 1)]
    inv = cv2.invertAffineTransform(Mw)
    corners = np.float32([[box_dst[0], box_dst[1]], [box_dst[0] + box_dst[2], box_dst[1]],
                          [box_dst[0], box_dst[1] + box_dst[3]], [box_dst[0] + box_dst[2], box_dst[1] + box_dst[3]]])
    back = cv2.transform(corners[None], inv)[0]
    sh, sw_ = src_img.shape
    bx0, by0 = np.clip(back.min(axis=0), 0, [sw_, sh])
    bx1, by1 = np.clip(back.max(axis=0), 0, [sw_, sh])
    box_src = [float(bx0), float(by0), float(bx1 - bx0), float(by1 - by0)]
    if geo["scale"] >= 1.0:
        box_a_m, box_b = box_dst, box_src
    else:
        box_a_m, box_b = box_src, box_dst
    box_a = list(box_a_m)
    if flip:
        box_a[0] = ga.shape[1] - box_a[0] - box_a[2]
    frac_a = min(1.0, box_a[2] * box_a[3] / area_a)
    frac_b = min(1.0, box_b[2] * box_b[3] / area_b)
    return Match(fa, fb, flip, M, int(m.sum()), float(n_dog), float(n_int), frac_a, frac_b,
                 [round(v, 1) for v in box_a], [round(v, 1) for v in box_b], geo)


def find(feats: list[Feat]) -> list[Match]:
    out = []
    for (i, j, flip), _ in sorted(candidates(feats).items(), key=lambda kv: -kv[1]):
        m = verify(feats[i], feats[j], flip)
        if m is not None and not _same_pixels(m):
            out.append(m)
    # one pair can be found both plain and mirrored (symmetric images); keep the better
    best: dict[tuple[str, str], Match] = {}
    for m in out:
        k = tuple(sorted((m.a.key, m.b.key)))
        if k not in best or (m.ncc_dog, m.inliers) > (best[k].ncc_dog, best[k].inliers):
            best[k] = m
    return list(best.values())


def _same_pixels(m: Match) -> bool:
    """Two panels of one figure that overlap on the page (an inset raster placed on a bigger one) match because they
    are the same rendered pixels, not because an image was reused: the fitted map sends every pixel of A to the very
    same place in the figure.  Panels with identical boxes are whole images handed in directly, not page regions."""
    pa, pb = m.a.panel, m.b.panel
    if m.flip or m.a.paper != m.b.paper or pa.figure_id != pb.figure_id or pa.box is None or pb.box is None \
            or pa.box.as_list() == pb.box.as_list():
        return False
    if float(np.abs(m.M[:, :2] - np.eye(2)).max()) > 0.03:
        return False
    off = np.float32([pa.box.x - pb.box.x, pa.box.y - pb.box.y])
    return float(np.abs(m.M[:, 2] - off).max()) <= max(3.0, 0.02 * max(m.b.g.shape))


def dhash(img: np.ndarray, size: int = 16) -> np.ndarray:
    g = cv2.resize(gray(img), (size + 1, size), interpolation=cv2.INTER_AREA).astype(np.int16)
    return (g[:, 1:] > g[:, :-1]).ravel()


def exact_charts(panels: list[tuple[int, Panel]]) -> list[tuple[tuple[int, Panel], tuple[int, Panel], int]]:
    """Charts and plots drawn twice (same plot under two labels).  Keypoints are unreliable on line art, so compare
    a gradient hash and confirm with a pixel correlation."""
    items = [(pp, dhash(pp[1].image)) for pp in panels
             if pp[1].kind == "chart" and pp[1].image is not None and min(pp[1].image.shape[:2]) >= 60]
    out = []
    for x in range(len(items)):
        for y in range(x + 1, len(items)):
            (pa, ha), (pb, hb) = items[x], items[y]
            d = int(np.count_nonzero(ha != hb))
            if d > 10:
                continue
            ia, ib = gray(pa[1].image), gray(pb[1].image)
            ib = cv2.resize(ib, (ia.shape[1], ia.shape[0]), interpolation=cv2.INTER_AREA)
            ink = (ia < 200) | (ib < 200)
            if ink.mean() < 0.02:
                continue
            if ncc(ia, ib, ink) > 0.97:
                out.append((pa, pb, d))
    return out


# ------------------------------------------------------------------ copies shown as a negative

INV_NCC_DOG_MIN = 0.5       # stricter than a plain copy: this path is new and a negative is a strong accusation
INV_NCC_INT_MIN = 0.6
INV_NCC_FINE_MIN = 0.45     # the camera-noise layer itself must agree, not only the shapes


def is_grey(img: np.ndarray) -> bool:
    if img is None:
        return False
    if img.ndim == 2:
        return True
    small = img[:: max(1, img.shape[0] // 128), :: max(1, img.shape[1] // 128)].astype(np.int16)
    return float(np.abs(small - small.mean(axis=2, keepdims=True)).mean()) < 4.0


def inverted_feature(f: Feat) -> Optional[Feat]:
    """The negative of a panel, ready to be matched against plain panels."""
    g = (255 - f.g).astype(np.uint8)
    h, w = g.shape
    kp, desc = _kp(g, fit_scale(h, w))
    if f.tmask is not None and len(kp):
        keep = ~f.tmask[np.clip(kp[:, 1].astype(int), 0, h - 1), np.clip(kp[:, 0].astype(int), 0, w - 1)]
        kp, desc = kp[keep], desc[keep]
    if len(kp) < MIN_KP:
        return None
    empty = np.zeros((0, 128), np.float32)
    return Feat(f.key + "~negative", f.paper, f.panel, g, f.tmask, kp, desc, np.zeros((0, 2), np.float32), empty)


def _fine_ncc(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    fa = cv2.GaussianBlur(a.astype(np.float32), (0, 0), 0.7) - cv2.GaussianBlur(a.astype(np.float32), (0, 0), 2.0)
    fb = cv2.GaussianBlur(b.astype(np.float32), (0, 0), 0.7) - cv2.GaussianBlur(b.astype(np.float32), (0, 0), 2.0)
    return ncc(fa, fb, mask)


def find_inverted(feats: list[Feat], skip: Optional[set] = None) -> list[Match]:
    """Pairs where one grey panel is the negative of another.  `skip` holds frozensets of panel keys already
    reported as copies.  Returned matches have `geo["inverted"] = True`; `b` is the panel shown as a negative and
    `b.g` holds its re-inverted pixels."""
    skip = skip or set()
    grey = [f for f in feats if is_grey(f.panel.image)]
    light = [f for f in grey if f.g.mean() >= 110]
    dark = [f for f in grey if f.g.mean() <= 145]
    if not light or not dark:
        return []
    inv = [x for x in (inverted_feature(f) for f in dark) if x is not None]
    if not inv:
        return []
    combined = light + inv
    nl = len(light)
    best: dict[frozenset, Match] = {}
    for (i, j, flip), _ in sorted(candidates(combined).items(), key=lambda kv: -kv[1]):
        if (i < nl) == (j < nl):
            continue                    # two plain panels (already compared) or two negatives
        a, b = (combined[i], combined[j]) if i < nl else (combined[j], combined[i])
        pair = frozenset((a.key, b.key.removesuffix("~negative")))
        if len(pair) < 2 or pair in skip:
            continue
        m = verify(a, b, flip)
        if m is None or m.ncc_dog < INV_NCC_DOG_MIN or m.ncc_int < INV_NCC_INT_MIN or _same_pixels(m):
            continue
        # the shapes agree; the camera noise must agree as well (a stain and its brightfield view share shapes)
        from .evidence import aligned
        ref, warped, valid = aligned(a.g, b.g, m.M, m.flip)
        valid = cv2.erode(valid.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
        if valid.sum() < 400 or _fine_ncc(ref, warped, valid) < INV_NCC_FINE_MIN:
            continue
        m.geo["inverted"] = True
        if pair not in best or (m.ncc_dog, m.inliers) > (best[pair].ncc_dog, best[pair].inliers):
            best[pair] = m
    return list(best.values())
