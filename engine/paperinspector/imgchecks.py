"""Image checks that run after the panel-by-panel comparison, and the Bik category of every image finding.

  mirrored clones   a region copied inside one image and flipped or turned before it was pasted
  band rows         one strip of bands (a loading control, typically) shown under two different blots
  negatives         a panel that is another panel with dark and light swapped
  embedded reuse    one embedded picture of the PDF drawn twice, under two panel labels

Each reuses an existing check id (image.clone, image.duplicate) and says what it found in `data["relation"]`, so
scoring, the catalogue, letters and exports need no change.  Every check is conservative: a candidate must agree on
the camera-noise layer (or be the very same embedded file), not only on shapes.

Bik categories (Bik, Casadevall and Fang, mBio 2016) sort duplications by how much work they took:
  I    simple duplication - the same image twice, unchanged
  II   duplication with repositioning - shifted, cropped to overlap, rotated, mirrored or rescaled
  III  duplication with alteration - regions copied inside one image, or a copy that was edited
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import blotrows, clone, evidence, forensics, imgdup
from .imgutil import dog, gray
from .models import Finding, Location, Panel, Paper

log = logging.getLogger(__name__)

BIK = {
    "I": "Bik category I (simple duplication): the same image appears twice, unchanged.",
    "II": "Bik category II (repositioned): the same image appears shifted, cropped to overlap, rotated, mirrored "
          "or rescaled.",
    "III": "Bik category III (altered): parts of an image were copied inside it, or a copy was edited.",
}
IMAGE_PANEL_KINDS = {"blot", "micrograph", "photo", "other"}


# ------------------------------------------------------------------ entry point

def check(paper: Paper, ev: Optional[Path], findings: list[Finding],
          progress: Optional[Callable[[str, float], None]] = None) -> list[Finding]:
    """New image findings for `paper`.  `findings` are the image findings already made; pairs they report are not
    reported again (an embedded-file match strengthens them instead).  `ev` is the evidence folder, or None."""
    if progress is not None:
        progress("Looking for flipped copies, negatives and reused band rows", 0.68)
    panels = [p for f in paper.figures for p in f.panels if p.image is not None]
    out: list[Finding] = []
    steps: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("mirrored clones", lambda: mirrored_clones(panels, ev, findings)),
        ("embedded reuse", lambda: embedded_reuse(paper, ev, findings)),
        ("negatives", lambda: negatives(panels, ev, findings + out)),
        ("band rows", lambda: band_rows(panels, ev, findings + out)),
    ]
    for name, run in steps:
        try:
            out += run()
        except Exception as e:          # noqa: BLE001 - one extra check must not sink a finished scan
            log.warning("image check %s stopped: %s", name, type(e).__name__)
    return out


def reported_pairs(findings: list[Finding]) -> set[frozenset]:
    """Panel pairs an image.duplicate finding already names."""
    out = set()
    for f in findings:
        if f.check != "image.duplicate":
            continue
        ids = [l.panel for l in f.locations if l.panel]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if ids[i] != ids[j]:
                    out.add(frozenset((ids[i], ids[j])))
    return out


def _supplementary(*ps: Panel) -> bool:
    return any(p.figure_id.startswith("S") or "-" in p.figure_id for p in ps)


def _down(sev: str) -> str:
    return {"critical": "high", "high": "medium"}.get(sev, sev)


def _files(ev: Optional[Path], make: Callable[[], list[str]]) -> list[str]:
    if ev is None:
        return []
    try:
        return make()
    except Exception as e:              # noqa: BLE001 - a picture that cannot be drawn must not lose the finding
        log.warning("evidence image failed: %s", type(e).__name__)
        return []


# ------------------------------------------------------------------ mirrored clones

def _contained(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / max(1e-6, min(a[2] * a[3], b[2] * b[3]))


def _channel_subset(img: np.ndarray, a: list, b: list) -> bool:
    """One region shows a subset of the other's colour channels: a channel beside its merge in one picture."""
    if img is None or img.ndim != 3:
        return False

    def crop(r):
        x, y, w, h = (int(v) for v in r)
        c = img[max(0, y):y + h, max(0, x):x + w].reshape(-1, 3).astype(np.float32)
        return c[c.min(axis=1) < 200]
    ca, cb = crop(a), crop(b)
    if len(ca) < 50 or len(cb) < 50:
        return False
    sa, sb = ca.std(axis=0), cb.std(axis=0)
    return any(sb[i] > 10 and sa[i] < 0.25 * sb[i] or sa[i] > 10 and sb[i] < 0.25 * sa[i] for i in range(3))


def mirrored_clones(panels: list[Panel], ev: Optional[Path], findings: list[Finding]) -> list[Finding]:
    out = []
    for p in panels:
        if p.kind in ("chart", "text") or min(p.image.shape[:2]) < 40:
            continue
        cl = clone.find_mirrored(p.image, p.text_mask)
        if not cl:
            continue
        # regions a same-orientation clone, a flat patch or a channel layout already explains
        known = [l.box for f in findings if f.check in ("image.clone", "image.flat")
                 for l in f.locations if l.panel == p.id and l.box]
        if p.kind in ("blot", "micrograph", "photo") and min(p.image.shape[:2]) >= 100:
            known += [fl.box for fl in forensics.flat_patches(p.image, p.text_mask)]
        cl = [c for c in cl if not any(_contained(c.src, k) > 0.5 or _contained(c.dst, k) > 0.5 for k in known)
              and not _channel_subset(p.image, c.src, c.dst)]
        if not cl:
            continue
        best = max(cl, key=lambda c: (c.ncc, c.blocks))
        how = evidence.MODE_WORDS.get(best.mode, best.mode)
        sev = "critical" if best.ncc >= 0.93 and best.blocks >= 12 else "high"
        if _supplementary(p):
            sev = _down(sev)
        more = f" {len(cl) - 1} more flipped region(s) in the same panel." if len(cl) > 1 else ""
        files: list[str] = []
        for k, c in enumerate(cl[:3]):
            files += _files(ev, lambda c=c, k=k: evidence.mirror_clone_evidence(ev, f"mclone-{p.id}-{k}", p.image,
                                                                                c.src, c.dst, c.mode, p.id))
        out.append(Finding(
            "image.clone", sev, f"{p.id}: a region appears twice inside the image, {how}",
            f"A {best.src[2]:.0f}×{best.src[3]:.0f}-pixel region appears again {np.hypot(*best.shift):.0f} pixels "
            f"away, {how}. Once the flip is undone, the fine texture of the two regions matches at {best.ncc:.2f}. "
            "Bands and cells can look alike, but camera noise never repeats, and never as a mirror image; a patch "
            f"that was copied and flipped carries its noise with it.{more}",
            float(np.clip(0.5 + (best.ncc - 0.8) * 2.5, 0.4, 0.97)),
            [loc for c in cl[:3] for loc in (Location(figure=p.figure_id, panel=p.id, box=[round(v, 1) for v in c.src]),
                                             Location(figure=p.figure_id, panel=p.id, box=[round(v, 1) for v in c.dst]))],
            evidence=files,
            data={"relation": "mirrored-clone", "mode": best.mode, "regions": len(cl),
                  "shift": [round(v, 1) for v in best.shift], "blocks": best.blocks, "ncc": round(best.ncc, 3),
                  "bik": "III"}))
    return out


# ------------------------------------------------------------------ band rows (loading controls)

def band_rows(panels: list[Panel], ev: Optional[Path], findings: list[Finding]) -> list[Finding]:
    out = []
    skip = reported_pairs(findings)
    for m in blotrows.find(panels, skip):
        pa, pb = m.a.panel, m.b.panel
        how = []
        if m.flip:
            how.append("mirrored")
        if m.inverted:
            how.append("shown as a negative")
        if abs(m.scale - 1) > 0.06:
            how.append(f"rescaled ×{max(m.scale, 1 / m.scale):.2f}")
        suffix = f" ({', '.join(how)})" if how else ""
        w, h = m.box_a[2], m.box_a[3]
        same = pa is pb
        transformed = bool(how)
        sev = "critical" if transformed and m.ncc_fine >= 0.8 else "high"
        if _supplementary(pa, pb):
            sev = _down(sev)
        conf = float(np.clip(0.45 + 0.6 * (m.ncc_fine - 0.4), 0.4, 0.95))
        detail = (f"A {w:.0f}×{h:.0f}-pixel strip of bands in {pa.id} appears again in {pb.id}"
                  f"{', ' + ', '.join(how) if how else ''}. After alignment the band intensities agree at "
                  f"{m.ncc_int:.2f} (rank correlation, so brightness and contrast edits do not matter) and the fine "
                  f"background noise at {m.ncc_fine:.2f}, in both halves of the strip. Strips from two different "
                  "blots share band shapes but never their noise.")
        if same:
            title = f"{pa.id}: one row of bands appears twice in the blot{suffix}"
            detail += " Two rows of one blot should come from two different probings or exposures."
            check, bik = "image.clone", "III"
        else:
            title = f"{pa.id} and {pb.id} share the same row of bands{suffix}"
            detail += (" If this is a loading control (actin, tubulin, GAPDH), showing it under two blots is "
                       "acceptable only when both were probed on the same membrane and the legend says so.")
            check, bik = "image.duplicate", "III" if m.inverted else ("II" if transformed else "I")
        files = _files(ev, lambda: evidence.strip_evidence(ev, f"rows-{pa.id}-{pb.id}", pa.image, pb.image, m.box_a,
                                                           m.box_b, pa.id, pb.id, m.flip, m.inverted))
        out.append(Finding(check, sev, title, detail, conf,
                           [Location(figure=pa.figure_id, panel=pa.id, box=m.box_a),
                            Location(figure=pb.figure_id, panel=pb.id, box=m.box_b)],
                           evidence=files,
                           data={"relation": "band-row", "ncc_texture": round(m.ncc_fine, 3),
                                 "ncc_intensity": round(m.ncc_int, 3), "halves": round(m.halves, 3),
                                 "mirrored": m.flip, "inverted": m.inverted, "scale": round(m.scale, 3),
                                 "bik": bik}))
    return out


# ------------------------------------------------------------------ negatives

def negatives(panels: list[Panel], ev: Optional[Path], findings: list[Finding]) -> list[Finding]:
    grey = [p for p in panels if p.kind in IMAGE_PANEL_KINDS and imgdup.is_grey(p.image)]
    if len(grey) < 2:
        return []
    feats = [x for x in (imgdup.features(p) for p in grey) if x is not None]
    skip = {frozenset(f"0/{i}" for i in pair) for pair in reported_pairs(findings)}
    out = []
    for m in imgdup.find_inverted(feats, skip):
        pa, pb = m.a.panel, m.b.panel
        rot = (m.geo["rotation"] + 180) % 360 - 180
        zoom = max(m.geo["scale"], 1 / m.geo["scale"])
        how = ["shown as a negative"]
        if m.flip:
            how.append("mirrored")
        if abs(rot) > 3:
            how.append(f"rotated {abs(rot):.0f}°")
        if zoom > 1.12:
            how.append(f"rescaled ×{zoom:.2f}")
        sev = "critical" if m.ncc_dog >= 0.6 else "high"
        if _supplementary(pa, pb):
            sev = _down(sev)
        conf = float(np.clip(0.45 + 0.6 * (m.ncc_dog - 0.4), 0.3, 0.95) * min(1.0, 0.6 + m.inliers / 60))
        files = _files(ev, lambda: evidence.negative_pair_evidence(ev, f"neg-{pa.id}-{pb.id}", pa.image, pb.image,
                                                                   pa.id, pb.id, m.box_a, m.box_b, m.M, m.flip))
        out.append(Finding(
            "image.duplicate", sev, f"{pb.id} appears to be {pa.id} {', '.join(how)}",
            f"With dark and light swapped, {pb.id} lines up with {pa.id}: {m.inliers} matching keypoints agree on one "
            f"geometric transform and the fine texture then correlates at {m.ncc_dog:.2f}; unrelated images score "
            "near 0. A negative of a blot, gel or micrograph is still the same picture, so the two panels cannot be "
            "two different samples unless the legend says they are one.",
            conf,
            [Location(figure=pa.figure_id, panel=pa.id, box=m.box_a),
             Location(figure=pb.figure_id, panel=pb.id, box=m.box_b)],
            evidence=files,
            data={"relation": "negative", "inverted": True, "inliers": m.inliers,
                  "ncc_texture": round(m.ncc_dog, 3), "ncc_intensity": round(m.ncc_int, 3),
                  "overlap_a": round(m.frac_a, 3), "overlap_b": round(m.frac_b, 3), "mirrored": m.flip,
                  "rotation": round(rot, 1), "scale": round(m.geo["scale"], 3), "bik": "III"}))
    return out


# ------------------------------------------------------------------ one embedded picture placed twice

def _inside(inner, outer) -> float:
    ix = max(0.0, min(inner[0] + inner[2], outer[0] + outer[2]) - max(inner[0], outer[0]))
    iy = max(0.0, min(inner[1] + inner[3], outer[1] + outer[3]) - max(inner[1], outer[1]))
    return ix * iy / max(1e-6, inner[2] * inner[3])


def _placement_panel(fig, ref: dict) -> Optional[Panel]:
    """The one panel a placement shows, when the placement is exactly one panel (not a composite of several)."""
    box = ref["box"]
    hits = [p for p in fig.panels if _inside(p.box.as_list(), box) >= 0.8]
    if len(hits) != 1:
        return None
    p = hits[0]
    if p.box.w * p.box.h < 0.5 * box[2] * box[3]:
        return None
    return p


def _noisy(p: Panel) -> bool:
    """A camera picture (it has noise), not a rendered icon or drawing."""
    return float(np.median(np.abs(dog(gray(p.image), 0.7, 2.0)))) >= 0.35


def embedded_reuse(paper: Paper, ev: Optional[Path], findings: list[Finding]) -> list[Finding]:
    groups: dict[str, list[tuple]] = {}
    for fig in paper.figures:
        for ref in getattr(fig, "image_refs", None) or []:
            if not ref.get("digest") or min(ref.get("width", 0), ref.get("height", 0)) < 48:
                continue
            groups.setdefault(ref["digest"], []).append((fig, ref))
    existing = {}
    for f in findings:
        if f.check == "image.duplicate":
            for pair in reported_pairs([f]):
                existing.setdefault(pair, f)
    out = []
    for digest, places in groups.items():
        if len(places) < 2 or len(places) > 3 or len({fig.page for fig, _ in places}) > 2:
            continue                    # a logo, an icon or a key drawn on many pages is layout, not data
        for i in range(len(places)):
            for j in range(i + 1, len(places)):
                (fa, ra), (fb, rb) = places[i], places[j]
                pa, pb = _placement_panel(fa, ra), _placement_panel(fb, rb)
                if pa is None or pb is None or pa is pb:
                    continue
                if pa.kind not in ("blot", "micrograph", "photo") or pb.kind not in ("blot", "micrograph", "photo"):
                    continue
                pair = frozenset((pa.id, pb.id))
                if pair in existing:
                    f = existing[pair]
                    if not f.data.get("same_embedded_image"):
                        f.data["same_embedded_image"] = True
                        if f.severity != "info":
                            f.detail += (" The PDF also stores both panels as one embedded picture drawn twice, "
                                         "which confirms the reuse.")
                            f.confidence = max(f.confidence, 0.9)
                    continue
                if not (_noisy(pa) and _noisy(pb)):
                    continue
                sa, sb = ra.get("scale", 0) or 0, rb.get("scale", 0) or 0
                if sa <= 0 or sb <= 0 or max(sa, sb) / min(sa, sb) > 1.2:
                    continue                # one is an enlarged view (an inset): the panel comparison judges those
                thumb = any(fig.id.startswith("P") and p.box.w * p.box.h >= 0.8 * fig.image.shape[0] * fig.image.shape[1]
                            for fig, p in ((fa, pa), (fb, pb)))
                if thumb:
                    continue                # a graphical abstract or thumbnail repeating a whole figure
                vis_a = ra["box"][2] * ra["box"][3] / max(1e-6, ra["full"][2] * ra["full"][3])
                vis_b = rb["box"][2] * rb["box"][3] / max(1e-6, rb["full"][2] * rb["full"][3])
                cropped = abs(vis_a - vis_b) > 0.15 or abs(pa.box.w / pa.box.h - pb.box.w / pb.box.h) > \
                    0.1 * max(pa.box.w / pa.box.h, pb.box.w / pb.box.h)
                how = []
                if ra.get("mirrored") != rb.get("mirrored"):
                    how.append("mirrored")
                if ra.get("rotation", 0) != rb.get("rotation", 0):
                    how.append(f"rotated {(rb.get('rotation', 0) - ra.get('rotation', 0)) % 360}°")
                if cropped:
                    how.append("cropped differently")
                sev = "medium" if _supplementary(pa, pb) else "high"
                one = "one embedded picture" + (f" (PDF object {ra['xref']})" if ra.get("xref") == rb.get("xref")
                                                and ra.get("xref") else " (two copies with identical data)")
                files = _files(ev, lambda: [evidence.write(ev / f"embed-{pa.id}-{pb.id}.png",
                                                           evidence.side_by_side(pa.image, pb.image, None, None,
                                                                                 pa.id, pb.id))])
                bik = "II" if how else "I"
                out.append(Finding(
                    "image.duplicate", sev,
                    f"{pa.id} and {pb.id} are the same picture placed twice in the PDF"
                    + (f" ({', '.join(how)})" if how else ""),
                    f"The PDF holds {one} that the page layout draws twice: once as {pa.id} and once as {pb.id}"
                    + (f", {', '.join(how)}" if how else "") + ". That is the same image file, not two similar "
                    "pictures. It is fine only if the legend says both panels show the same picture.",
                    0.9,
                    [Location(figure=pa.figure_id, panel=pa.id, page=fa.page, box=[0.0, 0.0, pa.box.w, pa.box.h]),
                     Location(figure=pb.figure_id, panel=pb.id, page=fb.page, box=[0.0, 0.0, pb.box.w, pb.box.h])],
                    evidence=files,
                    data={"relation": "same-embedded-image", "same_embedded_image": True, "digest": digest[:16],
                          "xref": [ra.get("xref"), rb.get("xref")], "mirrored": ra.get("mirrored") != rb.get("mirrored"),
                          "cropped": cropped, "bik": bik}))
                existing[pair] = out[-1]
    return out


# ------------------------------------------------------------------ Bik categories

def bik_category(f: Finding) -> Optional[str]:
    """I, II or III for a duplication finding; None for findings that are not duplications."""
    d = f.data or {}
    if d.get("bik") in BIK:
        return d["bik"]
    if f.check == "image.duplicate_chart":
        return "I"
    if f.check == "image.clone":
        return "III"
    if f.check != "image.duplicate" or f.severity == "info" or d.get("relation") == "channel-merge":
        return None
    if d.get("inverted"):
        return "III"
    rot = abs(((d.get("rotation") or 0) + 180) % 360 - 180)
    scale = d.get("scale") or 1.0
    sx, sy = d.get("sx") or 1.0, d.get("sy") or 1.0
    transformed = bool(d.get("mirrored")) or rot > 3 or max(scale, 1 / max(scale, 1e-6)) > 1.12 or \
        max(sx, sy) / max(min(sx, sy), 1e-6) > 1.08
    oa, ob = d.get("overlap_a"), d.get("overlap_b")
    partial = oa is not None and ob is not None and min(oa, ob) < 0.8
    return "II" if transformed or partial else "I"


def label_bik(findings: list) -> list:
    """Add the Bik category to every duplication finding (in place): `data["bik"]` and one sentence of detail."""
    for f in findings:
        if not isinstance(f, Finding) or not f.check.startswith("image."):
            continue
        cat = bik_category(f)
        if cat is None:
            continue
        f.data["bik"] = cat
        if "Bik category" not in f.detail:
            f.detail = f.detail.rstrip() + " " + BIK[cat]
    return findings
