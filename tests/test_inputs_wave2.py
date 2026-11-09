"""Smarter input (inputs.py) and a sturdier fetch (fetch.py): identifiers in any form, title search, batches,
retries with backoff, per-source status lines, and cancelling.  Nothing here touches the internet."""
import json
import time

import httpx
import pytest

from paperinspector import fetch, inputs


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: waits.append(s))
    return waits


def _client(handler, seen=None):
    def h(req):
        if seen is not None:
            seen.append(req.url)
        return handler(req)
    return httpx.Client(transport=httpx.MockTransport(h), follow_redirects=True)


def _offline(req):
    raise AssertionError(f"no request expected, got {req.url}")


def _patch_fetch_client(monkeypatch, handler):
    seen: list[httpx.URL] = []

    def init(self, timeout=30.0, should_cancel=None, client=None):
        self.c = _client(handler, seen)
        self.headers = {}
        self.should_cancel = should_cancel
        self.last_error = ""
    monkeypatch.setattr(fetch._Client, "__init__", init)
    return seen


# ---------------------------------------------------------------- parse_input: one identifier, no network

@pytest.mark.parametrize("raw, kind, value", [
    ("https://doi.org/10.1038/S41586-020-2012-7.", "doi", "10.1038/s41586-020-2012-7"),
    ("https://dx.doi.org/10.1038/s41586-020-2012-7", "doi", "10.1038/s41586-020-2012-7"),
    ("doi: 10.1000/xyz123", "doi", "10.1000/xyz123"),
    ("DOI:10.1000/XYZ123);", "doi", "10.1000/xyz123"),
    ("10.1371%2Fjournal.pone.0123456", "doi", "10.1371/journal.pone.0123456"),
    ("  <10.1000/xyz123>  ", "doi", "10.1000/xyz123"),
    ("10.1038/s41586‐020‐2012‐7", "doi", "10.1038/s41586-020-2012-7"),   # hyphens from a PDF
    ("10.1000/ab­c​", "doi", "10.1000/abc"),                                   # soft hyphen, zero width
    ("https://www.biorxiv.org/content/10.1101/2020.01.01.123456v2.full", "doi", "10.1101/2020.01.01.123456"),
    ("https://www.medrxiv.org/content/10.1101/2021.02.03.654321v1", "doi", "10.1101/2021.02.03.654321"),
    ("PMID: 31452104", "pmid", "31452104"),
    ("pmid 31452104", "pmid", "31452104"),
    ("31452104", "pmid", "31452104"),
    ("https://pubmed.ncbi.nlm.nih.gov/31452104/", "pmid", "31452104"),
    ("https://www.ncbi.nlm.nih.gov/pubmed/31452104", "pmid", "31452104"),
    ("https://europepmc.org/article/MED/31452104", "pmid", "31452104"),
    ("PMC6994123", "pmcid", "PMC6994123"),
    ("pmcid: 6994123", "pmcid", "PMC6994123"),
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC6994123/", "pmcid", "PMC6994123"),
    ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6994123/", "pmcid", "PMC6994123"),
    ("https://europepmc.org/article/PMC/PMC6994123", "pmcid", "PMC6994123"),
    ("arXiv:2101.12345v2", "arxiv", "2101.12345"),
    ("2101.12345", "arxiv", "2101.12345"),
    ("https://arxiv.org/abs/2101.12345v3", "arxiv", "2101.12345"),
    ("https://arxiv.org/pdf/2101.12345v3.pdf", "arxiv", "2101.12345"),
    ("hep-th/9901001", "arxiv", "hep-th/9901001"),
    ("https://www.nature.com/articles/s41586-020-2012-7", "url", "https://www.nature.com/articles/s41586-020-2012-7"),
    ("www.example.org/article/123", "url", "https://www.example.org/article/123"),
    ("Cell cycle control of chromatin accessibility in yeast", "title",
     "Cell cycle control of chromatin accessibility in yeast"),
])
def test_parse_input_kinds(raw, kind, value):
    it = inputs.parse_input(raw)
    assert (it["kind"], it["value"]) == (kind, value)
    assert it["label"] == inputs.LABELS[kind]


def test_parse_input_arxiv_gives_its_datacite_doi():
    it = inputs.parse_input("arXiv:2101.12345v2")
    assert it["doi"] == "10.48550/arxiv.2101.12345" and it["version"] == "v2"
    assert fetch.normalize("10.48550/arXiv.2101.12345v2") == "10.48550/arxiv.2101.12345"


@pytest.mark.parametrize("raw, kind", [
    ("", "empty"), ("   ", "empty"), ("not a doi", "unknown"), ("2020", "unknown"), ("1234", "unknown"),
    ("hello", "unknown"), ("2113.12345", "unknown"),            # month 13 is no arXiv ID
])
def test_parse_input_rejects_what_it_cannot_read(raw, kind):
    it = inputs.parse_input(raw)
    assert it["kind"] == kind and it.get("message")


def test_normalize_drops_unbalanced_closing_brace_from_bibtex():
    assert fetch.normalize("{10.1234/abc},") == "10.1234/abc"
    assert fetch.normalize("10.1234/a{b}") == "10.1234/a{b}"


# ---------------------------------------------------------------- parse_batch

def test_parse_batch_mixed_separators_and_deduplication():
    text = """10.1234/a,10.1234/b;https://doi.org/10.1234/A
    PMID: 123456, PMID: 234567
    123456 PMC1
    arXiv:2101.12345 10.48550/arXiv.2101.12345v2
    10.1002/(SICI)1097-4636(199706)35:4<492::AID-JBM9>3.0.CO;2-#
    """
    got = [(x["kind"], x["value"]) for x in inputs.parse_batch(text)]
    assert got == [("doi", "10.1234/a"), ("doi", "10.1234/b"), ("pmid", "123456"), ("pmid", "234567"),
                   ("pmcid", "PMC1"), ("arxiv", "2101.12345"),
                   ("doi", "10.1002/(sici)1097-4636(199706)35:4<492::aid-jbm9>3.0.co;2-#")]


def test_parse_batch_reads_titles_and_citations_but_not_numbers_inside_them():
    text = ("A study of 2020 results from 12345 mice in four labs\n"
            "Author A. A study of things in cells. J Biol 2020;1:2. doi:10.5555/cite.1\n")
    got = inputs.parse_batch(text)
    assert [(x["kind"], x["line"]) for x in got] == [("title", 1), ("doi", 2)]
    assert got[1]["value"] == "10.5555/cite.1"


def test_parse_batch_bibtex_and_ris_give_identifiers_only():
    text = """@article{key2020,
      title = {A long title of a paper that must not be searched},
      doi = {10.1234/ABC},
      url = {https://example.org/landing},
      pmid = {7654321},
    }
    TY  - JOUR
    TI  - Another long title that is not an identifier
    DO  - 10.5555/xyz
    """
    got = [(x["kind"], x["value"]) for x in inputs.parse_batch(text)]
    assert got == [("doi", "10.1234/abc"), ("pmid", "7654321"), ("doi", "10.5555/xyz")]


def test_parse_batch_limit_and_blank_input():
    assert inputs.parse_batch("") == []
    many = "\n".join(f"10.1234/x{i}" for i in range(30))
    assert len(inputs.parse_batch(many, limit=10)) == 10


# ---------------------------------------------------------------- resolve: no network needed

def test_resolve_doi_and_arxiv_without_network():
    c = _client(_offline)
    assert inputs.resolve("https://doi.org/10.1234/ABC.", client=c)["doi"] == "10.1234/abc"
    r = inputs.resolve("arXiv:2101.12345v1", client=c)
    assert r["doi"] == "10.48550/arxiv.2101.12345" and "preprint" in r["message"]
    r = inputs.resolve("not a doi", client=c)
    assert r["doi"] is None and r["candidates"] == [] and r["message"]


# ---------------------------------------------------------------- resolve: PubMed / PMC

def test_resolve_pmid_through_ncbi():
    def h(req):
        if req.url.host == "pmc.ncbi.nlm.nih.gov":
            assert "email" not in str(req.url)
            return httpx.Response(200, json={"records": [{"pmid": "31452104", "pmcid": "PMC6994123",
                                                          "doi": "10.1000/ABC.1"}]})
        return httpx.Response(404)
    r = inputs.resolve("PMID: 31452104", client=_client(h))
    assert r["doi"] == "10.1000/abc.1" and r["kind"] == "pmid"


def test_resolve_pmid_falls_back_to_europepmc():
    seen = []

    def h(req):
        if req.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, json={"records": [{"pmid": "123456", "status": "error",
                                                          "errmsg": "invalid article id"}]})
        if req.url.host == "www.ebi.ac.uk":
            assert req.url.params["query"] == "EXT_ID:123456 AND SRC:MED"
            return httpx.Response(200, json={"resultList": {"result": [{"pmid": "123456", "doi": "10.5555/old.7"}]}})
        return httpx.Response(404)
    r = inputs.resolve("123456", client=_client(h, seen))
    assert r["doi"] == "10.5555/old.7"
    assert [u.host for u in seen] == ["pmc.ncbi.nlm.nih.gov", "www.ebi.ac.uk"]


def test_resolve_pmid_without_doi_says_so():
    def h(req):
        if req.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, json={"records": [{"pmid": "123456", "errmsg": "not found"}]})
        return httpx.Response(200, json={"resultList": {"result": [{"pmid": "123456"}]}})
    r = inputs.resolve("PMID 123456", client=_client(h))
    assert r["doi"] is None and "No DOI is recorded" in r["message"] and "error" not in r


def test_resolve_pmid_offline_is_not_reported_as_missing(no_sleep):
    r = inputs.resolve("PMC6994123", client=_client(lambda req: httpx.Response(503)))
    assert r["doi"] is None and r["error"] == "network" and "did not answer" in r["message"]
    assert no_sleep and all(w <= 30 for w in no_sleep)          # backed off before giving up


# ---------------------------------------------------------------- resolve: landing pages

PAGE = """<html><head><meta name="citation_title" content="A paper">
<meta name="citation_doi" content="{doi}">{extra}</head><body>
<a href="https://doi.org/10.9999/someone.else">a reference</a></body></html>"""


def _page_handler(html, status=200, ctype="text/html; charset=utf-8"):
    def h(req):
        return httpx.Response(status, text=html, headers={"content-type": ctype})
    return h


def test_resolve_landing_page_by_citation_doi():
    r = inputs.resolve("https://journal.example/articles/abc123",
                       client=_client(_page_handler(PAGE.format(doi="doi:10.4321/J.ABC.123", extra=""))))
    assert r["doi"] == "10.4321/j.abc.123" and r["kind"] == "url"


def test_resolve_landing_page_with_dc_identifier_only():
    html = '<html><head><meta name="DC.Identifier" content="https://doi.org/10.4321/dc.9"></head></html>'
    assert inputs.resolve("https://journal.example/a/9", client=_client(_page_handler(html)))["doi"] == "10.4321/dc.9"


def test_resolve_landing_page_ignores_dois_in_links_and_references():
    html = ('<html><head><meta name="citation_title" content="x"></head><body>'
            '<a href="https://doi.org/10.9999/ref.1">ref</a> doi:10.9999/ref.2</body></html>')
    r = inputs.resolve("https://journal.example/a/1", client=_client(_page_handler(html)))
    assert r["doi"] is None and r["candidates"] == [] and "does not name a DOI" in r["message"]


def test_resolve_landing_page_with_two_dois_asks():
    html = PAGE.format(doi="10.4321/one", extra='<meta name="citation_doi" content="10.4321/two">')
    r = inputs.resolve("https://journal.example/a/2", client=_client(_page_handler(html)))
    assert r["doi"] is None and [c["doi"] for c in r["candidates"]] == ["10.4321/one", "10.4321/two"]


def test_resolve_landing_page_blocked_or_a_file(no_sleep):
    r = inputs.resolve("https://journal.example/a/3", client=_client(_page_handler("denied", status=403)))
    assert r["doi"] is None and r["error"] == "network" and "refused automated access" in r["message"]
    r = inputs.resolve("https://journal.example/a/3.pdf",
                       client=_client(_page_handler("%PDF-1.4", ctype="application/pdf")))
    assert r["doi"] is None and "a file" in r["message"]


def test_resolve_landing_page_retries_when_busy(no_sleep):
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, headers={"retry-after": "2"})
        return httpx.Response(200, text=PAGE.format(doi="10.4321/busy", extra=""), headers={"content-type": "text/html"})
    assert inputs.resolve("https://journal.example/a/4", client=_client(h))["doi"] == "10.4321/busy"
    assert len(calls) == 2 and sum(no_sleep) == pytest.approx(2.0)


@pytest.mark.parametrize("url", ["http://localhost:8000/x", "http://127.0.0.1/x", "http://192.168.1.10/paper",
                                 "http://[::1]/x", "ftp://journal.example/x"])
def test_resolve_never_fetches_local_addresses(url):
    r = inputs.resolve(url, client=_client(_offline))
    assert r["doi"] is None and r["message"]


# ---------------------------------------------------------------- resolve: titles via Crossref

TITLE = "Cell cycle control of chromatin accessibility in yeast"


def _crossref(items):
    def h(req):
        assert req.url.host == "api.crossref.org" and req.url.params.get("query.bibliographic")
        return httpx.Response(200, json={"message": {"items": items}})
    return h


def _rec(doi, title, typ="journal-article", year=2020):
    return {"DOI": doi, "title": [title], "type": typ, "container-title": ["J Test"],
            "issued": {"date-parts": [[year]]}, "author": [{"family": "Author", "given": "A"}]}


def test_resolve_title_one_confident_match():
    r = inputs.resolve(TITLE.upper() + ".", client=_client(_crossref([
        _rec("10.1000/Right", TITLE), _rec("10.1000/other", "Chromatin accessibility in flies")])))
    assert r["doi"] == "10.1000/right" and r["kind"] == "title"
    assert r["candidates"][0]["year"] == "2020"


def test_resolve_title_from_a_pasted_citation():
    cite = f"Author A, Author B. {TITLE}. J Test. 2020;12(3):45-67."
    r = inputs.resolve(cite, client=_client(_crossref([_rec("10.1000/right", TITLE)])))
    assert r["doi"] == "10.1000/right"


def test_resolve_title_preprint_and_journal_version_asks():
    r = inputs.resolve(TITLE, client=_client(_crossref([
        _rec("10.1000/journal", TITLE), _rec("10.1101/2019.01.01.000001", TITLE, typ="posted-content", year=2019)])))
    assert r["doi"] is None and {c["doi"] for c in r["candidates"]} == {"10.1000/journal", "10.1101/2019.01.01.000001"}
    assert "preprint" in r["message"]


def test_resolve_title_near_miss_returns_candidates_only():
    r = inputs.resolve(TITLE, client=_client(_crossref([
        _rec("10.1000/near", "Cell cycle control of chromatin accessibility in fission yeast and mice")])))
    assert r["doi"] is None and r["candidates"][0]["doi"] == "10.1000/near"
    assert r["candidates"][0]["match"] < inputs.TITLE_MATCH


def test_resolve_title_never_picks_a_peer_review_record():
    r = inputs.resolve(TITLE, client=_client(_crossref([_rec("10.1000/review", TITLE, typ="peer-review")])))
    assert r["doi"] is None and r["candidates"]


def test_resolve_title_crossref_down(no_sleep):
    r = inputs.resolve(TITLE, client=_client(lambda req: httpx.Response(502)))
    assert r["doi"] is None and r["error"] == "network" and "Could not search Crossref" in r["message"]


# ---------------------------------------------------------------- resolve_batch

def test_resolve_batch_marks_the_same_paper_once():
    def h(req):
        if req.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, json={"records": [{"pmid": "31452104", "doi": "10.1000/abc.1"}]})
        return httpx.Response(404)
    out = inputs.resolve_batch("10.1000/ABC.1\nPMID: 31452104\n10.1000/other", client=_client(h))
    assert [r["doi"] for r in out] == ["10.1000/abc.1", "10.1000/abc.1", "10.1000/other"]
    assert "duplicate_of" not in out[0] and out[1]["duplicate_of"] == "10.1000/abc.1"
    assert [r["line"] for r in out] == [1, 2, 3]


def test_resolve_batch_can_be_cancelled():
    stop = {"now": False}

    def h(req):
        stop["now"] = True
        return httpx.Response(200, json={"records": [{"doi": "10.1000/x"}]})
    with pytest.raises(inputs.Cancelled):
        inputs.resolve_batch("PMID: 111111\nPMID: 222222", client=_client(h), should_cancel=lambda: stop["now"])


# ---------------------------------------------------------------- fetch client: retries and reasons

def test_get_retries_busy_answers_with_backoff(no_sleep):
    answers = iter([httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(503),
                    httpx.Response(200, text="ok")])
    cl = fetch._Client(client=_client(lambda req: next(answers)))
    r = cl.get("https://example.org/x")
    assert r is not None and r.text == "ok"
    assert sum(no_sleep) == pytest.approx(3.0 + 2.0)            # Retry-After, then 1 s doubled


@pytest.mark.parametrize("status, reason", [(404, "not found"), (500, "server had an error"),
                                            (403, "refused automated access")])
def test_get_does_not_retry_lasting_errors(status, reason, no_sleep):
    seen = []
    cl = fetch._Client(client=_client(lambda req: httpx.Response(status), seen))
    assert cl.get("https://example.org/x") is None
    assert len(seen) == 1 and not no_sleep and reason in cl.last_error


def test_get_gives_up_after_the_retries():
    seen = []
    cl = fetch._Client(client=_client(lambda req: httpx.Response(503), seen))
    assert cl.get("https://example.org/x") is None and len(seen) == 3 and "HTTP 503" in cl.last_error


def test_download_retries_then_writes_the_file(tmp_path):
    answers = iter([httpx.Response(502), httpx.Response(200, content=b"%PDF-1.4 ok")])
    cl = fetch._Client(client=_client(lambda req: next(answers)))
    p = cl.download("https://example.org/a.pdf", tmp_path / "a.pdf")
    assert p is not None and p.read_bytes() == b"%PDF-1.4 ok"
    assert not list(tmp_path.glob("*.part"))


def test_download_does_not_retry_a_refusal(tmp_path):
    seen = []
    cl = fetch._Client(client=_client(lambda req: httpx.Response(403), seen))
    assert cl.download("https://example.org/a.pdf", tmp_path / "a.pdf") is None and len(seen) == 1


def test_lookup_retries_a_timeout_once_and_marks_it_failed():
    seen = []

    def h(req):
        seen.append(1)
        raise httpx.ReadTimeout("slow", request=req)
    cl = fetch._Client(client=_client(h))
    assert cl.lookup("https://api.example.org/x") == (None, "failed")
    assert len(seen) == 2 and "did not answer in time" in cl.last_error


def test_offline_is_not_retried():
    seen = []

    def h(req):
        seen.append(1)
        raise httpx.ConnectError("no route", request=req)
    cl = fetch._Client(client=_client(h))
    assert cl.get("https://example.org/x") is None and len(seen) == 1 and "offline" in cl.last_error


# ---------------------------------------------------------------- fetch: cancel and status lines

def _small_handler(req):
    host = req.url.host
    if host == "api.crossref.org":
        if req.url.path == "/works":
            return httpx.Response(200, json={"message": {"items": []}})
        return httpx.Response(200, json={"message": {"title": ["A paper"]}})
    if host == "api.openalex.org":
        return httpx.Response(200, json={"id": "https://openalex.org/W1", "ids": {},
                                         "primary_location": {"landing_page_url": "https://journal.example/a/1"}})
    if host == "journal.example":
        return httpx.Response(403)
    return httpx.Response(404)


def test_fetch_records_a_status_line_per_source(monkeypatch, tmp_path):
    _patch_fetch_client(monkeypatch, _small_handler)
    b = fetch.fetch("10.1234/abc", tmp_path)
    by = {}
    for x in b.status:
        by.setdefault(x["source"], []).append(x["state"])
    assert by["Crossref"] == ["ok"] and by["OpenAlex"] == ["ok"]
    assert by["Open-access PDF"] == ["none"] and by["Publisher page"] == ["failed"]
    page = next(x for x in b.status if x["source"] == "Publisher page")
    assert "refused automated access (HTTP 403)" in page["message"]
    assert [f"{x['source']}: {x['message']}" for x in b.status] == b.log
    s = fetch.summary(b)
    assert "the article PDF" in s["missing"] and s["unreachable"] == ["Publisher page"]
    saved = json.loads((b.dir / "bundle.json").read_text())
    assert saved["status"] == b.status


def test_bundle_load_accepts_old_and_newer_files(tmp_path):
    (tmp_path / "bundle.json").write_text(json.dumps({"doi": "10.1234/abc", "dir": "x", "meta": {}, "log": [],
                                                      "from_a_newer_version": 1}))
    b = fetch.Bundle.load(tmp_path)
    assert b.doi == "10.1234/abc" and b.status == [] and b.dir == tmp_path


def test_fetch_cancel_stops_and_removes_the_folder(monkeypatch, tmp_path):
    seen = _patch_fetch_client(monkeypatch, _small_handler)
    stop = {"now": False}

    def progress(msg, f):
        if "PDF" in msg:
            stop["now"] = True
    with pytest.raises(fetch.Cancelled):
        fetch.fetch("10.1234/abc", tmp_path, progress=progress, should_cancel=lambda: stop["now"])
    assert not any(p.is_dir() for p in tmp_path.iterdir())
    assert not any(u.host == "journal.example" for u in seen)   # nothing requested after the cancel


def test_fetch_without_cancel_callback_still_works(monkeypatch, tmp_path):
    _patch_fetch_client(monkeypatch, _small_handler)
    assert fetch.fetch("10.1234/abc", tmp_path).doi == "10.1234/abc"


def test_download_cancel_mid_file_leaves_no_partial(tmp_path):
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2                       # before the request, first chunk ok, then stop

    big = b"x" * (1 << 18)                          # four chunks
    cl = fetch._Client(client=_client(lambda req: httpx.Response(200, content=big)), should_cancel=cancel)
    with pytest.raises(fetch.Cancelled):
        cl.download("https://example.org/big.bin", tmp_path / "big.bin")
    assert list(tmp_path.iterdir()) == []


def test_fetch_arxiv_preprint_pdf(monkeypatch, tmp_path):
    def h(req):
        if req.url.host == "api.crossref.org":
            return httpx.Response(404) if req.url.path != "/works" else httpx.Response(200, json={"message": {"items": []}})
        if req.url.host == "api.openalex.org":
            return httpx.Response(200, json={"id": "https://openalex.org/W2", "title": "A preprint", "ids": {}})
        if req.url.host == "arxiv.org" and req.url.path == "/pdf/2101.12345":
            return httpx.Response(200, content=b"%PDF-1.5 preprint")
        return httpx.Response(404)
    seen = _patch_fetch_client(monkeypatch, h)
    b = fetch.fetch("arXiv DOI 10.48550/arXiv.2101.12345v2", tmp_path)
    assert b.doi == "10.48550/arxiv.2101.12345" and b.pdf == "paper.pdf"
    assert b.meta["preprint"]["server"] == "arxiv" and b.meta["title"] == "A preprint"
    assert any("DataCite, so this is expected" in line for line in b.log)
    assert sum(u.host == "arxiv.org" for u in seen) == 1


def test_no_email_in_requests_by_default():
    import os
    if os.environ.get("PAPERINSPECTOR_CONTACT"):
        pytest.skip("a contact address is set on purpose")
    assert "mailto" not in fetch.UA and "@" not in fetch.UA and "@" not in fetch.BROWSER_UA
