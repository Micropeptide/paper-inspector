"""The reference list, checked against the scholarly record (OpenAlex and Crossref).

  - every reference resolved: DOIs that do not exist and references no database knows (a sign of fabricated,
    often AI-generated, citations - or just typos)
  - retracted references
  - self-citation: references sharing an author with the paper
  - citation concentration: one outside author receiving a large share of the citations (coercive citation)
  - journal self-citation: a large share of references to the journal publishing the paper
  - off-topic references: works from fields unrelated to the paper (citation padding)
  - a cited DOI that belongs to a different paper than the cited title (a real DOI pasted onto an invented or
    wrong reference)
  - references that were already retracted when this paper was submitted (Crossref "updated-by" records, which
    include the Retraction Watch database), kept apart from those retracted later
  - the paper's own record: retraction, correction and concern notices from Crossref and Retraction Watch, and
    whether the paper sits in a special (guest-edited) issue.  These are added to `meta` for reporting.record().

PubPeer: there is no keyless public API (every endpoint asks for a developer key), so `pubpeer()` is an optional
hook that only runs when a key is saved in Settings under keys.pubpeer.  Without one nothing is sent.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Callable, Optional

import httpx

from .fetch import doi_path, retry_after, user_agent
from .models import Finding, Location

SELECT = "id,doi,title,publication_year,is_retracted,authorships,primary_location,topics"
NET_ERRORS = (httpx.HTTPError, httpx.InvalidURL, ValueError)
BUDGET = 300.0          # seconds for the sequential Crossref checks; what is left over is reported as unchecked
FAILED = object()       # a lookup that could not be made - never evidence that a reference does not exist
SKIP = object()         # a reference not expected to be in Crossref at all
CR_BATCH = 40           # DOIs per Crossref list query (one request at a time, as Crossref's public pool asks)
RETRACTION_TYPES = {"retraction", "removal", "withdrawal", "partial_retraction"}
CONCERN_TYPES = {"expression_of_concern"}
# how long before the citing paper's date a retraction must be before we say the authors could have known:
# literature searches happen before submission, and notices take time to reach the databases
GRACE_DAYS = {"received": 60, "accepted": 180, "published": 365}


def _client():
    return httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": user_agent()})


def contact_email() -> str:
    """An e-mail the user chose to share with Crossref (Settings: email).  Empty unless they set one: no address
    is ever sent otherwise."""
    try:
        from . import settings
        s = settings.load()
        v = s.get("email") or s.get("contact_email") or ""
        return v.strip() if isinstance(v, str) and "@" in v and len(v) < 200 else ""
    except Exception:
        return ""


def _cr_params(p: dict) -> dict:
    mail = contact_email()
    return {**p, "mailto": mail} if mail else p


def notice_type(t: str) -> str:
    """'Expression-of-Concern', 'expression of concern' and 'expression_of_concern' are one kind."""
    return (t or "").strip().lower().replace("-", "_").replace(" ", "_")


def cr_date(d, latest: bool = False) -> Optional[date]:
    """A Crossref date ({"date-parts": [[2020, 3, 5]]}) as a date.  A missing day or month is filled with the
    earliest possible value, or with the latest when `latest` (so that a vague date never makes a check fire)."""
    if not isinstance(d, dict):
        return None
    try:
        parts = [int(x) for x in ((d.get("date-parts") or [[]])[0] or []) if x is not None]
    except (TypeError, ValueError):
        return None
    if not parts or not 1900 <= parts[0] <= 2100:
        return None
    y = parts[0]
    m = parts[1] if len(parts) > 1 and 1 <= parts[1] <= 12 else (12 if latest else 1)
    if len(parts) > 2 and 1 <= parts[2] <= 31:
        day = parts[2]
    else:
        day = ((date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day) if latest else 1
    try:
        return date(y, m, day)
    except ValueError:
        return None


def parse_day(s: str, latest: bool = False) -> Optional[date]:
    """'2021-03-05', '2021-03', '2021', '5 March 2021', 'March 5, 2021' -> a date (vague ones as cr_date does)."""
    s = (s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?(?:T.*)?", s)
    if m:
        return cr_date({"date-parts": [[int(x) for x in m.groups() if x]]}, latest)
    try:
        from .reporting import _parse_date
        return _parse_date(s)
    except Exception:
        return None


def crossref_works(cl: httpx.Client, dois: list[str], cache: Optional[dict] = None) -> tuple[dict[str, dict], int]:
    """Title and update notices ("updated-by": retractions, corrections, concerns - from publishers and from the
    Retraction Watch database) for many DOIs, CR_BATCH per request.  Returns ({doi: record}, DOIs that could not be
    asked because Crossref was busy or unreachable).  A DOI Crossref answered for but does not know is simply
    absent.  `cache` (a dict) keeps answers for the rest of the run."""
    cache = {} if cache is None else cache
    todo = [d for d in dict.fromkeys(x.strip().lower() for x in dois if x) if d not in cache and "," not in d]
    busy = 0
    for i in range(0, len(todo), CR_BATCH):
        batch = todo[i:i + CR_BATCH]
        try:
            r = _polite_get(cl, "https://api.crossref.org/works",
                            params=_cr_params({"filter": ",".join(f"doi:{d}" for d in batch),
                                               "select": "DOI,title,updated-by", "rows": len(batch)}))
        except NET_ERRORS:
            busy += len(batch)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            busy += len(batch)
            continue
        if r.status_code != 200:
            continue                     # a refused query: no answer, but not a sign of an outage either
        try:
            msg = r.json().get("message")
            items = msg.get("items") if isinstance(msg, dict) else None
        except (ValueError, AttributeError):
            busy += len(batch)
            continue
        for d in batch:
            cache.setdefault(d, None)    # answered: unknown to Crossref unless an item below says otherwise
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or not it.get("DOI"):
                continue
            cache[str(it["DOI"]).lower()] = {"title": ((it.get("title") or [""])[0] or ""),
                                              "notices": _notices(it.get("updated-by"))}
    return {d: cache[d] for d in dict.fromkeys(x.strip().lower() for x in dois if x) if cache.get(d)}, busy


def _notices(updated_by) -> list[dict]:
    out = []
    for u in updated_by if isinstance(updated_by, list) else []:
        if not isinstance(u, dict):
            continue
        d = cr_date(u.get("updated"))
        out.append({"type": notice_type(u.get("type", "")), "doi": (u.get("DOI") or "").lower(),
                    "date": d.isoformat() if d else "", "source": u.get("source", ""), "label": u.get("label", "")})
    return out


SPECIAL_RE = re.compile(r"\b(special|themed|thematic|focus(?:ed)?|virtual special)\s+(issue|section|collection)\b"
                        r"|^\s*S\.?I\.?\s*\d*\s*$", re.I)


def special_issue_from_crossref(msg: dict) -> str:
    """The special-issue name when the Crossref record says so outright (issue label, assertion, subtitle), else ''.
    Most publishers deposit nothing about it, so an empty answer means 'not stated', not 'not a special issue'."""
    if not isinstance(msg, dict):
        return ""
    issue = str(msg.get("issue") or (msg.get("journal-issue") or {}).get("issue") or "")
    if issue and SPECIAL_RE.search(issue):
        return issue.strip()
    for a in msg.get("assertion") or []:
        if not isinstance(a, dict):
            continue
        key = f"{a.get('name', '')} {a.get('label', '')}"
        if re.search(r"special.?issue|themed.?issue|thematic.?issue|special.?collection", key, re.I) and \
                str(a.get("value") or "").strip():
            return str(a["value"]).strip()[:200]
    for t in (msg.get("subtitle") or []) + (msg.get("group-title") and [msg["group-title"]] or []):
        if isinstance(t, str) and re.search(r"\bspecial issue\b", t, re.I):
            return t.strip()[:200]
    return ""


def own_record(cl: httpx.Client, meta: dict) -> dict:
    """Ask Crossref for this paper's own record once and fold what it adds into `meta`, in place:
      meta["notices"]        + notices from "updated-by" (publisher deposits and Retraction Watch), no duplicates
      meta["special_issue"]  the special-issue name, when the record states one
    A successful answer also clears the "notices" lookup error: the record lists every notice Crossref knows.
    Returns what was learned, for the report."""
    info: dict = {}
    doi = (meta.get("doi") or "").strip().lower()
    if not doi:
        return info
    try:
        r = _polite_get(cl, f"https://api.crossref.org/works/{doi_path(doi)}", params=_cr_params({}))
        msg = r.json().get("message") if r.status_code == 200 else None
    except (NET_ERRORS + (AttributeError,)):
        msg = None
    if not isinstance(msg, dict):
        info["record"] = "unavailable"
        return info
    info["record"] = "ok"
    have = {(notice_type(n.get("type", "")), (n.get("doi") or "").lower()) for n in meta.get("notices") or []}
    added = []
    for n in _notices(msg.get("updated-by")):
        if n["type"] in ("", "new_version") or (n["type"], n["doi"]) in have:
            continue
        have.add((n["type"], n["doi"]))
        added.append({"type": n["type"], "doi": n["doi"], "title": n["label"], "date": n["date"],
                      "source": n["source"]})
    if added:
        meta["notices"] = list(meta.get("notices") or []) + added
        info["notices_added"] = len(added)
    if "notices" in (meta.get("lookup_errors") or []):
        meta["lookup_errors"] = [e for e in meta["lookup_errors"] if e != "notices"]
    special = special_issue_from_crossref(msg)
    if special and not meta.get("special_issue"):
        meta["special_issue"] = special
        info["special_issue"] = special
    return info


def pubpeer_key() -> str:
    try:
        from . import settings
        return ((settings.load().get("keys") or {}).get("pubpeer") or "").strip()
    except Exception:
        return ""


def pubpeer(cl: httpx.Client, dois: list[str], key: Optional[str] = None) -> Optional[dict[str, dict]]:
    """Optional hook: PubPeer comment counts by DOI.  PubPeer has no keyless public API, so this does nothing (returns
    None, sends nothing) unless a developer key is saved in Settings (keys.pubpeer).  With a key it asks PubPeer's
    publications endpoint once and returns {doi: {"comments": n, "url": ...}} for the DOIs PubPeer knows.
    The key goes only to pubpeer.com and is never logged or stored by this function."""
    key = key if key is not None else pubpeer_key()
    if not key or not dois:
        return None
    try:
        r = cl.post("https://pubpeer.com/v3/publications", params={"devkey": key},
                    json={"dois": [d.lower() for d in dois[:50]]}, timeout=20)
        data = r.json() if r.status_code == 200 else None
    except (NET_ERRORS + (AttributeError,)):
        return None
    out: dict[str, dict] = {}
    rows = (data or {}).get("feedbacks") if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        doi = str(row.get("doi") or row.get("id") or "").lower()
        n = row.get("total_comments", row.get("comments_count"))
        if doi and isinstance(n, int):
            out[doi] = {"comments": n, "url": str(row.get("url") or "")}
    return out


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def openalex_key() -> str:
    """An optional free OpenAlex key from Settings: lifts the small shared daily budget for list queries."""
    try:
        from . import settings
        return (settings.load().get("keys") or {}).get("openalex", "") or ""
    except Exception:
        return ""


def _openalex_batch(cl: httpx.Client, key: str, values: list[str]) -> tuple[list[dict], int]:
    """Look works up one by one: OpenAlex charges list queries against a small daily budget shared by everyone on
    the same network, while single-record lookups are free.  Returns (works, lookups that failed); a 404 is not a
    failure.  When OpenAlex says 'slow down' every worker waits, rather than each hammering it on its own."""
    api_key = openalex_key()
    pause = threading.Event()
    pause.set()
    lock = threading.Lock()
    failed = [0]

    def one(v: str):
        url = f"https://api.openalex.org/works/{'doi:' + doi_path(v) if key == 'doi' else doi_path(v)}"
        params = {"select": SELECT}
        if api_key:
            params["api_key"] = api_key
        for attempt in range(3):
            pause.wait(60)
            try:
                r = cl.get(url, params=params)
                if r.status_code == 200:
                    return r.json()
            except NET_ERRORS:
                break
            if r.status_code == 404:
                return None
            if r.status_code != 429 and r.status_code < 500:
                break
            if pause.is_set():
                pause.clear()
                time.sleep(retry_after(r, 1.0 + 2 * attempt))
                pause.set()
        with lock:
            failed[0] += 1
        return None

    with ThreadPoolExecutor(6) as ex:
        works = [w for w in ex.map(one, values[:250]) if isinstance(w, dict)]
    return works, failed[0]


def _polite_get(cl: httpx.Client, url: str, **kw) -> httpx.Response:
    """Crossref's public pool allows one request at a time; back off when it says so."""
    for attempt in range(4):
        r = cl.get(url, **kw)
        if r.status_code != 429 and r.status_code < 500 or attempt == 3:
            return r
        time.sleep(retry_after(r, 1.0 + attempt))
    return r


def _crossref_lookup(cl: httpx.Client, text: str):
    """The best Crossref match, None when Crossref answered and found nothing, or FAILED when it could not answer."""
    try:
        r = _polite_get(cl, "https://api.crossref.org/works", params={"query.bibliographic": text[:300], "rows": 2,
                                                                       "select": "DOI,title,author,issued,score"})
        if r.status_code != 200:
            return FAILED
        items = r.json().get("message", {}).get("items", [])
        return items[0] if items else None
    except NET_ERRORS + (AttributeError,):
        return FAILED


def doi_status(cl: httpx.Client, d: str) -> Optional[bool]:
    """True: the DOI exists.  False: doi.org itself says it does not.  None: no answer either way (rate limits,
    outages, a proxy or bot wall) - which is not evidence of anything."""
    try:
        r = _polite_get(cl, f"https://api.crossref.org/works/{doi_path(d)}")
        if r.status_code == 200:
            return True
        r = cl.head(f"https://doi.org/{doi_path(d)}", follow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            return True
        if r.status_code == 404:
            return False
    except NET_ERRORS:
        pass
    return None


def _similar(a: str, b: str) -> float:
    a, b = re.sub(r"\W+", " ", a.lower()).strip(), re.sub(r"\W+", " ", b.lower()).strip()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


STOP = set("""a an the of and or in on at to for from by with without into onto over under via as is are was were be
been being its their this that these those using use used study studies analysis new novel role effect effects
between among within after before during than versus vs""".split())


def _words(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", s) if len(w) >= 4 and w not in STOP}


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _mostly_latin(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) >= 0.9


def doi_title_mismatches(refs: list[dict], cr: dict[str, dict], works: list[dict]) -> list[dict]:
    """Deposited references whose DOI belongs to a paper with a clearly different title.  Conservative on purpose:
      - only DOIs the publisher deposited (a DOI Crossref matched itself is Crossref's guess, not the authors' error)
      - only when the cited title has enough words to compare, and the two titles share almost none of them
      - references given only as free text are compared only when most of this paper's free-text references do
        name their titles (styles that leave titles out would otherwise all look wrong)."""
    oa_titles = {(w.get("doi") or "").replace("https://doi.org/", "").lower(): w.get("title") or "" for w in works}
    titled, free = [], []
    for r in refs:
        d = str(r.get("DOI") or "").strip().lower()
        if not d or str(r.get("doi-asserted-by") or "").lower() == "crossref":
            continue
        found = (cr.get(d) or {}).get("title") or oa_titles.get(d) or ""
        if len(found) < 15 or not _mostly_latin(found) or len(_words(found)) < 3:
            continue
        if re.match(r"\s*(correction|erratum|corrigendum|retraction|retracted|reply|response|comment|editorial|"
                    r"withdrawn|expression of concern)\b", found, re.I):
            continue                     # notices and replies carry a different title by design
        title = " ".join(re.sub(r"<[^>]{0,400}>", " ", str(r.get("article-title") or "")).split())
        if title and title.lower() != str(r.get("journal-title") or "").strip().lower():
            if len(_words(title)) >= 3 and _mostly_latin(title):
                titled.append((d, title, found))
        elif r.get("unstructured") and len(str(r["unstructured"])) >= 60:
            free.append((d, re.sub(r"<[^>]{0,400}>", " ", str(r["unstructured"])), found))
    out = []
    for d, title, found in titled:
        if _similar(title, found) < 0.4 and _overlap(_words(title), _words(found)) < 0.2:
            out.append({"doi": d, "cited": title, "found": found})
    if len(free) >= 5:
        # free text: the found title's words should appear in the citation; check the style names titles at all
        miss = [(d, u, f) for d, u, f in free if len(_words(f) & _words(u)) / len(_words(f)) < 0.2]
        if len(miss) <= 0.3 * len(free):
            out += [{"doi": d, "cited": " ".join(u.split())[:200], "found": f} for d, u, f in miss]
    return out


def _retracted_refs(works: list[dict], cr: dict[str, dict]) -> list[dict]:
    """One row per retracted cited work: {doi, title, year, date (of the earliest retraction notice, or ''),
    type, source}."""
    rows: dict[str, dict] = {}
    for d, rec in cr.items():
        ret = sorted((n for n in rec["notices"] if n["type"] in RETRACTION_TYPES), key=lambda n: n["date"] or "9999")
        if ret:
            rows[d] = {"doi": d, "title": rec["title"], "year": None, "date": ret[0]["date"], "type": ret[0]["type"],
                       "source": ret[0]["source"], "notice": ret[0]["doi"]}
    for w in works:
        if not w.get("is_retracted"):
            continue
        d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
        key = d or w.get("id") or ""
        if key in rows:
            rows[key]["year"] = w.get("publication_year")
            rows[key]["title"] = rows[key]["title"] or w.get("title") or ""
        else:
            rows[key] = {"doi": d or w.get("id") or "", "title": w.get("title") or "",
                         "year": w.get("publication_year"), "date": "", "type": "retraction", "source": "openalex",
                         "notice": ""}
    return list(rows.values())


def citing_date(meta: dict) -> tuple[Optional[date], str]:
    """When this paper was written, as early as the record allows: (date, 'received' | 'accepted' | 'published')."""
    dates = meta.get("dates") or {}
    for key in ("received", "accepted"):
        d = parse_day(str(dates.get(key) or ""))
        if d:
            return d, key
    d = parse_day(str(meta.get("issued") or dates.get("published") or ""))
    return (d, "published") if d else (None, "")


def split_by_retraction_date(retracted: list[dict], meta: dict) -> tuple[list[dict], list[dict]]:
    """(retracted well before this paper was written, the rest).  The rest includes works with no known date."""
    when, basis = citing_date(meta)
    if when is None or (meta.get("type") or "journal-article") != "journal-article":
        return [], retracted
    early, later = [], []
    for r in retracted:
        d = parse_day(r.get("date") or "", latest=True)
        if d and (when - d).days > GRACE_DAYS[basis]:
            early.append({**r, "before": basis, "citing_date": when.isoformat(), "days_before": (when - d).days})
        else:
            later.append({**r, "after": bool(d and d >= when)})
    return early, later


def retraction_findings(early: list[dict], later: list[dict], talks: bool = False,
                        dated: bool = False) -> list[Finding]:
    """`dated`: this paper's own date was known, so works in `later` with a retraction date were retracted after
    it was written."""
    out = []
    if early:
        basis = early[0]["before"]
        what = {"received": "submitted", "accepted": "accepted", "published": "published"}[basis]
        sev = "info" if talks else "medium" if len(early) >= 2 else "low"
        out.append(Finding(
            "refs.cited_after_retraction", sev,
            f"Cites {len(early)} paper(s) that were already retracted",
            " ".join(f"“{r['title'][:90]}” was retracted on {r['date']}." for r in early[:4]) +
            f" This paper was {what} on {early[0]['citing_date']}, so the retraction was public when it was written. "
            + ("The text mentions retraction, so it may cite them as retracted work on purpose; check how they are "
               "used." if talks else "That is fine if the paper cites them as retracted work; otherwise its "
               "argument may rest on findings that were withdrawn."),
            0.8, [Location(quote=r["doi"]) for r in early[:6]],
            data={"works": early, "basis": basis}))
    if later:
        out.append(Finding(
            "refs.retracted", "medium" if len(later) >= 2 else "low",
            f"Cites {len(later)} retracted paper(s)",
            " ".join(f"“{(r.get('title') or '')[:90]}” ({r.get('date') or r.get('year') or 'date unknown'})."
                     for r in later[:4]) +
            (" They were retracted after this paper was written, which is not the authors' fault, but claims "
             "resting on them should be re-checked." if dated and all(r.get("after") for r in later) else
             " Citing a retracted paper is not misconduct, but claims resting on it should be re-checked."),
            0.85, [Location(quote=r["doi"]) for r in later[:6]],
            data={"works": [{"title": r.get("title"), "doi": r.get("doi"), "date": r.get("date"),
                             "source": r.get("source")} for r in later]}))
    return out


def analyse(meta: dict, online: bool = True,
            progress: Optional[Callable[[str, float], None]] = None, paper=None) -> tuple[list[Finding], dict]:
    """progress(stage, fraction of this step done).  `paper` (optional) lets the retraction check see whether the
    text itself talks about retracted work.  Also folds the paper's own Crossref record into `meta` (own_record)."""
    progress = progress or (lambda s, f: None)
    refs = meta.get("references") or []
    stats: dict = {"count": len(refs) or len(meta.get("referenced_works") or [])}
    if online and meta.get("doi"):
        try:
            with _client() as cl:
                progress("Checking this paper's record with Crossref and Retraction Watch", 0.0)
                rec = own_record(cl, meta)
                if rec:
                    stats["record"] = rec
                pp = pubpeer(cl, [meta["doi"]])
                if pp is not None:
                    stats["pubpeer"] = pp.get(str(meta["doi"]).lower()) or {"comments": 0}
        except Exception as e:          # noqa: BLE001 - an extra lookup must never cost the reference check
            stats["record"] = {"record": f"skipped ({type(e).__name__})"}
    if not online or not (refs or meta.get("referenced_works")):
        return [], stats
    out: list[Finding] = []
    notes: list[str] = []
    t0 = time.monotonic()
    dois = sorted({str(r.get("DOI") or "").strip().lower() for r in refs if r.get("DOI")} - {""})
    with _client() as cl:
        progress("Looking up the cited works in OpenAlex", 0.0)
        works, failed = _openalex_batch(cl, "doi", dois)
        progress("Asking Crossref about retractions of the cited works", 0.15)
        cr, cr_busy = crossref_works(cl, dois[:250])
        if cr_busy and cr_busy / max(1, min(len(dois), 250)) > 0.3:
            notes.append(f"Crossref did not answer for {cr_busy} cited DOI(s), so retraction dates and "
                         "DOI-title checks are incomplete.")
        known = {(w.get("doi") or "").replace("https://doi.org/", "").lower() for w in works} | set(cr)
        ow = [w.rsplit("/", 1)[-1] for w in meta.get("referenced_works") or [] if isinstance(w, str)]
        have = {(w.get("id") or "").rsplit("/", 1)[-1] for w in works}
        more, failed2 = _openalex_batch(cl, "openalex", [w for w in ow if w not in have])
        works += more
        tried = min(len(dois), 250) + min(len([w for w in ow if w not in have]), 250)
        if tried and (failed + failed2) / tried > 0.3:
            notes.append(f"OpenAlex did not answer for {failed + failed2} of {tried} cited works (daily limit or "
                         "outage), so retracted references, self-citation and citation patterns are incomplete. "
                         "A free OpenAlex key in Settings lifts the limit.")

        # DOIs nobody knows
        missing = [d for d in dois if d not in known][:40]
        bad: list[str] = []
        unchecked = 0
        for i, d in enumerate(missing):
            if time.monotonic() - t0 > BUDGET:
                unchecked += len(missing) - i
                break
            progress(f"Checking cited DOIs ({i + 1} of {len(missing)})", 0.2 + 0.3 * i / max(1, len(missing)))
            ok = doi_status(cl, d)
            if ok is False:
                bad.append(d)
            elif ok is None:
                unchecked += 1
        if unchecked:
            notes.append(f"{unchecked} cited DOI(s) could not be checked (Crossref or doi.org did not answer).")

        # references without a DOI: try to find them by their text
        nodoi = [r for r in refs if not r.get("DOI") and (r.get("unstructured") or r.get("article-title"))]
        unmatched: list[str] = []
        failed_lookups = 0
        tried_lookups = 0

        def find(r):
            text = r.get("unstructured") or " ".join(str(r.get(k, "")) for k in ("author", "article-title",
                                                                                 "journal-title", "year"))
            title = r.get("article-title") or ""
            if not re.search(r"(19|20)\d\d", text) or len(text) < 40:
                return SKIP              # websites, software, personal communications
            if re.search(r"\b(book|press|edition|eds?\.|chapter|thesis|dissertation|http|www\.|software|"
                         r"database|report|manual|patent|conference|proceedings)\b", text, re.I):
                return SKIP              # not expected to be in Crossref reliably
            hit = _crossref_lookup(cl, text)
            if hit is FAILED:
                return FAILED
            if hit is None:
                return text
            ht = (hit.get("title") or [""])[0]
            if title and _similar(title, ht) >= 0.7:
                return None
            if not title and hit.get("score", 0) >= 60:
                return None
            if _similar(text, ht) >= 0.5 or ht and ht.lower()[:40] in text.lower():
                return None
            return text
        todo = nodoi[:60]
        for i, r in enumerate(todo):
            if time.monotonic() - t0 > BUDGET:
                failed_lookups += len(todo) - i
                tried_lookups += len(todo) - i
                break
            progress(f"Looking up references without a DOI ({i + 1} of {len(todo)})", 0.5 + 0.5 * i / len(todo))
            res = find(r)
            if res is SKIP:
                continue
            tried_lookups += 1
            if res is None:
                continue
            if res is FAILED:
                failed_lookups += 1
            else:
                unmatched.append(res)
        if failed_lookups:
            notes.append(f"{failed_lookups} reference(s) without a DOI could not be looked up (Crossref did not "
                         "answer).")

    stats.update({"resolved": len(works), "dois": len(dois), "bad_dois": bad, "unmatched": len(unmatched),
                  "unchecked": unchecked + failed_lookups, "notes": notes})
    if bad:
        out.append(Finding("refs.bad_doi", "low" if len(bad) < 3 else "medium",
                           f"{len(bad)} cited DOI(s) do not exist",
                           "These DOIs resolve nowhere: " + ", ".join(bad[:6]) + ". Usually a typo; a pattern of "
                           "invented DOIs is a sign of references produced by an AI tool.",
                           0.7, [Location(quote=d) for d in bad[:6]], data={"dois": bad}))
    if failed_lookups > 0.2 * max(1, tried_lookups):
        unmatched = []                  # too many lookups failed for the misses to mean anything
    if len(unmatched) >= 2:
        sev = "medium" if len(unmatched) >= 4 else "low"
        out.append(Finding("refs.unverified", sev, f"{len(unmatched)} references could not be found in Crossref",
                           "No published work matches these references. Check them by hand: fabricated citations "
                           "(common in AI-written text) look exactly like this, but so do obscure or non-English "
                           "sources.", 0.5, [Location(quote=u[:200]) for u in unmatched[:6]],
                           data={"references": unmatched[:30]}))

    # retracted references: OpenAlex's flag and Crossref's notices (which carry the date), split by whether the
    # retraction came before this paper was written
    retracted = _retracted_refs(works, cr)
    early, later = split_by_retraction_date(retracted, meta)
    talks = False
    if paper is not None:
        from .refs import body_text
        talks = bool(re.search(r"\bretract", body_text(paper), re.I))
    out += retraction_findings(early, later, talks, citing_date(meta)[0] is not None)
    stats["retracted"] = len(retracted)
    stats["retracted_before_submission"] = len(early)
    concern = [(d, rec) for d, rec in cr.items()
               if any(n["type"] in CONCERN_TYPES for n in rec["notices"]) and d not in {x["doi"] for x in retracted}]
    if concern:
        out.append(Finding("refs.concern", "info", f"{len(concern)} cited work(s) carry an expression of concern",
                           "The journal has warned readers about these works: " +
                           " ".join(f"“{rec['title'][:90]}”." for _, rec in concern[:4]) +
                           " Claims resting on them deserve a second look.", 0.8,
                           [Location(quote=d) for d, _ in concern[:6]], data={"dois": [d for d, _ in concern]}))

    # a cited DOI that points at a different paper than the cited title
    mismatches = doi_title_mismatches(refs, cr, works)
    stats["doi_title_mismatches"] = len(mismatches)
    if mismatches:
        out.append(Finding("refs.doi_mismatch", "medium" if len(mismatches) >= 3 else "low",
                           f"{len(mismatches)} cited DOI(s) lead to a different paper",
                           "The DOI given in these references belongs to a paper with a different title: " +
                           " ".join(f"“{m['cited'][:70]}” has the DOI of “{m['found'][:70]}”."
                                    for m in mismatches[:3]) + " One such slip is usually a copy-paste error. Several are a sign of a reference list "
                           "assembled without reading the sources, or invented by an AI tool and given real DOIs.",
                           0.6, [Location(quote=m["doi"]) for m in mismatches[:6]], data={"references": mismatches}))
    if notes:
        out.append(Finding("refs.unchecked", "info", "Part of the reference check could not be done",
                           " ".join(notes) + " Nothing here is evidence about the paper; re-run later to complete "
                           "the check.", 0.9, data={"notes": notes}))

    # self-citation and concentration
    authors = meta.get("authors") or []
    my_ids = {a["openalex"].rsplit("/", 1)[-1] for a in authors if a.get("openalex")}
    my_names = {(_norm_name(a.get("family")), _norm_name((a.get("given") or "")[:1])) for a in authors}
    n = len(works)
    if n >= 8:
        self_cites = 0
        who: Counter = Counter()
        names: dict[str, str] = {}
        journals: Counter = Counter()
        for w in works:
            ids = set()
            mine = False
            for au in w.get("authorships") or []:
                a = au.get("author") or {}
                aid = (a.get("id") or "").rsplit("/", 1)[-1]
                nm = a.get("display_name") or ""
                parts = nm.split()
                key = (_norm_name(parts[-1]) if parts else "", _norm_name(parts[0][:1]) if parts else "")
                if aid in my_ids or key in my_names:
                    mine = True
                elif aid:
                    ids.add(aid)
                    names[aid] = nm
            self_cites += mine
            who.update(ids)
            src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
            if src:
                journals[src] += 1
        rate = self_cites / n
        stats.update({"self_citations": self_cites, "self_citation_rate": round(rate, 3), "checked": n})
        if rate >= 0.3 and self_cites >= 5:
            out.append(Finding("refs.self_citation", "medium" if rate >= 0.5 else "low",
                               f"High self-citation: {self_cites} of {n} references ({rate:.0%})",
                               f"{self_cites} of the {n} resolved references share at least one author with this "
                               "paper. Typical papers sit around 10-15%. High rates inflate the authors' citation "
                               "counts; they can also simply reflect a narrow field.", 0.7,
                               data={"rate": rate, "count": self_cites}))
        top = who.most_common(3)
        stats["top_cited_authors"] = [{"name": names.get(a, a), "count": c} for a, c in top]
        if top and top[0][1] >= 5 and top[0][1] / n >= 0.15:
            a, c = top[0]
            out.append(Finding("refs.citation_pushing", "medium" if c / n >= 0.25 else "low",
                               f"One outside author is cited in {c} of {n} references",
                               f"{names.get(a, a)}, who is not an author of this paper, appears in {c / n:.0%} of "
                               "the references. A reviewer or editor asking for citations to their own work "
                               "(coercive citation) leaves this pattern; so can a single dominant lab in a small "
                               "field.", 0.5, data={"author": names.get(a, a), "count": c}))
        journal = (meta.get("journal") or "").lower()
        same = sum(c for j, c in journals.items() if j.lower() == journal) if journal else 0
        stats["journal_self_citations"] = same
        if same >= 5 and same / n >= 0.3:
            out.append(Finding("refs.journal_self", "low", f"{same} of {n} references are to the same journal",
                               f"{same / n:.0%} of references cite {meta.get('journal')}. Journals sometimes press "
                               "authors to cite them, which inflates the impact factor.", 0.5,
                               data={"count": same}))
        # off-topic: references from fields the paper does not touch
        paper_topics = set(meta.get("topic_fields") or [])
        if paper_topics:
            off = [w for w in works if (w.get("topics") or []) and not
                   {(t.get("field") or {}).get("display_name") for t in w["topics"]} & paper_topics]
            stats["off_topic"] = len(off)
            if len(off) >= 5 and len(off) / n >= 0.25:
                out.append(Finding("refs.off_topic", "low", f"{len(off)} references are from unrelated fields",
                                   f"{len(off)} of {n} cited works belong to fields this paper does not touch "
                                   f"(paper: {', '.join(sorted(paper_topics))}). Padding a reference list with "
                                   "irrelevant citations is a known way to boost other people's metrics.", 0.4,
                                   [Location(quote=(w.get('title') or '')[:120]) for w in off[:6]],
                                   data={"titles": [w.get("title") for w in off[:20]]}))
    return out, stats
