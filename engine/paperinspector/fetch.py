"""From a DOI to a folder holding everything a reader could legally get: metadata, the PDF, the full-text XML,
the original figure files and the supplementary files.

Routes, tried in order and all recorded in `bundle.log`:
  Crossref            metadata, authors, reference list, editorial dates, retractions/corrections of this paper
  OpenAlex            open-access PDF locations, PMC id, author ids, referenced works
  PMC open access     a package with the PDF, JATS XML, original figures and supplementary files
  Europe PMC          full-text XML and the supplementary-files zip, when the PMC package is missing
  bioRxiv / medRxiv   preprint metadata and PDF
  publisher page      the `citation_pdf_url` the page itself advertises, and links to supplementary files

Nothing behind a paywall is bypassed.  When no open copy exists the user uploads the PDF and supplements.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlsplit

import httpx

from . import __version__
from .models import Cancelled as _UserStop

CONTACT = os.environ.get("PAPERINSPECTOR_CONTACT", "")    # an e-mail puts Crossref/OpenAlex calls in the polite pool
UA = (f"PaperInspector/{__version__} (research-integrity screening; +https://github.com/paper-inspector"
      + (f"; mailto:{CONTACT}" if CONTACT else "") + ")")


def user_agent() -> str:
    """The User-Agent for scholarly APIs, read each time a client is made: it carries a contact address only when
    the user set one in Settings (or PAPERINSPECTOR_CONTACT), never otherwise."""
    mail = CONTACT
    try:
        from .settings import contact_email
        mail = contact_email() or CONTACT
    except Exception:                  # noqa: BLE001 - unreadable settings: no address
        pass
    return (f"PaperInspector/{__version__} (research-integrity screening; +https://github.com/paper-inspector"
            + (f"; mailto:{mail}" if mail else "") + ")")


BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.0 Safari/605.1.15 PaperInspector/" + __version__)
SUPP_EXT = {".xlsx", ".xls", ".csv", ".tsv", ".txt", ".docx", ".doc", ".pdf", ".zip", ".tif", ".tiff", ".png",
            ".jpg", ".jpeg", ".gz", ".tgz", ".pptx", ".xml", ".json"}
MAX_FILE = 150 * 1024 * 1024
MAX_SUPP = 40
MAX_UNZIP = 600 * 1024 * 1024          # all members of one archive together
MAX_MEDIA = 200                        # files taken from one PMC package
FILE_DEADLINE = 300.0                  # seconds for one download, however slowly the server trickles

DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)
# what article pages append to the DOI in their links: /full, /abstract, .full.pdf, bioRxiv's v2 ...
URL_TAIL = re.compile(r"(/(full|abstract|abs|pdf|epdf|pdfdirect|fulltext|html|figures|references|"
                      r"supplementary|suppl_file|citedby|metrics|meta)|\.(full|abstract|pdf|html|full-text|"
                      r"supplementary-material))+$", re.I)


# what copying from a PDF or a web page puts into a DOI: typographic hyphens, soft hyphens, zero-width spaces
_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2212\ufe63\uff0d"), "-")
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"), None)


def tidy(text: str) -> str:
    """Undo what copy and paste does to identifiers: typographic dashes become '-', invisible characters go, and
    non-breaking spaces become spaces."""
    return (text or "").translate(_DASHES).translate(_INVISIBLE).replace("\u00a0", " ").replace("\u202f", " ")


def normalize(doi: str) -> str:
    """'https://doi.org/10.1/ABC', 'doi:10.1/abc ', a percent-encoded DOI, or an article link -> '10.1/abc'."""
    s = tidy(doi).strip()
    from_url = bool(re.match(r"(https?://|www\.)", s, re.I))
    if from_url:
        u = urlsplit(s if "://" in s else "https://" + s)
        cands = [unquote(u.path)] + [v for vs in parse_qs(u.query).values() for v in vs]
        m = next((m for m in (DOI_RE.search(c) for c in cands) if m), None)
    else:
        m = DOI_RE.search(unquote(s) if re.search(r"%[0-9a-f]{2}", s, re.I) else s)
    if not m:
        raise ValueError(f"'{doi}' does not look like a DOI (they start with 10.)")
    d = m.group(0)
    if from_url:
        d = URL_TAIL.sub("", d)
    while d and d[-1] in ".,;:'\"" or d.endswith(")") and d.count("(") < d.count(")") or \
            d.endswith("]") and d.count("[") < d.count("]") or d.endswith(">") and d.count("<") < d.count(">") or \
            d.endswith("}") and d.count("{") < d.count("}"):
        d = d[:-1]
    d = d.lower()
    if d.startswith("10.1101/") or d.startswith("10.48550/arxiv."):
        d = re.sub(r"v\d+$", "", d)    # bioRxiv / medRxiv / arXiv version suffix: not part of the DOI
    return d


def doi_path(doi: str) -> str:
    """A DOI made safe for a URL path: SICI DOIs carry '#', '?', '<', ';' that would otherwise cut the URL short."""
    return quote(doi, safe="/()")


@dataclass
class Bundle:
    doi: str
    dir: Path
    meta: dict = field(default_factory=dict)
    pdf: Optional[str] = None                 # file names relative to dir
    xml: Optional[str] = None
    figures: list[dict] = field(default_factory=list)       # {file, label, caption}
    supplements: list[dict] = field(default_factory=list)   # {file, name, label, source, status}
    log: list[str] = field(default_factory=list)
    # one line per source tried, for the UI: {source, state: ok | none | failed, message}
    status: list[dict] = field(default_factory=list)

    def save(self) -> None:
        d = asdict(self)
        d["dir"] = str(self.dir)
        (self.dir / "bundle.json").write_text(json.dumps(d, indent=1))

    @classmethod
    def load(cls, path: Path) -> "Bundle":
        d = json.loads((path / "bundle.json").read_text())
        d["dir"] = path
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def _note(b: Bundle, source: str, state: str, message: str) -> None:
    """Record what one source gave: a status line the UI can show, and the same words in the plain log.
    state: 'ok' (something was found), 'none' (looked, nothing there), 'failed' (could not be checked)."""
    b.status.append({"source": source, "state": state, "message": message})
    b.log.append(f"{source}: {message}")


def retry_after(r: httpx.Response, default: float, cap: float = 30.0) -> float:
    """Seconds to wait from a Retry-After header, which may also be an HTTP date or garbage."""
    v = r.headers.get("retry-after", "")
    try:
        return min(cap, max(0.0, float(v)))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return min(cap, max(0.0, parsedate_to_datetime(v).timestamp() - time.time()))
    except (TypeError, ValueError, IndexError):
        return default


class Cancelled(_UserStop):
    """The user stopped the scan.  Raised from inside a fetch at the next request or download chunk.  A subclass of
    models.Cancelled, so the server, the queue and the pipeline catch one class for every kind of cancel."""


# Answers that mean "busy, try again soon".  A plain 500 is usually a lasting server fault, and PLOS answers past
# its last supplementary file with errors, so only these are retried.
RETRY_STATUS = {429, 502, 503, 504}
BACKOFF = 1.0                          # seconds before the first retry; doubled each time, capped by retry_after


def _why(r: Optional[httpx.Response] = None, exc: Optional[BaseException] = None) -> str:
    """A short, plain reason a request did not work, for the status line the user sees."""
    if exc is not None:
        if isinstance(exc, httpx.TimeoutException):
            return "the server did not answer in time"
        if isinstance(exc, httpx.ConnectError):
            return "could not connect (offline, or the site is down)"
        if isinstance(exc, httpx.InvalidURL):
            return "the link is not a valid web address"
        return "the connection failed"
    if r is None:
        return ""
    c = r.status_code
    if c == 429:
        return "the server is rate-limiting requests (HTTP 429); try again in a few minutes"
    if c in (401, 403):
        return f"the site refused automated access (HTTP {c})"
    if c in (404, 410):
        return f"not found (HTTP {c})"
    if c >= 500:
        return f"the server had an error (HTTP {c})"
    return f"unexpected answer (HTTP {c})"


class _Client:
    # class-level defaults, so a test that swaps __init__ still gets a working client
    should_cancel: Optional[Callable[[], bool]] = None
    last_error: str = ""

    def __init__(self, timeout: float = 30.0, should_cancel: Optional[Callable[[], bool]] = None,
                 client: Optional[httpx.Client] = None):
        self.c = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": user_agent()})
        self.headers: dict = {}            # of the last successful download
        self.should_cancel = should_cancel
        self.last_error = ""               # why the last failed request failed, in plain words

    def check(self) -> None:
        """Raise Cancelled if the user has asked to stop.  Called before every request and between chunks."""
        f = self.should_cancel
        if f is not None and f():
            raise Cancelled("stopped by the user")

    def _wait(self, r: Optional[httpx.Response], attempt: int) -> None:
        """Back off before a retry: the server's Retry-After when it sends one, else 1 s, 2 s, 4 s...  Slept in
        half-second slices so a cancel is noticed quickly."""
        delay = retry_after(r, BACKOFF * 2 ** attempt) if r is not None else BACKOFF * 2 ** attempt
        n = max(1, int(delay / 0.5 + 0.999))
        for _ in range(n):
            self.check()
            time.sleep(delay / n)

    def lookup(self, url: str, **params) -> tuple[Optional[dict], str]:
        """(JSON, 'ok' | 'missing' | 'failed').  A 404 says the record does not exist; a busy server, a timeout or a
        garbled answer says nothing, so the caller must not read it as 'no record'."""
        self.last_error = ""
        for attempt in range(3):
            self.check()
            try:
                r = self.c.get(url, params=params or None)
            except httpx.TimeoutException as e:
                self.last_error = _why(exc=e)
                if attempt >= 1:
                    return None, "failed"
                self._wait(None, attempt)
                continue
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                self.last_error = _why(exc=e)
                return None, "failed"
            if r.status_code == 200:
                try:
                    return r.json(), "ok"
                except ValueError:
                    self.last_error = "the server sent an unreadable answer"
                    return None, "failed"
            self.last_error = _why(r)
            if r.status_code in (404, 410):
                return None, "missing"
            if r.status_code != 429 and r.status_code < 500 or attempt == 2:
                return None, "failed"
            self._wait(r, attempt)
        return None, "failed"

    def json(self, url: str, **params) -> Optional[dict]:
        return self.lookup(url, **params)[0]

    def get(self, url: str, browser: bool = False, timeout: float | None = None,
            retries: int = 2) -> Optional[httpx.Response]:
        """Small text responses only (pages, XML, listings); files go through download(), which caps their size.
        A busy answer (429, 502-504) or a timeout is retried with backoff."""
        self.last_error = ""
        for attempt in range(retries + 1):
            self.check()
            try:
                r = self.c.get(url, headers={"User-Agent": BROWSER_UA} if browser else None,
                               timeout=timeout or self.c.timeout)
            except httpx.TimeoutException as e:
                self.last_error = _why(exc=e)
                if attempt >= min(1, retries):
                    return None
                self._wait(None, attempt)
                continue
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                self.last_error = _why(exc=e)
                return None
            if r.status_code == 200:
                return r
            self.last_error = _why(r)
            if r.status_code not in RETRY_STATUS or attempt == retries:
                return None
            self._wait(r, attempt)
        return None

    def download(self, url: str, dest: Path, browser: bool = False, retries: int = 2) -> Optional[Path]:
        """Stream to a temporary name and rename at the end, so an interrupted transfer never leaves a truncated
        file under the real name.  Files over MAX_FILE, or slower than FILE_DEADLINE, are dropped.  A busy answer
        (429, 502-504) is retried with backoff before any bytes are written; a too-large or too-slow file is not."""
        self.last_error = ""
        for attempt in range(retries + 1):
            tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:6]}.part")
            retry: Optional[httpx.Response] = None
            try:
                self.check()
                t0 = time.monotonic()
                with self.c.stream("GET", url, headers={"User-Agent": BROWSER_UA} if browser else None) as r:
                    if r.status_code != 200:
                        self.last_error = _why(r)
                        if r.status_code not in RETRY_STATUS or attempt == retries:
                            return None
                        retry = r                   # only its headers are used (Retry-After)
                    else:
                        n = 0
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with open(tmp, "wb") as fh:
                            for chunk in r.iter_bytes(1 << 16):
                                self.check()
                                n += len(chunk)
                                if n > MAX_FILE:
                                    self.last_error = f"file larger than {MAX_FILE >> 20} MB"
                                    raise IOError("file too large")
                                if time.monotonic() - t0 > FILE_DEADLINE:
                                    self.last_error = "download too slow"
                                    raise IOError("download too slow")
                                fh.write(chunk)
                        self.headers = dict(r.headers)
                if retry is None:
                    os.replace(tmp, dest)
                    return dest
            except httpx.TimeoutException as e:
                self.last_error = _why(exc=e)
                if attempt >= min(1, retries):
                    return None
            except (httpx.HTTPError, httpx.InvalidURL, OSError) as e:
                if not isinstance(e, OSError):
                    self.last_error = _why(exc=e)
                return None
            finally:
                tmp.unlink(missing_ok=True)
            self._wait(retry, attempt)
        return None

    def close(self):
        self.c.close()


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "file"
    return name[:120]


def _is_pdf(p: Path) -> bool:
    try:
        with open(p, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def _is_html(p: Path) -> bool:
    """A login, cookie or 'access denied' page served with status 200 in place of the file asked for."""
    if p.suffix.lower() in (".html", ".htm", ".xml"):
        return False
    try:
        with open(p, "rb") as fh:
            head = fh.read(512).lstrip().lower()
    except OSError:
        return False
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<script"))


class _Links(HTMLParser):
    """Collects <meta name=citation_*> and <a href> from a landing page."""

    def __init__(self):
        super().__init__()
        self.meta: dict[str, list[str]] = {}
        self.links: list[tuple[str, str]] = []
        self._a: Optional[str] = None
        self._txt: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name", "").startswith("citation_") and a.get("content"):
            self.meta.setdefault(a["name"], []).append(a["content"])
        if tag == "a" and a.get("href"):
            self._a = a["href"]
            self._txt = []

    def handle_data(self, data):
        if self._a is not None:
            self._txt.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.links.append((self._a, " ".join("".join(self._txt).split())))
            self._a = None


CACHE_DAYS = 1.0


def _prune(root: Path) -> None:
    """Fetch folders are working space for one scan (the run keeps its own copies); drop the old ones."""
    cutoff = time.time() - CACHE_DAYS * 86400
    for d in root.iterdir() if root.exists() else []:
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def fetch(doi: str, root: Path, progress: Optional[Callable[[str, float], None]] = None,
          should_cancel: Optional[Callable[[], bool]] = None) -> Bundle:
    """Every fetch gets a folder of its own: two scans of one DOI at once must not write into each other's files,
    and files left from an earlier fetch (an older article version, a half-finished download) must not be read.

    should_cancel() is polled before every request and between download chunks; when it returns True the fetch
    stops, its half-filled folder is deleted and Cancelled is raised.  bundle.status says, source by source, what
    was found and what could not be reached."""
    progress = progress or (lambda s, f: None)
    doi = normalize(doi)
    _prune(root)
    key = hashlib.sha1(doi.encode()).hexdigest()[:12]
    b = Bundle(doi=doi, dir=root / f"{key}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}")
    b.dir.mkdir(parents=True)
    (b.dir / "supplements").mkdir()
    cl = _Client()
    cl.should_cancel = should_cancel       # set after construction: tests replace __init__ with a plain one
    try:
        cl.check()
        _metadata(cl, b, progress)
        pmcid = b.meta.get("pmcid")
        if pmcid:
            progress("Fetching the open-access package from PubMed Central", 0.25)
            _pmc_package(cl, b, pmcid)
            if not b.xml:
                progress("Asking Europe PMC for full text and supplements", 0.35)
                _europepmc(cl, b, pmcid)
        if doi.startswith("10.1371/") and (not b.figures or not b.supplements):
            progress("Fetching original figures from PLOS", 0.4)
            _plos(cl, b)
        if doi.startswith("10.1101/"):
            progress("Fetching the preprint from bioRxiv / medRxiv", 0.35)
            _biorxiv(cl, b)
        if doi.startswith("10.48550/arxiv.") and not b.pdf:
            progress("Fetching the preprint from arXiv", 0.4)
            _arxiv(cl, b)
        if not b.pdf:
            progress("Looking for an open-access PDF", 0.45)
            _oa_pdf(cl, b)
        progress("Reading the publisher's page for supplementary files", 0.55)
        _landing(cl, b)
    except _UserStop:                      # our own Cancelled, or one raised by the caller's progress callback
        shutil.rmtree(b.dir, ignore_errors=True)
        try:
            progress("Stopped", 1.0)
        except _UserStop:
            pass
        raise
    finally:
        cl.close()
    b.save()
    return b


def summary(b: Bundle) -> dict:
    """What the fetch got, in the words the UI shows: {found: [...], missing: [...], sources: b.status}."""
    found, missing = [], []
    (found if b.pdf else missing).append("the article PDF")
    if b.xml:
        found.append("the full-text XML")
    (found if b.figures else missing).append(f"{len(b.figures)} original figure files" if b.figures
                                             else "original figure files")
    (found if b.supplements else missing).append(f"{len(b.supplements)} supplementary files" if b.supplements
                                                 else "supplementary files")
    failed = [x["source"] for x in b.status if x["state"] == "failed"]
    return {"found": found, "missing": missing, "unreachable": sorted(set(failed)), "sources": list(b.status)}


def _metadata(cl: _Client, b: Bundle, progress) -> None:
    progress("Reading Crossref and OpenAlex metadata", 0.05)
    errors = []
    cr, st = cl.lookup(f"https://api.crossref.org/works/{doi_path(b.doi)}")
    msg = (cr or {}).get("message") or {}
    if msg:
        _note(b, "Crossref", "ok", "metadata found")
    elif st == "missing" and b.doi.startswith("10.48550/"):
        _note(b, "Crossref", "none", "no record (arXiv DOIs are registered with DataCite, so this is expected)")
    elif st == "missing":
        _note(b, "Crossref", "none", "no record (the DOI may be wrong, or registered elsewhere, e.g. DataCite)")
    else:
        errors.append("crossref")
        _note(b, "Crossref", "failed", "could not be reached (busy or offline); metadata and references are missing"
              + (f" - {cl.last_error}" if cl.last_error else ""))
    oa, st = cl.lookup(f"https://api.openalex.org/works/doi:{doi_path(b.doi)}")
    oa = oa or {}
    if st == "failed":
        errors.append("openalex")
    _note(b, "OpenAlex", "ok" if oa else "none" if st == "missing" else "failed",
          "record found" if oa else "no record" if st == "missing" else "could not be reached")
    upd, st = cl.lookup("https://api.crossref.org/works", filter=f"updates:{b.doi}", rows=20)
    upd = upd or {}
    if st != "ok":
        errors.append("notices")
        _note(b, "Crossref", "failed", "retraction and correction notices could not be checked")
    notices = []
    for it in (upd.get("message") or {}).get("items", []):
        for u in it.get("update-to", []):
            if u.get("DOI", "").lower() == b.doi:
                notices.append({"type": u.get("type", ""), "doi": it.get("DOI"), "title": (it.get("title") or [""])[0],
                                "date": _date(u.get("updated") or it.get("issued"))})
    ids = oa.get("ids") or {}
    pmcid = ids.get("pmcid") or ""
    pmcid = pmcid.rstrip("/").split("/")[-1] if pmcid else ""
    if not pmcid:
        conv = cl.json("https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/", ids=b.doi, format="json",
                       tool="paperinspector") or {}
        for rec in conv.get("records", []):
            pmcid = rec.get("pmcid") or pmcid
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = "PMC" + pmcid
    assertions = {a.get("name"): a.get("value") for a in msg.get("assertion", []) if a.get("name")}
    b.meta = {
        "doi": b.doi,
        "title": (msg.get("title") or [oa.get("title") or ""])[0],
        "journal": (msg.get("container-title") or [""])[0] or ((oa.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
        "publisher": msg.get("publisher", ""),
        "type": msg.get("type") or oa.get("type", ""),
        "issued": _date(msg.get("issued")) or oa.get("publication_date", ""),
        "authors": _authors(msg, oa),
        "references": msg.get("reference", []),
        "reference_count": msg.get("reference-count") or len(msg.get("reference", [])),
        "referenced_works": oa.get("referenced_works", []),
        "openalex_id": oa.get("id", ""),
        "is_retracted": bool(oa.get("is_retracted")),
        "cited_by_count": oa.get("cited_by_count"),
        "notices": notices,
        "license": [l.get("URL") for l in msg.get("license", [])][:3],
        "funders": [f.get("name") for f in msg.get("funder", [])][:20],
        "dates": {"received": assertions.get("received"), "accepted": assertions.get("accepted"),
                  "published": assertions.get("published") or _date(msg.get("published-online") or msg.get("published"))},
        "pmcid": pmcid,
        "pmid": (ids.get("pmid") or "").rstrip("/").split("/")[-1],
        "oa_pdf_urls": _oa_urls(oa, msg),
        "landing": (oa.get("primary_location") or {}).get("landing_page_url") or msg.get("URL") or
        f"https://doi.org/{doi_path(b.doi)}",
        "topics": [t.get("display_name") for t in (oa.get("topics") or [])][:5],
        "topic_fields": sorted({(t.get("field") or {}).get("display_name") for t in (oa.get("topics") or [])
                                if (t.get("field") or {}).get("display_name")}),
        "abstract": _abstract(oa, msg),
        "lookup_errors": errors,
    }


def _date(d) -> str:
    if not isinstance(d, dict):
        return ""
    parts = ((d.get("date-parts") or [[None]])[0]) or []
    try:
        parts = [int(p) for p in parts if p]
    except (TypeError, ValueError):
        return ""
    return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))


def _authors(msg: dict, oa: dict) -> list[dict]:
    out = []
    by_pos = {i: a for i, a in enumerate(oa.get("authorships") or [])}
    for i, a in enumerate(msg.get("author") or []):
        o = by_pos.get(i, {})
        out.append({"given": a.get("given", ""), "family": a.get("family", a.get("name", "")),
                    "orcid": (a.get("ORCID") or "").rsplit("/", 1)[-1],
                    "openalex": ((o.get("author") or {}).get("id") or ""),
                    "affiliations": [x.get("name") for x in a.get("affiliation", [])][:3] or
                    [x.get("display_name") for x in o.get("institutions", [])][:3]})
    if not out:
        for o in oa.get("authorships") or []:
            name = (o.get("author") or {}).get("display_name", "")
            out.append({"given": " ".join(name.split()[:-1]), "family": name.split()[-1] if name else "",
                        "orcid": ((o.get("author") or {}).get("orcid") or "").rsplit("/", 1)[-1],
                        "openalex": (o.get("author") or {}).get("id", ""),
                        "affiliations": [x.get("display_name") for x in o.get("institutions", [])][:3]})
    return out


def _abstract(oa: dict, msg: dict) -> str:
    inv = oa.get("abstract_inverted_index")
    if inv:
        pos = sorted((p, w) for w, ps in inv.items() for p in ps)
        return " ".join(w for _, w in pos)
    return re.sub(r"<[^>]+>", " ", msg.get("abstract", "")).strip()


def _oa_urls(oa: dict, msg: dict) -> list[str]:
    urls = []
    for loc in [oa.get("best_oa_location") or {}, oa.get("primary_location") or {}] + (oa.get("locations") or []):
        u = loc.get("pdf_url")
        if u and u not in urls:
            urls.append(u)
    for l in msg.get("link", []):
        if l.get("content-type") == "application/pdf" and l.get("URL") not in urls:
            urls.append(l["URL"])
    return urls


def _add_supp(b: Bundle, path: Path, source: str, label: str = "") -> None:
    if any(s["file"] == str(path.relative_to(b.dir)) for s in b.supplements):
        return
    b.supplements.append({"file": str(path.relative_to(b.dir)), "name": path.name, "label": label or path.stem,
                          "source": source, "status": "downloaded"})


PMC_S3 = "https://pmc-oa-opendata.s3.amazonaws.com"


def _pmc_package(cl: _Client, b: Bundle, pmcid: str) -> None:
    """The PMC Cloud Service (public S3 bucket) - the replacement for the retired OA web service.  Each article
    version has a JSON manifest listing the PDF, the JATS XML and every media file (figures, supplements)."""
    r = cl.get(f"{PMC_S3}/?list-type=2&prefix={pmcid}.")
    keys = re.findall(r"<Key>([^<]+)</Key>", r.text) if r is not None else []
    versions = sorted({k.split("/")[0] for k in keys if k.split("/")[0].startswith(pmcid + ".")},
                      key=lambda v: int(v.rsplit(".", 1)[-1]) if v.rsplit(".", 1)[-1].isdigit() else 0)
    if not versions:
        if r is None and not cl.last_error.startswith("not found"):
            _note(b, "PMC", "failed", f"the open-access archive could not be reached ({cl.last_error or 'no answer'})")
        else:
            _note(b, "PMC", "none", "this article is not in the open-access set")
        return
    v = versions[-1]
    man = cl.json(f"{PMC_S3}/{v}/{v}.json") or {}
    if man.get("is_retracted"):
        b.meta["is_retracted"] = True
    pmc = b.dir / "pmc"
    pmc.mkdir(exist_ok=True)

    def http(u: str) -> str:
        return u.split("?")[0].replace("s3://pmc-oa-opendata", PMC_S3)

    def name_of(u) -> str:
        return _safe_name(http(u).rsplit("/", 1)[-1]) if isinstance(u, str) and u else ""

    got: list[Path] = []
    media = [u for u in (man.get("media_urls") or []) if isinstance(u, str)][:MAX_MEDIA]
    for u in [man.get("xml_url"), man.get("pdf_url")] + media:
        name = name_of(u)
        if not name or re.search(r"\.t\d+\.(jpg|gif|png)$", name):
            continue                        # pictures of tables: the XML has the tables themselves
        p = cl.download(http(u), pmc / name)
        if p is not None:
            got.append(p)
    _note(b, "PMC", "ok" if got else "failed", f"{len(got)} files from the open-access package ({v})")
    # the manifest names this version's XML and PDF; never pick a file by globbing
    xml = pmc / name_of(man.get("xml_url")) if name_of(man.get("xml_url")) else None
    if xml is not None and xml in got:
        b.xml = str(xml.relative_to(b.dir))
        _from_jats(b, xml.read_text(errors="replace"), pmc)
    pdf = pmc / (name_of(man.get("pdf_url")) or f"{v}.pdf")
    if pdf in got and _is_pdf(pdf) and not b.pdf:
        b.pdf = str(pdf.relative_to(b.dir))
    # media the XML did not label: supplementary by PMC's naming (*.s001.xlsx), never figures (*.g001.jpg)
    named = {s["file"] for s in b.supplements} | {f["file"] for f in b.figures}
    for p in sorted(got):
        rel = str(p.relative_to(b.dir))
        if rel in named or p.suffix.lower() in (".xml", ".txt", ".json") or rel == b.pdf:
            continue
        if re.search(r"\.s\d+\.", p.name) or p.suffix.lower() in (".xlsx", ".xls", ".csv", ".docx", ".zip"):
            _add_supp(b, p, "PMC")


def _from_jats(b: Bundle, xml: str, folder: Path) -> None:
    """Figures and supplementary material named in the JATS XML."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml.encode())
    except ET.ParseError:
        return
    XL = "{http://www.w3.org/1999/xlink}href"
    present = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")]
    files = {p.stem.lower(): p for p in present}
    files.update({p.name.lower(): p for p in present})
    for fig in root.iter("fig"):
        label = "".join((fig.find("label").itertext() if fig.find("label") is not None else [])).strip()
        cap = " ".join("".join(fig.find("caption").itertext()).split()) if fig.find("caption") is not None else ""
        g = fig.find(".//graphic")
        if g is None:
            continue
        href = (g.get(XL) or "").lower()
        p = files.get(href) or files.get(Path(href).stem)
        if p is None:
            continue
        b.figures.append({"file": str(p.relative_to(b.dir)), "label": label or p.stem, "caption": f"{label} {cap}".strip()})
    for sm in root.iter("supplementary-material"):
        label = "".join(sm.find("label").itertext()).strip() if sm.find("label") is not None else ""
        for media in sm.iter("media"):
            href = (media.get(XL) or "").lower()
            p = files.get(href) or files.get(Path(href).stem)
            if p is not None:
                _add_supp(b, p, "PMC", label)


def _europepmc(cl: _Client, b: Bundle, pmcid: str) -> None:
    base = "https://www.ebi.ac.uk/europepmc/webservices/rest"
    if not b.xml:
        r = cl.get(f"{base}/{pmcid}/fullTextXML")
        if r is not None and r.text.lstrip().startswith("<"):
            p = b.dir / "europepmc.xml"
            p.write_text(r.text)
            b.xml = p.name
            _note(b, "Europe PMC", "ok", "full-text XML")
    if not b.supplements:
        z = cl.download(f"{base}/{pmcid}/supplementaryFiles", b.dir / "europepmc-supplements.zip")
        if z is not None and zipfile.is_zipfile(z):
            n = _unzip(b, z, "Europe PMC")
            _note(b, "Europe PMC", "ok" if n else "none", f"{n} supplementary files")
        else:
            _note(b, "Europe PMC", "none", "no supplementary files")
        if z is not None:
            z.unlink(missing_ok=True)


def _unzip(b: Bundle, archive: Path, source: str) -> int:
    """Members become supplements, up to MAX_SUPP for the whole bundle and MAX_UNZIP bytes in all.  A damaged
    archive is logged and skipped: one bad download must not end the fetch."""
    n = total = 0
    folder = b.dir / "supplements"
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if len(b.supplements) >= MAX_SUPP:
                    break
                if info.is_dir() or info.file_size > MAX_FILE or "__MACOSX" in info.filename:
                    continue
                if total + info.file_size > MAX_UNZIP:
                    b.log.append(f"{source}: archive larger than {MAX_UNZIP >> 20} MB; the rest was skipped")
                    break
                name = _safe_name(Path(info.filename).name)
                if not name or name.startswith("_"):
                    continue
                dest = folder / name
                k = 1
                while dest.exists():                # a/data.xlsx and b/data.xlsx are different files
                    k += 1
                    dest = folder / f"{Path(name).stem}-{k}{Path(name).suffix}"
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)   # bounded by the member's declared size
                total += info.file_size
                _add_supp(b, dest, source)
                n += 1
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError, NotImplementedError) as e:
        b.log.append(f"{source}: archive unreadable ({type(e).__name__}); kept {n} files from it")
    return n


PLOS_SLUG = {"pone": "plosone", "pbio": "plosbiology", "pmed": "plosmedicine", "pgen": "plosgenetics",
             "pcbi": "ploscompbiol", "ppat": "plospathogens", "pntd": "plosntds", "pclm": "climate",
             "pdig": "digitalhealth", "pgph": "globalpublichealth", "pwat": "water"}


def _plos(cl: _Client, b: Bundle) -> None:
    """PLOS serves original figure files and supplements at stable URLs, without the RETRACTED stamp that is
    printed over retracted PDFs (and PMC withholds the media of retracted papers)."""
    m = re.match(r"10\.1371/journal\.([a-z]+)\.\d+", b.doi)
    if not m or m.group(1) not in PLOS_SLUG:
        return
    base = f"https://journals.plos.org/{PLOS_SLUG[m.group(1)]}/article"
    xml = (b.dir / b.xml).read_text(errors="replace") if b.xml else ""
    if not xml:
        r = cl.get(f"{base}/file?id={quote(b.doi, safe='/')}&type=manuscript")
        if r is not None and r.text.lstrip().startswith("<"):
            (b.dir / "plos.xml").write_text(r.text)
            b.xml, xml = "plos.xml", r.text
    folder = b.dir / "plos"
    folder.mkdir(exist_ok=True)
    got_f = got_s = 0
    if not b.figures:
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(xml.encode()) if xml else None
        except ET.ParseError:
            root = None
        figs = []
        if root is not None:
            for fig in root.iter("fig"):
                label = "".join(fig.find("label").itertext()).strip() if fig.find("label") is not None else ""
                cap = " ".join("".join(fig.find("caption").itertext()).split()) if fig.find("caption") is not None else ""
                g = fig.find(".//graphic")
                href = (g.get("{http://www.w3.org/1999/xlink}href") or "") if g is not None else ""
                if href:
                    figs.append((href.rsplit("/", 1)[-1].replace(".tif", "").replace(".jpg", ""), label, cap))
        for k, (fid, label, cap) in enumerate(figs[:40]):
            ident = fid if fid.startswith("10.1371") else f"{b.doi}.{fid.split('.')[-1]}"
            p = cl.download(f"{base}/figure/image?size=original&id={quote(ident, safe='/')}",
                            folder / f"{_safe_name(fid)}.img")
            if p is None:
                continue
            if _is_html(p):
                p.unlink(missing_ok=True)
                continue
            got_f += 1
            b.figures.append({"file": str(p.relative_to(b.dir)), "label": label or f"Fig {k + 1}",
                              "caption": f"{label} {cap}".strip()})
    if not b.supplements:
        for k in range(1, MAX_SUPP + 1):
            ident = f"{b.doi}.s{k:03d}"
            tmp = cl.download(f"{base}/file?type=supplementary&id={quote(ident, safe='/')}",
                              b.dir / "supplements" / f".plos-s{k}")
            if tmp is None or tmp.stat().st_size == 0 or \
                    "text/html" in cl.headers.get("content-type", "") or _is_html(tmp):
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
                break                        # PLOS answers past the last file with an error page
            cd = cl.headers.get("content-disposition", "")
            nm = re.search(r'filename="?([^";]+)', cd)
            name = _safe_name(nm.group(1) if nm else f"S{k}{_ext_for(cl.headers.get('content-type', ''))}")
            dest = b.dir / "supplements" / name
            if dest.exists():
                dest = dest.with_name(f"S{k}-{name}")
            os.replace(tmp, dest)
            _add_supp(b, dest, "PLOS", f"S{k}")
            got_s += 1
    _note(b, "PLOS", "ok" if got_f or got_s else "none", f"{got_f} original figures, {got_s} supplementary files")


def _ext_for(ct: str) -> str:
    return {"application/pdf": ".pdf", "image/tiff": ".tif", "image/png": ".png", "image/jpeg": ".jpg",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "text/csv": ".csv", "application/zip": ".zip", "application/vnd.ms-excel": ".xls"}.get(ct.split(";")[0], "")


def _biorxiv(cl: _Client, b: Bundle) -> None:
    for server in ("biorxiv", "medrxiv"):
        d = cl.json(f"https://api.biorxiv.org/details/{server}/{doi_path(b.doi)}/na/json") or {}
        coll = d.get("collection") or []
        if not coll:
            continue
        last = coll[-1]
        b.meta.setdefault("preprint", {"server": server, "version": last.get("version"), "date": last.get("date")})
        _note(b, server, "ok", f"version {last.get('version')}")
        if not b.pdf:
            url = f"https://www.{server}.org/content/{doi_path(b.doi)}v{last.get('version')}.full.pdf"
            p = cl.download(url, b.dir / "paper.pdf", browser=True)
            if p and _is_pdf(p):
                b.pdf = p.name
            else:
                if p:
                    p.unlink(missing_ok=True)
                _note(b, server, "failed", "the preprint PDF could not be downloaded"
                      + (f" ({cl.last_error})" if cl.last_error and not p else ""))
        return


def _oa_pdf(cl: _Client, b: Bundle) -> None:
    for url in b.meta.get("oa_pdf_urls", [])[:6]:
        p = cl.download(url, b.dir / "paper.pdf", browser=True)
        if p and _is_pdf(p):
            b.pdf = p.name
            _note(b, "Open-access PDF", "ok", urlparse(url).netloc)
            return
        if p:
            p.unlink(missing_ok=True)
    _note(b, "Open-access PDF", "none" if not b.meta.get("oa_pdf_urls") else "failed",
          "none could be downloaded" if b.meta.get("oa_pdf_urls") else "none could be downloaded (no open copy listed)")


def _arxiv(cl: _Client, b: Bundle) -> None:
    """arXiv preprints: the PDF from arxiv.org itself (one request; arXiv asks automated readers to go slowly)."""
    ident = b.doi.split("10.48550/arxiv.", 1)[-1]
    if not ident:
        return
    p = cl.download(f"https://arxiv.org/pdf/{quote(ident, safe='/.')}", b.dir / "paper.pdf")
    if p and _is_pdf(p):
        b.pdf = p.name
        b.meta.setdefault("preprint", {"server": "arxiv", "id": ident})
        _note(b, "arXiv", "ok", "preprint PDF")
        return
    if p:
        p.unlink(missing_ok=True)
    _note(b, "arXiv", "failed", "the preprint PDF could not be downloaded" + (f" ({cl.last_error})" if cl.last_error else ""))


SUPP_WORDS = re.compile(r"suppl|supporting|additional file|appendix|source data|mmc\d|media-\d|dataset|\bS\d+\b", re.I)


def _landing(cl: _Client, b: Bundle) -> None:
    r = cl.get(b.meta.get("landing") or f"https://doi.org/{doi_path(b.doi)}", browser=True)
    if r is None:
        _note(b, "Publisher page", "failed", "not reachable (many publishers block automated readers)"
              + (f" - {cl.last_error}" if cl.last_error else ""))
        return
    page = _Links()
    try:
        page.feed(r.text)
    except Exception:
        return
    base = str(r.url)
    if not b.pdf:
        for u in page.meta.get("citation_pdf_url", [])[:2]:
            p = cl.download(urljoin(base, u), b.dir / "paper.pdf", browser=True)
            if p and _is_pdf(p):
                b.pdf = p.name
                _note(b, "Publisher page", "ok", "PDF")
                break
            if p:
                p.unlink(missing_ok=True)
    have = {s["name"].lower() for s in b.supplements}
    got = 0
    for href, text in page.links:
        if len(b.supplements) >= MAX_SUPP:
            break
        url = urljoin(base, href)
        ext = Path(urlparse(url).path).suffix.lower()
        if ext not in SUPP_EXT or ext in (".xml", ".json"):
            continue
        if not (SUPP_WORDS.search(text) or SUPP_WORDS.search(urlparse(url).path)):
            continue
        name = _safe_name(Path(urlparse(url).path).name)
        if name.lower() in have:
            continue
        p = cl.download(url, b.dir / "supplements" / name, browser=True)
        if p is None:
            continue
        if _is_html(p):
            p.unlink(missing_ok=True)
            _note(b, "Publisher page", "failed", f"{name} came back as a web page (login or cookie wall), not the file")
            have.add(name.lower())
            continue
        if ext == ".zip" and zipfile.is_zipfile(p):
            got += _unzip(b, p, "publisher page")
            p.unlink(missing_ok=True)
        else:
            _add_supp(b, p, "publisher page", text[:80])
            got += 1
        have.add(name.lower())
        time.sleep(0.3)
    _note(b, "Publisher page", "ok" if got else "none", f"{got} supplementary files")
