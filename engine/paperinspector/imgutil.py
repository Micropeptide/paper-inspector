"""Small image helpers shared by the checks."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional, Union

import cv2
import numpy as np

MAX_DECODE_PIXELS = 150_000_000     # a larger image is a decompression bomb or a slide scan nobody can screen
MAX_SIDE = 2400                     # very large images: keep detail, bound time and memory


def read_image(src: Union[str, Path, bytes], max_side: Optional[int] = MAX_SIDE) -> Optional[np.ndarray]:
    """Any image file (or its bytes) as 8-bit BGR, or None.  16-bit microscope data is stretched instead of cut to
    its top 8 bits (a 12-bit TIFF would otherwise come out nearly black with ~16 grey levels, and every noise-based
    check would go blind), and transparency is laid on white so black labels stay visible."""
    from PIL import Image

    data = src if isinstance(src, (bytes, bytearray)) else None
    mode, plain = None, False
    try:
        with Image.open(io.BytesIO(data) if data is not None else src) as im:
            if im.size[0] * im.size[1] > MAX_DECODE_PIXELS:
                return None
            mode = im.mode
            # 8-bit colour or grey without transparency (CMYK too: OpenCV converts it properly) keeps the plain
            # decoder, which also applies the EXIF orientation
            plain = mode in ("RGB", "L", "CMYK", "YCbCr") and "transparency" not in im.info
    except Exception:
        pass
    img = None
    if plain:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data is not None \
            else cv2.imread(str(src), cv2.IMREAD_COLOR)
    elif mode != "CMYK":
        raw = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED) if data is not None \
            else cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
        img = to_bgr8(raw) if raw is not None else None
    if img is None:
        img = _read_pil(src)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max_side and max(h, w) > max_side:
        s = max_side / max(h, w)
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(img)


def _read_pil(src) -> Optional[np.ndarray]:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src) as im:
            if im.size[0] * im.size[1] > MAX_DECODE_PIXELS:
                return None
            if im.mode in ("P", "PA"):
                im = im.convert("RGBA")
            elif im.mode == "1":
                im = im.convert("L")
            elif im.mode not in ("L", "LA", "RGB", "RGBA", "I", "F") and not im.mode.startswith("I;16"):
                im = im.convert("RGB")
            arr = np.array(im)
    except Exception:
        return None
    if arr.ndim == 3 and arr.shape[2] >= 3:
        arr = np.ascontiguousarray(arr[:, :, [2, 1, 0] + list(range(3, arr.shape[2]))])    # RGB(A) -> BGR(A)
    return to_bgr8(arr)


def to_bgr8(arr: np.ndarray) -> Optional[np.ndarray]:
    """Decoded pixels of any depth and channel count -> 8-bit BGR.  Alpha is composited onto white before any
    intensity stretch; deeper-than-8-bit data has its 0.1-99.9th percentiles stretched to 0..255."""
    if arr is None or arr.size == 0 or arr.ndim not in (2, 3):
        return None
    if arr.dtype == bool:
        arr = arr.astype(np.uint8) * 255
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    alpha = None
    if arr.ndim == 3 and arr.shape[2] in (2, 4):
        alpha, arr = arr[:, :, -1], arr[:, :, :-1]
        if arr.shape[2] == 1:
            arr = arr[:, :, 0]
    if arr.ndim == 3 and arr.shape[2] > 3:
        arr = arr[:, :, :3]
    f = arr.astype(np.float32)
    if arr.dtype == np.uint8:
        lo, hi = 0.0, 255.0
    else:
        top = float(np.iinfo(arr.dtype).max) if np.issubdtype(arr.dtype, np.integer) else float(np.nanmax(f))
        finite = f[np.isfinite(f)]
        lo, hi = (np.percentile(finite, (0.1, 99.9)) if finite.size else (0.0, top))
        if not hi > lo:
            lo, hi = 0.0, max(top, 1.0)
    if alpha is not None:
        amax = float(np.iinfo(alpha.dtype).max) if np.issubdtype(alpha.dtype, np.integer) else 1.0
        a = np.clip(alpha.astype(np.float32) / amax, 0, 1)
        if f.ndim == 3:
            a = a[:, :, None]
        f = f * a + hi * (1 - a)              # white, on the image's own intensity scale
    out = np.clip((np.nan_to_num(f, nan=lo) - lo) * (255.0 / (hi - lo)), 0, 255)
    out = (out + 0.5).astype(np.uint8)
    return cv2.cvtColor(out, cv2.COLOR_GRAY2BGR) if out.ndim == 2 else np.ascontiguousarray(out)


def gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def dog(g: np.ndarray, s1: float = 0.8, s2: float = 3.0) -> np.ndarray:
    """Band-pass (difference of Gaussians).  Keeps texture and speckle, drops illumination and contrast changes —
    so two copies of one image agree here even after brightness edits, while two different images of the same kind
    of tissue do not."""
    f = g.astype(np.float32)
    return cv2.GaussianBlur(f, (0, 0), s1) - cv2.GaussianBlur(f, (0, 0), s2)


def ncc(a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None) -> float:
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    if mask is not None:
        m = mask.astype(bool)
        if m.sum() < 30:
            return 0.0
        a, b = a[m], b[m]
    else:
        a, b = a.ravel(), b.ravel()
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


def fit_scale(h: int, w: int, max_side: int = 720, min_side: int = 140) -> float:
    """Working scale: shrink big panels for speed, enlarge thin strips (blots) so features exist."""
    s = min(1.0, max_side / max(h, w))
    if min(h, w) * s < min_side:
        s = min(min_side / max(1, min(h, w)), max_side * 1.4 / max(h, w), 4.0)
    return float(s)


def to_png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes() if ok else b""


def content_mask(g: np.ndarray) -> np.ndarray:
    """Pixels that are not flat page background."""
    return ~((g >= 240) | (cv2.Laplacian(g, cv2.CV_32F, ksize=3) == 0) & (g >= 225))


def hull_area(pts: np.ndarray) -> float:
    if len(pts) < 3:
        return 0.0
    return float(cv2.contourArea(cv2.convexHull(pts.astype(np.float32))))


def decompose(M: np.ndarray) -> dict:
    """Scale, rotation (degrees) and anisotropy of a 2x3 affine map."""
    A = M[:, :2]
    sx = float(np.hypot(A[0, 0], A[1, 0]))
    sy = float(np.hypot(A[0, 1], A[1, 1]))
    rot = float(np.degrees(np.arctan2(A[1, 0], A[0, 0])))
    return {"scale": (sx + sy) / 2, "sx": sx, "sy": sy, "rotation": rot, "aniso": max(sx, sy) / max(min(sx, sy), 1e-6)}


def periodicity(g: np.ndarray) -> float:
    """Highest autocorrelation of the band-passed texture away from zero shift (0..1).  Camera noise and tissue
    decorrelate within a pixel or two; printed halftone screens, dither, hatching and grids repeat - and a repeating
    pattern matches itself under any shift or mirror, so it must not count as evidence of copying."""
    if g.size < 400:
        return 0.0
    h, w = g.shape[:2]
    if max(h, w) > 256:
        s = 256 / max(h, w)
        g = cv2.resize(g, (max(16, int(w * s)), max(16, int(h * s))), interpolation=cv2.INTER_AREA)
    d = dog(g, 0.6, 2.0)
    d = d - d.mean()
    F = np.fft.fft2(d)
    ac = np.real(np.fft.ifft2(F * np.conj(F)))
    ac /= ac.flat[0] + 1e-9
    ac = np.fft.fftshift(ac)
    cy, cx = ac.shape[0] // 2, ac.shape[1] // 2
    r = 12                                   # screens and dither repeat within a few pixels; lanes and cells do not
    win = ac[max(0, cy - r):cy + r + 1, max(0, cx - r):cx + r + 1].copy()
    c0y, c0x = cy - max(0, cy - r), cx - max(0, cx - r)
    # only off-axis repeats: a long band or edge correlates with itself along its own direction; a printed
    # screen repeats in two directions at once
    win[max(0, c0y - 3):c0y + 4, :] = 0
    win[:, max(0, c0x - 3):c0x + 4] = 0
    return float(win.max())


def ocr_mask(img: np.ndarray) -> np.ndarray | None:
    """Where printed text (panel letters, axis numbers, percentages, scale-bar labels) is burned into a raster.
    Text repeats identical glyph shapes, which a copy detector would otherwise read as cloned regions.  Uses the
    `tesseract` command when installed; returns None when it is not."""
    import shutil
    import subprocess

    exe = shutil.which("tesseract") or ("/opt/homebrew/bin/tesseract" if
                                        __import__("os").path.exists("/opt/homebrew/bin/tesseract") else None)
    if exe is None or img is None:
        return None
    g = gray(img)
    h, w = g.shape
    up = 2.0 if max(h, w) < 900 else 1.0
    src = cv2.resize(g, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC) if up != 1 else g
    mask = np.zeros((h, w), bool)
    for im in (src, 255 - src):                         # dark text on light, and light text on dark
        ok, buf = cv2.imencode(".png", im)
        try:
            out = subprocess.run([exe, "stdin", "stdout", "--psm", "11", "tsv"], input=buf.tobytes(),
                                 capture_output=True, timeout=30).stdout.decode("utf-8", errors="replace")
        except (subprocess.SubprocessError, OSError):
            return None
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 12:
                continue
            txt = parts[11].strip()
            try:
                conf = float(parts[10])
                x, y, bw, bh = (int(int(v) / up) for v in parts[6:10])
            except ValueError:
                continue
            if conf < 70 or not txt or not any(ch.isalnum() or ch in "%μ*#†‡" for ch in txt):
                continue
            if len(txt) < 2 and txt not in "%*#":
                continue                                    # single letters are often texture read as text
            if bh > 0.6 * h and h > 80 or bw > 0.95 * w and w > 200:
                continue                                    # a "word" that big is image content misread
            pad = max(2, bh // 4)
            mask[max(0, y - pad):y + bh + pad, max(0, x - pad):x + bw + pad] = True
    return mask if mask.any() else None
