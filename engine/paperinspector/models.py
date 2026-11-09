"""Plain data that flows through a scan: the paper, its figures and panels, and what the checks found."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import numpy as np

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_WEIGHT = {"critical": 40.0, "high": 22.0, "medium": 9.0, "low": 3.0, "info": 0.0}


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    def as_list(self) -> list[float]:
        return [round(self.x, 1), round(self.y, 1), round(self.w, 1), round(self.h, 1)]


@dataclass
class Panel:
    id: str                 # "F2.c" - figure id + panel index letter
    figure_id: str
    box: Box                # inside the figure image, pixels
    kind: str = "other"     # blot | micrograph | photo | chart | text | other
    image: Optional[np.ndarray] = field(default=None, repr=False)   # BGR uint8
    text_mask: Optional[np.ndarray] = field(default=None, repr=False)  # True where PDF text (letters, labels) lies

    def public(self) -> dict:
        return {"id": self.id, "figure": self.figure_id, "box": self.box.as_list(), "kind": self.kind}


@dataclass
class Figure:
    id: str                 # "F2" (numbered from the caption when we can read one), else "P3.1"
    label: str              # what a person calls it: "Figure 2"
    page: int               # 1-based
    page_box: Box           # where it sits on the page, PDF points
    caption: str = ""
    image: Optional[np.ndarray] = field(default=None, repr=False)
    panels: list[Panel] = field(default_factory=list)
    dpi: float = 0.0
    text_boxes: list[list[float]] = field(default_factory=list)    # PDF text drawn over the figure, figure pixels
    image_boxes: list[list[float]] = field(default_factory=list)   # each placed raster image, figure pixels

    def public(self) -> dict:
        h, w = (self.image.shape[:2] if self.image is not None else (0, 0))
        return {"id": self.id, "label": self.label, "page": self.page, "page_box": self.page_box.as_list(),
                "caption": self.caption[:600], "width": w, "height": h, "dpi": round(self.dpi),
                "panels": [p.public() for p in self.panels]}


@dataclass
class Paper:
    source: str
    title: str = ""
    pages: list[str] = field(default_factory=list)        # text per page
    figures: list[Figure] = field(default_factory=list)
    page_count: int = 0
    doi: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    def figure(self, fid: str) -> Optional[Figure]:
        return next((f for f in self.figures if f.id == fid), None)

    def panel(self, pid: str) -> Optional[Panel]:
        for f in self.figures:
            for p in f.panels:
                if p.id == pid:
                    return p
        return None


@dataclass
class Location:
    figure: str = ""
    panel: str = ""
    page: int = 0
    box: Optional[list[float]] = None    # in figure pixels
    quote: str = ""                       # for text findings: the offending text
    file: str = ""                        # a supplementary file, when the finding is in one
    sheet: str = ""                       # spreadsheet sheet or table name
    cells: str = ""                       # e.g. "B4:B20"

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", 0, None)}


@dataclass
class Finding:
    check: str              # detector id, e.g. "image.duplicate"
    severity: str
    title: str
    detail: str
    confidence: float = 0.5           # 0..1, how sure the detector is that this is real
    locations: list[Location] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)   # file names inside the run's evidence folder
    data: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    @property
    def category(self) -> str:
        return category_of(self.check)

    @property
    def kind(self) -> str:
        return kind_of(self.check)

    def public(self) -> dict:
        return {"id": self.id, "check": self.check, "severity": self.severity, "title": self.title,
                "category": self.category, "kind": self.kind,
                "detail": self.detail, "confidence": round(self.confidence, 3),
                "locations": [l.public() for l in self.locations], "evidence": self.evidence,
                "data": self.data}


CATEGORIES = {
    "image": "Images", "data": "Data", "stats": "Statistics", "text": "Text", "refs": "References",
    "report": "Reporting", "record": "Publication record",
}

# Checks whose findings say "the work may have been manipulated" (they set the risk score).  Everything else is a
# quality note: an honest slip, a missing statement, a habit worth knowing about - listed, but kept out of the score.
# Only ids a detector emits belong here (tests/test_data_edge.py checks).  refs.bad_doi stays a quality note on
# purpose: a DOI that resolves nowhere is usually a typo.
INTEGRITY = {
    "image.duplicate", "image.clone", "image.splice", "image.flat", "image.duplicate_chart",
    "data.duplicate_block", "data.random_formula", "data.terminal_digits", "data.repeated_values",
    "data.too_similar", "data.mixed_fonts",
    "stats.recompute", "stats.grim", "stats.repeated_se",
    "text.ai_statement", "text.prompt_injection", "text.hidden_tags", "text.tortured",
    "refs.sneaked", "refs.citation_pushing", "record.retracted", "record.concern", "record.fast_acceptance",
    # added in wave 2: each is as specific as the checks above (tested quiet on honest data and real papers)
    "data.offset_copy", "stats.grimmer", "stats.ci_p", "stats.carlisle",
}


def category_of(check: str) -> str:
    return check.split(".", 1)[0]


def kind_of(check: str) -> str:
    return "integrity" if check in INTEGRITY else "quality"


REPEAT_CAP = 2.0          # one check adds at most this many times its strongest finding


def risk_score(findings: list) -> int:
    """0-100, from integrity findings only.  Each adds weight x confidence; repeats of one check count less and less
    and together at most REPEAT_CAP times its strongest hit, so a detector that misfires on every column of a large
    workbook cannot outvote one strong hit."""
    rows = [(f["check"], f["severity"], f["confidence"]) if isinstance(f, dict) else (f.check, f.severity, f.confidence)
            for f in findings]
    by_check: dict[str, list[float]] = {}
    for check, sev, conf in rows:
        if kind_of(check) == "integrity":
            by_check.setdefault(check, []).append(SEVERITY_WEIGHT[sev] * conf)
    total = 0.0
    for ws in by_check.values():
        ws.sort(reverse=True)
        total += min(sum(w / (1 + 0.6 * n) for n, w in enumerate(ws)), REPEAT_CAP * ws[0])
    return int(round(100 * (1 - np.exp(-total / 45.0))))


def verdict(score: int, findings: list[Finding]) -> str:
    top = next((s for s in SEVERITIES if any(f.severity == s and f.kind == "integrity" for f in findings)), "info")
    if score >= 70 or top == "critical":
        return "Serious concerns: at least one problem a reader would want explained."
    if score >= 40 or top == "high":
        return "Worth a closer look: strong signals that need a human check."
    if score >= 15:
        return "Minor questions: weak or ambiguous signals only."
    return "Nothing notable found by the automatic checks."


# --- helpers shared by the server, exports, letters and the apps ------------------------------------------------

class Cancelled(BaseException):
    """Raised from a progress callback when the user cancels a scan.  A BaseException on purpose: the pipeline wraps
    optional steps in `except Exception` so one failing lookup cannot sink a scan, and a cancel must not be mistaken
    for such a failure."""


# A person's ruling on one finding.  'unreviewed' is the default and is never stored.
RULINGS = ("unreviewed", "confirmed", "false_positive", "needs_raw_data")
RULING_LABELS = {"unreviewed": "Not reviewed", "confirmed": "Confirmed", "false_positive": "False positive",
                 "needs_raw_data": "Needs raw data"}

FP_BOX_CHECKS = {"image.flat"}              # several findings of one check can sit in one panel: tell them apart by place
FP_DATA_KEYS = ("axis", "column", "columns", "test", "relation", "key", "statistic", "sheet")
FP_NEAR = 30.0                               # pixels: a re-segmented panel may move a box by a few pixels
_FP_BOX = re.compile(r"^([0-9a-f]{12})-b(-?\d+)_(-?\d+)(?:-(\d+))?$")


def _fp_text(s: str) -> str:
    return " ".join(re.sub(r"[^\w.%<>=+\-]+", " ", str(s).lower()).split())[:160]


def fingerprint_basis(f) -> str:
    """What identifies a finding across scans of the same paper: the check, where it is (figure, panel, file, sheet,
    cells, quoted text) and a few measurements that tell neighbours apart.  Not the run id, the finding's number, its
    confidence or its wording, which all change between scans and engine versions."""
    d = f.public() if isinstance(f, Finding) else f
    check = d.get("check", "")
    data = d.get("data") or {}
    locs = []
    for l in d.get("locations") or []:
        parts = [str(l.get(k) or "") for k in ("figure", "panel", "file", "sheet", "cells")]
        parts.append(_fp_text(l.get("quote") or ""))
        if not any(parts) and l.get("page"):
            parts.append(f"p{l['page']}")
        locs.append("|".join(parts))
    extra = []
    for k in FP_DATA_KEYS:
        v = data.get(k)
        if isinstance(v, (str, int, bool)) or (isinstance(v, list) and all(isinstance(x, (str, int)) for x in v)):
            extra.append(f"{k}={v}")
    if check == "image.splice" and isinstance(data.get("position"), (int, float)):
        extra.append(f"pos={round(data['position'] / 5)}")
    return "\n".join([check] + sorted(locs) + extra)


def fingerprints(findings: list) -> list[str]:
    """One stable id per finding, in order.  Checks that can fire several times in one panel (FP_BOX_CHECKS) add the
    centre of their box, '-b<x>_<y>', which fp_near() matches with some tolerance.  Findings that still look
    identical get -2, -3... in the order the engine lists them."""
    import hashlib
    out, seen = [], {}
    for f in findings:
        d = f.public() if isinstance(f, Finding) else f
        h = hashlib.sha1(fingerprint_basis(d).encode()).hexdigest()[:12]
        if d.get("check") in FP_BOX_CHECKS:
            box = next((l["box"] for l in d.get("locations") or [] if l.get("box") and len(l["box"]) >= 4), None)
            if box:
                h += f"-b{round(box[0] + box[2] / 2)}_{round(box[1] + box[3] / 2)}"
        n = seen.get(h, 0) + 1
        seen[h] = n
        out.append(h if n == 1 else f"{h}-{n}")
    return out


def fp_near(a: str, b: str, tol: float = FP_NEAR) -> bool:
    """Two fingerprints name the same finding: equal, or the same boxed finding moved by at most tol pixels."""
    if a == b:
        return True
    ma, mb = _FP_BOX.match(a), _FP_BOX.match(b)
    if not ma or not mb or ma.group(1) != mb.group(1) or (ma.group(4) or "1") != (mb.group(4) or "1"):
        return False
    return ((int(ma.group(2)) - int(mb.group(2))) ** 2 + (int(ma.group(3)) - int(mb.group(3))) ** 2) <= tol * tol


def match_fingerprint(fp: str, stored, taken=()) -> "Optional[str]":
    """The key in stored (a dict or set of fingerprints) that names the same finding as fp, or None."""
    if fp in stored and fp not in taken:
        return fp
    if "-b" not in fp:
        return None
    near = [k for k in stored if k not in taken and fp_near(fp, k)]
    if not near:
        return None
    m = _FP_BOX.match(fp)
    return min(near, key=lambda k: (int(_FP_BOX.match(k).group(2)) - int(m.group(2))) ** 2 +
               (int(_FP_BOX.match(k).group(3)) - int(m.group(3))) ** 2)


def where(f) -> str:
    """Where a finding is, in words a person uses: 'F2.c, F3.a' or 'S1.xlsx, sheet “Raw”, cells B4:B20' or 'page 5'."""
    d = f.public() if isinstance(f, Finding) else f
    out = []
    for l in d.get("locations") or []:
        if l.get("file"):
            s = l["file"]
            if l.get("sheet"):
                s += f", sheet “{l['sheet']}”"
            if l.get("cells"):
                s += f", cells {l['cells']}"
        else:
            s = l.get("panel") or l.get("figure") or (f"page {l['page']}" if l.get("page") else "")
        if s and s not in out:
            out.append(s)
    return ", ".join(out[:6]) + (" and more" if len(out) > 6 else "")


def ruling_of(f: dict) -> str:
    r = f.get("ruling")
    if isinstance(r, dict):
        r = r.get("ruling")
    return r if r in RULINGS else "unreviewed"


def reviewed_score(findings: list[dict]) -> int:
    """The risk score once a person has reviewed the findings: findings they ruled a false positive no longer count."""
    return risk_score([f for f in findings if ruling_of(f) != "false_positive"])
