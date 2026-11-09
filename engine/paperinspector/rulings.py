"""A person's ruling on each finding (confirmed / false positive / needs raw data), with a note.

Rulings belong to the paper, not to one scan: they are keyed by the paper (its DOI, else a hash of the source file)
and, inside it, by each finding's fingerprint (models.fingerprints), so scanning the same paper again - or with a
newer engine - keeps them.  They live in <data dir>/rulings/<paper key>.json, apart from the run folder, so neither a
rescan nor the AI investigator can overwrite them.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from . import pipeline
from .models import (RULING_LABELS, RULINGS, SEVERITIES, fingerprints, match_fingerprint, reviewed_score, ruling_of,
                     where)

MAX_NOTE = 4000
MAX_HISTORY = 50
_LOCK = threading.RLock()
_KEYS: dict[tuple, str] = {}          # (run id, source mtime) -> paper key; hashing a large PDF once is enough


def _dir() -> Path:
    return pipeline.HOME / "rulings"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def paper_key(report: dict, run: Optional[Path] = None) -> str:
    """Same paper, same key: by DOI when there is one, else by the bytes of the source file, else by the run."""
    p = report.get("paper") or {}
    if report.get("case"):
        ids = sorted((c.get("doi") or c.get("source") or "") for c in report["case"])
        return "case-" + hashlib.sha1("|".join(ids).encode()).hexdigest()[:16]
    doi = (p.get("doi") or (report.get("meta") or {}).get("doi") or "").strip().lower()
    if doi:
        return "doi-" + hashlib.sha1(doi.encode()).hexdigest()[:16]
    src = next(iter(sorted(run.glob("source.*"))), None) if run else None
    if src is not None and src.is_file():
        k = (report.get("id"), src.stat().st_mtime_ns)
        if k not in _KEYS:
            _KEYS[k] = "file-" + _sha(src)[:16]
        return _KEYS[k]
    return "run-" + hashlib.sha1(str(report.get("id", "")).encode()).hexdigest()[:16]


def _path(key: str) -> Path:
    if not key or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in key):
        raise ValueError("bad paper key")
    return _dir() / f"{key}.json"


def load(key: str) -> dict:
    try:
        d = json.loads(_path(key).read_text())
        if isinstance(d, dict) and isinstance(d.get("rulings"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"paper": {}, "rulings": {}}


def _save(key: str, d: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_dir(), prefix=".rulings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(d, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, _path(key))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def set_ruling(report: dict, run: Optional[Path], finding_id: str, ruling: str, note: Optional[str] = None) -> dict:
    """Record a ruling (and optionally a note) for one finding of this run.  'unreviewed' with an empty note clears it.
    Returns the stored record (or the unreviewed default)."""
    if ruling not in RULINGS:
        raise ValueError(f"ruling must be one of {', '.join(RULINGS)}")
    findings = report.get("findings") or []
    fps = fingerprints(findings)
    idx = next((i for i, f in enumerate(findings) if f.get("id") == finding_id), None)
    if idx is None:
        raise KeyError(finding_id)
    f, fp = findings[idx], fps[idx]
    key = paper_key(report, run)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _LOCK:
        d = load(key)
        p = report.get("paper") or {}
        d["paper"] = {"title": p.get("title") or p.get("source") or "", "doi": p.get("doi") or ""}
        hit = match_fingerprint(fp, d["rulings"])
        if hit and hit != fp:               # the same finding, moved a little since it was ruled on: re-key it
            d["rulings"][fp] = d["rulings"].pop(hit)
        old = d["rulings"].get(fp) or {}
        note = old.get("note", "") if note is None else str(note)[:MAX_NOTE].strip()
        hist = list(old.get("history") or [])
        if old:
            hist.append({"ruling": old.get("ruling"), "note": old.get("note", ""), "at": old.get("updated"),
                         "run": old.get("run")})
        if ruling == "unreviewed" and not note:
            d["rulings"].pop(fp, None)
            rec = {"ruling": "unreviewed", "note": "", "updated": now}
        else:
            rec = {"ruling": ruling, "note": note, "updated": now, "run": report.get("id"), "check": f.get("check"),
                   "title": f.get("title", "")[:300], "where": where(f), "history": hist[-MAX_HISTORY:]}
            d["rulings"][fp] = rec
        _save(key, d)
    return {"fingerprint": fp, **rec}


def annotate(report: dict, run: Optional[Path] = None) -> dict:
    """Add to a report (in place) each finding's fingerprint and ruling, and a 'review' summary:
    counts per ruling, how many of the findings a person has looked at, the score after review, and how many earlier
    rulings matched nothing in this scan (after a rescan with a newer engine)."""
    findings = report.get("findings") or []
    key = paper_key(report, run)
    stored = load(key)["rulings"]
    used = set()
    for f, fp in zip(findings, fingerprints(findings)):
        f["fingerprint"] = fp
        hit = match_fingerprint(fp, stored, used)
        rec = stored.get(hit) if hit else None
        if rec:
            used.add(hit)
            f["ruling"] = {"ruling": rec.get("ruling", "unreviewed"), "note": rec.get("note", ""),
                           "updated": rec.get("updated")}
        else:
            f["ruling"] = {"ruling": "unreviewed", "note": "", "updated": None}
    counted = [f for f in findings if f.get("severity") != "info"]
    counts = {r: sum(ruling_of(f) == r for f in counted) for r in RULINGS}
    report["review"] = {"paper_key": key, "counts": counts, "total": len(counted),
                        "reviewed": len(counted) - counts["unreviewed"],
                        "score_reviewed": reviewed_score(findings) if findings else report.get("score", 0),
                        "unmatched": len(set(stored) - used), "labels": RULING_LABELS}
    return report


def for_run(report: dict, run: Optional[Path] = None) -> dict:
    """What GET /api/runs/{rid}/rulings returns."""
    findings = report.get("findings") or []
    key = paper_key(report, run)
    d = load(key)
    return {"paper_key": key, "rulings": d["rulings"],
            "by_finding": {f.get("id"): fp for f, fp in zip(findings, fingerprints(findings))}}


def confirmed(report: dict, statuses=("confirmed",)) -> list[dict]:
    """Annotated findings with one of these rulings, most serious first."""
    rows = [f for f in report.get("findings") or [] if ruling_of(f) in statuses]
    return sorted(rows, key=lambda f: (f.get("kind") != "integrity", SEVERITIES.index(f.get("severity", "info"))
                                       if f.get("severity") in SEVERITIES else 9))
