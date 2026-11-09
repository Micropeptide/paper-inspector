"""Evidence a person can check in five seconds: annotated side-by-sides and blink animations.

A blink GIF flips between the two aligned copies.  If they are the same picture, nothing moves except labels; if
they are different, everything shimmers.  It is how astronomers found Pluto and how image sleuths present reuse.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .imgutil import decompose

COLORS = [(56, 189, 248), (250, 204, 21), (244, 114, 182), (52, 211, 153), (251, 146, 60), (167, 139, 250)]  # BGR-ish
MAX_WIDTH = 1400          # a 30x1800 blot strip scaled to a fixed height would be 18000 px wide
MAX_UPSCALE = 4.0


def _fit(img: np.ndarray, height: int, max_width: int = MAX_WIDTH) -> tuple[np.ndarray, float]:
    """Scale to `height`, unless that makes the image wider than `max_width` or enlarges it absurdly.  Returns the
    image and the one scale used, which box and line overlays must share."""
    h, w = img.shape[:2]
    s = min(height / max(h, 1), max_width / max(w, 1), MAX_UPSCALE)
    out = cv2.resize(img, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                     interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    return out, s


def _join(images: list[np.ndarray], gap: int = 12) -> np.ndarray:
    """Side by side on white; one above the other when the images are wide strips (blots)."""
    wide = sum(i.shape[1] for i in images) > 3 * max(i.shape[0] for i in images)
    if wide:
        W = max(i.shape[1] for i in images)
        rows = []
        for k, i in enumerate(images):
            if k:
                rows.append(np.full((gap, W, 3), 255, np.uint8))
            rows.append(cv2.copyMakeBorder(i, 0, 0, 0, W - i.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255)))
        return np.vstack(rows)
    H = max(i.shape[0] for i in images)
    cols = []
    for k, i in enumerate(images):
        if k:
            cols.append(np.full((H, gap, 3), 255, np.uint8))
        cols.append(cv2.copyMakeBorder(i, 0, H - i.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=(255, 255, 255)))
    return np.hstack(cols)


def _box(img, box, color, s=1.0, thick=2):
    x, y, w, h = [v * s for v in box]
    cv2.rectangle(img, (int(x), int(y)), (int(x + w), int(y + h)), color, thick, cv2.LINE_AA)


def _label(img, text, org, color=(255, 255, 255)):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)


def side_by_side(a: np.ndarray, b: np.ndarray, box_a, box_b, name_a: str, name_b: str, color=COLORS[0],
                 height: int = 300) -> np.ndarray:
    (A, sa), (B, sb) = _fit(a, height), _fit(b, height)
    if box_a:
        _box(A, box_a, color, sa)
    if box_b:
        _box(B, box_b, color, sb)
    _label(A, name_a, (6, 18))
    _label(B, name_b, (6, 18))
    return _join([A, B])


def aligned(a: np.ndarray, b: np.ndarray, M: np.ndarray, flip: bool):
    """Bring B onto (mirrored) A, or A onto B when B is the smaller copy.  Returns (reference, moved, valid mask)."""
    a2 = np.ascontiguousarray(a[:, ::-1]) if flip else a
    geo = decompose(M)
    if geo["scale"] >= 1.0:
        ref, mov, W = a2, b, cv2.invertAffineTransform(M)
    else:
        ref, mov, W = b, a2, M
    h, w = ref.shape[:2]
    warped = cv2.warpAffine(mov, W, (w, h), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
    valid = cv2.warpAffine(np.full(mov.shape[:2], 255, np.uint8), W, (w, h), flags=cv2.INTER_NEAREST) > 0
    return ref, warped, valid


def crop_valid(ref, warped, valid, pad=2):
    ys, xs = np.where(valid)
    if len(ys) == 0:
        return ref, warped
    y0, y1, x0, x1 = max(ys.min() + pad, 0), ys.max() - pad, max(xs.min() + pad, 0), xs.max() - pad
    if y1 <= y0 or x1 <= x0:
        return ref, warped
    return ref[y0:y1, x0:x1], warped[y0:y1, x0:x1]


def blink_gif(frame_a: np.ndarray, frame_b: np.ndarray, path: Path, label_a: str, label_b: str,
              height: int = 320, ms: int = 650) -> None:
    fa, fb = _fit(frame_a, height)[0], _fit(frame_b, height)[0]
    if fb.shape != fa.shape:
        fb = cv2.resize(fb, (fa.shape[1], fa.shape[0]))
    _label(fa, label_a, (6, 18))
    _label(fb, label_b, (6, 18), (250, 204, 21))
    frames = [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in (fa, fb)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=ms, loop=0, optimize=True)


def difference(frame_a: np.ndarray, frame_b: np.ndarray) -> np.ndarray:
    """Black where the two copies agree (paint.net's "Difference" blend, as sleuths use it)."""
    d = cv2.absdiff(frame_a, frame_b).astype(np.float32)
    d = np.clip(d * 3.0, 0, 255).astype(np.uint8)
    return d


def write(path: Path, img: np.ndarray) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return path.name


def pair_evidence(out: Path, stem: str, a_img, b_img, a_name, b_name, box_a, box_b, M, flip) -> list[str]:
    files = []
    files.append(write(out / f"{stem}-pair.png", side_by_side(a_img, b_img, box_a, box_b, a_name, b_name)))
    ref, warped, valid = aligned(a_img, b_img, M, flip)
    r, w = crop_valid(ref, warped, valid)
    if r.size and w.size:
        blink_gif(r, w, out / f"{stem}-blink.gif", "original", "copy, aligned")
        files.append(f"{stem}-blink.gif")
        files.append(write(out / f"{stem}-diff.png", difference(r, w)))
    return files


def clone_evidence(out: Path, stem: str, img, src, dst, name) -> list[str]:
    A, s = _fit(img, 300)
    _box(A, src, COLORS[1], s)
    _box(A, dst, COLORS[1], s)
    c0 = (int((src[0] + src[2] / 2) * s), int((src[1] + src[3] / 2) * s))
    c1 = (int((dst[0] + dst[2] / 2) * s), int((dst[1] + dst[3] / 2) * s))
    cv2.arrowedLine(A, c0, c1, COLORS[1], 2, cv2.LINE_AA, tipLength=0.06)
    _label(A, name, (6, 18))
    files = [write(out / f"{stem}-clone.png", A)]
    x, y, w, h = (int(v) for v in src)
    X, Y = int(dst[0]), int(dst[1])
    pa, pb = img[y:y + h, x:x + w], img[Y:Y + h, X:X + w]
    if pa.size and pa.shape == pb.shape:
        blink_gif(pa, pb, out / f"{stem}-blink.gif", "region 1", "region 2", height=200)
        files.append(f"{stem}-blink.gif")
    return files


def region_evidence(out: Path, stem: str, img, boxes, name, view_img=None, lines=None) -> list[str]:
    panels = []
    for base in ([img] + ([view_img] if view_img is not None else [])):
        A, s = _fit(base, 300)
        for b in boxes:
            _box(A, b, COLORS[2], s)
        for axis, pos in (lines or []):
            if axis == "x":
                cv2.line(A, (int(pos * s), 0), (int(pos * s), A.shape[0]), COLORS[2], 1, cv2.LINE_AA)
            else:
                cv2.line(A, (0, int(pos * s)), (A.shape[1], int(pos * s)), COLORS[2], 1, cv2.LINE_AA)
        panels.append(A)
    _label(panels[0], name, (6, 18))
    if len(panels) > 1:
        _label(panels[1], "contrast revealed", (6, 18))
    return [write(out / f"{stem}-region.png", _join(panels))]


# ------------------------------------------------------------------ flipped clones, band rows, negatives

MODE_WORDS = {"mirror-h": "mirrored left to right", "mirror-v": "flipped top to bottom", "rot180": "turned 180°"}


def _unflip(patch: np.ndarray, mode: str) -> np.ndarray:
    if mode == "mirror-h":
        return np.ascontiguousarray(patch[:, ::-1])
    if mode == "mirror-v":
        return np.ascontiguousarray(patch[::-1])
    if mode == "rot180":
        return np.ascontiguousarray(patch[::-1, ::-1])
    return patch


def mirror_clone_evidence(out: Path, stem: str, img, src, dst, mode: str, name: str) -> list[str]:
    """The panel with both regions boxed, and a blink between one region and the other with the flip undone."""
    A, s = _fit(img, 300)
    _box(A, src, COLORS[1], s)
    _box(A, dst, COLORS[4], s)
    c0 = (int((src[0] + src[2] / 2) * s), int((src[1] + src[3] / 2) * s))
    c1 = (int((dst[0] + dst[2] / 2) * s), int((dst[1] + dst[3] / 2) * s))
    cv2.arrowedLine(A, c0, c1, COLORS[1], 2, cv2.LINE_AA, tipLength=0.06)
    _label(A, f"{name}: {MODE_WORDS.get(mode, mode)}", (6, 18))
    files = [write(out / f"{stem}-clone.png", A)]
    x, y, w, h = (int(round(v)) for v in src)
    X, Y = int(round(dst[0])), int(round(dst[1]))
    pa, pb = img[y:y + h, x:x + w], img[Y:Y + h, X:X + w]
    if pa.size and pa.shape == pb.shape:
        blink_gif(pa, _unflip(pb, mode), out / f"{stem}-blink.gif", "region 1", "region 2, flip undone", height=200)
        files.append(f"{stem}-blink.gif")
    return files


def strip_evidence(out: Path, stem: str, a_img, b_img, box_a, box_b, name_a: str, name_b: str, flip: bool = False,
                   inverted: bool = False) -> list[str]:
    """Both panels with the shared strip boxed, and a blink between the two strips brought to one size, with any
    mirror or negative undone."""
    files = [write(out / f"{stem}-pair.png", side_by_side(a_img, b_img, box_a, box_b, name_a, name_b, COLORS[2]))]

    def cut(img, box):
        x, y, w, h = (int(round(v)) for v in box)
        return img[max(0, y):y + h, max(0, x):x + w]
    sa, sb = cut(a_img, box_a), cut(b_img, box_b)
    if sa.size and sb.size:
        if flip:
            sb = np.ascontiguousarray(sb[:, ::-1])
        if inverted:
            sb = (255 - sb).astype(np.uint8)
        sb = cv2.resize(sb, (sa.shape[1], sa.shape[0]), interpolation=cv2.INTER_AREA)
        blink_gif(sa, sb, out / f"{stem}-blink.gif", name_a, name_b + (" (flip and negative undone)" if flip and inverted
                                                                     else " (flip undone)" if flip else
                                                                     " (negative undone)" if inverted else ""),
                  height=120)
        files.append(f"{stem}-blink.gif")
    return files


def negative_pair_evidence(out: Path, stem: str, a_img, b_img, a_name, b_name, box_a, box_b, M, flip) -> list[str]:
    """Like `pair_evidence`, for a copy shown as a negative: the blink compares A with B turned back to a positive."""
    files = [write(out / f"{stem}-pair.png", side_by_side(a_img, b_img, box_a, box_b, a_name, b_name))]
    ref, warped, valid = aligned(a_img, (255 - b_img).astype(np.uint8), M, flip)
    r, w = crop_valid(ref, warped, valid)
    if r.size and w.size:
        blink_gif(r, w, out / f"{stem}-blink.gif", "original", "copy, negative undone")
        files.append(f"{stem}-blink.gif")
        files.append(write(out / f"{stem}-diff.png", difference(r, w)))
    return files
