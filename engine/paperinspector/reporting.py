"""Paper-level checks: the publication record and how the work is reported.

Most of these are not misconduct.  They are the things an editor, reviewer or reader wants to know: the paper was
retracted; the trial was never registered; a cell line used is known to be HeLa; the data are "available on
request"; the text cites Figure 8 but there are seven; the abstract quotes a number the results never give.
"""
from __future__ import annotations

import re
from datetime import date

from .models import Finding, Location, Paper

# Fallback list of commonly used lines that the ICLAC register (iclac.org) lists as misidentified, with what they
# really are.  When online, the full register is downloaded at run time instead (it is CC BY-NC-ND, so it is not
# shipped with the app).
MISIDENTIFIED = {
    "HEp-2": "HeLa", "Hep-2C": "HeLa", "INT 407": "HeLa", "Intestine 407": "HeLa", "KB": "HeLa",
    "Chang liver": "HeLa", "L-02": "HeLa", "HL-7702": "HeLa", "QSG-7701": "HeLa", "SMMC-7721": "HeLa",
    "BEL-7402": "HeLa / HCT-8", "BEL-7404": "HeLa", "QGY-7703": "HeLa", "WRL 68": "HeLa", "SGC-7901": "HeLa",
    "BGC-823": "HeLa", "MGC-803": "HeLa / unknown hybrid", "MKN28": "MKN74", "CNE-1": "HeLa", "CNE-2": "HeLa",
    "HONE-1": "HeLa", "SUNE1": "HeLa", "Tca8113": "HeLa", "ACC-2": "HeLa", "ACC-M": "HeLa", "SPC-A1": "HeLa",
    "GLC-82": "HeLa", "L-132": "HeLa", "HO-8910": "HeLa", "WISH": "HeLa", "Detroit-98": "HeLa",
    "MDA-MB-435": "M14 melanoma", "NCI/ADR-RES": "OVCAR-8 ovarian", "MCF-7/AdrR": "OVCAR-8 ovarian",
    "HBL-100": "unknown origin", "ECV-304": "T-24 bladder carcinoma", "ECV304": "T-24 bladder carcinoma",
    "EJ-1": "T-24 bladder carcinoma", "TSU-Pr1": "T-24 bladder carcinoma", "ARO81-1": "HT-29 colon carcinoma",
    "KAT4": "HT-29 colon carcinoma", "KAT7": "HT-29 colon carcinoma", "DRO90-1": "A-375 melanoma",
    "NPA'87": "M14 melanoma", "SK-N-MC": "Ewing sarcoma (unknown donor)", "SNB-19": "U-251 MG",
    "SK-OV-4": "C-33A cervical carcinoma",
}
SHORT_NAMES = {"KB", "WISH", "EJ-1", "KAT4", "KAT7"}
ICLAC_URL = "https://iclac.org/wp-content/uploads/Cross-Contaminations_v14_distribution.xlsx"


def _iclac(online: bool) -> dict[str, str]:
    """The full ICLAC register when available (cached for 30 days), else the built-in list."""
    import json
    import os
    import time
    from pathlib import Path

    home = Path(os.environ.get("PAPERINSPECTOR_HOME", Path.home() / ".paperinspector"))
    cache = home / "cache" / "iclac.json"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < 30 * 86400:
            return {**MISIDENTIFIED, **json.loads(cache.read_text())}
    except (OSError, ValueError):
        pass
    if not online:
        return MISIDENTIFIED
    try:
        import io

        import httpx
        import openpyxl
        from .fetch import BROWSER_UA
        r = httpx.get(ICLAC_URL, timeout=40, follow_redirects=True, headers={"User-Agent": BROWSER_UA})
        if r.status_code != 200 or r.content[:2] != b"PK":
            return MISIDENTIFIED
        wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
        found: dict[str, str] = {}
        for ws in wb.worksheets:
            if re.search(r"withdrawn|reference", ws.title, re.I):
                continue
            header = None
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() if c is not None else "" for c in row]
                if header is None:
                    if "Misidentified Cell Line" in cells and "Contaminating Cell Line" in cells:
                        header = cells
                    continue
                try:
                    name = cells[header.index("Misidentified Cell Line")]
                    real = cells[header.index("Contaminating Cell Line")]
                except (ValueError, IndexError):
                    continue
                if not name or not real:
                    continue
                base = re.split(r"\s*\(", name)[0].strip()
                syn = re.findall(r"\(([^)]+)\)", name)
                for n in [base] + [x.strip() for part in syn for x in part.split(",")]:
                    if 2 <= len(n) <= 30 and re.search(r"[A-Za-z]", n) and \
                            not n.lower().startswith(("see ", "also", "synonym", "derivative")):
                        found[n] = real
        if found:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(found))
            return {**MISIDENTIFIED, **found}
    except Exception:
        pass
    return MISIDENTIFIED


CELL_WORD = re.compile(r"\b(cell lines?|cells|cell culture)\b", re.I)


def _find(text: str, pattern: str, flags=re.I):
    return re.search(pattern, text, flags)


def _quote(text: str, m, pad=60) -> str:
    a, b = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return " ".join(text[a:b].split())


def _page(paper: Paper, snippet: str) -> int:
    for i, p in enumerate(paper.pages):
        if snippet[:40] in " ".join(p.split()):
            return i + 1
    return 0


RETRACTED = {"retraction", "removal", "withdrawal", "partial_retraction"}


def _kind(notice: dict) -> str:
    """Crossref update type, normalised: 'Expression-of-Concern' and 'expression_of_concern' are one kind."""
    return (notice.get("type") or "").lower().replace("-", "_").replace(" ", "_")


def record(meta: dict) -> list[Finding]:
    """Retractions, expressions of concern and corrections of the paper itself."""
    out = []
    notices = meta.get("notices") or []
    types = {_kind(n) for n in notices}
    if meta.get("is_retracted") or types & RETRACTED:
        n = next((x for x in notices if _kind(x) in RETRACTED), {})
        what = {"withdrawal": "withdrawal", "partial_retraction": "partial retraction"}.get(_kind(n), "retraction")
        partial = what == "partial retraction"
        rw = " It is recorded in the Retraction Watch database." if n.get("source") == "retraction-watch" else ""
        out.append(Finding("record.retracted", "high" if partial else "critical",
                           "This paper has been " + ("partly retracted" if partial else
                                                     "withdrawn" if what == "withdrawal" else "retracted"),
                           f"A {what} notice exists{(' (' + n['date'] + ')') if n.get('date') else ''}"
                           f"{(': ' + n['doi']) if n.get('doi') else ''}.{rw} "
                           + ("The retracted parts should not be relied on." if partial else
                              "Its findings should not be relied on."),
                           0.99, data={"notices": notices}))
    if "expression_of_concern" in types:
        out.append(Finding("record.concern", "high", "Expression of concern", "The publisher has issued an expression "
                           "of concern about this paper.", 0.95, data={"notices": notices}))
    corr = [n for n in notices if _kind(n) in ("correction", "erratum", "corrigendum", "addendum", "clarification")]
    if corr:
        out.append(Finding("record.correction", "info", f"{len(corr)} correction notice(s)",
                           "The paper has been corrected: " + "; ".join(f"{n['type']} {n.get('date', '')}".strip()
                                                                        for n in corr[:4]) + ". Check the corrected "
                           "version; corrections sometimes replace figures that were themselves problematic.",
                           0.9, data={"notices": corr}))
    if meta.get("special_issue"):
        dates = meta.get("dates") or {}
        rec, acc = (_meta_day(dates.get(k)) for k in ("received", "accepted"))
        out.append(special_issue_finding(str(meta["special_issue"]), "the publisher's metadata",
                                         (acc - rec).days if rec and acc else None))
    return out


def _meta_day(v):
    if not v:
        return None
    v = str(v)
    if re.match(r"\d{4}-\d{2}-\d{2}", v):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return _parse_date(v)


def special_issue_finding(name: str, where: str, days: int | None = None) -> Finding:
    """A paper in a special (guest-edited) issue.  Context, not a problem: most special issues are fine, but they
    have been the main route for paper-mill articles, so together with very fast acceptance it is worth a look."""
    name = " ".join(name.split()).strip(" .:;\"'“”")[:160]
    fast = days is not None and 0 <= days <= 21
    return Finding("record.special_issue", "low" if fast else "info",
                   "Special-issue paper accepted in " + f"{days} day(s)" if fast else "Published in a special issue",
                   (f"According to {where}, the paper is part of a special issue" +
                    (f" (“{name}”)" if name else "") +
                    ". Most special issues are run properly. They have also been the main route for paper-mill "
                    "articles, because guest editors can control who reviews the papers; several publishers have "
                    "retracted whole special issues for this reason. Check who edited the issue.") +
                   (f" This paper was accepted {days} day(s) after it was received, which makes a careful review "
                    "less likely." if fast else ""),
                   0.7 if fast else 0.8, data={"name": name, "source": where, "days_to_accept": days})


SPECIAL_TEXT_RE = re.compile(
    r"(?:belongs to|is part of|part of (?:the|a|an))\s+(?:the\s+)?(?:virtual\s+)?(special issue|themed issue|"
    r"thematic issue|special collection|research topic)\b\s*(?:on|entitled|titled|called)?\s*:?\s*"
    r"[“\"']?([^\n.”\"']{3,140})", re.I)


def special_issue_text(paper: Paper) -> list[Finding]:
    """Front-matter statements such as "This article belongs to the Special Issue ..." (first two pages only, so a
    cited title in the reference list never counts)."""
    front = " ".join(" ".join(paper.pages[:2]).split())
    m = SPECIAL_TEXT_RE.search(front)
    if not m:
        return []
    _, info = timeline(paper, {})
    f = special_issue_finding(m.group(2), "the paper's own front matter", info.get("days_to_accept"))
    f.locations = [Location(page=_page(paper, m.group(0)), quote=m.group(0)[:200])]
    return [f]


DATE_RE = r"(\d{1,2}\s+[A-Z][a-z]+\.?\s+\d{4}|[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})"


def _strptime(s: str, fmt: str):
    from datetime import datetime
    try:
        return datetime.strptime(s, fmt).date()
    except ValueError:
        return None


def _parse_date(s: str):
    s = s.replace(",", "").replace(".", "").strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%Y-%m-%d", "%d/%m/%Y"):
        d = _strptime(s, fmt)
        if d:
            return d
    return None


def _slash(s: str) -> dict | None:
    """Both readings of a numeric slash date: 5/1/2021 is 1 May in the US and 5 January elsewhere."""
    if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", s.strip()):
        return None
    return {"us": _strptime(s.strip(), "%m/%d/%Y"), "dmy": _strptime(s.strip(), "%d/%m/%Y")}


def _resolve_slash(slash: dict[str, dict]) -> tuple[dict, bool]:
    """Pick one convention for all of a paper's slash dates.  A date that only parses one way settles it; otherwise
    the convention that keeps acceptance after submission.  (dates, ambiguous)"""
    def pick(conv):
        return {k: v[conv] for k, v in slash.items() if v[conv]}
    us_only = any(v["us"] and not v["dmy"] for v in slash.values())
    dmy_only = any(v["dmy"] and not v["us"] for v in slash.values())
    if us_only != dmy_only:
        return pick("us" if us_only else "dmy"), False
    us, dmy = pick("us"), pick("dmy")
    if us == dmy:
        return us, False

    def ok(g):
        return "received" not in g or "accepted" not in g or g["accepted"] >= g["received"]

    def gap(g):
        return (g["accepted"] - g["received"]).days if "received" in g and "accepted" in g else None
    if ok(us) and not ok(dmy):
        return us, False
    if ok(dmy) and not ok(us):
        return dmy, False
    return dmy, gap(us) != gap(dmy)


def timeline(paper: Paper, meta: dict) -> tuple[list[Finding], dict]:
    text = " ".join(paper.text.split())[:20000]
    got, slash, failed = {}, {}, set()
    for key, word in (("received", r"Received"), ("revised", r"Revised|Received in revised form"),
                      ("accepted", r"Accepted"), ("published", r"Published(?: online)?")):
        m = re.search(rf"\b(?:{word})\s*:?\s*(?:on\s+)?{DATE_RE}", text)
        if not m:
            continue
        sl = _slash(m.group(1))
        if sl:
            slash[key] = sl
            continue
        d = _parse_date(m.group(1))
        if d:
            got[key] = d
        else:
            failed.add(key)
    ambiguous = False
    meta_dates = meta.get("dates") or {}
    if slash:
        resolved, ambiguous = _resolve_slash(slash)
        for k, d in resolved.items():
            # an ambiguous text date gives way to the publisher's own record
            if not (ambiguous and k in ("received", "accepted") and meta_dates.get(k)):
                got[k] = d
        failed |= set(slash) - set(resolved)
    src = {k: "text" for k in got}
    for key in ("received", "accepted"):
        v = meta_dates.get(key)
        if v and key not in got:
            d = _parse_date(v) if not re.match(r"\d{4}-\d{2}-\d{2}", v or "") else date.fromisoformat(v[:10])
            if d:
                got[key] = d
                src[key] = "meta"
    ambiguous = ambiguous and any(src.get(k) == "text" and k in slash for k in ("received", "accepted"))
    out = []
    info = {k: v.isoformat() for k, v in got.items()}
    if ambiguous:
        info["date_format_ambiguous"] = True
    # a date the text gives but that could not be read must not be paired with the other one from Crossref
    mixed = src.get("received") != src.get("accepted") and failed & {"received", "accepted"}
    if "received" in got and "accepted" in got and not ambiguous and not mixed:
        days = (got["accepted"] - got["received"]).days
        info["days_to_accept"] = days
        if days < 0:
            out.append(Finding("report.timeline", "low", "Accepted before it was received",
                               f"The stated dates put acceptance {-days} days before submission: a typo, or careless "
                               "editorial handling.", 0.6, data=info))
        elif days <= 14 and (meta.get("type") or "journal-article") == "journal-article":
            out.append(Finding("record.fast_acceptance", "medium" if days <= 7 else "low",
                               f"Accepted {days} day(s) after submission",
                               "Peer review that takes a week or less is unusual for a research article. Very fast "
                               "acceptance is common in paper-mill special issues; it also happens for invited or "
                               "fast-tracked papers.", 0.5, data=info))
    return out, info


STATEMENTS = {
    "data": (r"data availability|availability of data|data and materials availability|data are available|data access",
             "No data availability statement"),
    "coi": (r"conflicts? of interest|competing interests?|declaration of interests?|disclosures?",
            "No conflict-of-interest statement"),
    "funding": (r"\bfunding\b|funded by|supported by (?:a |the )?grant|grant (?:no|number)|acknowledg", "No funding statement"),
}
ANIMAL = r"\b(mice|mouse|rats?|rabbits?|zebrafish|pigs?|dogs?|primates?|macaques?|in vivo)\b"
HUMAN = r"\b(patients|participants|volunteers|human subjects|biopsies|blood samples from)\b"
# not a bare "licence" (every open-access footer has one) or "approved by" (the FDA, all authors)
ETHICS = r"ethic|IACUC|institutional review board|\bIRB\b|animal care and use|informed consent|" \
         r"approved by (?:the |an? )?[^.]{0,60}?(?:ethic|committee|board|IACUC|IRB|authorit|ministry)|" \
         r"Declaration of Helsinki|ARRIVE|Home Office|(?:project|animal|personal|establishment) licen[cs]e"
TRIAL = r"randomi[sz]ed(?:,)? (?:controlled |clinical |double-blind )*trial|\bRCT\b"
REG_ID = r"\b(NCT\d{8}|ISRCTN\d{8}|ChiCTR[-A-Z0-9]{6,}|ACTRN\d{14}|EUCTR[-\d]+|DRKS\d{8}|CTRI/\d{4}/\d+/\d+|" \
         r"jRCT\w+|UMIN\d{9}|IRCT\w+|KCT\d{7}|PACTR\d+|TCTR\d{11}|SLCTR/\d{4}/\d+|RBR-\w{6,8}|NTR\d{3,5}|" \
         r"(?:EudraCT|EU CT|CTIS)\s*(?:number|no\.?|#)?\s*:?\s*\d{4}-\d{6}-\d{2}(?:-\d{2})?)\b"

# Registry numbers with a fixed shape: prefix and digit count.  A number that has the prefix but the wrong length
# is a typo, or a number that was never issued.
TRIAL_FORMATS = {"NCT": 8, "ISRCTN": 8, "ACTRN": 14, "DRKS": 8, "UMIN": 9, "KCT": 7, "PACTR": 15, "TCTR": 11}
TRIAL_NAMES = {"NCT": "ClinicalTrials.gov", "ISRCTN": "ISRCTN", "ACTRN": "ANZCTR", "DRKS": "DRKS", "UMIN": "UMIN-CTR",
               "KCT": "CRIS (Korea)", "PACTR": "PACTR", "TCTR": "Thai Clinical Trials Registry", "ChiCTR": "ChiCTR",
               "EudraCT": "EudraCT"}


def trial_id_problems(text: str) -> list[tuple[str, str]]:
    """[(as written, what is wrong)] for registry numbers whose shape is impossible.  A number split by a PDF line
    break ("NCT0123 4567") is joined first, so it is not reported."""
    out: list[tuple[str, str]] = []
    seen = set()
    for m in re.finditer(r"\b(NCT|ISRCTN|ACTRN|DRKS|UMIN|KCT|PACTR|TCTR)\s?(\d+)(?:[ -](\d+))?", text):
        pre, d1, d2 = m.group(1), m.group(2), m.group(3) or ""
        want = TRIAL_FORMATS[pre]
        if len(d1) == want or (d2 and len(d1) < want and len(d1) + len(d2) == want):
            continue
        if not want - 3 <= len(d1) <= want + 3 or (pre == "KCT" and len(d1) < 6):
            continue                     # too far off to be meant as a registry number
        raw = pre + d1
        if raw not in seen:
            seen.add(raw)
            out.append((raw, f"{TRIAL_NAMES[pre]} numbers are {pre} followed by {want} digits; this one has "
                             f"{len(d1)}"))
    for m in re.finditer(r"\bChiCTR\s?-?\s?([A-Z]{2,4})?\s?-?\s?(\d+)", text):
        letters, digits = m.group(1), m.group(2)
        ok = (not letters and len(digits) == 10) or (letters and len(digits) == 8)
        raw = " ".join(m.group(0).split())
        if not ok and 6 <= len(digits) <= 13 and raw not in seen:
            seen.add(raw)
            out.append((raw, "ChiCTR numbers are ChiCTR followed by 10 digits (or, for older trials, a letter code "
                             "and 8 digits)"))
    for m in re.finditer(r"\bEudraCT\s*(?:number|no\.?|#)?\s*:?\s*(\d[\d-]{6,16}\d)", text):
        v = m.group(1)
        if not re.fullmatch(r"\d{4}-\d{6}-\d{2}", v) and v not in seen:
            seen.add(v)
            out.append(("EudraCT " + v, "EudraCT numbers have the form YYYY-NNNNNN-CC"))
    return out


def statements(paper: Paper, meta: dict | None = None) -> list[Finding]:
    """`meta` (optional): the publisher's record; when it already names a special issue, the text is not searched
    for one again."""
    text = " ".join(paper.text.split())
    low = text.lower()
    out = []
    if not (meta or {}).get("special_issue"):
        out += special_issue_text(paper)
    bad_ids = trial_id_problems(text)
    if bad_ids:
        out.append(Finding("report.trial_id_format", "low",
                           f"Trial registration number{'s' if len(bad_ids) > 1 else ''} with an impossible format",
                           " ".join(f"{raw}: {why}." for raw, why in bad_ids[:4]) + " It may be a typo, or a number "
                           "that was never issued. Look it up in the registry before relying on the registration.",
                           0.6, [Location(page=_page(paper, raw), quote=raw) for raw, _ in bad_ids[:6]],
                           data={"ids": [raw for raw, _ in bad_ids]}))
    if len(text) < 3000:
        return out
    for key, (pat, title) in STATEMENTS.items():
        if not re.search(pat, text, re.I):
            out.append(Finding(f"report.missing_{key}", "info", title,
                               "No such statement was found in the text. Many journals require one.", 0.4))
    m = re.search(r"(available|provided|obtained)\s+(from the (corresponding )?authors?\s+)?(up)?on\s+(reasonable\s+)?request",
                  text, re.I)
    if m:
        out.append(Finding("report.data_on_request", "low", "Data “available on request”",
                           "The data are only available on request. Studies find that most such requests go "
                           "unanswered, so the claims cannot be checked independently.", 0.6,
                           [Location(page=_page(paper, _quote(text, m)), quote=_quote(text, m))]))
    # a passing mention ("patients with this disease...") does not make the paper human or animal research
    animal = len(re.findall(ANIMAL, text, re.I)) >= 3
    human = len(re.findall(HUMAN, text, re.I)) >= 3
    if (animal or human) and not re.search(ETHICS, text, re.I):
        out.append(Finding("report.no_ethics", "medium", "No ethics approval mentioned",
                           f"The paper works with {'animals' if animal else 'people'} but no ethics approval, "
                           "committee or consent statement was found.", 0.55))
    if re.search(TRIAL, text, re.I) and not re.search(REG_ID, text):
        out.append(Finding("report.no_registration", "medium", "Clinical trial without a registration number",
                           "The paper describes a randomised trial but gives no registry number (NCT, ISRCTN, "
                           "ChiCTR…). Unregistered trials can change their outcomes after seeing the data.", 0.6))
    n = len(re.findall(r"data not shown", low))
    if n >= 2:
        out.append(Finding("report.data_not_shown", "low", f"“Data not shown” used {n} times",
                           "Claims supported only by unshown data cannot be checked.", 0.5))
    sem = re.search(r"mean\s*(±|\+/-)\s*s\.?e\.?m\.?", text, re.I)
    small_n = re.search(r"\bn\s*=\s*[23]\b", text)
    if sem and small_n:
        out.append(Finding("report.sem_small_n", "low", "SEM error bars with n = 2-3",
                           "Error bars are SEM and some groups have n of 2 or 3. SEM shrinks with n and hides the "
                           "spread of the data; with so few points, showing the individual values is better.", 0.45,
                           [Location(page=_page(paper, _quote(text, small_n)), quote=_quote(text, small_n, 40))]))
    return out


def cell_lines(paper: Paper, online: bool = False, client=None) -> list[Finding]:
    """Cell lines that ICLAC lists as misidentified; online, also lines that Cellosaurus marks as contaminated or
    misidentified (Cellosaurus covers more lines than the ICLAC register)."""
    text = " ".join(paper.text.split())
    if not CELL_WORD.search(text):
        return []
    hits = {}
    iclac = _iclac(online)
    for name, real in iclac.items():
        if not real or len(name) < 2:
            continue
        pat = r"(?<![\w-])" + re.escape(name).replace(r"\ ", r"[\s-]?").replace(r"\-", r"[\s-]?") + r"(?![\w-])"
        short = len(name) <= 4 or name in SHORT_NAMES
        m = re.search(pat, text, 0 if short else re.I)
        if not m:
            continue
        ctx = text[max(0, m.start() - 80):m.end() + 80]
        if short and not CELL_WORD.search(ctx):
            continue
        if re.search(re.escape(real.split()[0]), ctx, re.I):
            continue                     # the paper already says what the line really is
        hits[name] = (real, m)
    sources = {k: "ICLAC" for k in hits}
    if online:
        known = {_cl_key(n) for n in iclac}
        for name, (real, m) in _cellosaurus_hits(text, known, client).items():
            if name not in hits:
                hits[name] = (real, m)
                sources[name] = "Cellosaurus"
    if not hits:
        return []
    by = sorted(set(sources.values()))
    return [Finding("report.misidentified_cells", "medium", "Uses cell lines known to be misidentified",
                    f"These lines are listed by {' and '.join(by)} as misidentified or contaminated: " +
                    "; ".join(f"{k} is actually {v}" for k, (v, _) in list(hits.items())[:8]) +
                    ". Results attributed to the named tissue may come from a different cancer. Check whether the "
                    "authors authenticated the lines (STR profiling).", 0.75,
                    [Location(page=_page(paper, _quote(text, m)), quote=_quote(text, m, 40))
                     for _, (_, m) in list(hits.items())[:6]],
                    data={"lines": {k: v for k, (v, _) in hits.items()}, "sources": sources})]


def _cl_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# Lines named in "<name> cells": letters and digits together (SGC-7901, MDA-MB-435, HEK293T), at most 20 characters
CL_CANDIDATE_RE = re.compile(r"(?<![\w/-])([A-Z][A-Za-z]{0,7}(?:[- ]?[A-Z]{1,4})?[- ]?\d{1,5}[A-Za-z]{0,3}"
                             r"(?:[-/][A-Za-z0-9]{1,5})?)\s+(?:cells?|cell lines?)\b")
# common lines with no Cellosaurus problem worth a lookup, and immune-cell words that are not lines at all
CL_SKIP = {_cl_key(x) for x in (
    "HEK293 HEK293T HEK-293 HEK-293T A549 MCF-7 MCF7 HepG2 Hep G2 U2OS HCT116 HCT-116 SW480 SW620 HT-29 HT29 "
    "THP-1 RAW264.7 RAW 264.7 NIH3T3 3T3 COS-7 COS7 PC-3 PC3 DU145 LNCaP K562 HL-60 SH-SY5Y Caco-2 MDA-MB-231 "
    "MDA-MB-468 T47D SKOV3 SK-OV-3 BEAS-2B HaCaT A375 B16 B16F10 4T1 CT26 MC38 LLC Hep3B Huh7 Huh-7 PANC-1 "
    "U87 U-87 U251 U-251 HeLa Jurkat Vero CHO A431 MG-63 Saos-2 C2C12 H9c2 PC12 HUVEC Sf9 S2").split()} | \
    {_cl_key(x) for x in "CD4 CD8 CD3 CD19 CD34 CD14 CD11b Th1 Th2 Th17 NK1 B220 iPS1 H1 H9".split()}
CELLOSAURUS = "https://api.cellosaurus.org/search/cell-line"


def _cellosaurus_hits(text: str, skip: set[str], client=None, limit: int = 10) -> dict:
    """{name: (what it really is, match)} for lines named in the text that Cellosaurus marks 'Problematic cell line:
    Contaminated' or 'Misidentified'.  Exact name or synonym matches only; one polite request at a time; gives up
    quietly after two failures (offline, or the service is down)."""
    counts: dict[str, list] = {}
    for m in CL_CANDIDATE_RE.finditer(text):
        name = m.group(1).strip()
        key = _cl_key(name)
        if len(key) < 3 or key in skip or key in CL_SKIP or not re.search(r"[a-z]", key) or not re.search(r"\d", key):
            continue
        if " " in name and sum(c.isupper() for c in name) < 2:
            continue                     # "Figure 2 cells", "Day 3 cells": a word and a number, not a line
        counts.setdefault(key, [name, m, 0])[2] += 1
    if not counts:
        return {}
    import httpx

    from .fetch import user_agent
    cl = client or httpx.Client(timeout=12, follow_redirects=True, headers={"User-Agent": user_agent()})
    found: dict = {}
    cache: dict[str, object] = {}
    fails = 0
    try:
        for key, (name, m, _) in sorted(counts.items(), key=lambda kv: -kv[1][2])[:limit]:
            if fails >= 2:
                break
            if key in cache:
                continue
            q = name.replace('"', "")
            try:
                r = cl.get(CELLOSAURUS, params={"q": f'id:"{q}" OR sy:"{q}"', "fields": "id,sy,cc",
                                                "format": "json", "rows": 50})
                data = r.json() if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                data = None
            if data is None:
                fails += 1
                continue
            cache[key] = data
            real = _problematic(data, key)
            if not real:
                continue
            ctx = text[max(0, m.start() - 80):m.end() + 80]
            if re.search(re.escape(real.split()[0]), ctx, re.I):
                continue                 # the paper already says what the line really is
            found[name] = (real, m)
    finally:
        if client is None:
            cl.close()
    return found


def _problematic(data, key: str) -> str:
    """What a Cellosaurus entry named exactly `key` (identifier or synonym) really is, when it is flagged as
    contaminated or misidentified; '' otherwise."""
    try:
        lines = data["Cellosaurus"]["cell-line-list"]
    except (KeyError, TypeError):
        return ""
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        names = {_cl_key(n.get("value", "")) for n in line.get("name-list") or [] if isinstance(n, dict)}
        if key not in names:
            continue
        for c in line.get("comment-list") or []:
            if not isinstance(c, dict) or c.get("category") != "Problematic cell line":
                continue
            v = str(c.get("value") or "")
            if not re.match(r"\s*(Contaminated|Misidentified)\b", v):
                continue                 # 'Possibly contaminated', 'Partially' ...: not firm enough
            m = re.search(r"[Ss]hown to be (?:an? )?(.+?)(?: derivative| cell line|\s*\(|\.)", v)
            return (m.group(1) if m else v.split(".")[0]).strip()[:80] or "another cell line"
    return ""


# ClinicalTrials.gov's public API (v2, no key).  Other registries have no comparable free API.
CTGOV = "https://clinicaltrials.gov/api/v2/studies/"
MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|" \
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
RECRUIT_RE = re.compile(rf"\b(?:recruited|enrolled|enrol(?:l)?ment|recruitment|randomi[sz]ed|conducted|carried out)"
                        rf"\b[^.]{{0,80}}?\b(?:between|from)\s+((?:{MONTH}\s+)?(?:19|20)\d\d)\s+(?:and|to|until|"
                        rf"through|-|–)\s+((?:{MONTH}\s+)?(?:19|20)\d\d)", re.I)


def _month_year(s: str, latest: bool):
    """'March 2019', 'Sept. 2019' or '2019' -> the first (or, with `latest`, the last) day it can mean."""
    from calendar import monthrange
    s = s.replace(".", "").strip()
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", s)
    if m:
        d = _strptime(m.group(1)[:3].title() + " " + m.group(2), "%b %Y")
        if d is None:
            return None
        return d.replace(day=monthrange(d.year, d.month)[1]) if latest else d
    if re.fullmatch(r"\d{4}", s):
        return date(int(s), 12, 31) if latest else date(int(s), 1, 1)
    return None


def _registry_day(v: str, latest: bool):
    """ClinicalTrials.gov dates: '2020-02-21' or '2020-02'."""
    from calendar import monthrange
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", (v or "").strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if m.group(3):
        return date(y, mo, int(m.group(3)))
    return date(y, mo, monthrange(y, mo)[1] if latest else 1)


def trials(paper: Paper, meta: dict | None = None, online: bool = False, client=None) -> list[Finding]:
    """Look up the paper's own ClinicalTrials.gov registration (online only) and compare its dates with the paper:
      not found              the number does not exist (often a typo)                         low
      registered late        registered after the paper was submitted, or after recruitment   medium
                             ended by the paper's own account (outcomes could be chosen after the data)
      retrospective          an interventional trial registered more than 6 months after it   low
                             started, by the registry's own dates
    Only numbers the text presents as a registration are checked, and only when the paper names at most three
    (reviews and meta-analyses list many trials that are not their own)."""
    if not online:
        return []
    from .refs import split
    body = " ".join(split(paper.text)[0].split())
    ids = list(dict.fromkeys(re.findall(r"\bNCT\s?(\d{8})\b", body)))
    if not ids or len(ids) > 3:
        return []
    own = []
    for d in ids:
        m = re.search(r"\bNCT\s?" + d, body)
        ctx = body[max(0, m.start() - 160):m.end() + 60]
        if re.search(r"regist|clinicaltrials\.gov|identifier|trial (?:number|no\.?|id)", ctx, re.I):
            own.append("NCT" + d)
    if not own:
        return []
    _, tl = timeline(paper, meta or {})
    received = date.fromisoformat(tl["received"]) if tl.get("received") and not tl.get("date_format_ambiguous") \
        else None
    window = RECRUIT_RE.search(body)
    recruit_end = _month_year(window.group(2), latest=True) if window else None

    import httpx

    from .fetch import user_agent
    cl = client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": user_agent()})
    out, unchecked = [], []
    try:
        for nct in own:
            try:
                r = cl.get(CTGOV + nct, params={"fields": "NCTId,BriefTitle,StudyFirstSubmitDate,StartDate,"
                                                          "StudyType"})
            except httpx.HTTPError:
                unchecked.append(nct)
                continue
            if r.status_code == 404:
                out.append(Finding("report.trial_not_found", "low", f"{nct} is not in ClinicalTrials.gov",
                                   f"The paper gives {nct} as its registration, but ClinicalTrials.gov has no study "
                                   "with that number. It may be a typo; if the trial is not registered under "
                                   "another number, its outcomes could have been chosen after the data were in.",
                                   0.6, [Location(page=_page(paper, nct), quote=nct)], data={"id": nct}))
                continue
            try:
                ps = r.json()["protocolSection"] if r.status_code == 200 else None
            except (ValueError, KeyError, TypeError):
                ps = None
            if not isinstance(ps, dict):
                unchecked.append(nct)
                continue
            status, design = ps.get("statusModule") or {}, ps.get("designModule") or {}
            reg = _registry_day(status.get("studyFirstSubmitDate", ""), latest=False)
            start = _registry_day((status.get("startDateStruct") or {}).get("date", ""), latest=True)
            info = {"id": nct, "registered": reg.isoformat() if reg else None,
                    "start": (status.get("startDateStruct") or {}).get("date"),
                    "type": design.get("studyType"), "title": (ps.get("identificationModule") or {}).get("briefTitle")}
            if not reg:
                continue
            loc = [Location(page=_page(paper, nct), quote=nct)]
            if received and reg > received:
                out.append(Finding("report.trial_registered_late", "medium",
                                   f"{nct} was registered after the paper was submitted",
                                   f"ClinicalTrials.gov first received {nct} on {reg.isoformat()}, but the paper was "
                                   f"received on {received.isoformat()}. A trial registered after its results were "
                                   "written up could have chosen its outcomes after seeing the data.", 0.75, loc,
                                   data={**info, "paper_received": received.isoformat()}))
            elif recruit_end and (reg - recruit_end).days > 30:
                out.append(Finding("report.trial_registered_late", "medium",
                                   f"{nct} was registered after recruitment ended",
                                   f"The paper says recruitment ran until {window.group(2)}, but ClinicalTrials.gov "
                                   f"first received {nct} on {reg.isoformat()}. A trial registered after recruitment "
                                   "could have chosen its outcomes after seeing the data.", 0.7, loc,
                                   data={**info, "recruitment_end": window.group(2)}))
            elif start and design.get("studyType") == "INTERVENTIONAL" and (reg - start).days > 182:
                months = (reg - start).days // 30
                out.append(Finding("report.trial_retrospective", "low", f"{nct} was registered after it started",
                                   f"By ClinicalTrials.gov's own dates the trial started in {info['start']} and was "
                                   f"registered on {reg.isoformat()}, about {months} months later. Trials should be "
                                   "registered before the first patient is enrolled; late registration leaves room to "
                                   "change the planned outcomes.", 0.65, loc, data=info))
    finally:
        if client is None:
            cl.close()
    if unchecked:
        out.append(Finding("report.trial_unchecked", "info", "Trial registration could not be checked",
                           f"ClinicalTrials.gov did not answer for {', '.join(unchecked)}. Nothing here is evidence "
                           "about the paper; re-run later.", 0.9, data={"ids": unchecked}))
    return out


# Supplementary Figure 9 and Extended Data Fig. 9 are not main figures; "Fig. 10." ends a sentence
MAIN_FIG_RE = re.compile(r"(?<!Supplementary )(?<!Supplemental )(?<!Suppl\. )(?<!Suppl )(?<!Supp\. )(?<!Supp )"
                         r"(?<!Extended Data )(?<!Appendix )\bFig(?:ure)?s?\.?\s*(\d{1,2})(?!\d|\.\d)")


def consistency(paper: Paper, n_figures: int, n_supp_figures: int) -> list[Finding]:
    text = " ".join(paper.text.split())
    out = []
    main = [int(x) for x in MAIN_FIG_RE.findall(text)]
    if n_figures and main and max(main) > n_figures + 0 and max(main) <= 30:
        out.append(Finding("report.figure_count", "low", f"Text cites Figure {max(main)}, but {n_figures} figures were found",
                           "Either a figure is missing from the paper, a citation is wrong, or the figure extraction "
                           "missed one (check by eye).", 0.35, data={"max_cited": max(main), "found": n_figures}))
    # numbers in the abstract that never appear again
    m = re.search(r"\babstract\b(.{200,2500}?)\b(introduction|background|keywords|key words)\b", text, re.I)
    if m:
        abstract, rest = m.group(1), text[m.end():]
        nums = set(re.findall(r"\b\d+(?:\.\d+)?\s?%", abstract)) | set(re.findall(r"\b\d+\.\d+\b", abstract))
        missing = sorted(n for n in nums if n.replace(" ", "") not in rest.replace(" ", ""))
        if len(missing) >= 2:
            out.append(Finding("report.abstract_numbers", "low",
                               "Numbers in the abstract not found in the paper body",
                               f"The abstract gives {', '.join(missing[:6])}, which do not appear anywhere else in "
                               "the text. Often a rounding or a value only shown in a figure; sometimes an abstract "
                               "that no longer matches the results.", 0.4, data={"numbers": missing}))
    return out
