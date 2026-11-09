"""The run library: tags on runs and search over past scans by title, DOI, author, journal and tag.

Tags live in <data dir>/tags.json.  Searching reads each report.json once and keeps a small summary of it in memory
(re-read only when the file changes), so a library of hundreds of scans stays quick.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Optional

from . import pipeline

MAX_TAGS = 20
MAX_TAG_LEN = 40
_LOCK = threading.RLock()
_INDEX: dict[str, tuple[int, dict]] = {}   # run id -> (report.json mtime, summary)


def _tags_path() -> Path:
    return pipeline.HOME / "tags.json"


def clean_tag(t: str) -> str:
    """A tag is short plain text: control characters dropped, spaces collapsed."""
    t = "".join(ch for ch in unicodedata.normalize("NFC", str(t)) if unicodedata.category(ch)[0] != "C")
    return " ".join(t.split())[:MAX_TAG_LEN].strip()


def _load_tags() -> dict[str, list[str]]:
    try:
        d = json.loads(_tags_path().read_text())
        runs = d.get("runs") if isinstance(d, dict) else None
        return {k: [t for t in v if isinstance(t, str)] for k, v in (runs or {}).items() if isinstance(v, list)}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_tags(runs: dict[str, list[str]]) -> None:
    pipeline.HOME.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pipeline.HOME, prefix=".tags.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"runs": {k: v for k, v in runs.items() if v}}, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, _tags_path())
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def tag_map() -> dict[str, list[str]]:
    return _load_tags()


def tags_of(rid: str) -> list[str]:
    return _load_tags().get(rid, [])


def edit_tags(rid: str, add: Optional[list[str]] = None, remove: Optional[list[str]] = None,
              replace: Optional[list[str]] = None) -> list[str]:
    """Add and remove tags (case-insensitive match; the first spelling used is kept).  Returns the run's tags."""
    with _LOCK:
        runs = _load_tags()
        cur = list(runs.get(rid, [])) if replace is None else []
        for t in (replace or []) + (add or []):
            t = clean_tag(t)
            if t and t.lower() not in {c.lower() for c in cur}:
                if len(cur) >= MAX_TAGS:
                    raise ValueError(f"at most {MAX_TAGS} tags per scan")
                cur.append(t)
        gone = {clean_tag(t).lower() for t in remove or []}
        cur = [t for t in cur if t.lower() not in gone]
        runs[rid] = cur
        _save_tags(runs)
    return cur


def forget(rid: str) -> None:
    """A deleted run takes its tags with it.  (Rulings stay: they belong to the paper.)"""
    with _LOCK:
        runs = _load_tags()
        if runs.pop(rid, None) is not None:
            _save_tags(runs)
        _INDEX.pop(rid, None)


def all_tags() -> list[dict]:
    counts: dict[str, list] = {}
    for ts in _load_tags().values():
        for t in ts:
            c = counts.setdefault(t.lower(), [t, 0])
            c[1] += 1
    return sorted(({"tag": t, "count": n} for t, n in counts.values()), key=lambda x: (-x["count"], x["tag"].lower()))


def _authors(meta: dict) -> list[str]:
    out = []
    for a in (meta.get("authors") or [])[:40]:
        if isinstance(a, dict):
            n = f"{a.get('given', '')} {a.get('family', '')}".strip() or a.get("name", "")
        else:
            n = str(a)
        if n:
            out.append(n)
    return out


def summary(rid: str) -> Optional[dict]:
    """A short, searchable summary of one run, or None if it has no readable report."""
    rp = pipeline.RUNS / rid / "report.json"
    try:
        mt = rp.stat().st_mtime_ns
    except OSError:
        return None
    hit = _INDEX.get(rid)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        r = json.loads(rp.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(r, dict):
        return None
    p = r.get("paper") or {}
    meta = r.get("meta") or {}
    s = {"id": r.get("id", rid), "title": p.get("title") or meta.get("title") or p.get("source") or rid,
         "source": p.get("source", ""), "doi": p.get("doi") or meta.get("doi") or "",
         "journal": meta.get("journal") or "", "authors": _authors(meta), "created": r.get("created", ""),
         "score": r.get("score", 0), "score_ai": r.get("score_ai"), "verdict": r.get("verdict", ""),
         "counts": r.get("counts") or {}, "kinds": r.get("kinds") or {}, "case": "case" in r,
         "coverage": r.get("coverage", ""), "version": r.get("version", "")}
    _INDEX[rid] = (mt, s)
    return s


def search(q: str = "", tags: Optional[list[str]] = None, min_score: Optional[int] = None,
           max_score: Optional[int] = None, sort: str = "created", limit: int = 200) -> list[dict]:
    """Every word of q must appear in the title, DOI, authors, journal, file name or tags (any case).  tags: the run
    must carry all of them."""
    words = [w for w in re.split(r"\s+", (q or "").lower().strip()) if w]
    want = {clean_tag(t).lower() for t in tags or [] if clean_tag(t)}
    tagmap = _load_tags()
    out = []
    if not pipeline.RUNS.exists():
        return out
    for d in pipeline.RUNS.iterdir():
        if not d.is_dir():
            continue
        s = summary(d.name)
        if s is None:
            continue
        ts = tagmap.get(d.name, [])
        if want and not want <= {t.lower() for t in ts}:
            continue
        if min_score is not None and s["score"] < min_score or max_score is not None and s["score"] > max_score:
            continue
        hay = " ".join([s["title"], s["doi"], s["source"], s["journal"], " ".join(s["authors"]), " ".join(ts)]).lower()
        if any(w not in hay for w in words):
            continue
        out.append({**s, "tags": ts})
    keys = {"score": lambda s: (-(s["score"] or 0), s["created"]), "title": lambda s: s["title"].lower(),
            "created": lambda s: s["created"]}
    out.sort(key=keys.get(sort, keys["created"]), reverse=sort == "created")
    return out[:max(1, min(limit, 1000))]
