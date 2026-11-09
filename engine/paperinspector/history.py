"""Online checks of the paper's place in the record (all optional, all through free public APIs):

  sneaked references   the publisher deposited more references with Crossref than the paper prints
                       (a documented way of inflating citation counts)
  duplicate title      the same authors published a near-identical title elsewhere (duplicate publication or
                       salami slicing - or a preprint, which is excluded)
  author retractions   how many of the authors' other papers are retracted (context, not evidence)
  data accessions      GEO / SRA / BioProject numbers in the text that do not exist or are not public
"""
from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from typing import Callable, Optional

import httpx

from . import refs
from .fetch import retry_after, user_agent
from .models import Finding, Location, Paper

ACCESSION = {"GSE": "gds", "GDS": "gds", "SRP": "sra", "SRR": "sra", "SRX": "sra", "PRJNA": "bioproject"}
ACC_RE = re.compile(r"\b(GSE|GDS|SRP|SRR|SRX|PRJNA)(\d{3,9})\b")


def _params(p: dict) -> dict:
    from .refcheck import openalex_key
    k = openalex_key()
    return {**p, "api_key": k} if k else p


def check(meta: dict, paper: Paper, progress: Optional[Callable[[str, float], None]] = None
          ) -> tuple[list[Finding], dict]:
    """Each check runs on its own: one failing (a garbled answer, an unexpected error) leaves a note, not a crash."""
    progress = progress or (lambda s, f: None)
    out: list[Finding] = []
    info: dict = {"notes": []}
    with httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": user_agent()}) as cl:
        steps = [("Comparing printed and deposited references", lambda: _sneaked(meta, paper, info)),
                 ("Searching for the same title elsewhere", lambda: _duplicates(cl, meta, info)),
                 ("Counting the authors' retractions", lambda: _author_retractions(cl, meta, info)),
                 ("Checking data accession numbers", lambda: _accessions(cl, paper, info))]
        for k, (stage, fn) in enumerate(steps):
            progress(stage, k / len(steps))
            try:
                out += fn()
            except Exception as e:      # noqa: BLE001 - an online extra must never sink the scan
                info["notes"].append(f"{stage}: skipped ({type(e).__name__})")
    return out, info


def _sneaked(meta: dict, paper: Paper, info: dict) -> list[Finding]:
    deposited = meta.get("reference_count") or 0
    printed = meta.get("printed_refs")
    if not printed:
        _, entries = refs.split(paper.text)
        cited = refs.cited(paper.text)
        if len(entries) < 10 or (cited and len(entries) < 0.8 * max(cited)):
            return []                   # the PDF's reference list could not be read reliably
        printed = len(entries)
    info["references_deposited"], info["references_printed"] = deposited, printed
    if printed >= 5 and deposited - printed >= max(5, 0.2 * printed):
        return [Finding("refs.sneaked", "medium", f"{deposited - printed} more references deposited than printed",
                        f"The publisher's metadata lists {deposited} references but the paper prints {printed}. Extra "
                        "references in the metadata give citations to papers this article never cites ('sneaked "
                        "references'). It can also be a parsing problem - compare the two lists.", 0.5,
                        data={"deposited": deposited, "printed": printed})]
    return []


def _sim(a: str, b: str) -> float:
    a, b = re.sub(r"\W+", " ", a.lower()).strip(), re.sub(r"\W+", " ", b.lower()).strip()
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _duplicates(cl: httpx.Client, meta: dict, info: dict) -> list[Finding]:
    title = meta.get("title") or ""
    authors = [a for a in meta.get("authors") or [] if a.get("openalex")]
    if len(title) < 25 or not authors:
        return []
    me = (meta.get("openalex_id") or "").rsplit("/", 1)[-1]
    hits = []
    for a in {authors[0]["openalex"], authors[-1]["openalex"]}:
        aid = a.rsplit("/", 1)[-1]
        try:
            r = cl.get("https://api.openalex.org/works", params=_params({
                "filter": f"author.id:{aid}", "search": " ".join(re.findall(r"\w+", title)[:12]),
                "per-page": 8, "select": "id,doi,title,type,publication_year,primary_location"}))
            if r.status_code == 429:
                info.setdefault("notes", []).append("OpenAlex daily budget used up: duplicate-title search skipped "
                                                    "(add a free OpenAlex key in Settings)")
                return []
            works = r.json().get("results", []) if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            continue
        for w in works:
            wid = (w.get("id") or "").rsplit("/", 1)[-1]
            wt = w.get("title") or ""
            src = (((w.get("primary_location") or {}).get("source") or {}).get("type") or "")
            if wid == me or w.get("type") not in ("article", "review", "letter") or src == "repository":
                continue
            if re.search(r"^(correction|erratum|retraction|retracted|expression of concern|reply|response)", wt, re.I):
                continue
            if (w.get("doi") or "").lower().endswith((meta.get("doi") or "")) or _sim(title, wt) < 0.9:
                continue
            if wid not in {h["id"] for h in hits}:
                hits.append({"id": wid, "title": wt, "doi": w.get("doi"), "year": w.get("publication_year")})
    info["same_title_elsewhere"] = hits
    if not hits:
        return []
    return [Finding("record.duplicate_title", "medium", "The same authors published a near-identical title elsewhere",
                    " ".join(f"“{h['title'][:120]}” ({h['year']}, {h['doi'] or h['id']})." for h in hits[:3]) +
                    " Could be a duplicate publication, a split ('salami') paper, or a conference version; compare "
                    "the two.", 0.6, [Location(quote=h["doi"] or h["id"]) for h in hits[:3]], data={"works": hits})]


def _author_retractions(cl: httpx.Client, meta: dict, info: dict) -> list[Finding]:
    counts = []
    for a in (meta.get("authors") or [])[:12]:
        aid = (a.get("openalex") or "").rsplit("/", 1)[-1]
        if not aid:
            continue
        try:
            r = cl.get("https://api.openalex.org/works", params=_params({"filter": f"author.id:{aid},is_retracted:true",
                                                                         "per-page": 1, "select": "id"}))
            if r.status_code == 429:
                info.setdefault("notes", []).append("OpenAlex daily budget used up: authors' retraction counts "
                                                    "skipped (add a free OpenAlex key in Settings)")
                break
            n = r.json().get("meta", {}).get("count", 0) if r.status_code == 200 else 0
        except (httpx.HTTPError, ValueError):
            n = 0
        if n:
            counts.append((f"{a.get('given', '')} {a.get('family', '')}".strip(), n))
    info["author_retractions"] = counts
    if not counts:
        return []
    total = sum(n for _, n in counts)
    return [Finding("record.author_retractions", "low" if total < 3 else "medium",
                    f"Authors have {total} retracted paper(s) elsewhere",
                    "Retracted papers by authors of this paper (OpenAlex; names can be matched to the wrong person): "
                    + ", ".join(f"{n} ({k})" for n, k in counts[:6]) + ". Context only - it says nothing about this "
                    "paper - but integrity problems often cluster in a lab.", 0.5, data={"authors": counts})]


def _ncbi_count(cl: httpx.Client, db: str, acc: str) -> int:
    """Records NCBI holds for an accession, or -1 when it could not say.  E-utilities reports its own failures
    (rate limits, backend errors) inside a 200 answer, so only an explicit count of '0' means 'not there'."""
    for attempt in range(2):
        try:
            r = cl.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                       params={"db": db, "term": f"{acc}[ACCN]" if db == "gds" else acc, "retmode": "json",
                               "tool": "paperinspector"})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(retry_after(r, 1.0))
                continue
            if r.status_code != 200:
                return -1
            d = r.json()
            res = d.get("esearchresult") if isinstance(d, dict) else None
            if not isinstance(res, dict) or "error" in d or "ERROR" in res or "count" not in res:
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                return -1
            return int(res["count"])
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError):
            return -1
    return -1


def _accessions(cl: httpx.Client, paper: Paper, info: dict) -> list[Finding]:
    found = sorted({(m.group(1), m.group(1) + m.group(2)) for m in ACC_RE.finditer(paper.text)})[:12]
    missing, unknown = [], []
    for prefix, acc in found:
        n = _ncbi_count(cl, ACCESSION[prefix], acc)
        if n == 0:
            missing.append(acc)
        elif n < 0:
            unknown.append(acc)
        time.sleep(0.34)               # NCBI allows 3 requests a second without a key
    info["accessions"] = {"cited": [a for _, a in found], "not_found": missing, "unchecked": unknown}
    if unknown:
        info["notes"].append(f"NCBI did not answer for {', '.join(unknown)}; not checked")
    if not missing:
        return []
    return [Finding("report.accession_missing", "medium", "Data accession numbers that cannot be found",
                    f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} cited as where the data live, but "
                    "NCBI has no public record. The data may still be under embargo, the number may be mistyped, or "
                    "the data were never deposited.", 0.6, [Location(quote=a) for a in missing], data={"missing": missing})]
