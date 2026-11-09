"""Whatever the user pastes, turned into a DOI the fetcher can use.

Accepted, one at a time or many at once:
  DOI            10.1/abc, doi:10.1/abc, https://doi.org/10.1/abc, percent-encoded, with trailing punctuation
  PubMed ID      PMID: 12345678, a pubmed.ncbi.nlm.nih.gov link, or a bare 5-9 digit number
  PMC ID         PMC1234567, a pmc.ncbi.nlm.nih.gov or Europe PMC link
  arXiv          arXiv:2101.12345v2, hep-th/9901001, an arxiv.org/abs or /pdf link
  bioRxiv/medRxiv links (they carry the DOI)
  any other article link   resolved through the DOI the page itself names in its <meta> tags
  a title or a citation    resolved through a Crossref search, and only when one record clearly matches

parse_input() and parse_batch() never touch the network.  resolve() and resolve_batch() do, one request at a
time, with timeouts and retries, and say in plain words what went wrong when they cannot find a DOI.
"""
from __future__ import annotations

import difflib
import ipaddress
import re
import time
import unicodedata
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

import httpx

from .fetch import BROWSER_UA, DOI_RE, RETRY_STATUS, Cancelled, _Client, _why, normalize, tidy

MAX_BATCH = 500                 # identifiers taken from one paste
MAX_PAGE = 2 * 1024 * 1024      # bytes of a landing page read when looking for its DOI
PAGE_TIMEOUT = 20.0
TITLE_MIN_WORDS = 4
TITLE_MATCH = 0.95              # similarity a Crossref title needs to count as the same title
NOT_ARTICLES = {"peer-review", "component", "grant", "dataset", "database"}
PAUSE = 0.2                     # seconds between lookups in a batch

LABELS = {"doi": "DOI", "pmid": "PubMed ID", "pmcid": "PubMed Central ID", "arxiv": "arXiv ID",
          "url": "web page", "title": "title or citation", "empty": "nothing", "unknown": "not recognised"}

_ARXIV_NEW = r"(\d{2}(?:0[1-9]|1[0-2])\.\d{4,5})(v\d+)?"
_ARXIV_OLD = r"([a-z][a-z\-]+(?:\.[a-z]{2})?/\d{2}(?:0[1-9]|1[0-2])\d{3})(v\d+)?"
ARXIV_RE = re.compile(rf"^(?:{_ARXIV_NEW}|{_ARXIV_OLD})$", re.I)
ARXIV_PREFIXED = re.compile(rf"\barxiv\s*:\s*(?:{_ARXIV_NEW}|{_ARXIV_OLD})", re.I)
PMCID_RE = re.compile(r"\bPMC\s?(\d{1,9})\b", re.I)
PMID_PREFIXED = re.compile(r"\b(?:pmid|pubmed(?:\s*id)?)\s*[:#]?\s*(\d{1,9})\b", re.I)
PMCID_PREFIXED = re.compile(r"\bpmcid\s*[:#]?\s*(?:PMC)?\s?(\d{1,9})\b", re.I)
BARE_PMID = re.compile(r"^\d{5,9}$")
URL_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.I)
# a line of a BibTeX or RIS export: only its identifier fields are read, never its title line
STRUCTURED = re.compile(r"^\s*(?:@\w+\s*[{(]|\w[\w-]*\s*=\s*[{\"]?|[A-Z][A-Z0-9]\s{1,2}-\s?)")


# ---------------------------------------------------------------- parsing (no network)

def _item(kind: str, value: str = "", text: str = "", **extra) -> dict:
    d = {"kind": kind, "value": value, "label": LABELS[kind], "input": text}
    d.update(extra)
    return d


def _arxiv(m: re.Match) -> dict:
    ident = (m.group(1) or m.group(3)).lower()
    version = m.group(2) or m.group(4) or ""
    return _item("arxiv", ident, m.group(0), version=version.lower(), doi=f"10.48550/arxiv.{ident}")


def _from_url(s: str) -> Optional[dict]:
    """Links whose path names the paper directly: PubMed, PMC, Europe PMC, arXiv, and any link holding a DOI."""
    u = urlsplit(s if "://" in s else "https://" + s)
    host = (u.hostname or "").lower()
    path = unquote(u.path)
    if host.endswith("arxiv.org"):
        m = re.search(r"/(?:abs|pdf|html|format)/(.+?)(?:\.pdf)?/?$", path, re.I)
        if m:
            a = ARXIV_RE.match(m.group(1))
            if a:
                return _arxiv(a)
    if host in ("pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov") and "/pmc/" not in path:
        m = re.match(r"^/(?:pubmed/)?(\d{1,9})/?$", path)
        if m:
            return _item("pmid", m.group(1), s)
    if host.endswith("ncbi.nlm.nih.gov") or host.endswith("europepmc.org"):
        m = re.search(r"/(?:articles|article/PMC|abstract/PMC)/PMC(\d{1,9})\b", path, re.I)
        if m:
            return _item("pmcid", "PMC" + m.group(1), s)
    if host.endswith("europepmc.org"):
        m = re.search(r"/(?:article|abstract)/MED/(\d{1,9})\b", path, re.I)
        if m:
            return _item("pmid", m.group(1), s)
    try:
        return _item("doi", normalize(s), s)
    except ValueError:
        return None


def parse_input(text: str) -> dict:
    """What one pasted identifier is: {kind, value, label, input} and, for arXiv, the arXiv DOI.
    kind: doi | pmid | pmcid | arxiv | url | title | empty | unknown.  Never touches the network."""
    raw = text or ""
    s = " ".join(tidy(raw).split()).strip().strip("<>\"'`“”‘’").strip()
    if not s:
        return _item("empty", "", raw, message="Paste a DOI, a PubMed or PMC ID, an arXiv ID, a link or a title.")
    if re.search(r"%[0-9a-f]{2}", s, re.I) and not URL_RE.match(s):
        s = unquote(s)
    if URL_RE.match(s):
        hit = _from_url(s)
        if hit:
            return hit
        return _item("url", s if "://" in s else "https://" + s, raw)
    m = ARXIV_PREFIXED.search(s)
    if m and not DOI_RE.search(s):
        return _arxiv(m)
    if DOI_RE.search(s):
        try:
            return _item("doi", normalize(s), raw)
        except ValueError:
            pass
    m = PMCID_PREFIXED.search(s) or PMCID_RE.search(s)
    if m and len(s) <= 40:
        return _item("pmcid", "PMC" + m.group(1), raw)
    m = PMID_PREFIXED.search(s)
    if m and len(s) <= 40:
        return _item("pmid", m.group(1).lstrip("0") or "0", raw)
    if BARE_PMID.match(s):
        return _item("pmid", s.lstrip("0") or "0", raw)
    m = ARXIV_RE.match(s)
    if m:
        return _arxiv(m)
    words = re.findall(r"[^\W\d_]{2,}", s)
    if len(words) >= TITLE_MIN_WORDS and len(s) >= 20:
        return _item("title", s[:500], raw)
    return _item("unknown", s, raw, message="This does not look like a DOI, PubMed ID, PMC ID, arXiv ID, link or "
                                            "title.")


def _key(it: dict) -> tuple:
    k = it["kind"]
    if k == "arxiv":
        return ("doi", it["doi"])
    if k == "title":
        return (k, _norm_title(it["value"]))
    if k == "url":
        return (k, it["value"].rstrip("/").lower())
    return (k, it["value"].lower())


def _strong(token: str) -> Optional[dict]:
    """A token that names a paper on its own, whatever else is on the line."""
    t = token.strip(",;.()[]{}<>\"'")
    if not t:
        return None
    if URL_RE.match(t) or DOI_RE.search(t) or re.match(r"^(PMC\d{1,9}|arxiv:.+|pmid:?\d+|pmcid:?\S+)$", t, re.I):
        it = parse_input(t)
        return it if it["kind"] not in ("unknown", "empty", "title") else None
    return None


def _weak(token: str) -> Optional[dict]:
    """A bare number or a bare arXiv ID: an identifier only when the whole line is a list of them."""
    t = token.strip(",;.()[]{}\"'")
    if BARE_PMID.match(t) or ARXIV_RE.match(t):
        return parse_input(t)
    return None


def _tokens(line: str) -> list[str]:
    out = []
    # "PMID: 123" and "doi: 10.1/x" are one token each
    line = re.sub(r"\b(pmid|pmcid|doi|arxiv)\s*:\s+(?=\S)", lambda m: m.group(1) + ":", line, flags=re.I)
    for tok in line.split():
        if re.fullmatch(r"[\d,;]+", tok):                   # 123456,234567
            out += [x for x in re.split(r"[,;]", tok) if x]
            continue
        # 10.1/a,10.1/b and PMC1;PMC2 - cut only where the next identifier starts, never inside a DOI
        out += [x for x in re.split(r"[,;](?=(?:10\.\d{4,9}/|https?://|doi:|pmc\d|pmid|arxiv:))", tok, flags=re.I)
                if x]
    return out


def parse_batch(text: str, limit: int = MAX_BATCH) -> list[dict]:
    """Many identifiers pasted at once: one per line, or separated by commas, semicolons or spaces, in any mix of
    kinds.  A line with no identifier is read as a title.  Lines of a BibTeX or RIS export give only their DOI,
    PMID or PMC fields.  Duplicates (the same DOI written two ways, an arXiv ID and its arXiv DOI) are dropped,
    and at most `limit` items are returned.  Each item is a parse_input() result with its line number."""
    seen: set[tuple] = set()
    out: list[dict] = []

    def add(it: dict, n: int) -> bool:
        k = _key(it)
        if k in seen:
            return True
        seen.add(k)
        it["line"] = n
        out.append(it)
        return len(out) < limit

    for n, raw in enumerate(tidy(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", "%")) or re.fullmatch(r"[\W_]+", line):
            continue
        if STRUCTURED.match(line):
            f = re.match(r"^\s*(pmid|pmcid)\s*=\s*[{\"]?\s*(PMC)?(\d{1,9})", line, re.I)
            its = [parse_input(("PMC" if f.group(1).lower() == "pmcid" else "PMID ") + f.group(3))] if f else \
                [it for it in map(_strong, _tokens(line)) if it and it["kind"] != "url"]
        else:
            toks = _tokens(line)
            strong = [_strong(t) for t in toks]
            if all(strong):
                its = strong
            elif all(s or _weak(t) for s, t in zip(strong, toks)):
                its = [s or _weak(t) for s, t in zip(strong, toks)]
            elif any(strong):
                its = [s for s in strong if s]                 # a citation that carries its DOI or PMID
            else:
                one = parse_input(line)
                its = [one]
        for it in its:
            if it is None:
                continue
            if not add(it, n):
                return out
    return out


# ---------------------------------------------------------------- resolving (network)

def _result(item: dict, doi: Optional[str], message: str, candidates: Optional[list] = None, **extra) -> dict:
    d = {"kind": item["kind"], "input": item.get("input", ""), "value": item.get("value", ""), "doi": doi,
         "candidates": candidates or [], "message": message}
    d.update(extra)
    return d


def _ncbi_idconv(cl: _Client, ident: str) -> tuple[Optional[str], str]:
    data, st = cl.lookup("https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/", ids=ident, format="json",
                         tool="paperinspector")
    for rec in (data or {}).get("records", []):
        if rec.get("doi") and not rec.get("errmsg"):
            return rec["doi"], "ok"
    return None, st


def _europepmc(cl: _Client, query: str) -> tuple[Optional[str], str]:
    data, st = cl.lookup("https://www.ebi.ac.uk/europepmc/webservices/rest/search", query=query, format="json",
                         resultType="lite", pageSize=2)
    hits = ((data or {}).get("resultList") or {}).get("result") or []
    dois = {h["doi"].lower() for h in hits if h.get("doi")}
    if len(dois) == 1:
        return dois.pop(), "ok"
    return None, st


def _from_pubmed(cl: _Client, item: dict) -> dict:
    v = item["value"]
    name = LABELS[item["kind"]]
    doi, st1 = _ncbi_idconv(cl, v)
    st2 = "ok"
    if not doi:
        time.sleep(PAUSE)
        q = f"PMCID:{v}" if item["kind"] == "pmcid" else f"EXT_ID:{v} AND SRC:MED"
        doi, st2 = _europepmc(cl, q)
    if doi:
        try:
            return _result(item, normalize(doi), f"Found the DOI for {name} {v}.")
        except ValueError:
            pass
    if st1 == "failed" and st2 == "failed":
        return _result(item, None, f"Could not look up {name} {v}: PubMed Central and Europe PMC did not answer "
                                   f"({cl.last_error or 'offline?'}). Try again, or paste the DOI.", error="network")
    return _result(item, None, f"No DOI is recorded for {name} {v}. Paste the DOI from the article page, or upload "
                               "the PDF.")


class _Meta(HTMLParser):
    """<meta name|property=... content=...> pairs, lower-cased names; stops caring after </head>."""

    def __init__(self):
        super().__init__()
        self.meta: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        name = (a.get("name") or a.get("property") or "").strip().lower()
        if name and a.get("content"):
            self.meta.setdefault(name, []).append(a["content"].strip())


DOI_META = ("citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi", "bepress_citation_doi", "eprints.doi")


def _private_host(host: str) -> bool:
    """A link to this computer or the local network is never fetched: the app only reads public article pages."""
    h = host.strip("[]").lower()
    if h in ("localhost", "") or h.endswith((".local", ".localhost", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def dois_in_page(html: str) -> list[str]:
    """DOIs a landing page declares about itself, from the first meta field that has any.  Links and references
    on the page are ignored: they name other papers."""
    p = _Meta()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    for name in DOI_META:
        found = []
        for v in p.meta.get(name, []):
            v = re.sub(r"^\s*(doi|info:doi/)\s*:?\s*", "", v, flags=re.I)
            if not DOI_RE.match(v.strip()) and not v.lower().startswith(("http", "doi.org")):
                continue
            try:
                d = normalize(v)
            except ValueError:
                continue
            if d not in found:
                found.append(d)
        if found:
            return found
    return []


def _read_page(cl: _Client, url: str) -> tuple[Optional[str], str]:
    """(page text, final URL) of a public page, at most MAX_PAGE bytes; (None, reason) when it cannot be read."""
    attempts = 2
    for attempt in range(attempts):
        cl.check()
        try:
            with cl.c.stream("GET", url, headers={"User-Agent": BROWSER_UA}, timeout=PAGE_TIMEOUT) as r:
                if r.status_code != 200:
                    if r.status_code in RETRY_STATUS and attempt + 1 < attempts:
                        cl._wait(r, attempt)
                        continue
                    return None, _why(r)
                ctype = r.headers.get("content-type", "")
                if ctype and "html" not in ctype and "xml" not in ctype:
                    return None, "the link is a file, not an article page"
                buf = bytearray()
                for chunk in r.iter_bytes(1 << 16):
                    cl.check()
                    buf += chunk
                    if len(buf) >= MAX_PAGE:
                        break
                enc = r.encoding or "utf-8"
                return bytes(buf).decode(enc, errors="replace"), str(r.url)
        except httpx.TimeoutException:
            if attempt + 1 < attempts:
                cl._wait(None, attempt)
                continue
            return None, "the site did not answer in time"
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            return None, _why(exc=e)
    return None, "the site did not answer"


def _from_page(cl: _Client, item: dict) -> dict:
    url = item["value"]
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or _private_host(u.hostname or ""):
        return _result(item, None, "Only public http(s) article links can be read.")
    html, info = _read_page(cl, url)
    if html is None:
        return _result(item, None, f"Could not read that page: {info}. Paste the DOI instead, or upload the PDF.",
                       error="network")
    if info != url and parse_input(info)["kind"] == "doi":         # redirected to a link that holds the DOI
        return _result(item, parse_input(info)["value"], "Found the DOI in the page address.")
    found = dois_in_page(html)
    if len(found) == 1:
        return _result(item, found[0], "Found the DOI the page names for itself.")
    if len(found) > 1:
        return _result(item, None, "That page names more than one DOI. Pick the right one.",
                       candidates=[{"doi": d, "title": "", "journal": "", "year": "", "first_author": "",
                                    "type": "", "match": 0.0} for d in found[:5]])
    return _result(item, None, "That page does not name a DOI. Paste the DOI instead, or upload the PDF.")


def _norm_title(t: str) -> str:
    t = re.sub(r"<[^>]+>", " ", t or "")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^\w]+", " ", t).replace("_", " ").split())


def _year(it: dict) -> str:
    for k in ("issued", "published-print", "published-online", "published"):
        parts = ((it.get(k) or {}).get("date-parts") or [[None]])[0] or [None]
        if parts and parts[0]:
            return str(parts[0])
    return ""


def _candidate(it: dict, nq: str) -> dict:
    title = re.sub(r"<[^>]+>", "", (it.get("title") or [""])[0]).strip()
    nt = _norm_title(title)
    authors = it.get("author") or []
    first = (authors[0].get("family") or authors[0].get("name") or "") if authors else ""
    sim = difflib.SequenceMatcher(None, nq, nt).ratio() if nt else 0.0
    inside = bool(nt) and len(nt.split()) >= 6 and f" {nt} " in f" {nq} "
    return {"doi": (it.get("DOI") or "").lower(), "title": title, "journal": (it.get("container-title") or [""])[0],
            "year": _year(it), "first_author": first, "type": it.get("type", ""),
            "match": round(1.0 if inside else sim, 3)}


def _from_title(cl: _Client, item: dict) -> dict:
    """A Crossref bibliographic search.  Accepted only when exactly one record's title is the same as the text
    pasted (or, for a pasted citation, appears whole inside it); otherwise the best records come back to pick
    from.  A preprint and its journal version share a title, so they always come back as a choice."""
    q = item["value"]
    nq = _norm_title(q)
    data, st = cl.lookup("https://api.crossref.org/works", **{
        "query.bibliographic": q[:300], "rows": 5,
        "select": "DOI,title,container-title,issued,author,type"})
    if st != "ok":
        return _result(item, None, f"Could not search Crossref ({cl.last_error or 'offline?'}). Try again, or paste "
                                   "the DOI.", error="network")
    items = [it for it in ((data or {}).get("message") or {}).get("items", []) if it.get("DOI") and it.get("title")]
    cands = [_candidate(it, nq) for it in items]
    # a peer-review report or a figure record can share the article's title; never pick one of those
    matches = {c["doi"] for c in cands if c["match"] >= TITLE_MATCH and c["type"] not in NOT_ARTICLES}
    cands.sort(key=lambda c: -c["match"])
    if len(matches) == 1 and len(nq.split()) >= TITLE_MIN_WORDS:
        doi = matches.pop()
        try:
            return _result(item, normalize(doi), "Found one article with this title.",
                           candidates=[c for c in cands if c["doi"] == doi][:1])
        except ValueError:
            pass
    if not cands:
        return _result(item, None, "Crossref found no article for this text. Check the title, or paste the DOI.")
    msg = ("More than one article has this title (for example a preprint and the journal version). Pick one."
           if len(matches) > 1 else "No article matched this text exactly. Pick one of these, or paste the DOI.")
    return _result(item, None, msg, candidates=cands[:5])


def resolve(text: str, client: Optional[httpx.Client] = None,
            should_cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Turn one pasted identifier into a DOI.
    Returns {kind, input, value, doi, candidates, message}: `doi` is set when there is one confident answer;
    otherwise `candidates` lists records to pick from (possibly none) and `message` says why, in plain words.
    `error: 'network'` is added when the answer is unknown because a service could not be reached.
    Raises Cancelled when should_cancel() turns true."""
    item = parse_input(text)
    return _resolve_item(item, client, should_cancel)


def _resolve_item(item: dict, client: Optional[httpx.Client], should_cancel) -> dict:
    k = item["kind"]
    if k == "doi":
        return _result(item, item["value"], "")
    if k == "arxiv":
        return _result(item, item["doi"], "This is an arXiv preprint. If it was later published in a journal, paste "
                                          "that DOI to screen the published version.")
    if k in ("empty", "unknown"):
        return _result(item, None, item.get("message", ""))
    own = client is None
    cl = _Client(timeout=PAGE_TIMEOUT, should_cancel=should_cancel, client=client)
    try:
        if k in ("pmid", "pmcid"):
            return _from_pubmed(cl, item)
        if k == "url":
            return _from_page(cl, item)
        return _from_title(cl, item)
    finally:
        if own:
            cl.close()


def resolve_batch(text: str, client: Optional[httpx.Client] = None,
                  should_cancel: Optional[Callable[[], bool]] = None, limit: int = MAX_BATCH,
                  progress: Optional[Callable[[str, float], None]] = None) -> list[dict]:
    """parse_batch() then resolve() each item, one at a time with a short pause between network lookups.  Two
    inputs that turn out to be the same paper (a PMID and its DOI) are kept once; the later one is marked
    `duplicate_of` with the DOI and carries no work."""
    items = parse_batch(text, limit)
    out: list[dict] = []
    seen: dict[str, int] = {}
    for i, it in enumerate(items):
        if progress:
            progress(f"Reading item {i + 1} of {len(items)}", i / max(1, len(items)))
        needs_net = it["kind"] in ("pmid", "pmcid", "url", "title")
        r = _resolve_item(it, client, should_cancel)
        r["line"] = it.get("line")
        if r["doi"] and r["doi"] in seen:
            r["duplicate_of"] = r["doi"]
        elif r["doi"]:
            seen[r["doi"]] = len(out)
        out.append(r)
        if needs_net and i + 1 < len(items):
            time.sleep(PAUSE)
    return out


__all__ = ["parse_input", "parse_batch", "resolve", "resolve_batch", "dois_in_page", "Cancelled", "LABELS"]
