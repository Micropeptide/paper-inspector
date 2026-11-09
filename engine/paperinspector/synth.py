"""Synthetic science images and papers with planted problems.

Used three ways: as the test bench (every check is measured against known plants and clean controls), as the demo
paper in the app (no real author is accused of anything), and as a teaching aid ("this is what a mirrored reuse
looks like").  Every image is procedurally generated from a seed, so two different seeds are two honest, different
experiments - exactly the hard negative a duplicate finder must not confuse.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import cv2
import numpy as np


# ---------------------------------------------------------------- images

def _smooth_noise(rng, h, w, scale):
    small = rng.standard_normal((max(2, h // scale), max(2, w // scale))).astype(np.float32)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


def _blobs(rng, h, w, n, rmin, rmax):
    m = np.zeros((h, w), np.float32)
    for _ in range(n):
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        ax, ay = rng.uniform(rmin, rmax), rng.uniform(rmin, rmax)
        cv2.ellipse(m, (int(cx), int(cy)), (int(ax), int(ay)), float(rng.uniform(0, 180)), 0, 360,
                    float(rng.uniform(0.6, 1.0)), -1, cv2.LINE_AA)
    return m


def fluorescence(rng, h=320, w=320) -> np.ndarray:
    nuc = cv2.GaussianBlur(_blobs(rng, h, w, int(rng.integers(35, 80)), 5, 12), (0, 0), 1.6)
    cyto = cv2.GaussianBlur(_blobs(rng, h, w, int(rng.integers(20, 40)), 14, 30), (0, 0), 5) * \
        (0.6 + 0.4 * _smooth_noise(rng, h, w, 12))
    img = np.zeros((h, w, 3), np.float32)
    img[:, :, 0] = 12 + 200 * nuc                       # DAPI blue
    img[:, :, 1] = 10 + 150 * np.clip(cyto, 0, 1.2)     # green stain
    img[:, :, 2] = 8 + 40 * np.clip(cyto * nuc, 0, 1)
    img = rng.poisson(np.clip(img, 0, None)).astype(np.float32) + rng.normal(0, 2.5, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def histology(rng, h=320, w=320) -> np.ndarray:
    tex = 0.55 * _smooth_noise(rng, h, w, 18) + 0.3 * _smooth_noise(rng, h, w, 6) + 0.15 * _smooth_noise(rng, h, w, 2)
    nuc = cv2.GaussianBlur(_blobs(rng, h, w, int(rng.integers(120, 220)), 2, 5), (0, 0), 0.8)
    lumen = cv2.GaussianBlur(_blobs(rng, h, w, int(rng.integers(2, 6)), 15, 45), (0, 0), 4)
    pink = np.array([200, 150, 225], np.float32)        # BGR eosin
    purple = np.array([150, 60, 90], np.float32)        # haematoxylin
    white = np.array([240, 238, 245], np.float32)
    t = np.clip(0.5 + 0.35 * tex, 0, 1)[..., None]
    img = pink * (0.75 + 0.25 * t) + (purple - pink) * np.clip(nuc, 0, 1)[..., None]
    img = img * (1 - np.clip(lumen, 0, 1)[..., None]) + white * np.clip(lumen, 0, 1)[..., None]
    img += rng.normal(0, 4, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def phase_contrast(rng, h=300, w=300) -> np.ndarray:
    base = 140 + 18 * _smooth_noise(rng, h, w, 40)
    cells = _blobs(rng, h, w, int(rng.integers(25, 60)), 6, 16)
    halo = cv2.GaussianBlur(cells, (0, 0), 2) - cv2.GaussianBlur(cells, (0, 0), 5)
    g = base + 160 * halo - 30 * cv2.GaussianBlur(cells, (0, 0), 1.2) + rng.normal(0, 5, (h, w))
    g = np.clip(g, 0, 255).astype(np.uint8)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def blot(rng, lanes=6, h=64, lane_w=42, ys=None, strengths=None) -> np.ndarray:
    w = lanes * lane_w
    g = np.full((h, w), 228, np.float32) - 10 * np.linspace(0, 1, w)[None, :] + 6 * _smooth_noise(rng, h, w, 16)
    ys = ys if ys is not None else [h * 0.5]
    for y in ys:
        for l in range(lanes):
            s = strengths[l] if strengths is not None else rng.uniform(0.3, 1.0)
            cx = lane_w * (l + 0.5) + rng.normal(0, 1.2)
            band = np.zeros((h, w), np.float32)
            cv2.ellipse(band, (int(cx), int(y + rng.normal(0, 0.8))),
                        (int(lane_w * rng.uniform(0.32, 0.4)), int(rng.uniform(3, 5.5))), 0, 0, 360, 1.0, -1,
                        cv2.LINE_AA)
            band = cv2.GaussianBlur(band, (0, 0), rng.uniform(1.4, 2.4))
            g -= 170 * s * band * (0.85 + 0.3 * rng.random((h, w)))
    g += rng.normal(0, 3.0, (h, w))
    g = np.clip(g, 0, 255).astype(np.uint8)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def photo(rng, h=300, w=360) -> np.ndarray:
    """Animal / tumour / plate style photograph: a coloured object on a textured backdrop."""
    bg = np.array([90, 120, 150], np.float32) + 25 * _smooth_noise(rng, h, w, 30)[..., None]
    obj = cv2.GaussianBlur(_blobs(rng, h, w, 1, 60, 110), (0, 0), 6)[..., None]
    col = np.array([120, 120, 210], np.float32) + 30 * _smooth_noise(rng, h, w, 10)[..., None]
    img = bg * (1 - obj) + col * obj + rng.normal(0, 5, (h, w, 3)) + 18 * _smooth_noise(rng, h, w, 3)[..., None]
    return np.clip(img, 0, 255).astype(np.uint8)


def chart(rng, h=260, w=320) -> np.ndarray:
    img = np.full((h, w, 3), 255, np.uint8)
    cv2.line(img, (40, h - 30), (w - 10, h - 30), (0, 0, 0), 1)
    cv2.line(img, (40, 10), (40, h - 30), (0, 0, 0), 1)
    n = int(rng.integers(3, 6))
    for i in range(n):
        v = rng.uniform(0.2, 0.95)
        x0 = 55 + i * (w - 70) // n
        cv2.rectangle(img, (x0, int(h - 30 - v * (h - 50))), (x0 + (w - 70) // n - 12, h - 31),
                      (int(rng.integers(40, 200)), int(rng.integers(40, 200)), int(rng.integers(40, 200))), -1)
    return img


GENERATORS = {"fluorescence": fluorescence, "histology": histology, "phase": phase_contrast,
              "photo": photo, "blot": lambda rng: blot(rng, lanes=int(rng.integers(4, 9)))}


# ---------------------------------------------------------------- manipulations

def flip_h(img):
    return np.ascontiguousarray(img[:, ::-1])


def rotate(img, deg):
    if deg % 90 == 0:
        return np.ascontiguousarray(np.rot90(img, k=int(deg // 90) % 4))
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    out = cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REFLECT)
    m = int(0.12 * min(h, w))
    return out[m:h - m, m:w - m].copy()


def rescale(img, sx, sy=None):
    sy = sx if sy is None else sy
    h, w = img.shape[:2]
    return cv2.resize(img, (int(w * sx), int(h * sy)), interpolation=cv2.INTER_CUBIC if sx > 1 else cv2.INTER_AREA)


def adjust(img, gain=1.15, bias=-12):
    return np.clip(img.astype(np.float32) * gain + bias, 0, 255).astype(np.uint8)


def crop(img, x0, y0, x1, y1):
    h, w = img.shape[:2]
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)].copy()


def clone_patch(img, rng, size=0.22, n=1):
    """Copy-move: paste a patch of the image elsewhere in the same image (same orientation)."""
    out = img.copy()
    h, w = img.shape[:2]
    ps = int(min(h, w) * size)
    boxes = []
    for _ in range(n):
        sx, sy = int(rng.uniform(0, w - ps)), int(rng.uniform(0, h - ps))
        for _ in range(50):
            dx, dy = int(rng.uniform(0, w - ps)), int(rng.uniform(0, h - ps))
            if abs(dx - sx) > ps * 1.1 or abs(dy - sy) > ps * 1.1:
                break
        out[dy:dy + ps, dx:dx + ps] = img[sy:sy + ps, sx:sx + ps]
        boxes.append(((sx, sy, ps, ps), (dx, dy, ps, ps)))
    return out, boxes


def paint_patch(img, rng, value=None):
    """A brush stroke of one flat colour (covering a band or a cell)."""
    out = img.copy()
    h, w = img.shape[:2]
    v = tuple(int(c) for c in (img[0, 0] if value is None else value))
    pts = np.int32([[rng.uniform(0.2, 0.8) * w, rng.uniform(0.2, 0.8) * h] for _ in range(5)])
    cv2.polylines(out, [pts], False, v, thickness=max(4, int(min(h, w) * 0.07)))
    return out


def splice_lanes(b1, b2, lane_w, k):
    """Lanes 0..k-1 from one blot, the rest from another exposure - a hard vertical seam."""
    out = b1.copy()
    out[:, k * lane_w:] = adjust(b2[:, k * lane_w:], 0.92, 14)
    return out


def jpeg(img, q=80):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


# ---------------------------------------------------------------- papers

@dataclass
class Plant:
    check: str              # which detector should find it
    where: list[str]        # panel tags involved ("F1.b"), as laid out
    note: str = ""


@dataclass
class FigureSpec:
    number: int
    caption: str
    panels: list[np.ndarray]
    cols: int = 3
    tags: list[str] = field(default_factory=list)


@dataclass
class PaperSpec:
    title: str
    abstract: str
    body: list[str]
    figures: list[FigureSpec]
    references: list[str] = field(default_factory=list)
    plants: list[Plant] = field(default_factory=list)


def render_pdf(spec: PaperSpec) -> bytes:
    """Lay a paper out as a real PDF: text, figures as embedded rasters, panel letters as PDF text."""
    import pymupdf

    doc = pymupdf.open()
    W, H = 612, 792
    margin = 54

    def new_page():
        return doc.new_page(width=W, height=H)

    page = new_page()
    y = margin
    rect = pymupdf.Rect(margin, y, W - margin, y + 60)
    page.insert_textbox(rect, spec.title, fontsize=16, fontname="hebo")
    y += 70
    blocks = ["Abstract. " + spec.abstract] + spec.body
    for para in blocks:
        r = pymupdf.Rect(margin, y, W - margin, H - margin)
        need = page.insert_textbox(r, para, fontsize=9.5, fontname="helv")
        if need < 0:
            page = new_page()
            y = margin
            r = pymupdf.Rect(margin, y, W - margin, H - margin)
            need = page.insert_textbox(r, para, fontsize=9.5, fontname="helv")
        used = (H - margin - y) - need
        y += used + 10

    for fs in spec.figures:
        page = new_page()
        cols = fs.cols
        n = len(fs.panels)
        rows = (n + cols - 1) // cols
        cell_w = (W - 2 * margin - (cols - 1) * 8) / cols
        total = sum(max(cell_w * p.shape[0] / p.shape[1] for p in fs.panels[r * cols:(r + 1) * cols]) + 10
                    for r in range(rows))
        avail = H - 2 * margin - 70
        if total > avail:
            cell_w *= avail / total
        cy = margin
        for r_ in range(rows):
            row = fs.panels[r_ * cols:(r_ + 1) * cols]
            heights = [cell_w * p.shape[0] / p.shape[1] for p in row]
            rh = max(heights)
            for c_, p in enumerate(row):
                x0 = margin + c_ * (cell_w + 8)
                hh = heights[c_]
                rect = pymupdf.Rect(x0, cy, x0 + cell_w, cy + hh)
                ok, buf = cv2.imencode(".png", p)
                page.insert_image(rect, stream=buf.tobytes())
                idx = r_ * cols + c_
                page.insert_text((x0 + 3, cy + 11), chr(ord("A") + idx), fontsize=10, fontname="hebo",
                                 color=(1, 1, 1) if p.mean() < 110 else (0, 0, 0))
            cy += rh + 10
        cap = pymupdf.Rect(margin, cy + 4, W - margin, H - margin)
        page.insert_textbox(cap, f"Figure {fs.number}. {fs.caption}", fontsize=8.5, fontname="helv")

    if spec.references:
        page = new_page()
        text = "References\n" + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(spec.references))
        page.insert_textbox(pymupdf.Rect(margin, margin, W - margin, H - margin), text, fontsize=8, fontname="helv")
    out = doc.tobytes()
    doc.close()
    return out


def demo_paper(seed: int = 7) -> PaperSpec:
    """A fictional paper with one example of most problems Paper Inspector looks for."""
    rng = np.random.default_rng(seed)
    plants: list[Plant] = []

    # Figure 1: immunofluorescence grid, panel E is panel A mirrored and re-contrasted
    f1 = [fluorescence(rng) for _ in range(6)]
    f1[4] = adjust(flip_h(f1[0]), 1.2, -10)
    plants.append(Plant("image.duplicate", ["F1.a", "F1.e"], "mirrored + brightened"))
    # Figure 2: histology; B and D are overlapping crops of one larger field; F has a cloned region
    big = histology(rng, 420, 560)
    f2 = [histology(rng) for _ in range(6)]
    f2[1] = crop(big, 0.0, 0.0, 0.62, 0.75)
    f2[3] = crop(big, 0.38, 0.25, 1.0, 1.0)
    plants.append(Plant("image.duplicate", ["F2.b", "F2.d"], "overlapping fields"))
    f2[5], _ = clone_patch(f2[5], rng, 0.25)
    plants.append(Plant("image.clone", ["F2.f"], "cloned patch"))
    # Figure 3: western blots; the actin strip is reused, one blot is spliced, one band painted over
    actin = blot(rng, lanes=6)
    b1 = blot(rng, lanes=6)
    b2 = splice_lanes(blot(rng, lanes=6), blot(rng, lanes=6), 42, 3)
    plants.append(Plant("image.splice", ["F3.c"], "two exposures joined"))
    b3 = paint_patch(blot(rng, lanes=6), rng, value=(40, 40, 40))
    plants.append(Plant("image.flat", ["F3.d"], "painted stroke"))
    f3 = [b1, actin, b2, b3, rotate(actin, 180), blot(rng, lanes=6)]
    plants.append(Plant("image.duplicate", ["F3.b", "F3.e"], "loading control reused, rotated 180 deg"))
    # Figure 4: phase contrast "different days", E is a zoomed, stretched crop of B
    f4 = [phase_contrast(rng) for _ in range(6)]
    f4[4] = rescale(crop(f4[1], 0.1, 0.15, 0.85, 0.9), 1.3, 1.15)
    plants.append(Plant("image.duplicate", ["F4.b", "F4.e"], "rescaled, stretched crop"))
    figs = [
        FigureSpec(1, "Immunofluorescence of control (A-C) and treated (D-F) cells; DAPI (blue), marker (green).",
                   f1),
        FigureSpec(2, "H&E staining of liver sections from control (A-C) and treated (D-F) mice.", f2),
        FigureSpec(3, "Western blots of target proteins; beta-actin loading control in B and E.", f3, cols=2),
        FigureSpec(4, "Phase-contrast images of wound closure at 0, 12, 24 h (A-C) and treated (D-F).", f4),
    ]
    body = [
        "Introduction. Hepatic fibrosis remains a major cause of morbidity. Here we report that compound X reverses "
        "fibrosis in mice through counterfeit consciousness guided screening and profound learning models [1-3].",
        "Methods. Mice (n = 8 per group) were treated for 28 days. Data are mean ± SD. Groups were compared with "
        "Student's t-test; P < 0.05 was considered significant.",
        "Results. Treatment reduced the fibrosis score (control 3.42 ± 0.51 vs treated 1.87 ± 0.49, n = 8; "
        "t(14) = 2.10, p < 0.001). ALT fell from 88.1 to 41.3 U/L (t(14) = 6.25, p = 0.21). 12 of 40 mice (35%) "
        "developed ascites. The mean number of lesions per mouse was 2.47 (n = 8). Serum AST: control 102.4 ± 3.24, "
        "treated 61.7 ± 3.24, recovery 74.2 ± 3.24, sham 99.8 ± 3.24 (SE).",
        "Discussion. As an AI language model, I cannot verify these results, but the findings suggest compound X "
        "is a prognostic biomarker and therapeutic target. Certainly! Here is a revised version of the discussion.",
        "Author statement. No generative AI tools were used in the preparation of this manuscript.",
    ]
    refs = [f"Author {chr(65 + i % 26)}, et al. Study {i + 1} of liver fibrosis. J Hepatol Res. 20{10 + i % 14};"
            f"{i + 3}:{100 + i}-{110 + i}." for i in range(14)]
    refs.append(refs[2])
    plants += [Plant("text.ai_boilerplate", []), Plant("text.tortured", []), Plant("stats.recompute", []),
               Plant("stats.grim", []), Plant("stats.percent", []), Plant("stats.repeated_se", []),
               Plant("refs.uncited", []), Plant("refs.duplicate", [])]
    return PaperSpec("Compound X reverses hepatic fibrosis via the Y signalling axis", "We show that compound X "
                     "reverses fibrosis in a mouse model.", body, figs, refs, plants)


def to_pdf_file(spec: PaperSpec, path) -> None:
    with open(path, "wb") as fh:
        fh.write(render_pdf(spec))


def demo_supplement(path, seed: int = 11) -> None:
    """A supplementary workbook to go with the demo paper, with data problems planted in it."""
    import datetime

    import openpyxl
    from openpyxl.styles import Font

    rng = np.random.default_rng(seed)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fig 2 source data"
    ws.append(["Mouse", "Control ALT (U/L)", "Treated ALT (U/L)", "Liver weight (g)", "Fibrosis area (%)"])
    ctrl = np.round(rng.normal(88.1, 9.7, 30), 2)
    trt = np.round(rng.normal(41.3, 8.2, 30), 2)
    trt[8:20] = ctrl[2:14]                        # a block of control values pasted into the treated column
    for i in range(30):
        ws.append([i + 1, float(ctrl[i]), float(trt[i]), float(np.round(rng.normal(1.42, 0.11), 3)),
                   float(np.round(rng.uniform(2, 18), 2))])
    for r in range(22, 32):                       # these rows were pasted in from another file
        ws.cell(row=r, column=4).font = Font(name="Cambria", size=11)
    ws["G2"] = "=RAND()*20+80"
    ws["F1"] = "note"
    genes = wb.create_sheet("RNA-seq hits")
    genes.append(["Gene", "log2 fold change", "p value"])
    names = ["TP53", "MYC", "COL1A1", "TGFB1", "ACTA2", "TIMP1", "MMP2", "SMAD3", "CTGF", "LOX",
             "SERPINE1", "FN1", "VIM", "PDGFRB", "IL6", "TNF", "CCL2", "SPP1", "LGALS3", "MMP9", "COL3A1"]
    for g in names:
        genes.append([g, float(np.round(rng.normal(1.2, 0.8), 3)), float(np.round(rng.uniform(0, 0.05), 4))])
    genes.append([datetime.datetime(2026, 9, 2), 2.31, 0.0012])     # SEPT2 -> 2-Sep
    genes.append([datetime.datetime(2026, 3, 1), 1.87, 0.0043])     # MARCH1 -> 1-Mar
    table = wb.create_sheet("Table S3")
    table.append(["Group", "Mean AST", "SE"])
    for g, m in (("Control", 102.4), ("Treated", 61.7), ("Recovery", 74.2), ("Sham", 99.8), ("Vehicle", 101.1)):
        table.append([g, m, 3.24])
    hidden = wb.create_sheet("excluded")
    hidden.sheet_state = "hidden"
    for i in range(12):
        hidden.append([i + 1] + [float(np.round(v, 2)) for v in rng.normal(60, 12, 3)])
    wb.save(path)


# ---------------------------------------------------------------- more manipulations (wave 2; existing outputs unchanged)

def invert(img):
    """A negative: light bands on dark, as blots and gels are sometimes shown."""
    return (255 - img).astype(np.uint8)


def gamma(img, g=0.5):
    """A monotonic, non-linear contrast change."""
    return np.clip(255.0 * (img.astype(np.float32) / 255.0) ** g, 0, 255).astype(np.uint8)


def stacked_blot(strips):
    """Several blot strips (target proteins, then the loading control) in one raster, with no gap between them."""
    w = min(s.shape[1] for s in strips)
    return np.ascontiguousarray(np.vstack([s[:, :w] for s in strips]))


def reused_loading_control(rng, lanes=6, lane_w=42):
    """Two stacked blots with different target strips and one shared loading-control strip at the bottom."""
    control = blot(rng, lanes=lanes, h=40, lane_w=lane_w)
    a = stacked_blot([blot(rng, lanes=lanes, h=56, lane_w=lane_w), control])
    b = stacked_blot([blot(rng, lanes=lanes, h=56, lane_w=lane_w), control])
    return a, b


def mirror_clone_patch(img, rng, size=0.25, mode="mirror-h"):
    """Copy-move with a flip: paste a patch elsewhere in the same image, mirrored left-right ("mirror-h"),
    top-bottom ("mirror-v") or turned 180 degrees ("rot180")."""
    out = img.copy()
    h, w = img.shape[:2]
    ps = int(min(h, w) * size)
    sx, sy = int(rng.uniform(0, w - ps)), int(rng.uniform(0, h - ps))
    for _ in range(50):
        dx, dy = int(rng.uniform(0, w - ps)), int(rng.uniform(0, h - ps))
        if abs(dx - sx) > ps * 1.1 or abs(dy - sy) > ps * 1.1:
            break
    patch = img[sy:sy + ps, sx:sx + ps]
    patch = patch[:, ::-1] if mode == "mirror-h" else patch[::-1] if mode == "mirror-v" else patch[::-1, ::-1]
    out[dy:dy + ps, dx:dx + ps] = patch
    return out, ((sx, sy, ps, ps), (dx, dy, ps, ps))


def mirror_band(img, lane_w, src_lane, dst_lane, flip="h"):
    """A blot whose lane `dst_lane` is lane `src_lane` flipped (the hidden way to duplicate a band)."""
    out = img.copy()
    a = img[:, src_lane * lane_w:(src_lane + 1) * lane_w]
    out[:, dst_lane * lane_w:(dst_lane + 1) * lane_w] = a[:, ::-1] if flip == "h" else a[::-1]
    return out
