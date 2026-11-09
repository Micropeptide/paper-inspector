"""One scan, start to finish: read the paper, run every check, write the report and its evidence to a run folder.

A run folder holds everything the apps need and nothing else:
  report.json           paper, figures, panels, findings, score
  figures/<id>.png      each figure as analysed
  evidence/*.png|gif    annotated side-by-sides, blink animations, difference images
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from . import (__version__, clone, datacheck, evidence, fetch, forensics, imgchecks, imgdup, panels, pdfdoc, refcheck,
               refs, history, reporting, sources, stats, textcheck)
from .models import CATEGORIES, Cancelled, Finding, Location, Paper, SEVERITIES, risk_score, verdict

HOME = Path(os.environ.get("PAPERINSPECTOR_HOME", Path.home() / ".paperinspector"))
RUNS = HOME / "runs"

Progress = Callable[[str, float], None]


def _noop(stage: str, frac: float) -> None:
    pass


def _rid(tag: str) -> str:
    """Time first (runs sort by it), then what was scanned, then a random part: two scans of the same paper in the
    same second are still two runs."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}-{uuid.uuid4().hex[:6]}"


def run_id_for(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return _rid(h.hexdigest()[:8])


def write_report(run: Path, report: dict) -> None:
    """Write report.json in one step, so a reader never sees half a file while a scan or a review saves it."""
    tmp = run / f".report.{uuid.uuid4().hex[:6]}.tmp"
    try:
        tmp.write_text(json.dumps(report, indent=1, default=_json))
        os.replace(tmp, run / "report.json")
    finally:
        tmp.unlink(missing_ok=True)


def _match_finding(m: imgdup.Match, cross_paper: bool, names: list[str]) -> Finding:
    pa, pb = m.a.panel, m.b.panel
    geo = m.geo
    rot = (geo["rotation"] + 180) % 360 - 180
    rotated = abs(rot) > 3
    zoom = max(geo["scale"], 1 / geo["scale"])
    stretched = geo["aniso"] > 1.08
    full = max(m.frac_a, m.frac_b) >= 0.8
    same_fig = not cross_paper and pa.figure_id == pb.figure_id

    how = []
    if m.flip:
        how.append("mirrored")
    if rotated:
        how.append(f"rotated {abs(rot):.0f}°")
    if stretched:
        how.append(f"stretched (×{geo['sx']:.2f} by ×{geo['sy']:.2f})")
    elif zoom > 1.12:
        how.append(f"rescaled ×{zoom:.2f}")
    transformed = m.flip or rotated or stretched

    a_name = (f"{names[m.a.paper]}: " if cross_paper else "") + pa.id
    b_name = (f"{names[m.b.paper]}: " if cross_paper else "") + pb.id
    merge = _channel_relation(m)
    if not merge and not cross_paper and not transformed and _channel_agreement(m) < 0.6:
        merge = (pa.id, pb.id)
    if merge and not cross_paper:
        locs = [Location(figure=pa.figure_id, panel=pa.id, box=m.box_a),
                Location(figure=pb.figure_id, panel=pb.id, box=m.box_b)]
        return Finding("image.duplicate", "info", f"{merge[0]} and {merge[1]} show the same field in different channels or views",
                       "The two panels share the same field, but their colour channels do not move together: one is a "
                       "single channel, a different stain, brightfield vs fluorescence, or an overlay. That is the normal "
                       "way to present channels; it only matters if the legend says they are different fields.", 0.5, locs,
                       data={"ncc_texture": round(m.ncc_dog, 3), "relation": "channel-merge"})
    contained = min(m.frac_a, m.frac_b) < 0.6 and max(m.frac_a, m.frac_b) >= 0.8
    if zoom > 1.25 and not m.flip and abs(rot) <= 3 and (full or contained) and not cross_paper:
        title = f"{b_name} may be a magnified view of {a_name}" if geo["scale"] > 1 else \
            f"{a_name} may be a magnified view of {b_name}"
        sev, conf = "medium", 0.35
        detail = (f"One panel shows part of the other enlarged ×{zoom:.2f} (texture match {m.ncc_dog:.2f}). That is "
                  "normal for an inset or zoom panel the legend describes; it is a problem if the two are "
                  "presented as different samples or conditions. Check the legend.")
    else:
        if full:
            what = "the same image"
        else:
            what = "overlapping fields of view"
        title = f"{a_name} and {b_name} appear to show {what}" + (f" ({', '.join(how)})" if how else "")
        sev = "critical" if transformed and m.ncc_dog >= 0.6 else "high"
        if cross_paper:
            sev = "critical" if m.ncc_dog >= 0.6 else "high"
        if m.ncc_dog < 0.55 and sev == "critical":
            sev = "high"
        elif m.ncc_dog < 0.5:
            sev = "medium"
        supplementary = any("-" in p_.id.split(".")[0] or p_.id.startswith("S") for p_ in (pa, pb))
        crop_of = contained and abs(geo["scale"] - 1) < 0.1 and not transformed
        if not transformed and not cross_paper and (supplementary or crop_of):
            # supplements routinely re-show main images (uncropped, enlarged); later figures re-show regions of an
            # overview. Real reuse looks the same, so it stays a finding - one step lower
            sev = {"critical": "high", "high": "medium"}.get(sev, sev)
        if crop_of and not cross_paper:
            small, big = (a_name, b_name) if m.frac_a > m.frac_b else (b_name, a_name)
            title = f"{small} is a crop of {big}"
        conf = float(np.clip(0.45 + 0.6 * (m.ncc_dog - 0.4), 0.3, 0.98) * min(1.0, 0.6 + m.inliers / 60))
        parts = [f"{m.inliers} matching keypoints agree on one geometric transform, and after alignment the fine "
                 f"texture (camera noise, speckle) correlates at {m.ncc_dog:.2f}; unrelated images of the same "
                 f"tissue score near 0."]
        if not full:
            parts.append(f"The shared area is {m.frac_a:.0%} of {a_name} and {m.frac_b:.0%} of {b_name}.")
        if transformed:
            parts.append("The copy was " + " and ".join(how) + "; a reused image that has been transformed is harder "
                         "to explain as an honest mix-up.")
        if pa.kind == "blot" and pb.kind == "blot" and full and not transformed:
            parts.append("Re-using one loading-control blot is acceptable only if both panels come from the same "
                         "membrane and the legend says so.")
        detail = " ".join(parts)
    locs = [Location(figure=pa.figure_id, panel=pa.id, box=m.box_a), Location(figure=pb.figure_id, panel=pb.id,
                                                                              box=m.box_b)]
    return Finding("image.duplicate", sev, title, detail, conf, locs,
                   data={"inliers": m.inliers, "ncc_texture": round(m.ncc_dog, 3), "ncc_intensity": round(m.ncc_int, 3),
                         "overlap_a": round(m.frac_a, 3), "overlap_b": round(m.frac_b, 3), "mirrored": m.flip,
                         "rotation": round(rot, 1), "scale": round(geo["scale"], 3), "sx": round(geo["sx"], 3),
                         "sy": round(geo["sy"], 3), "cross_paper": cross_paper,
                         "papers": [m.a.paper, m.b.paper] if cross_paper else None})


def _channel_agreement(m: imgdup.Match) -> float:
    """Lowest per-channel correlation of the two aligned copies, over channels that carry signal in both.  A copy
    keeps every channel in step (even after brightness edits); the same field shown as brightfield vs fluorescence,
    one stain vs another, or a channel vs an overlay does not."""
    a, b = m.a.panel.image, m.b.panel.image
    if a is None or b is None or a.ndim != 3 or b.ndim != 3:
        return 1.0
    ref, warped, valid = evidence.aligned(a, b, m.M, m.flip)
    clean = valid & ~((ref.min(axis=2) > 200) | (warped.min(axis=2) > 200))
    if clean.sum() < 400:
        return 1.0
    worst = 1.0
    for c in range(3):
        x, y = ref[:, :, c][clean].astype(np.float32), warped[:, :, c][clean].astype(np.float32)
        if x.std() < 6 or y.std() < 6:
            continue
        worst = min(worst, float(np.corrcoef(x, y)[0, 1]))
    gx = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)[clean].astype(np.float32)
    gy = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)[clean].astype(np.float32)
    if gx.std() > 3 and gy.std() > 3:
        worst = min(worst, float(np.corrcoef(gx, gy)[0, 1]))
    return worst


def _channel_relation(m: imgdup.Match):
    """(single-channel panel, merged panel) when one aligned copy carries only a subset of the other's colour
    channels - a fluorescence channel shown beside the merge - else None."""
    a, b = m.a.panel.image, m.b.panel.image
    if a is None or b is None or a.ndim != 3 or b.ndim != 3:
        return None
    ref, warped, valid = evidence.aligned(a, b, m.M, m.flip)
    if valid.sum() < 400:
        return None
    # ignore printed labels (white or near-white in every channel) - they are not part of either image
    clean = valid & ~((ref.min(axis=2) > 200) | (warped.min(axis=2) > 200))
    clean = cv2.erode(clean.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    if clean.sum() < 400:
        return None
    sa = ref[clean].astype(np.float32).std(axis=0)         # how much each colour channel varies
    sb = warped[clean].astype(np.float32).std(axis=0)
    # a channel that carries signal in one image and is empty in the other: a single channel beside its merge
    only_b = [i for i in range(3) if sb[i] > 10 and sa[i] < 0.25 * sb[i]]
    only_a = [i for i in range(3) if sa[i] > 10 and sb[i] < 0.25 * sa[i]]
    if bool(only_a) == bool(only_b):
        return None
    a_is_ref = m.geo.get("scale", 1.0) >= 1.0
    ref_id, mov_id = (m.a.panel.id, m.b.panel.id) if a_is_ref else (m.b.panel.id, m.a.panel.id)
    return (ref_id, mov_id) if only_b else (mov_id, ref_id)


def _channel_subset(img: np.ndarray, a: list, b: list) -> bool:
    """Two regions of one composite image where one shows a subset of the other's colour channels: a channel next
    to its merge, laid out inside a single picture."""
    if img is None or img.ndim != 3:
        return False
    def crop(r):
        x, y, w, h = (int(v) for v in r)
        c = img[y:y + h, x:x + w].reshape(-1, 3).astype(np.float32)
        return c[c.min(axis=1) < 200]
    ca, cb = crop(a), crop(b)
    if len(ca) < 50 or len(cb) < 50:
        return False
    sa, sb = ca.std(axis=0), cb.std(axis=0)
    return any(sb[i] > 10 and sa[i] < 0.25 * sb[i] or sa[i] > 10 and sb[i] < 0.25 * sa[i] for i in range(3))


def _image_findings(paper: Paper, ev: Path, progress: Progress) -> list[Finding]:
    out: list[Finding] = []
    all_panels = [p for f in paper.figures for p in f.panels]
    progress("Reading text printed inside images", 0.2)
    _add_ocr_masks([p for p in all_panels if p.image is not None and p.kind not in ("chart", "text")])
    for p in all_panels:
        if p.text_mask is not None and p.kind not in ("chart", "text"):
            share = p.text_mask.mean()
            if share > 0.35:
                p.kind = "text"           # a label, a legend, a word: not a picture
            elif share > 0.08 and panels.stats(p.image)["white"] > 0.3:
                p.kind = "chart"          # axis numbers and labels on a white page: a plot
    feats = []
    for i, p in enumerate(all_panels):
        x = imgdup.features(p)
        if x is not None:
            feats.append(x)
        if i % 10 == 0:
            progress("Reading image features", 0.2 + 0.25 * i / max(1, len(all_panels)))
    progress("Comparing every panel with every other", 0.45)
    for m in imgdup.find(feats):
        f = _match_finding(m, False, [paper.source])
        stem = f"dup-{m.a.panel.id}-{m.b.panel.id}"
        f.evidence = evidence.pair_evidence(ev, stem, m.a.panel.image, m.b.panel.image, m.a.panel.id, m.b.panel.id,
                                            m.box_a, m.box_b, m.M, m.flip)
        out.append(f)
    for (_, pa), (_, pb), d in imgdup.exact_charts([(0, p) for p in all_panels]):
        out.append(Finding("image.duplicate_chart", "medium", f"{pa.id} and {pb.id} are the same chart",
                           "Two plots are pixel-for-pixel identical. Fine if they show the same data on purpose; a "
                           "problem if they are labelled as different experiments.", 0.7,
                           [Location(figure=pa.figure_id, panel=pa.id), Location(figure=pb.figure_id, panel=pb.id)],
                           evidence=[evidence.write(ev / f"chart-{pa.id}-{pb.id}.png",
                                                    evidence.side_by_side(pa.image, pb.image, None, None, pa.id,
                                                                          pb.id))]))

    progress("Looking inside each image", 0.6)
    for i, p in enumerate(all_panels):
        if p.image is None or p.kind in ("chart", "text"):
            continue
        flats = forensics.flat_patches(p.image, p.text_mask) \
            if p.kind in ("blot", "micrograph", "photo") and min(p.image.shape[:2]) >= 100 else []
        for k, fl in enumerate(flats[:3]):
            sev = "medium" if fl.area > 0.01 * p.image.shape[0] * p.image.shape[1] else "low"
            out.append(Finding("image.flat", sev, f"{p.id}: a patch with no camera noise",
                               "Every real photograph or scan carries noise. This patch is perfectly flat while the "
                               f"pixels around it are noisy (noise level {fl.ring_noise:.1f}), which is what a brush "
                               "stroke or a pasted block of solid colour leaves behind.", 0.6,
                               [Location(figure=p.figure_id, panel=p.id, box=fl.box)],
                               evidence=evidence.region_evidence(ev, f"flat-{p.id}-{k}", p.image, [fl.box], p.id,
                                                                 forensics.view(p.image, "reveal")),
                               data={"value": fl.value, "area": fl.area}))
        clones = [c for c in clone.find(p.image, p.text_mask)
                  if not any(_overlaps(c.src, fl.box) or _overlaps(c.dst, fl.box) for fl in flats)
                  and not _channel_subset(p.image, c.src, c.dst)]
        if clones:
            best = max(clones, key=lambda c: (c.ncc, c.blocks))
            sev = "critical" if best.ncc >= 0.93 and best.blocks >= 12 else "high"
            if p.figure_id.startswith("S"):
                sev = {"critical": "high", "high": "medium"}[sev]   # supplementary pages are often montages
            more = f" {len(clones) - 1} more repeated region(s) in the same panel." if len(clones) > 1 else ""
            files = []
            for k, c in enumerate(clones[:3]):
                files += evidence.clone_evidence(ev, f"clone-{p.id}-{k}", p.image, c.src, c.dst, p.id)
            out.append(Finding("image.clone", sev, f"{p.id}: a region appears twice inside the image",
                               f"A {best.src[2]:.0f}×{best.src[3]:.0f}-pixel region repeats {np.hypot(*best.shift):.0f} "
                               f"pixels away with the same orientation; its fine texture matches at {best.ncc:.2f}. "
                               "Tissue repeats its shapes but a camera never repeats its noise; a clone tool copies "
                               f"both.{more}",
                               float(np.clip(0.5 + (best.ncc - 0.8) * 2.5, 0.4, 0.97)),
                               [loc for c in clones[:3] for loc in (Location(figure=p.figure_id, panel=p.id, box=c.src),
                                                                    Location(figure=p.figure_id, panel=p.id, box=c.dst))],
                               evidence=files,
                               data={"regions": len(clones), "shift": [round(v, 1) for v in best.shift],
                                     "blocks": best.blocks, "ncc": round(best.ncc, 3)}))
        if p.kind == "blot":
            sms = forensics.seams(p.image, p.text_mask)
            marked = {round(s.pos) for s in sms for t in sms if s is not t and abs(s.pos - t.pos) <= 5
                      and np.sign(s.step) != np.sign(t.step)}
            for s in sms:
                if round(s.pos) in marked:
                    continue        # a drawn dividing line: a splice that is declared, as journals require
                h, w = p.image.shape[:2]
                box = [s.pos - 2, 0, 4, h] if s.axis == "x" else [0, s.pos - 2, w, 4]
                out.append(Finding("image.splice", "medium" if s.z >= 8 and s.consistency >= 0.75 else "low",
                                   f"{p.id}: possible splice (a straight seam in the background)",
                                   f"The background jumps by {abs(s.step):.0f} grey levels along a straight line that "
                                   f"runs through {s.consistency:.0%} of the blot. Lanes from different gels or "
                                   "exposures joined together must be separated by a visible line and explained.",
                                   float(np.clip(0.4 + s.consistency / 3, 0.3, 0.8)),
                                   [Location(figure=p.figure_id, panel=p.id, box=box)],
                                   evidence=evidence.region_evidence(ev, f"seam-{p.id}-{round(s.pos)}", p.image, [],
                                                                     p.id, forensics.view(p.image, "reveal"),
                                                                     [(s.axis, s.pos)]),
                                   data={"axis": s.axis, "position": round(s.pos, 1), "step": round(s.step, 1)}))
    return out


def _add_ocr_masks(panels_: list) -> None:
    """Mask text printed inside the pixels (OCR), on top of the PDF's own text layer."""
    from concurrent.futures import ThreadPoolExecutor

    from .imgutil import ocr_mask

    def one(p):
        m = ocr_mask(p.image)
        if m is not None:
            p.text_mask = m if p.text_mask is None else (p.text_mask | m)
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, panels_))


def _overlaps(a, b) -> bool:
    return not (a[0] + a[2] < b[0] or b[0] + b[2] < a[0] or a[1] + a[3] < b[1] or b[1] + b[3] < a[1])


class _RunGuard:
    """Remove a run folder this scan created if the scan does not finish (cancelled, or an error), so a half-written
    run never lingers next to finished ones.  A folder the caller handed in (run_dir) is left alone."""

    def __init__(self, run: Path, owned: bool = True):
        self.run, self.owned = run, owned

    def __enter__(self) -> Path:
        return self.run

    def __exit__(self, et, e, tb) -> bool:
        if et is not None and self.owned and not (self.run / "report.json").exists():
            shutil.rmtree(self.run, ignore_errors=True)
        return False


def _new_run(run: Path, reuse: bool = False) -> Path:
    """Create a run folder.  An existing run is never deleted: it may belong to a scan still in progress."""
    run.mkdir(parents=True, exist_ok=reuse)
    (run / "figures").mkdir(exist_ok=reuse)
    (run / "evidence").mkdir(exist_ok=reuse)
    return run


def scan(path: str | Path, progress: Optional[Progress] = None, online: bool = False,
         run_dir: Optional[Path] = None, supplements: Optional[list] = None) -> dict:
    """Screen an uploaded paper (PDF or a single figure image), with any supplementary files the user adds."""
    progress = progress or _noop
    path = Path(path)
    t0 = time.time()
    rid = run_id_for(path)
    run = _new_run(run_dir, reuse=True) if run_dir else _new_run(RUNS / rid)
    with _RunGuard(run, owned=run_dir is None):
        shutil.copy(path, run / ("source" + path.suffix.lower()))
        progress("Reading the paper and its supplementary files", 0.03)
        src = sources.load(path, [(Path(x), Path(x).name, "upload") for x in supplements or []])
        return _analyse(run, rid, src, path if path.suffix.lower() == ".pdf" else None, {}, online, progress, t0, [])


def _as_doi(text: str) -> str:
    """A DOI as typed, or an arXiv ID turned into its DOI.  Only forms that need no network are accepted here:
    PMIDs, PMCIDs, links and titles are looked up by the server (inputs.resolve) before a scan starts.  Raises
    ValueError with a plain message otherwise."""
    try:
        return fetch.normalize(text)
    except ValueError as e:
        from . import inputs
        r = inputs.parse_input(text)
        if r.get("kind") == "arxiv" and r.get("doi"):
            return r["doi"]
        raise e


def scan_doi(doi: str, progress: Optional[Progress] = None, online: bool = True,
             should_cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Paste a DOI: fetch the paper, its figures, data and supplementary files, then screen everything.
    online=False skips the reference and publication-record lookups (the fetch itself still needs the network).
    should_cancel() is polled inside the fetch, so a cancel stops a download in progress (raises Cancelled)."""
    progress = progress or _noop
    t0 = time.time()
    doi = _as_doi(doi)
    b = fetch.fetch(doi, HOME / "cache", progress=lambda s, f: progress(s, 0.02 + 0.2 * f),
                    should_cancel=should_cancel)
    rid = _rid(hashlib.sha1(b.doi.encode()).hexdigest()[:8])
    run = _new_run(RUNS / rid)
    with _RunGuard(run):
        main = b.dir / b.pdf if b.pdf else None
        if main is not None:
            shutil.copy(main, run / "source.pdf")
        progress("Reading the paper and its supplementary files", 0.24)
        src = sources.load(main, [(b.dir / s["file"], s["label"], s["source"]) for s in b.supplements], meta=b.meta,
                           xml=(b.dir / b.xml) if b.xml else None,
                           jats_figures=[(b.dir / f["file"], f["label"], f["caption"]) for f in b.figures])
        b.meta["fetch"] = fetch.summary(b)
        return _analyse(run, rid, src, main, b.meta, online, progress, t0, b.log)


def _analyse(run: Path, rid: str, src: "sources.Sources", main_pdf: Optional[Path], meta: dict, online: bool,
             progress: Progress, t0: float, log: list[str]) -> dict:
    paper = src.paper
    ev = run / "evidence"
    progress("Finding figures and panels", 0.28)
    for f in paper.figures:
        panels.segment(f)
        cv2.imwrite(str(run / "figures" / f"{f.id}.png"), f.image)
    timings = {"read": time.time() - t0}

    findings = _image_findings(paper, ev, progress)
    try:
        findings += imgchecks.check(paper, ev, findings, progress)
    except Exception as e:          # noqa: BLE001 - the core image checks above must not be lost
        findings.append(_skipped("image.unchecked", "The extra image checks (flipped copies, negatives, band rows) "
                                 "could not run", e))
    imgchecks.label_bik(findings)
    timings["images"] = time.time() - t0 - timings["read"]
    progress("Checking text and statistics", 0.72)
    findings += textcheck.check(paper, main_pdf)
    findings += stats.check(paper)
    for sp in src.supp_papers:
        for f in stats.check(sp) + textcheck.check(sp):
            for loc in f.locations:
                loc.file = sp.source
            f.detail += f" (in {sp.source})"
            findings.append(f)
    progress("Checking data tables and supplementary data", 0.78)
    tl, tl_info = reporting.timeline(paper, meta)
    findings += datacheck.check(src.tables, dates=_paper_dates(meta, tl_info),
                                files=[(sf.path, sf.name) for sf in src.files if sf.path and sf.kind != "article"])
    try:
        findings += stats.check_tables(src.tables, paper)
    except Exception as e:          # noqa: BLE001
        findings.append(_skipped("stats.unchecked", "The baseline-table check could not run", e))
    findings += refs.check(paper, meta)
    ref_stats: dict = {}
    history_info: dict = {}
    if online and meta:
        try:
            f, ref_stats = refcheck.analyse(meta, progress=lambda s, x: progress(s, 0.84 + 0.06 * x), paper=paper)
            findings += f
        except Exception as e:          # noqa: BLE001 - finished image work must not be lost to a lookup
            findings.append(_skipped("refs.unchecked", "The reference check could not run", e))
        try:
            f, history_info = history.check(meta, paper, progress=lambda s, x: progress(s, 0.9 + 0.04 * x))
            findings += f
            # refs.sneaked from refs.sneaked() names each extra reference; drop history's count-only twin
            if any(x.check == "refs.sneaked" and "references" in x.data for x in findings):
                findings = [x for x in findings if not (x.check == "refs.sneaked" and "references" not in x.data)]
        except Exception as e:          # noqa: BLE001
            findings.append(_skipped("record.unchecked", "The publication-record checks could not run", e))
    elif online:
        progress("Asking Crossref about retracted references", 0.84)
        findings += refs.retractions(paper)
    if "notices" in (meta.get("lookup_errors") or []):
        findings.append(Finding("record.unchecked", "info", "Retraction status could not be checked",
                                "Crossref did not answer when asked for retractions, corrections and expressions of "
                                "concern about this paper, so their absence here means nothing. Re-run the scan "
                                "later, or look the DOI up on Retraction Watch.", 0.9))
    progress("Checking reporting and the publication record", 0.94)
    n_main = sum(1 for f in paper.figures if not f.id.startswith("S"))
    findings += reporting.record(meta) + tl + reporting.statements(paper, meta) + \
        reporting.cell_lines(paper, online) + reporting.consistency(paper, n_main, 0)
    try:
        findings += reporting.trials(paper, meta, online)
    except Exception as e:          # noqa: BLE001 - a registry lookup must not sink the scan
        findings.append(_skipped("report.trial_unchecked", "The trial-registry check could not run", e))
    for sf in src.files:
        sw = (sf.exif or {}).get("Software", "")
        if sw:
            findings.append(Finding("image.metadata", "info", f"{sf.name}: saved by {sw}",
                                    "The image file records the software that last saved it. Editing software is "
                                    "normal for adjusting brightness or adding labels; the raw instrument file is "
                                    "what an editor would ask for.", 0.5, [Location(file=sf.name)],
                                    data={"exif": sf.exif}))
    sources.copy_into(run, src.files)
    extra = {
        "meta": {k: meta.get(k) for k in ("doi", "title", "journal", "publisher", "issued", "authors", "notices",
                                          "dates", "topics", "cited_by_count", "is_retracted", "pmcid", "license",
                                          "funders", "abstract", "special_issue")} if meta else {},
        "references": ref_stats, "timeline": tl_info, "history": history_info,
        "sources": [x.public() for x in src.files], "log": log, "fetch": meta.get("fetch") if meta else None,
        "tables": len(src.tables),
        "coverage": _coverage(main_pdf, paper, src),
    }
    return _finish(run, rid, paper, findings, timings, t0, progress, extra)


def _paper_dates(meta: dict, tl_info: dict) -> Optional[dict]:
    """The paper's dates for the supplementary-file timeline: the publisher's record when there is one, else the
    dates printed in an uploaded PDF.  None (timeline skipped) when neither gives a date."""
    d = {k: v for k, v in ((meta or {}).get("dates") or {}).items() if v}
    if meta and meta.get("issued"):
        d["issued"] = meta["issued"]
    for k in ("received", "accepted", "published"):
        if k not in d and (tl_info or {}).get(k):
            d[k] = tl_info[k]
    return d or None


def _skipped(check: str, title: str, e: Exception) -> Finding:
    return Finding(check, "info", title, f"It stopped with {type(e).__name__}: {str(e)[:200]}. Nothing here is "
                   "evidence about the paper; re-run the scan to try again.", 0.9)


def _coverage(main_pdf: Optional[Path], paper: Paper, src: "sources.Sources") -> str:
    """How much of the paper was actually examined: 'full', 'text only' (no figures) or 'metadata only'."""
    if main_pdf is not None or paper.figures:
        return "full"
    if paper.text.strip() or src.supp_papers or src.tables:
        return "text only"
    return "metadata only"


NOT_SCREENED = {
    "metadata only": "Not screened: no open full text was found, so figures, data and text were not checked. "
                     "Upload the PDF to screen it.",
    "text only": "Figures not screened: no open PDF or figure files were found, so only the text and data were "
                 "checked. Upload the PDF to check the figures.",
}


def _finish(run: Path, rid: str, paper: Paper, findings: list[Finding], timings: dict, t0: float,
            progress: Progress, extra: Optional[dict] = None) -> dict:
    findings.sort(key=lambda f: (f.kind != "integrity", SEVERITIES.index(f.severity), -f.confidence))
    for i, f in enumerate(findings):
        f.id = f"{i + 1:03d}"
    score = risk_score(findings)
    report = {
        "id": rid, "version": __version__, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "paper": {"source": paper.source, "title": paper.title, "doi": paper.doi, "pages": paper.page_count},
        "figures": [f.public() for f in paper.figures],
        "findings": [f.public() for f in findings],
        "score": score, "verdict": _verdict(score, findings, (extra or {}).get("coverage", "full")),
        "counts": {s: sum(f.severity == s for f in findings) for s in SEVERITIES},
        "kinds": {k: sum(f.kind == k and f.severity != "info" for f in findings) for k in ("integrity", "quality")},
        "categories": {c: sum(f.category == c for f in findings) for c in CATEGORIES},
        "panels": sum(len(f.panels) for f in paper.figures),
        "timings": {k: round(v, 2) for k, v in {**timings, "total": time.time() - t0}.items()},
        **(extra or {}),
    }
    write_report(run, report)
    progress("Done", 1.0)
    return json.loads(json.dumps(report, default=_json))     # plain JSON types for every caller (CLI, server)


def _verdict(score: int, findings: list[Finding], coverage: str) -> str:
    """'Nothing notable found' must not be said about a paper whose figures nobody could look at."""
    v = verdict(score, findings)
    return NOT_SCREENED[coverage] if coverage in NOT_SCREENED and v.startswith("Nothing notable") else v


def _json(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def scan_case(paths: list[str | Path], progress: Optional[Progress] = None, name: str = "case") -> dict:
    """Pull the thread: compare every image across several papers (one lab, one journal issue, one author).
    Within-paper findings are in each paper's own scan; this report holds the cross-paper reuse."""
    progress = progress or _noop
    t0 = time.time()
    paths = [Path(p) for p in paths]
    h = hashlib.sha1("|".join(sorted(p.name for p in paths)).encode()).hexdigest()[:8]
    rid = _rid("case-" + h)
    run = _new_run(RUNS / rid)
    with _RunGuard(run):
        return _scan_case(run, rid, paths, progress, name, t0)


def _scan_case(run: Path, rid: str, paths: list[Path], progress: Progress, name: str, t0: float) -> dict:
    ev = run / "evidence"
    papers = []
    feats = []
    for i, p in enumerate(paths):
        progress(f"Reading {p.name}", 0.05 + 0.5 * i / len(paths))
        paper = pdfdoc.load(p)
        for f in paper.figures:
            panels.segment(f)
            f.id = f"{i + 1}_{f.id}"
            for pn in f.panels:
                pn.figure_id = f.id
                pn.id = f"{i + 1}_{pn.id}"
                x = imgdup.features(pn, paper=i)
                if x is not None:
                    feats.append(x)
            cv2.imwrite(str(run / "figures" / f"{f.id}.png"), f.image)
        papers.append(paper)
    progress("Comparing images across papers", 0.6)
    names = [p.source for p in papers]
    findings = []
    for m in imgdup.find(feats):
        if m.a.paper == m.b.paper:
            continue
        f = _match_finding(m, True, names)
        f.evidence = evidence.pair_evidence(ev, f"xdup-{m.a.panel.id}-{m.b.panel.id}",
                                            m.a.panel.image, m.b.panel.image, m.a.panel.id, m.b.panel.id, m.box_a,
                                            m.box_b, m.M, m.flip)
        findings.append(f)
    imgchecks.label_bik(findings)
    merged = Paper(source=name, title=f"{len(papers)} papers", page_count=sum(p.page_count for p in papers),
                   figures=[f for p in papers for f in p.figures])
    report = _finish(run, rid, merged, findings, {}, t0, progress)
    report["case"] = [{"index": i + 1, "source": p.source, "title": p.title, "doi": p.doi} for i, p in enumerate(papers)]
    write_report(run, report)
    return report


def list_runs() -> list[dict]:
    out = []
    if not RUNS.exists():
        return out
    for d in sorted(RUNS.iterdir(), reverse=True):
        rp = d / "report.json"
        if not rp.exists():
            continue
        try:
            r = json.loads(rp.read_text())
        except Exception:
            continue
        out.append({"id": r["id"], "title": r["paper"].get("title") or r["paper"]["source"],
                    "source": r["paper"]["source"], "score": r["score"], "counts": r["counts"],
                    "created": r["created"], "case": "case" in r})
    return out
