"""Edge inputs for the image path: odd pixel formats, thin and masked panels, rotated pages, overlapping rasters,
colliding figure labels and hostile archives.  Each case once crashed, went blind or raised a false alarm."""
from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile

import cv2
import numpy as np
import pymupdf
import pytest
from PIL import Image

from paperinspector import clone, evidence, imgdup, imgutil, panels, pdfdoc, sources, synth
from paperinspector.models import Box, Figure, Panel


def _png(img: np.ndarray) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


# ------------------------------------------------------------------ pixel formats

def test_16bit_microscope_tiff_keeps_its_grey_levels(tmp_path):
    rng = np.random.default_rng(1)
    raw = np.clip(rng.normal(2000, 200, (200, 240)), 0, 4095).astype(np.uint16)     # 12-bit data in 16 bits
    p = tmp_path / "raw.tif"
    Image.fromarray(raw).save(p)
    for img in (sources._read_image(p), pdfdoc.load(p).figures[0].image):
        assert img.dtype == np.uint8 and img.shape == (200, 240, 3)
        assert len(np.unique(img[:, :, 0])) > 100 and img.std() > 20
    # the noise layer is there for the forensic checks to read
    assert np.median(np.abs(imgutil.dog(imgutil.gray(sources._read_image(p)), 0.7, 2.0))) > 0.8


def test_transparent_png_is_laid_on_white(tmp_path):
    rgba = np.zeros((120, 160, 4), np.uint8)              # transparent background stored as black
    rgba[40:80, 50:110] = (0, 0, 0, 255)                  # an opaque black label
    p = tmp_path / "plot.png"
    Image.fromarray(rgba, "RGBA").save(p)
    img = sources._read_image(p)
    assert img[5, 5].min() >= 250 and img[60, 80].max() <= 5
    la = np.zeros((60, 60, 2), np.uint8)
    la[20:40, 20:40] = (0, 255)
    Image.fromarray(la, "LA").save(tmp_path / "la.png")
    img = imgutil.read_image(tmp_path / "la.png")
    assert img.shape == (60, 60, 3) and img[2, 2].min() >= 250 and img[30, 30].max() <= 5


def test_grayscale_cmyk_and_bitonal_images(tmp_path):
    rng = np.random.default_rng(2)
    rgb = synth.histology(rng, 120, 160)[:, :, ::-1].copy()
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    Image.fromarray(g).save(tmp_path / "g.png")
    assert np.array_equal(imgutil.read_image(tmp_path / "g.png")[:, :, 0], g)
    for ext in ("tif", "jpg"):
        Image.fromarray(rgb).convert("CMYK").save(tmp_path / f"c.{ext}")
        img = imgutil.read_image(tmp_path / f"c.{ext}")
        assert img.shape == (120, 160, 3) and np.abs(img[:, :, ::-1].astype(int) - rgb).mean() < 12
    Image.fromarray(g > 128).convert("1").save(tmp_path / "b.png")
    img = imgutil.read_image(tmp_path / "b.png")
    assert img.shape == (120, 160, 3) and set(np.unique(img)) <= {0, 255}


def test_image_upload_is_capped_and_gif_is_read(tmp_path):
    big = np.full((900, 3000, 3), 200, np.uint8)
    cv2.imwrite(str(tmp_path / "wide.png"), big)
    assert max(pdfdoc.load(tmp_path / "wide.png").figures[0].image.shape[:2]) == 2400
    Image.new("RGB", (90, 70), (10, 200, 30)).save(tmp_path / "f.gif")
    assert pdfdoc.load(tmp_path / "f.gif").figures[0].image.shape == (70, 90, 3)
    assert imgutil.read_image(b"not an image") is None


def test_decompression_bomb_header_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(imgutil, "MAX_DECODE_PIXELS", 10_000)
    cv2.imwrite(str(tmp_path / "x.png"), np.zeros((200, 200, 3), np.uint8))
    assert imgutil.read_image(tmp_path / "x.png") is None


# ------------------------------------------------------------------ panels and features

def _panel(img, pid="F1.a", box=None, kind="micrograph", mask=None):
    h, w = img.shape[:2]
    return Panel(pid, pid.split(".")[0], box or Box(0, 0, w, h), kind, img, mask)


def test_fully_text_masked_panels_do_not_crash_the_process():
    code = (
        "import numpy as np\n"
        "from paperinspector import imgdup, synth\n"
        "from paperinspector.models import Box, Panel\n"
        "ps = [Panel(f'F{k}.a', f'F{k}', Box(0, 0, 320, 320), 'micrograph', synth.histology(np.random.default_rng(k)),"
        " np.ones((320, 320), bool)) for k in (1, 2)]\n"
        "assert all(imgdup.features(p) is None for p in ps)\n"
        "f = [x for x in map(imgdup.features, ps) if x is not None]\n"
        "assert imgdup.find(f) == []\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]


def test_one_masked_panel_leaves_the_others_comparable():
    a = synth.histology(np.random.default_rng(5))
    ps = [_panel(a, "F1.a", Box(0, 0, 320, 320)), _panel(a.copy(), "F2.a", Box(0, 0, 320, 320)),
          _panel(a.copy(), "F3.a", Box(0, 0, 320, 320), mask=np.ones((320, 320), bool))]
    feats = [f for f in map(imgdup.features, ps) if f is not None]
    assert len(feats) == 2
    ms = imgdup.find(feats)
    assert len(ms) == 1 and {ms[0].a.panel.id, ms[0].b.panel.id} == {"F1.a", "F2.a"}


def test_thin_and_tiny_panels():
    strip = synth.blot(np.random.default_rng(6), lanes=40, h=30, lane_w=45)      # 30 x 1800
    f = imgdup.features(_panel(strip, kind="blot"))
    if f is not None:          # keypoints come back in the strip's own pixels, not stretched past its edge
        assert f.kp[:, 1].max() < strip.shape[0] + 1 and f.kp[:, 0].max() < strip.shape[1] + 1
    for img in (np.full((10, 10, 3), 128, np.uint8), np.full((30, 500, 3), 90, np.uint8)):
        assert imgdup.features(_panel(img)) is None
        assert clone.find(img) == []
    fig = Figure("F1", "Figure 1", 1, Box(0, 0, 40, 40), image=np.full((40, 40, 3), 255, np.uint8))
    assert panels.segment(fig) == []


def test_hub_panel_partners_are_capped():
    base = synth.histology(np.random.default_rng(7))
    rng = np.random.default_rng(8)
    ps = [_panel(np.clip(base.astype(int) + rng.integers(-3, 4, base.shape), 0, 255).astype(np.uint8), f"F{k}.a")
          for k in range(16)]
    keep = imgdup.candidates([imgdup.features(p) for p in ps])
    per: dict[int, int] = {}
    for i, j, _ in keep:
        per[i] = per.get(i, 0) + 1
        per[j] = per.get(j, 0) + 1
    assert keep and max(per.values()) <= imgdup.MAX_PARTNERS


def test_inset_raster_on_a_bigger_raster_is_not_a_duplicate_of_itself():
    big = synth.histology(np.random.default_rng(9), 600, 800)
    inset = synth.fluorescence(np.random.default_rng(10), 220, 220)
    img = big.copy()
    img[20:240, 560:780] = inset
    fig = Figure("F2", "Figure 2", 1, Box(0, 0, 800, 600), image=img,
                 image_boxes=[[0, 0, 800, 600], [560, 20, 220, 220]])
    ps = panels.segment(fig)
    assert len(ps) == 2
    for p in ps:
        p.kind = "micrograph"
    feats = [f for f in map(imgdup.features, ps) if f is not None]
    assert imgdup.find(feats) == []
    # and without the overlay mask, the same-place backstop still holds
    raw = [_panel(img, "F2.a", Box(0, 0, 800, 600)), _panel(img[20:240, 560:780].copy(), "F2.b", Box(560, 20, 220, 220))]
    assert imgdup.find([f for f in map(imgdup.features, raw) if f is not None]) == []


def test_a_real_copy_inside_one_figure_is_still_found():
    a = synth.histology(np.random.default_rng(11))
    img = np.full((340, 700, 3), 255, np.uint8)
    img[10:330, 10:330] = a
    img[10:330, 370:690] = a
    fig = Figure("F3", "Figure 3", 1, Box(0, 0, 700, 340), image=img)
    ps = panels.segment(fig)
    feats = [f for f in map(imgdup.features, ps) if f is not None]
    assert len(imgdup.find(feats)) == 1


# ------------------------------------------------------------------ PDFs

def _rotated_pdf(tmp_path, rect, rotation=90):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    img = synth.histology(np.random.default_rng(12), 300, 400)
    page.insert_image(pymupdf.Rect(*rect), stream=_png(img))
    page.insert_text((rect[0] + 10, rect[1] + 20), "A", fontsize=14, color=(1, 1, 1))
    page.insert_text((rect[0], rect[3] + 16), "Figure 1. Rotated landscape figure", fontsize=9)
    page.set_rotation(rotation)
    p = tmp_path / f"rot{rotation}.pdf"
    doc.save(p)
    return p


@pytest.mark.parametrize("rotation", [90, 180, 270])
@pytest.mark.parametrize("rect", [(50, 100, 250, 250), (50, 560, 450, 760)])
def test_rotated_pages_render_their_figures(tmp_path, rect, rotation):
    paper = pdfdoc.load(_rotated_pdf(tmp_path, rect, rotation))
    assert len(paper.figures) == 1
    f = paper.figures[0]
    h, w = f.image.shape[:2]
    assert (f.image > 245).mean() < 0.2 and f.image.std() > 15     # the picture, not a blank crop
    for x, y, bw, bh in f.image_boxes + f.text_boxes:
        assert -3 <= x and -3 <= y and x + bw <= w + 3 and y + bh <= h + 3
    assert f.id == "F1"


def test_unrotated_page_is_unchanged(tmp_path):
    f = pdfdoc.load(_rotated_pdf(tmp_path, (50, 100, 250, 250), 0)).figures[0]
    assert f.page_box.as_list() == [50, 100, 200, 150]
    assert f.image_boxes and abs(f.image_boxes[0][2] - f.image.shape[1]) <= 2


def test_poster_sized_render_is_capped(tmp_path, monkeypatch):
    doc = pymupdf.open()
    page = doc.new_page(width=5000, height=5000)
    page.insert_image(page.rect, stream=_png(np.random.default_rng(13).integers(0, 255, (16, 16, 3), np.uint8)))
    p = tmp_path / "poster.pdf"
    doc.save(p)
    monkeypatch.setattr(pdfdoc, "MAX_PIXELS", 4_000_000)       # uncapped: 150 dpi, 10417 x 10417 px
    img = pdfdoc.load(p).figures[0].image
    assert img.shape[0] * img.shape[1] <= 4_200_000


# ------------------------------------------------------------------ figure ids

def test_jats_figure_ids_read_the_whole_label():
    cases = {"Figure 1": "F1", "Fig. 3": "F3", "Extended Data Fig. 1": "ED1", "Figure S2": "FS2",
             "Supplementary Figure 4": "FS4", "Figure 1—figure supplement 2": "F1-S2",
             "Figure 2 - figure supplement 1": "F2-S1", "Appendix 1—figure 2": "A1-F2", "5": "F5"}
    for label, fid in cases.items():
        assert sources._fig_id(label, 0) == fid, label
    assert sources._fig_id("pone.0055487.g001", 0, stem="pone.0055487.g001") == "F1"
    assert sources._fig_id("pone.0055487.g012", 0, stem="pone.0055487.g012") == "F12"
    assert sources._fig_id("image_7", 4, stem="image_7") == "F5"


def test_colliding_jats_labels_get_unique_ids(tmp_path):
    rng = np.random.default_rng(14)
    figs = []
    for k, label in enumerate(["Figure 1", "Extended Data Fig. 1", "Figure 1—figure supplement 1", "Figure 1A",
                               "Figure 1B", "", ""]):
        p = tmp_path / f"pone.0055487.g{k + 1:03d}.png"
        cv2.imwrite(str(p), synth.histology(rng, 100, 120))
        figs.append((p, label or p.stem, ""))
    src = sources.load(None, [], meta={"doi": "10.1/x"}, jats_figures=figs)
    ids = [f.id for f in src.paper.figures]
    assert len(ids) == 7 and len(set(ids)) == 7
    assert ids[:3] == ["F1", "ED1", "F1-S1"]


# ------------------------------------------------------------------ archives

def _zip(path, members: dict):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)


def test_zip_with_more_than_26_members_and_same_named_files(tmp_path):
    rng = np.random.default_rng(15)
    members = {f"raw/blot_{k:02d}.png": _png(synth.histology(rng, 90, 110)) for k in range(28)}
    xa, xb = synth.histology(rng, 90, 110), synth.fluorescence(rng, 90, 110)
    members.update({"Fig1/x.png": _png(xa), "Fig2/x.png": _png(xb), "__MACOSX/._x.png": b"junk", ".DS_Store": b"x"})
    p = tmp_path / "S1.zip"
    _zip(p, members)
    src = sources.load(None, [(p, "S1.zip", "upload")])
    ids = [f.id for f in src.paper.figures]
    assert len(ids) == 30 and len(set(ids)) == 30
    assert "S1aa" in ids and "S1ad" in ids
    arch = next(f for f in src.files if f.kind == "archive")
    assert arch.status == "unpacked: 30 files"
    names = [f.name for f in src.files if f.kind == "image"]
    assert len(set(names)) == 30 and "x.png" in names and "x_2.png" in names
    shown = {f.label.rsplit(" / ", 1)[-1]: f for f in src.paper.figures}
    a, b = shown["Fig1/x.png"].image, shown["Fig2/x.png"].image
    assert not np.array_equal(a, b)            # neither overwrote the other


def test_archive_member_cap_and_traversal(tmp_path):
    rng = np.random.default_rng(16)
    members = {f"img{k}.png": _png(synth.histology(rng, 60, 60)) for k in range(35)}
    members["../../evil.txt"] = b"a,b\n1,2\n"
    members["/abs/evil2.csv"] = b"a,b\n1,2\n"
    p = tmp_path / "up" / "S1.zip"
    p.parent.mkdir()
    _zip(p, members)
    out = tmp_path / "up" / "out"
    out.mkdir()
    kept, skipped = sources._unpack(p, out)
    assert len(kept) == sources.MAX_MEMBERS and skipped == 37 - sources.MAX_MEMBERS
    assert all(k.parent == out for k, _ in kept)
    assert {q.relative_to(tmp_path).parts[:2] for q in tmp_path.rglob("*")} <= {("up",), ("up", "S1.zip"), ("up", "out")}
    assert {q.name for q in out.iterdir()} == {k.name for k, _ in kept}


def test_zip_bomb_is_not_extracted(tmp_path, monkeypatch):
    p = tmp_path / "bomb.zip"
    _zip(p, {"zeros.csv": b"0" * (40 * 2 ** 20), "ok.csv": b"a,b\n1,2\n"})
    assert p.stat().st_size < 2 ** 20
    out = tmp_path / "out"
    out.mkdir()
    kept, skipped = sources._unpack(p, out)
    assert [n for _, n in kept] == ["ok.csv"] and skipped == 1
    assert sum(f.stat().st_size for f in out.iterdir()) < 100
    # the per-archive byte budget holds even for members with a plausible ratio
    monkeypatch.setattr(sources, "MAX_ARCHIVE_BYTES", 5000)
    rnd = np.random.default_rng(17)
    p2 = tmp_path / "many.zip"
    _zip(p2, {f"r{k}.csv": rnd.integers(0, 255, 3000, np.uint8).tobytes() for k in range(4)})
    out2 = tmp_path / "out2"
    out2.mkdir()
    kept, skipped = sources._unpack(p2, out2)
    assert len(kept) == 1 and skipped == 3
    assert sum(f.stat().st_size for f in out2.iterdir()) <= 5000


def test_tar_links_are_not_followed(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    p = tmp_path / "t.tgz"
    with tarfile.open(p, "w:gz") as t:
        data = b"a,b\n1,2\n"
        info = tarfile.TarInfo("d/ok.csv")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("d/link.csv")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        t.addfile(link)
    kept, skipped = sources._unpack(p, out)
    assert [n for _, n in kept] == ["d/ok.csv"] and [q.name for q in out.iterdir()] == ["ok.csv"]


def test_archive_prefix_letters_never_repeat():
    labels = [sources._letters(k) for k in range(60)]
    assert labels[0] == "a" and labels[25] == "z" and labels[26] == "aa" and len(set(labels)) == 60


# ------------------------------------------------------------------ files and evidence

def test_multipage_tiff_says_only_page_one_was_screened(tmp_path):
    rng = np.random.default_rng(18)
    pages = [Image.fromarray(synth.histology(rng, 80, 90)) for _ in range(3)]
    p = tmp_path / "stack.tif"
    pages[0].save(p, save_all=True, append_images=pages[1:])
    src = sources.load(None, [(p, "stack.tif", "upload")])
    assert src.files[-1].status == "analysed (page 1 of 3)" and len(src.paper.figures) == 1


def test_photoshop_icc_profile_is_noted(tmp_path):
    p = tmp_path / "ps.png"
    Image.fromarray(synth.histology(np.random.default_rng(19), 60, 60)).save(
        p, icc_profile=b"\x00" * 40 + b"Adobe Photoshop profile")
    assert sources._exif(p).get("Profile") == "Photoshop"
    Image.fromarray(synth.histology(np.random.default_rng(19), 60, 60)).save(tmp_path / "plain.png")
    assert "Profile" not in sources._exif(tmp_path / "plain.png")


def test_evidence_for_thin_strips_stays_a_sane_size(tmp_path):
    strip = synth.blot(np.random.default_rng(20), lanes=40, h=30, lane_w=45)
    M = np.float32([[1, 0, 0], [0, 1, 0]])
    files = evidence.pair_evidence(tmp_path, "dup", strip, strip.copy(), "A", "B", [0, 0, 900, 30],
                                   [0, 0, 900, 30], M, False)
    pair = cv2.imread(str(tmp_path / files[0]))
    assert pair.shape[1] <= evidence.MAX_WIDTH and pair.shape[0] < pair.shape[1]
    with Image.open(tmp_path / "dup-blink.gif") as g:
        assert g.size[0] <= evidence.MAX_WIDTH
    a = synth.histology(np.random.default_rng(21))
    sbs = evidence.side_by_side(a, a, None, None, "A", "B")
    assert sbs.shape == (300, 612, 3)                     # ordinary panels still sit side by side at 300 px
    reg = cv2.imread(str(tmp_path / evidence.region_evidence(tmp_path, "r", strip, [[10, 5, 40, 20]], "A",
                                                             strip)[0]))
    assert reg.shape[1] <= evidence.MAX_WIDTH
