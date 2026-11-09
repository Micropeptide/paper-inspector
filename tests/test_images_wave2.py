"""Wave-2 image checks: flipped clones inside one image, a band row reused under two blots, copies shown as a
negative, one embedded picture placed twice in the PDF, and Bik categories.  Every check has a planted positive and
clean negatives; precision matters more than recall."""
from __future__ import annotations

import cv2
import numpy as np
import pymupdf
import pytest

from paperinspector import blotrows, clone, imgchecks, imgdup, panels, pdfdoc, synth
from paperinspector.models import Box, Finding, Location, Panel, Paper


def _panel(img: np.ndarray, i: int, fig: str = "F1", kind: str | None = None) -> Panel:
    return Panel(id=f"{fig}.{'abcdefghijkl'[i]}", figure_id=fig, box=Box(i * 1000, 0, img.shape[1], img.shape[0]),
                 kind=kind or panels.classify(img), image=img)


def _png(img: np.ndarray) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


# ------------------------------------------------------------------ mirrored clones

@pytest.mark.parametrize("kind", ["fluorescence", "histology", "phase", "photo"])
@pytest.mark.parametrize("mode", ["mirror-h", "mirror-v", "rot180"])
def test_flipped_patch_inside_one_image_is_found(kind, mode):
    rng = np.random.default_rng(11)
    img = synth.GENERATORS[kind](rng)
    out, (src, dst) = synth.mirror_clone_patch(img, rng, 0.25, mode)
    found = clone.find_mirrored(out)
    assert found, (kind, mode)
    c = found[0]
    assert c.mode == mode and c.ncc > 0.9
    boxes = [src, dst]
    for got in (c.src, c.dst):         # both boxes land on the planted pair, in either order
        assert any(abs(got[0] - b[0]) < 12 and abs(got[1] - b[1]) < 12 for b in boxes)


@pytest.mark.parametrize("flip", ["h", "v"])
def test_flipped_lane_inside_a_blot_is_found(flip):
    rng = np.random.default_rng(4)
    b = synth.mirror_band(synth.blot(rng, lanes=6), 42, 1, 4, flip)
    found = clone.find_mirrored(b)
    assert found and found[0].mode == ("mirror-h" if flip == "h" else "mirror-v")
    xs = sorted([found[0].src[0], found[0].dst[0]])
    assert abs(xs[0] - 42) < 6 and abs(xs[1] - 168) < 6


def test_clean_images_have_no_mirrored_clones():
    rng = np.random.default_rng(21)
    for kind in ("fluorescence", "histology", "phase", "photo", "blot"):
        for _ in range(4):
            img = synth.GENERATORS[kind](rng)
            assert clone.find_mirrored(img) == [], kind
            assert clone.find(img) == [], kind


def test_symmetric_shapes_without_shared_noise_are_not_clones():
    """A drawn symmetric object (mirror-symmetric bands, a ring) matches its own mirror image in shape, but its noise
    on each side is independent."""
    rng = np.random.default_rng(5)
    g = np.full((200, 300), 200, np.float32)
    for cx in (50, 150, 250):
        cv2.ellipse(g, (cx, 100), (30, 12), 0, 0, 360, 60, -1, cv2.LINE_AA)
    g = cv2.GaussianBlur(g, (0, 0), 2) + rng.normal(0, 3, g.shape)
    img = cv2.cvtColor(np.clip(g, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    assert clone.find_mirrored(img) == []


def test_find_still_returns_only_same_orientation_copies():
    rng = np.random.default_rng(8)
    img = synth.histology(rng)
    flipped, _ = synth.mirror_clone_patch(img, rng, 0.25, "mirror-h")
    assert clone.find(flipped) == []            # the pipeline's same-orientation text stays true
    assert clone.find_mirrored(flipped)         # served from the shared pass
    shifted, _ = synth.clone_patch(img, rng, 0.25)
    assert clone.find(shifted) and clone.find_mirrored(shifted) == []


def test_mirrored_clone_finding_reads_plainly(tmp_path):
    rng = np.random.default_rng(13)
    img, _ = synth.mirror_clone_patch(synth.histology(rng), rng, 0.25, "mirror-h")
    p = _panel(img, 2)
    out = imgchecks.mirrored_clones([p], tmp_path, [])
    assert len(out) == 1
    f = out[0]
    assert f.check == "image.clone" and f.kind == "integrity" and f.severity in ("critical", "high")
    assert "mirrored left to right" in f.title and f.data["relation"] == "mirrored-clone"
    assert f.data["bik"] == "III" and len(f.locations) == 2
    assert all((tmp_path / e).exists() for e in f.evidence) and any(e.endswith(".gif") for e in f.evidence)
    # a region another finding already explains (a same-orientation clone, a flat patch) is not reported again
    known = [Finding("image.clone", "high", "t", "d", 0.8, [Location(panel=p.id, box=l.box) for l in f.locations])]
    assert imgchecks.mirrored_clones([p], None, known) == []
    # charts and text are never searched
    assert imgchecks.mirrored_clones([_panel(img, 3, kind="chart")], None, []) == []


# ------------------------------------------------------------------ band rows (loading controls)

def _blots(rng, n=6):
    return [synth.blot(rng, lanes=int(rng.integers(4, 9))) for _ in range(n)]


@pytest.mark.parametrize("variant", ["plain", "mirrored", "negative", "rescaled"])
def test_reused_loading_control_strip_is_found(variant):
    hits = 0
    for seed in range(3):
        rng = np.random.default_rng(100 + seed)
        ims = _blots(rng)
        a, b = synth.reused_loading_control(rng)
        if variant == "mirrored":
            b = np.ascontiguousarray(np.vstack([b[:56], b[56:, ::-1]]))
        elif variant == "negative":
            b = synth.invert(b)
        elif variant == "rescaled":
            b = synth.rescale(b, 1.3)
        ims[0], ims[4] = a, b
        ms = blotrows.find([_panel(im, i) for i, im in enumerate(ims)])
        assert all({m.a.panel.id, m.b.panel.id} == {"F1.a", "F1.e"} for m in ms), variant
        if ms:
            m = ms[0]
            hits += 1
            assert m.ncc_fine >= blotrows.FINE_MIN and m.halves >= blotrows.HALF_MIN
            assert m.flip == (variant == "mirrored") and m.inverted == (variant == "negative")
            assert m.box_a[1] >= 50 and m.box_b[1] >= (50 if variant != "rescaled" else 65)   # the bottom strip
    assert hits >= (2 if variant == "rescaled" else 3), variant


def test_independent_blots_share_no_band_rows():
    for seed in range(6):
        rng = np.random.default_rng(300 + seed)
        ims = _blots(rng, 8) + [synth.stacked_blot([synth.blot(rng, h=56), synth.blot(rng, h=40)]) for _ in range(3)]
        assert blotrows.find([_panel(im, i) for i, im in enumerate(ims)]) == [], seed


def test_band_row_skips_what_is_already_reported_and_overlapping_panels():
    rng = np.random.default_rng(101)
    a, b = synth.reused_loading_control(rng)
    pa, pb = _panel(a, 0), _panel(b, 1)
    assert blotrows.find([pa, pb])
    assert blotrows.find([pa, pb], skip={frozenset(("F1.a", "F1.b"))}) == []
    # two panels cut from overlapping parts of one figure show the same rendered pixels
    pb_same = Panel("F1.b", "F1", Box(0, 0, a.shape[1], a.shape[0]), "blot", a.copy())
    pa_same = Panel("F1.a", "F1", Box(0, 0, a.shape[1], a.shape[0]), "blot", a)
    assert blotrows.find([pa_same, pb_same]) == []


def test_band_row_finding(tmp_path):
    rng = np.random.default_rng(102)
    a, b = synth.reused_loading_control(rng)
    ps = [_panel(a, 0), _panel(synth.blot(rng), 1), _panel(b, 2, fig="F2")]
    out = imgchecks.band_rows(ps, tmp_path, [])
    assert len(out) == 1
    f = out[0]
    assert f.check == "image.duplicate" and f.severity in ("high", "critical") and f.data["relation"] == "band-row"
    assert "F1.a and F2.c share the same row of bands" in f.title and "same membrane" in f.detail
    assert f.data["bik"] == "I" and {l.panel for l in f.locations} == {"F1.a", "F2.c"}
    assert any(e.endswith(".gif") for e in f.evidence)
    # an existing duplicate finding for the pair wins
    dup = Finding("image.duplicate", "high", "t", "d", 0.8, [Location(panel="F1.a"), Location(panel="F2.c")])
    assert imgchecks.band_rows(ps, None, [dup]) == []


def test_one_row_twice_inside_one_blot_is_a_clone():
    rng = np.random.default_rng(103)
    row = synth.blot(rng, h=40)
    img = synth.stacked_blot([row, synth.blot(rng, h=56), row])
    out = imgchecks.band_rows([_panel(img, 0)], None, [])
    assert len(out) == 1 and out[0].check == "image.clone" and "appears twice in the blot" in out[0].title


# ------------------------------------------------------------------ negatives

@pytest.mark.parametrize("kind", ["blot", "phase"])
def test_copy_shown_as_a_negative_is_found(kind):
    hits = 0
    for seed in range(4):
        rng = np.random.default_rng(seed)
        ims = [synth.GENERATORS[kind](rng) for _ in range(5)]
        ims[3] = synth.invert(ims[0] if seed % 2 else synth.flip_h(ims[0]))
        feats = [f for f in (imgdup.features(_panel(im, i)) for i, im in enumerate(ims)) if f]
        ms = imgdup.find_inverted(feats)
        assert all({m.a.panel.id, m.b.panel.id} == {"F1.a", "F1.d"} for m in ms)
        hits += bool(ms)
        if ms:
            assert ms[0].geo["inverted"] and ms[0].flip == (seed % 2 == 0)
    assert hits >= 3


def test_no_negatives_among_clean_grey_panels():
    for kind in ("blot", "phase"):
        for seed in range(6):
            rng = np.random.default_rng(50 + seed)
            ims = [synth.GENERATORS[kind](rng) for _ in range(4)] + [synth.invert(synth.GENERATORS[kind](rng))]
            feats = [f for f in (imgdup.features(_panel(im, i)) for i, im in enumerate(ims)) if f]
            assert imgdup.find_inverted(feats) == [], (kind, seed)


def test_colour_images_are_not_searched_for_negatives():
    rng = np.random.default_rng(3)
    ims = [synth.fluorescence(rng) for _ in range(3)]
    ims[2] = synth.invert(ims[0])
    feats = [f for f in (imgdup.features(_panel(im, i)) for i, im in enumerate(ims)) if f]
    assert imgdup.find_inverted(feats) == []


def test_negative_finding(tmp_path):
    rng = np.random.default_rng(1)
    ims = [synth.blot(rng, lanes=7) for _ in range(4)]
    ims[2] = synth.invert(ims[0])
    ps = [_panel(im, i) for i, im in enumerate(ims)]
    out = imgchecks.negatives(ps, tmp_path, [])
    assert len(out) == 1
    f = out[0]
    assert f.check == "image.duplicate" and f.data["inverted"] and f.data["bik"] == "III"
    assert "negative" in f.title and f.severity in ("high", "critical")
    assert all((tmp_path / e).exists() for e in f.evidence)
    dup = Finding("image.duplicate", "high", "t", "d", 0.8, [Location(panel="F1.a"), Location(panel="F1.c")])
    assert imgchecks.negatives(ps, None, [dup]) == []


# ------------------------------------------------------------------ one embedded picture placed twice

def _pdf(tmp_path, place, name="fig.pdf", pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=612, height=792)
    place(doc)
    for pg in doc:
        pg.insert_textbox(pymupdf.Rect(54, 700, 558, 760), "Figure 1. Panels of the experiment.", fontsize=9)
    p = tmp_path / name
    doc.save(p)
    doc.close()
    paper = pdfdoc.load(p)
    for f in paper.figures:
        panels.segment(f)
    return paper


def test_same_embedded_picture_placed_twice_is_found(tmp_path):
    rng = np.random.default_rng(2)
    a, b, c = synth.histology(rng), synth.histology(rng), synth.histology(rng)

    def place(doc):
        pg = doc[0]
        x = pg.insert_image(pymupdf.Rect(54, 80, 214, 240), stream=_png(a))
        pg.insert_image(pymupdf.Rect(230, 80, 390, 240), stream=_png(b))
        pg.insert_image(pymupdf.Rect(54, 300, 214, 460), stream=_png(c))
        pg.insert_image(pymupdf.Rect(230, 300, 390, 460), xref=x, rotate=90)      # the first picture again
    paper = _pdf(tmp_path, place)
    refs = paper.figures[0].image_refs
    assert len(refs) == 4 and len({r["digest"] for r in refs}) == 3
    assert sorted(r["rotation"] for r in refs) == [0, 0, 0, 270]
    out = imgchecks.embedded_reuse(paper, tmp_path, [])
    assert len(out) == 1
    f = out[0]
    assert f.check == "image.duplicate" and f.severity == "high" and f.data["relation"] == "same-embedded-image"
    assert "placed twice in the PDF" in f.title and "rotated" in f.title and f.data["bik"] == "II"
    assert len({l.panel for l in f.locations}) == 2
    # a pair the panel comparison already reported is strengthened instead of reported twice
    ids = [l.panel for l in f.locations]
    dup = Finding("image.duplicate", "high", "t", "Same field.", 0.6, [Location(panel=ids[0]), Location(panel=ids[1])])
    assert imgchecks.embedded_reuse(paper, None, [dup]) == []
    assert dup.data["same_embedded_image"] and dup.confidence >= 0.9 and "embedded picture" in dup.detail


def test_different_pictures_and_layout_images_are_not_embedded_reuse(tmp_path):
    rng = np.random.default_rng(6)
    ims = [synth.histology(rng) for _ in range(4)]

    def place(doc):
        for k, im in enumerate(ims):
            doc[0].insert_image(pymupdf.Rect(54 + 170 * (k % 2), 80 + 170 * (k // 2), 214 + 170 * (k % 2),
                                             240 + 170 * (k // 2)), stream=_png(im))
    assert imgchecks.embedded_reuse(_pdf(tmp_path, place), None, []) == []

    logo = synth.photo(rng, 120, 120)

    def logos(doc):                     # one picture on every page: a logo or banner, part of the layout
        for pg in doc:
            pg.insert_image(pymupdf.Rect(54, 80, 214, 240), stream=_png(logo))
    assert imgchecks.embedded_reuse(_pdf(tmp_path, logos, "logo.pdf", pages=4), None, []) == []

    chart = synth.chart(rng)

    def charts(doc):                    # a drawn chart placed twice is the chart check's business
        x = doc[0].insert_image(pymupdf.Rect(54, 80, 214, 210), stream=_png(chart))
        doc[0].insert_image(pymupdf.Rect(230, 80, 390, 210), xref=x)
    assert imgchecks.embedded_reuse(_pdf(tmp_path, charts, "chart.pdf"), None, []) == []


def test_image_boxes_are_unchanged_by_the_new_record(tmp_path):
    rng = np.random.default_rng(7)
    a = synth.histology(rng)

    def place(doc):
        doc[0].insert_image(pymupdf.Rect(54, 80, 214, 240), stream=_png(a))
    fig = _pdf(tmp_path, place).figures[0]
    assert len(fig.image_boxes) == 1 and len(fig.image_refs) == 1
    assert np.allclose(fig.image_boxes[0], fig.image_refs[0]["box"])
    assert fig.image_refs[0]["mirrored"] is False and fig.image_refs[0]["xref"] > 0


# ------------------------------------------------------------------ Bik categories

def _dup(**data) -> Finding:
    base = {"mirrored": False, "rotation": 0.0, "scale": 1.0, "sx": 1.0, "sy": 1.0, "overlap_a": 1.0, "overlap_b": 1.0}
    base.update(data)
    return Finding("image.duplicate", "high", "t", "Two panels match.", 0.8, data=base)


def test_bik_categories():
    assert imgchecks.bik_category(_dup()) == "I"
    assert imgchecks.bik_category(_dup(mirrored=True)) == "II"
    assert imgchecks.bik_category(_dup(rotation=180.0)) == "II"
    assert imgchecks.bik_category(_dup(rotation=-179.0)) == "II"
    assert imgchecks.bik_category(_dup(rotation=359.0)) == "I"
    assert imgchecks.bik_category(_dup(scale=1.4)) == "II"
    assert imgchecks.bik_category(_dup(sx=1.25, sy=1.0, scale=1.12)) == "II"
    assert imgchecks.bik_category(_dup(overlap_a=0.5, overlap_b=0.6)) == "II"
    assert imgchecks.bik_category(_dup(inverted=True)) == "III"
    assert imgchecks.bik_category(Finding("image.clone", "high", "t", "d")) == "III"
    assert imgchecks.bik_category(Finding("image.duplicate_chart", "medium", "t", "d")) == "I"
    merge = Finding("image.duplicate", "info", "t", "d", data={"relation": "channel-merge"})
    for f in (merge, Finding("image.splice", "medium", "t", "d"), Finding("image.flat", "medium", "t", "d"),
              Finding("data.duplicate_block", "high", "t", "d")):
        assert imgchecks.bik_category(f) is None


def test_label_bik_writes_data_and_one_sentence():
    fs = [_dup(mirrored=True), Finding("image.splice", "medium", "t", "Seam."), _dup()]
    imgchecks.label_bik(fs)
    imgchecks.label_bik(fs)            # idempotent
    assert fs[0].data["bik"] == "II" and fs[0].detail.count("Bik category II") == 1
    assert "bik" not in fs[1].data and fs[1].detail == "Seam."
    assert fs[2].data["bik"] == "I" and fs[2].public()["data"]["bik"] == "I"


# ------------------------------------------------------------------ the whole set on a rendered paper

def _paper(tmp_path, panels_, name, cols=2):
    spec = synth.PaperSpec("Synthetic study", "Abstract.", ["Methods."],
                           [synth.FigureSpec(1, "Synthetic panels.", panels_, cols=cols)])
    p = tmp_path / name
    synth.to_pdf_file(spec, p)
    paper = pdfdoc.load(p)
    for f in paper.figures:
        panels.segment(f)
    return paper


def test_check_on_a_rendered_paper_finds_the_plants_and_nothing_else(tmp_path):
    rng = np.random.default_rng(42)
    a, b = synth.reused_loading_control(rng)
    flipped, _ = synth.mirror_clone_patch(synth.histology(rng), rng, 0.25, "mirror-v")
    ims = [a, synth.blot(rng), flipped, synth.blot(rng), b, synth.histology(rng)]
    paper = _paper(tmp_path, ims, "plants.pdf")
    out = imgchecks.check(paper, tmp_path, [])
    got = {(f.check, f.data["relation"], frozenset(l.panel for l in f.locations)) for f in out}
    assert ("image.clone", "mirrored-clone", frozenset({"F1.c"})) in got
    assert ("image.duplicate", "band-row", frozenset({"F1.a", "F1.e"})) in got
    assert len(out) == 2, [f.title for f in out]


def test_check_on_clean_rendered_papers_is_silent(tmp_path):
    for seed in range(3):
        rng = np.random.default_rng(700 + seed)
        for kind in ("blot", "histology", "phase"):
            ims = [synth.GENERATORS[kind](rng) for _ in range(6)]
            paper = _paper(tmp_path, ims, f"clean-{kind}-{seed}.pdf", cols=2 if kind == "blot" else 3)
            assert imgchecks.check(paper, None, []) == [], (kind, seed)


def test_a_failing_step_does_not_lose_the_others(monkeypatch):
    rng = np.random.default_rng(9)
    img, _ = synth.mirror_clone_patch(synth.phase_contrast(rng), rng, 0.25, "rot180")
    paper = Paper(source="x.pdf")
    from paperinspector.models import Figure
    fig = Figure(id="F1", label="Figure 1", page=1, page_box=Box(0, 0, 1, 1), image=img)
    fig.panels = [_panel(img, 0)]
    paper.figures = [fig]

    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(imgchecks.blotrows, "find", boom)
    out = imgchecks.check(paper, None, [])
    assert [f.data["relation"] for f in out] == ["mirrored-clone"]
