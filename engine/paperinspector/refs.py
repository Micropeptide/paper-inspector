"""Reference list checks: padded bibliographies, duplicates, sneaked references and (optionally, online) retracted
references.

A bibliography that runs to [89] when the text never cites past [41] is a citation-padding tell; references listed
twice are sloppiness at best.  Sneaked references are works the publisher deposited with Crossref as this paper's
references although the paper never prints them: they earn citations for someone without the paper citing them.
The online check asks Crossref (which includes the Retraction Watch database) whether a cited DOI was retracted.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Optional

from .models import Finding, Location, Paper

HEAD_RE = re.compile(r"^\s*(references|bibliography|literature cited|works cited)\s*$", re.I | re.M)
ENTRY_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})\.)\s+(.+)", re.M)
CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*[-–,]\s*\d{1,3})*)\]")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>,;]+", re.I)


def split(text: str) -> tuple[str, list[tuple[int, str]]]:
    """(body text, [(number, entry text)])."""
    heads = list(HEAD_RE.finditer(text))
    if not heads:
        return text, []
    h = heads[-1]
    body, tail = text[:h.start()], text[h.end():]
    entries: list[tuple[int, str]] = []
    ms = list(ENTRY_RE.finditer(tail))
    for i, m in enumerate(ms):
        num = int(m.group(1) or m.group(2))
        end = ms[i + 1].start() if i + 1 < len(ms) else len(tail)
        entries.append((num, " ".join(tail[m.start(3):end].split())))
    # keep the longest run that counts 1, 2, 3...: a numbered list after the bibliography (legends, protocol steps)
    # must not replace it
    runs: list[list[tuple[int, str]]] = [[]]
    for e in entries:
        if e[0] == len(runs[-1]) + 1:
            runs[-1].append(e)
        elif e[0] == 1:
            runs.append([e])
    return body, max(runs, key=len)


def cited(body: str) -> set[int]:
    out: set[int] = set()
    for m in CITE_RE.finditer(body):
        nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
        if 0 in nums:
            continue                       # an interval or index ("[0, 1]"), not a citation
        for part in re.split(r"\s*,\s*", m.group(1)):
            if re.search(r"[-–]", part):
                a, b = (int(x) for x in re.split(r"\s*[-–]\s*", part)[:2])
                if 0 < a <= b <= a + 60:
                    out.update(range(a, b + 1))
            elif part.strip().isdigit():
                out.add(int(part))
    return out


def _bracket_style(c: set[int], n: int) -> bool:
    """Bracketed numbers are the paper's citation style, not a few stray '[2]' or '[5]' in a paper that cites with
    superscripts (which PDF text turns into plain digits): they start at [1], run [1]-[3] or are numerous, and cover
    most of the range they span."""
    if not c or 1 not in c or len(c) < 0.6 * max(c):
        return False
    return len(c) >= min(10, 0.3 * n) or {1, 2, 3} <= c


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower())


def check(paper: Paper, meta: Optional[dict] = None) -> list[Finding]:
    """Offline checks of the printed list.  With `meta` (the publisher's Crossref record), also lists the deposited
    references the paper does not print (sneaked())."""
    out: list[Finding] = sneaked(paper, meta) if meta else []
    body, entries = split(paper.text)
    if len(entries) < 5:
        return out
    n = len(entries)
    c = {k for k in cited(body) if k <= n + 20}
    if _bracket_style(c, n):
        missing = [k for k, _ in entries if k not in c]
        if len(missing) >= max(5, 0.25 * n):
            out.append(Finding(
                "refs.uncited", "medium" if len(missing) >= 0.5 * n else "low",
                "Many references are never cited",
                f"{len(missing)} of {n} references are not cited in the text (highest citation in the text: "
                f"[{max(c)}]). Padding a bibliography inflates citation counts of the listed papers.",
                confidence=0.6, data={"uncited": missing[:60], "total": n, "max_cited": max(c)}))
        beyond = sorted(k for k in c if k > n)
        if beyond and n >= 0.7 * max(c):     # otherwise the list was not fully read from the PDF
            out.append(Finding("refs.missing", "low", "Citations to references that do not exist",
                               f"The text cites {', '.join(f'[{k}]' for k in beyond[:6])} but the list has {n} entries.",
                               confidence=0.5, data={"beyond": beyond}))
    dups = []
    normed = [(k, _norm(e)) for k, e in entries]
    for i in range(len(normed)):
        for j in range(i + 1, len(normed)):
            a, b = normed[i][1], normed[j][1]
            if len(a) < 25 or len(b) < 25:
                continue
            if a == b or SequenceMatcher(None, a, b).quick_ratio() > 0.93 and SequenceMatcher(None, a, b).ratio() > 0.93:
                dups.append((normed[i][0], normed[j][0]))
    if dups:
        out.append(Finding("refs.duplicate", "low", "Duplicated references",
                           "The same work is listed more than once: " +
                           ", ".join(f"[{a}] = [{b}]" for a, b in dups[:6]) + ".",
                           confidence=0.8, data={"pairs": dups}))
    return out


def _plain(s) -> str:
    """Deposited reference text without the JATS markup some publishers leave in it (<ext-link ...>, &amp;)."""
    import html
    return html.unescape(re.sub(r"<[^>]{0,400}>", " ", str(s or "")))


def _squash(s: str) -> str:
    """Letters and digits only, lower case: survives PDF line breaks, hyphenation, ligatures and punctuation."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _spaced(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]", " ", s).split())


SUPP_REFS_RE = re.compile(r"references?\s*(?:\(?\d{1,3}\)?\s*[-–]\s*\(?\d{1,3}\)?\s*)?(?:are|is)\s+(?:only\s+)?"
                          r"cited\s+(?:only\s+)?in\s+the\s+(?:supplementary|supporting|supplemental|online)|"
                          r"supplementary\s+references|references\s+for\s+(?:the\s+)?supplementary", re.I)


def _first_author(ref: dict) -> str:
    a = ref.get("author") or ""
    if not a and ref.get("unstructured"):
        m = re.match(r"\s*([A-Z][A-Za-z'\-]+)", _plain(ref["unstructured"]))
        a = m.group(1) if m else ""
    parts = re.findall(r"[A-Za-z][A-Za-z'\-]+", str(a))
    return parts[0] if parts else ""


def _ref_label(ref: dict) -> str:
    title = " ".join(_plain(ref.get("article-title") or ref.get("volume-title")).split())
    if title:
        bits = [_first_author(ref), str(ref.get("year") or ""), title, str(ref.get("journal-title") or "")]
        return ". ".join(b for b in bits if b)[:220]
    if ref.get("unstructured"):
        return " ".join(_plain(ref["unstructured"]).split())[:220]
    bits = [_first_author(ref), str(ref.get("year") or ""), str(ref.get("journal-title") or ""),
            str(ref.get("volume") or ""), str(ref.get("first-page") or ""), str(ref.get("DOI") or "")]
    return ", ".join(b for b in bits if b)[:220]


def _printed(ref: dict, sq: str, sp: str, dois_printed: bool) -> Optional[bool]:
    """Is this deposited reference in the printed list?  True / False, or None when the deposit holds too little to
    tell (for example a bare DOI, when the paper does not print DOIs)."""
    doi = _squash(str(ref.get("DOI") or ""))
    if len(doi) >= 10 and doi in sq:
        return True
    clues = False
    title = _squash(_plain(ref.get("article-title") or ref.get("volume-title")))
    if len(title) >= 20:
        clues = True
        if title[:25] in sq or title[-25:] in sq:
            return True
    un = _squash(_plain(ref.get("unstructured")))
    if len(un) >= 50:
        clues = True
        windows = [un[int(f * len(un)):int(f * len(un)) + 22] for f in (0.2, 0.45, 0.7)]
        if any(w in sq for w in windows):
            return True
    family = _spaced(_first_author(ref))
    year = str(ref.get("year") or "")[:4]
    loc = [str(ref.get(k) or "") for k in ("first-page", "volume") if str(ref.get(k) or "").strip()]
    if len(family) >= 3 and re.fullmatch(r"\d{4}", year) and loc:
        clues = True
        for m in re.finditer(r"\b" + re.escape(family) + r"\b", sp):
            window = sp[max(0, m.start() - 60):m.end() + 400]
            if re.search(r"\b" + year + r"\b", window) and any(
                    re.search(r"\b" + re.escape(_spaced(x)) + r"\b", window) for x in loc if _spaced(x)):
                return True
    if len(doi) >= 10 and dois_printed:
        clues = True
    return False if clues else None


def sneaked(paper: Paper, meta: dict) -> list[Finding]:
    """Name the references the publisher deposited with Crossref that the printed reference list does not contain.
    Only speaks when the comparison is trustworthy: the printed list was found and read (most deposited references
    are found in it), the misses are not just the end of a list the PDF reader cut short, and the paper does not say
    that some references are printed only in the supplement."""
    deposited = [r for r in (meta or {}).get("references") or [] if isinstance(r, dict)]
    if len(deposited) < 10:
        return []
    text = paper.text
    heads = list(HEAD_RE.finditer(text))
    if not heads or SUPP_REFS_RE.search(text):
        return []
    tail = text[heads[-1].end():]
    sq, sp = _squash(tail), _spaced(tail)
    _, entries = split(text)
    dois_printed = len(DOI_RE.findall(tail)) >= 0.5 * max(len(entries), 10)
    verdicts = [_printed(r, sq, sp, dois_printed) for r in deposited]
    judged = [i for i, v in enumerate(verdicts) if v is not None]
    missing = [i for i in judged if verdicts[i] is False]
    if len(judged) < 10 or (len(judged) - len(missing)) < 0.6 * len(judged):
        return []                        # the printed list was not read well enough to compare
    printed_n = len(entries) if len(entries) >= 10 else 0
    # a run of misses at the very end looks like a reference list the PDF reader cut short
    if missing and missing == judged[-len(missing):] and not (
            printed_n and len(deposited) - printed_n >= 0.8 * len(missing)):
        return []
    if printed_n:
        if len(missing) < 3 or len(deposited) - printed_n < 0.6 * len(missing):
            return []                    # same count printed and deposited: a matching problem, not extra works
    elif len(missing) < 5:
        return []
    rows = [{"key": deposited[i].get("key", ""), "doi": str(deposited[i].get("DOI") or "").lower(),
             "author": _first_author(deposited[i]), "year": str(deposited[i].get("year") or ""),
             "reference": _ref_label(deposited[i])} for i in missing]
    who = Counter(r["author"].lower() for r in rows if r["author"])
    top = who.most_common(1)
    benefit = ""
    if top and top[0][1] >= 3 and top[0][1] >= 0.4 * len(rows):
        name = next(r["author"] for r in rows if r["author"].lower() == top[0][0])
        benefit = f" {top[0][1]} of them have the same first author ({name}), the pattern seen when one person " \
                  "gains from the extra citations."
    sev = "medium" if len(rows) >= 5 else "low"
    return [Finding("refs.sneaked", sev, f"{len(rows)} deposited references are not printed in the paper",
                    f"The publisher's metadata (Crossref) lists {len(deposited)} references for this paper. These "
                    f"{len(rows)} do not appear in its printed reference list: " +
                    "; ".join(r["reference"][:90] for r in rows[:4]) + ("…" if len(rows) > 4 else "") +
                    ". Deposited references count as citations for the cited authors even though the paper never "
                    f"cites them ('sneaked references').{benefit} It can also be a metadata error by the publisher; "
                    "compare the list with the article page.", 0.6,
                    [Location(quote=r["reference"][:200]) for r in rows[:6]],
                    data={"references": rows[:60], "deposited": len(deposited), "printed": printed_n or None,
                          "missing": len(rows)})]


def retractions(paper: Paper, timeout: float = 8.0, limit: int = 60, client=None) -> list[Finding]:
    """Ask Crossref whether any DOI in the printed list was retracted (Crossref includes the Retraction Watch data).
    Network call; only runs when the user allows it.  Used when there is no publisher metadata (an uploaded PDF).
    Retractions that predate the paper's own 'Received' date are reported apart (refs.cited_after_retraction)."""
    import httpx

    from . import refcheck, reporting

    _, entries = split(paper.text)
    dois: dict[str, int] = {}
    for k, e in entries:
        m = DOI_RE.search(e)
        if m:
            dois.setdefault(m.group(0).rstrip(".)").lower(), k)
    if not dois:
        return []
    from .fetch import user_agent
    cl = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": user_agent()})
    try:
        works, _ = refcheck.crossref_works(cl, list(dois)[:limit])
    except Exception:
        return []
    finally:
        if client is None:
            cl.close()
    retracted = refcheck._retracted_refs([], works)
    for r in retracted:
        r["ref"] = dois.get(r["doi"])
    concern = [(d, w) for d, w in works.items() if any(n["type"] in refcheck.CONCERN_TYPES for n in w["notices"])
               and d not in {r["doi"] for r in retracted}]
    _, info = reporting.timeline(paper, {})
    meta = {"dates": {k: info[k] for k in ("received", "accepted") if k in info}, "type": "journal-article"}
    early, later = refcheck.split_by_retraction_date(retracted, meta)
    talks = bool(re.search(r"\bretract", body_text(paper), re.I))
    out = refcheck.retraction_findings(early, later, talks, refcheck.citing_date(meta)[0] is not None)
    if concern:
        out.append(Finding("refs.concern", "info", f"{len(concern)} cited work(s) carry an expression of concern",
                           " ".join(f"[{dois[d]}] {d}." for d, _ in concern[:6]) + " The journal has warned "
                           "readers about these works; claims resting on them deserve a second look.", 0.8,
                           [Location(quote=d) for d, _ in concern[:6]], data={"dois": [d for d, _ in concern]}))
    return out


def body_text(paper: Paper) -> str:
    """The paper without its reference list (titles of cited retracted works would otherwise count as 'mentions')."""
    return split(paper.text)[0]
