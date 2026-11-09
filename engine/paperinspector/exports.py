"""Exports of one run: findings as CSV or XLSX, a PDF report (PyMuPDF), and an evidence pack ZIP whose manifest
lists a SHA-256 hash of every file, so a recipient can check nothing was changed.

Paper text is untrusted: a title or quote starting with '=' must not become a spreadsheet formula, so every text
cell that a spreadsheet would read as a formula is prefixed with an apostrophe.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

from . import __version__, catalog, rulings
from .models import RULING_LABELS, SEVERITIES, ruling_of, where

COLUMNS = ["id", "fingerprint", "check", "check_label", "kind", "category", "severity", "confidence", "title",
           "where", "detail", "ai_verdict", "ruling", "note", "evidence"]
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def load(run: Path) -> dict:
    """report.json with each finding's fingerprint and ruling."""
    return rulings.annotate(json.loads((run / "report.json").read_text()), run)


def _safe(v):
    """Neutralise text a spreadsheet would run as a formula."""
    if isinstance(v, str) and v.startswith(FORMULA_START):
        return "'" + v
    return v


def rows(report: dict, include_info: bool = False, include_notes: bool = True) -> list[dict]:
    out = []
    for f in report.get("findings") or []:
        if f.get("severity") == "info" and not include_info:
            continue
        r = f.get("ruling") or {}
        out.append({"id": f.get("id", ""), "fingerprint": f.get("fingerprint", ""), "check": f.get("check", ""),
                    "check_label": catalog.label(f.get("check", "")), "kind": f.get("kind", ""),
                    "category": f.get("category", ""), "severity": f.get("severity", ""),
                    "confidence": f.get("confidence", ""), "title": f.get("title", ""), "where": where(f),
                    "detail": f.get("detail", ""), "ai_verdict": (f.get("ai") or {}).get("verdict", "") or "",
                    "ruling": RULING_LABELS[ruling_of(f)], "note": r.get("note", "") if isinstance(r, dict) and include_notes else "",
                    "evidence": " ".join(f.get("evidence") or [])})
    return out


def findings_csv(report: dict, include_info: bool = False, include_notes: bool = True) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows(report, include_info, include_notes):
        w.writerow({k: _safe(v) for k, v in r.items()})
    return ("﻿" + buf.getvalue()).encode("utf-8")     # the BOM makes Excel read UTF-8 correctly


def _paper_rows(report: dict) -> list[tuple[str, str]]:
    p = report.get("paper") or {}
    meta = report.get("meta") or {}
    rev = report.get("review") or {}
    return [("Title", p.get("title") or p.get("source", "")), ("DOI", p.get("doi") or meta.get("doi") or ""),
            ("Journal", meta.get("journal") or ""), ("Source", p.get("source", "")),
            ("Scanned", report.get("created", "")), ("Engine version", report.get("version", "")),
            ("Risk score", str(report.get("score", ""))), ("Score after review", str(rev.get("score_reviewed", ""))),
            ("Verdict", report.get("verdict", "")), ("Reviewed", f"{rev.get('reviewed', 0)} of {rev.get('total', 0)}"),
            ("Exported", time.strftime("%Y-%m-%dT%H:%M:%S")), ("Exported by", f"Paper Inspector {__version__}")]


def findings_xlsx(report: dict, include_info: bool = False) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Findings"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows(report, include_info):
        ws.append([_safe(r[k]) for k in COLUMNS])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if isinstance(c.value, str):
                c.data_type = "s"           # never a formula, whatever the text says
    widths = {"title": 50, "detail": 80, "where": 28, "note": 40, "check": 22, "check_label": 22}
    for i, k in enumerate(COLUMNS, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = widths.get(k, 12)
        if k in ("title", "detail", "note"):
            for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                row[0].alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ps = wb.create_sheet("Paper")
    for k, v in _paper_rows(report):
        ps.append([k, _safe(v)])
        ps.cell(ps.max_row, 2).data_type = "s"
    ps.column_dimensions["A"].width = 20
    ps.column_dimensions["B"].width = 90
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- PDF ----------------------------------------------------------------------------------------------------------

SEV_COLOR = {"critical": "#dc2626", "high": "#ea580c", "medium": "#ca8a04", "low": "#2563eb", "info": "#6b7280"}
PDF_CSS = """
body { font-family: sans-serif; font-size: 10pt; color: #0f172a; }
h1 { font-size: 18pt; margin: 0 0 6pt 0; } h2 { font-size: 13pt; margin: 14pt 0 4pt 0; }
h3 { font-size: 11pt; margin: 10pt 0 2pt 0; }
p { margin: 2pt 0 4pt 0; } .mute { color: #64748b; } .score { font-size: 30pt; font-weight: bold; }
.sev { color: #ffffff; font-weight: bold; font-size: 8pt; } .rule { font-weight: bold; }
"""
IMG_W, IMG_H = 480.0, 230.0       # points: one evidence image at most this big


def _image_size(path: Path) -> Optional[tuple[float, float]]:
    try:
        import pymupdf
        pm = pymupdf.Pixmap(str(path))
        w, h = pm.width, pm.height
    except Exception:          # noqa: BLE001 - an unreadable image is left out, not fatal
        return None
    if w <= 0 or h <= 0:
        return None
    s = min(IMG_W / w, IMG_H / h, 1.0)
    return w * s, h * s


def _pdf_html(report: dict, run: Path, include_info: bool, include_notes: bool = False) -> str:
    esc = html.escape
    p = report.get("paper") or {}
    rev = report.get("review") or {}
    parts = [f"<h1>{esc(p.get('title') or p.get('source') or 'Untitled paper')}</h1>",
             f"<p class='mute'>{esc(p.get('source', ''))}{(' · DOI ' + esc(p['doi'])) if p.get('doi') else ''}</p>",
             f"<p class='mute'>Scanned {esc(report.get('created', ''))} with Paper Inspector "
             f"{esc(str(report.get('version', '')))}. Report made {esc(time.strftime('%Y-%m-%d %H:%M'))}.</p>",
             f"<p><span class='score'>{esc(str(report.get('score', 0)))}</span> / 100 risk score</p>",
             f"<p>{esc(report.get('verdict', ''))}</p>"]
    if rev.get("reviewed"):
        parts.append(f"<p>Reviewed by a person: {rev['reviewed']} of {rev.get('total', 0)} findings. "
                     f"Score after review: {rev.get('score_reviewed', '')}.</p>")
    counts = report.get("counts") or {}
    parts.append("<p class='mute'>" + esc(", ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts.get(s))) +
                 f"; {len(report.get('figures') or [])} figures, {report.get('panels', 0)} panels.</p>")
    parts.append("<p class='mute'>This is an automated screen. It finds candidates, not verdicts: every item needs a "
                 "person to look at it, and most have an innocent explanation.</p>")
    shown = None
    items = [f for f in report.get("findings") or [] if include_info or f.get("severity") != "info"]
    items.sort(key=lambda f: f.get("kind") != "integrity")
    if not items:
        parts.append("<h2>Findings</h2><p>No findings.</p>")
    for f in items:
        kind = f.get("kind", "integrity")
        if kind != shown:
            shown = kind
            parts.append("<h2>Integrity signals</h2><p class='mute'>Could indicate a problem with the work. These set "
                         "the score.</p>" if kind == "integrity" else
                         "<h2>Quality notes</h2><p class='mute'>Honest slips and missing information.</p>")
        sev = f.get("severity", "info")
        rl = ruling_of(f)
        note = (f.get("ruling") or {}).get("note", "") if isinstance(f.get("ruling"), dict) else ""
        loc = where(f)
        parts.append(f"<h3><span class='sev' style='background-color:{SEV_COLOR.get(sev, '#6b7280')}'>"
                     f"&nbsp;{esc(sev.upper())}&nbsp;</span> {esc(f.get('id', ''))}. {esc(f.get('title', ''))}</h3>")
        parts.append(f"<p class='mute'>{esc(catalog.label(f.get('check', '')))}"
                     f"{(' · ' + esc(loc)) if loc else ''} · confidence {float(f.get('confidence') or 0):.2f}</p>")
        parts.append(f"<p>{esc(f.get('detail', ''))}</p>")
        if rl != "unreviewed":
            parts.append(f"<p class='rule'>Your review: {esc(RULING_LABELS[rl])}</p>")
        if note and include_notes:
            parts.append(f"<p class='mute'>Note: {esc(note)}</p>")
        ai = (f.get("ai") or {}).get("verdict")
        if ai:
            parts.append(f"<p class='mute'>AI investigator: {esc(str(ai).replace('_', ' '))}</p>")
        for e in [x for x in f.get("evidence") or [] if x.lower().endswith((".png", ".jpg", ".jpeg"))][:2]:
            name = Path(e).name
            size = _image_size(run / "evidence" / name)
            if size:
                parts.append(f"<p><img src='{esc(name)}' width='{size[0]:.0f}' height='{size[1]:.0f}'/></p>")
    return "<html><body>" + "".join(parts) + "</body></html>"


def report_pdf(run: Path, report: Optional[dict] = None, include_info: bool = False,
               include_notes: bool = False) -> bytes:
    """A PDF report: cover with score and verdict, then each finding with its evidence images.  Private notes are
    left out unless include_notes (the PDF is usually sent to someone)."""
    import pymupdf

    report = report or load(run)
    ev = run / "evidence"
    archive = pymupdf.Archive(str(ev)) if ev.is_dir() else pymupdf.Archive()
    story = pymupdf.Story(html=_pdf_html(report, run, include_info, include_notes), user_css=PDF_CSS, archive=archive)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page = pymupdf.paper_rect("a4")
    box = page + (48, 48, -48, -56)
    more = 1
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(box)
        story.draw(dev)
        writer.end_page()
    writer.close()
    doc = pymupdf.open("pdf", buf.getvalue())
    p = report.get("paper") or {}
    doc.set_metadata({"title": f"Paper Inspector report: {p.get('title') or p.get('source') or ''}"[:300],
                      "creator": f"Paper Inspector {__version__}", "producer": "PyMuPDF"})
    for i, pg in enumerate(doc):
        pg.insert_text((48, page.height - 28), f"Paper Inspector · automated screen · page {i + 1} of {len(doc)}",
                       fontsize=8, color=(0.4, 0.45, 0.5))
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


# --- evidence pack ----------------------------------------------------------------------------------------------

README = """Paper Inspector evidence pack

report.pdf / report.html   the screen's report, readable without the app
findings.csv               one row per finding, with your rulings and notes
report.json                everything the scan measured
rulings.json               your rulings for this paper
figures/, evidence/        each figure as analysed, and the annotated evidence images
source.*, supplements/     the paper and supplementary files as they were scanned
manifest.json              size and SHA-256 hash of every file in this pack
SHA256SUMS.txt             the same hashes, checkable with:  shasum -a 256 -c SHA256SUMS.txt

This is an automated screen. It finds candidates, not verdicts; every item needs a person to look at it.
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _without_notes(d: dict) -> dict:
    out = {}
    for fp, rec in (d.get("rulings") or {}).items():
        rec = {k: v for k, v in rec.items() if k != "note"}
        rec["history"] = [{k: v for k, v in h.items() if k != "note"} for h in rec.get("history") or []]
        out[fp] = rec
    return {**d, "rulings": out}


def evidence_zip(run: Path, out_dir: Optional[Path] = None, include_notes: bool = False) -> Path:
    """Write the evidence pack to a temporary file and return its path (the caller deletes it).  Private notes are
    left out unless include_notes."""
    from .report import html_report

    report = load(run)
    rid = report.get("id") or run.name
    top = f"paperinspector-{rid}"
    rul = rulings.for_run(report, run)
    made: dict[str, bytes] = {"README.txt": README.encode(),
                              "findings.csv": findings_csv(report, include_info=True, include_notes=include_notes),
                              "rulings.json": json.dumps(rul if include_notes else _without_notes(rul), indent=1,
                                                         ensure_ascii=False).encode()}
    try:
        made["report.html"] = html_report(run).encode()
    except Exception as e:      # noqa: BLE001 - one broken part leaves a note, not an empty pack
        made["report-html-error.txt"] = f"The HTML report could not be made: {type(e).__name__}".encode()
    try:
        made["report.pdf"] = report_pdf(run, report, include_notes=include_notes)
    except Exception as e:      # noqa: BLE001
        made["report-pdf-error.txt"] = f"The PDF report could not be made: {type(e).__name__}".encode()
    files = []
    for p in sorted(run.rglob("*")):
        rel = p.relative_to(run).as_posix()
        if not p.is_file() or p.is_symlink() or any(part.startswith(".") for part in p.relative_to(run).parts):
            continue
        files.append((rel, p))
    manifest = {"pack": top, "made": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "engine": __version__,
                "run": rid, "scanned": report.get("created"), "scan_engine": report.get("version"),
                "paper": report.get("paper"), "sources": report.get("sources") or [],
                "log": report.get("log") or [], "files": []}
    out_dir = Path(out_dir or tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".evidence-", suffix=".zip")
    os.close(fd)
    sums = []
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, p in files:
                if rel in made:
                    continue
                h = _file_sha256(p)
                z.write(p, f"{top}/{rel}")
                manifest["files"].append({"path": rel, "bytes": p.stat().st_size, "sha256": h, "made": False})
                sums.append((h, rel))
            for rel, data in made.items():
                h = _sha256(data)
                z.writestr(f"{top}/{rel}", data)
                manifest["files"].append({"path": rel, "bytes": len(data), "sha256": h, "made": True})
                sums.append((h, rel))
            manifest["files"].sort(key=lambda x: x["path"])
            mdata = json.dumps(manifest, indent=1, ensure_ascii=False).encode()
            z.writestr(f"{top}/manifest.json", mdata)
            sums.append((_sha256(mdata), "manifest.json"))
            z.writestr(f"{top}/SHA256SUMS.txt", "".join(f"{h}  {r}\n" for h, r in sorted(sums, key=lambda x: x[1])))
    except BaseException:
        os.unlink(tmp)
        raise
    return Path(tmp)


# --- a batch --------------------------------------------------------------------------------------------------------

BATCH_COLUMNS = ["input", "doi", "status", "run", "title", "score", "serious", "top_checks", "error"]


def batch_csv(items: list[dict], runs_dir: Path) -> bytes:
    """A triage table for a batch: one row per paper, with its score and the checks that fired most seriously."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, BATCH_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for it in items:
        row = {"input": it.get("input", ""), "doi": it.get("doi") or "", "status": it.get("status", ""),
               "run": it.get("run") or "", "title": it.get("title") or "", "score": it.get("score", ""),
               "error": it.get("error") or ""}
        rid = it.get("run")
        rp = runs_dir / str(rid) / "report.json" if rid and "/" not in str(rid) and ".." not in str(rid) else None
        if rp is not None and rp.is_file():
            try:
                r = json.loads(rp.read_text())
                c = r.get("counts") or {}
                row["serious"] = (c.get("critical") or 0) + (c.get("high") or 0)
                top = [f["check"] for f in r.get("findings") or []
                       if f.get("kind") == "integrity" and f.get("severity") in ("critical", "high", "medium")]
                row["top_checks"] = " ".join(list(dict.fromkeys(top))[:5])
            except (OSError, ValueError, KeyError, TypeError):
                pass
        w.writerow({k: _safe(v) for k, v in row.items()})
    return ("﻿" + buf.getvalue()).encode("utf-8")
