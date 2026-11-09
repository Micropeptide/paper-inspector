"""Read a paper: text per page, the figures (raster images placed on pages) and their captions.

A figure is the union of the raster images that sit together on a page, rendered at about the resolution the authors
embedded, so tiny western-blot bands keep their pixels.  Captions ("Figure 3.", "Fig. S2 |") name the figures; a
figure without a readable caption is called by its page.
"""
from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from .imgutil import read_image
from .models import Box, Figure, Paper

CAPTION_RE = re.compile(r"^\s*(?:Supplementa(?:l|ry)\s+)?(fig(?:ure)?s?)\.?\s*(S?\d+)\s*[\.:|\-–—)]?", re.I)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+)", re.I)
MIN_SIDE_PT = 40          # smaller placements are logos, icons, equation bits
MIN_AREA_PT = 60 * 60
MAX_DPI, MIN_DPI = 300, 150
MAX_PIXELS = 40_000_000   # one rendered figure; a tiny image stretched over a poster-sized page must not take GBs
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}


def load(path: str | Path) -> Paper:
    path = Path(path)
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return _load_image(path)
    return _load_pdf(path)


def _load_image(path: Path) -> Paper:
    img = read_image(path)
    if img is None:
        raise ValueError(f"cannot read image {path.name}")
    h, w = img.shape[:2]
    fig = Figure(id="F1", label=path.stem, page=1, page_box=Box(0, 0, w, h), image=img, dpi=72)
    return Paper(source=path.name, title=path.stem, pages=[""], figures=[fig], page_count=1)


def _load_pdf(path: Path) -> Paper:
    doc = pymupdf.open(str(path))
    paper = Paper(source=path.name, page_count=doc.page_count)
    paper.pages = [p.get_text("text") for p in doc]
    paper.title = _title(doc)
    m = DOI_RE.search(" ".join(paper.pages[:2]))
    paper.doi = m.group(1).rstrip(".)") if m else ""

    captions = [_captions(p) for p in doc]
    figs: list[Figure] = []
    for pno, page in enumerate(doc):
        regions = _image_regions(page)
        if not regions:
            continue
        caps = captions[pno]
        owner = _assign_captions(regions, caps)
        if not caps and pno + 1 < doc.page_count:       # full-page figure, legend at the top of the next page
            nxt = [c for c in captions[pno + 1] if c[0].y0 < _bounds(doc[pno + 1]).height * 0.3]
            if nxt and len(regions) == 1:
                owner = {0: nxt[0]}
        grouped: dict[int, list[pymupdf.Rect]] = {}
        loose: list[pymupdf.Rect] = []
        for i, r in enumerate(regions):
            cap = owner.get(i)
            if cap is None:
                loose.append(r)
            else:
                grouped.setdefault(id(cap), []).append(r)
        by_id = {id(c): c for c in caps}
        if not caps and owner:
            by_id = {id(owner[0]): owner[0]}
        for key, rects in grouped.items():
            cap = by_id[key]
            figs.append(_render(page, _union(rects), cap[1], cap[2]))
        for k, r in enumerate(loose):
            figs.append(_render(page, r, "", ""))

    _name_figures(figs)
    paper.figures = figs
    doc.close()
    return paper


def _title(doc: pymupdf.Document) -> str:
    t = (doc.metadata or {}).get("title") or ""
    if len(t) > 12 and not t.lower().endswith((".pdf", ".doc", ".docx")):
        return t.strip()
    if doc.page_count == 0:
        return ""
    best, size = "", 0.0
    for b in doc[0].get_text("dict").get("blocks", []):
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                txt = s.get("text", "").strip()
                if len(txt) > 8 and s.get("size", 0) > size + 0.5:
                    best, size = txt, s["size"]
                elif len(txt) > 3 and abs(s.get("size", 0) - size) < 0.5 and best and len(best) < 250:
                    best += " " + txt
    return best[:300]


def _captions(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, str, str]]:
    """[(rect, number, caption text)] for each block that starts like a figure legend."""
    out = []
    for b in page.get_text("blocks"):
        if b[6] != 0:
            continue
        text = " ".join(b[4].split())
        m = CAPTION_RE.match(text)
        if not m:
            continue
        rest = text[m.end():].lstrip()
        # body text says "Figure 2 shows" / "Fig. 3A and B"; a legend continues with a capital or a bold title
        if rest[:1].islower() or re.match(r"^[A-H](?:[\s,–-]|$)", rest[:2]) and len(text) < 60:
            continue
        out.append((pymupdf.Rect(b[:4]), m.group(2).upper(), text))
    return out


def _bounds(page: pymupdf.Page) -> pymupdf.Rect:
    """The page in unrotated coordinates.  Image boxes and text blocks are unrotated; page.rect (and the clip of
    get_pixmap) are rotated, so a /Rotate 90 page would otherwise crop or blank its figures."""
    return page.rect * page.derotation_matrix


def _image_regions(page: pymupdf.Page) -> list[pymupdf.Rect]:
    rects = []
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & _bounds(page)
        if r.is_empty or r.width < MIN_SIDE_PT and r.height < MIN_SIDE_PT:
            continue
        rects.append(r)
    # images that touch or nearly touch belong together (multi-image figures)
    merged = True
    while merged:
        merged = False
        out: list[pymupdf.Rect] = []
        for r in rects:
            for i, o in enumerate(out):
                if (r + (-6, -6, 6, 6)).intersects(o):
                    out[i] = o | r
                    merged = True
                    break
            else:
                out.append(pymupdf.Rect(r))
        rects = out
    return [r for r in rects if r.width * r.height >= MIN_AREA_PT and min(r.width, r.height) >= 24]


def _assign_captions(regions, caps) -> dict[int, tuple]:
    """Each image region takes the nearest legend below it in the same column (however far: a tall figure's top row
    is far from its legend), else the nearest legend just above it."""
    owner = {}
    for i, r in enumerate(regions):
        best, best_d = None, 1e9
        for c in caps:
            cr = c[0]
            overlap = min(r.x1, cr.x1) - max(r.x0, cr.x0)
            if overlap < min(r.width, cr.width) * 0.15:
                continue
            if cr.y0 >= r.y1 - 4:
                d = cr.y0 - r.y1
            elif cr.y1 <= r.y0 + 4:
                d = (r.y0 - cr.y1) * 3 + 40        # legends above figures are rarer
                if r.y0 - cr.y1 > 120:
                    continue
            else:
                continue
            if d < best_d:
                best, best_d = c, d
        if best is not None:
            owner[i] = best
    # grid neighbours of an owned image share its legend (the right column often sits beyond the legend's text)
    changed = True
    while changed:
        changed = False
        for i, r in enumerate(regions):
            if i in owner:
                continue
            for j, q in owner.items():
                if (r + (-24, -24, 24, 24)).intersects(regions[j]):
                    owner[i] = q
                    changed = True
                    break
    return owner


def _union(rects):
    u = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        u |= r
    return u


def _native_dpi(page: pymupdf.Page, clip: pymupdf.Rect) -> float:
    best = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if not r.intersects(clip) or r.width <= 0:
            continue
        best = max(best, info["width"] / r.width * 72.0, info["height"] / max(r.height, 1e-3) * 72.0)
    return best


def _render(page: pymupdf.Page, clip: pymupdf.Rect, number: str, caption: str) -> Figure:
    """`clip` is in unrotated page space like every box read from the page; it is rotated only for the pixmap, and
    the word and image boxes are rotated the same way so they land on the rendered pixels."""
    M = page.rotation_matrix
    rclip = clip * M
    dpi = float(np.clip(_native_dpi(page, clip) or MIN_DPI, MIN_DPI, MAX_DPI))
    px = (rclip.width / 72 * dpi) * (rclip.height / 72 * dpi)
    if px > MAX_PIXELS:
        dpi = max(18.0, dpi * (MAX_PIXELS / px) ** 0.5)
    pix = page.get_pixmap(clip=rclip, dpi=int(dpi), alpha=False, colorspace=pymupdf.csRGB)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    img = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2BGR)
    k = pix.width / max(rclip.width, 1e-3)

    def to_fig(r: pymupdf.Rect) -> list[float]:
        r = r * M
        return [(r.x0 - rclip.x0) * k, (r.y0 - rclip.y0) * k, r.width * k, r.height * k]

    boxes = [[b[0] - 1, b[1] - 1, b[2] + 2, b[3] + 2] for b in
             (to_fig(pymupdf.Rect(w[:4])) for w in page.get_text("words", clip=clip))]
    placed = []
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & clip
        if r.is_empty or r.width < 8 or r.height < 8:
            continue
        placed.append(to_fig(r))
    fig = Figure(id="", label="", page=page.number + 1,
                 page_box=Box(rclip.x0, rclip.y0, rclip.width, rclip.height), caption=caption, image=img, dpi=dpi,
                 text_boxes=boxes, image_boxes=placed)
    fig._number = number   # type: ignore[attr-defined]
    fig.image_refs = _image_refs(page, clip, to_fig)   # type: ignore[attr-defined]
    return fig


def _image_refs(page: pymupdf.Page, clip: pymupdf.Rect, to_fig) -> list[dict]:
    """Which embedded picture each placement on the figure shows: its PDF object number and a hash of its data,
    where it is drawn (figure pixels) and how the layout turned it.  The same picture placed twice under two labels
    is a reuse the PDF itself records."""
    try:
        infos = page.get_image_info(hashes=True, xrefs=True)
    except Exception:                   # noqa: BLE001 - an odd image must not stop the paper from loading
        return []
    out = []
    for info in infos:
        full = pymupdf.Rect(info["bbox"])
        r = full & clip
        if r.is_empty or r.width < 8 or r.height < 8:
            continue
        a, b, c, d = (float(v) for v in info.get("transform", (1, 0, 0, 1, 0, 0))[:4])
        digest = info.get("digest") or b""
        out.append({"box": to_fig(r), "full": to_fig(full), "xref": int(info.get("xref") or 0),
                    "digest": digest.hex() if isinstance(digest, (bytes, bytearray)) else str(digest),
                    "width": int(info.get("width") or 0), "height": int(info.get("height") or 0),
                    "mirrored": a * d - b * c < 0, "rotation": int(round(np.degrees(np.arctan2(b, a)) / 90.0)) * 90 % 360,
                    "scale": float(np.hypot(a, b) / max(1, int(info.get("width") or 1)))})
    return out


def _name_figures(figs: list[Figure]) -> None:
    used: dict[str, int] = {}
    for f in figs:
        num = getattr(f, "_number", "")
        if num:
            base = f"F{num}"
            n = used.get(base, 0)
            used[base] = n + 1
            f.id = base if n == 0 else f"{base}-{n + 1}"
            f.label = ("Figure " + num) + ("" if n == 0 else f" (part {n + 1})")
        else:
            k = used.get(f"P{f.page}", 0) + 1
            used[f"P{f.page}"] = k
            f.id = f"P{f.page}.{k}"
            f.label = f"Page {f.page} image {k}"
