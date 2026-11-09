"""References and the publication record, second wave: DOI-title mismatches, references cited after their
retraction, the sneaked-reference list, Crossref / Retraction Watch notices, special issues, trial registrations
and Cellosaurus.  Every network call is mocked; each check has a case that must fire and a clean case that must not."""
from __future__ import annotations

import json

import httpx
import pytest

from paperinspector import refcheck, refs, reporting
from paperinspector.models import Paper


def _paper(*pages: str) -> Paper:
    return Paper(source="t.pdf", pages=list(pages), page_count=len(pages))


def _checks(found) -> set[str]:
    return {f.check for f in found}


def _mock(handler):
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        return handler(req)
    return httpx.Client(transport=httpx.MockTransport(h)), seen


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch):
    """No e-mail, no PubPeer key, no OpenAlex key: whatever is saved on this computer must not leak into tests."""
    monkeypatch.setattr(refcheck, "contact_email", lambda: "")
    monkeypatch.setattr(refcheck, "pubpeer_key", lambda: "")
    monkeypatch.setattr(refcheck, "openalex_key", lambda: "")


def _rw(doi, kind="retraction", day=(2019, 1, 10), source="retraction-watch"):
    return {"DOI": doi, "type": kind, "label": kind.title(), "source": source,
            "updated": {"date-parts": [list(day)]}}


# ------------------------------------------------------------------------------------------------ dates

def test_dates_are_read_conservatively():
    assert str(refcheck.cr_date({"date-parts": [[2020, 3]]})) == "2020-03-01"
    assert str(refcheck.cr_date({"date-parts": [[2020, 2]]}, latest=True)) == "2020-02-29"
    assert str(refcheck.cr_date({"date-parts": [[2020]]}, latest=True)) == "2020-12-31"
    assert refcheck.cr_date({"date-parts": [[None]]}) is None and refcheck.cr_date("x") is None
    assert str(refcheck.parse_day("5 March 2021")) == "2021-03-05"
    assert str(refcheck.parse_day("2021-03")) == "2021-03-01"


# ------------------------------------------------------------------------------------------------ own record

def _works_handler(msg):
    def h(req):
        if req.url.host == "api.crossref.org" and req.url.path.startswith("/works/"):
            return httpx.Response(200, json={"message": msg})
        return httpx.Response(404)
    return h


def test_own_record_adds_retraction_watch_notice_and_reporting_says_so():
    cl, _ = _mock(_works_handler({"DOI": "10.1/p", "updated-by": [_rw("10.1/notice")]}))
    meta = {"doi": "10.1/p", "notices": [], "lookup_errors": ["notices"]}
    info = refcheck.own_record(cl, meta)
    assert info["notices_added"] == 1 and meta["notices"][0]["source"] == "retraction-watch"
    assert "notices" not in meta["lookup_errors"]           # the record answered: retraction status is known
    f = reporting.record(meta)
    assert _checks(f) == {"record.retracted"} and "Retraction Watch" in f[0].detail
    # asking again adds nothing twice
    refcheck.own_record(cl, meta)
    assert len(meta["notices"]) == 1


def test_own_record_clean_paper_stays_clean():
    cl, _ = _mock(_works_handler({"DOI": "10.1/p", "issue": "7"}))
    meta = {"doi": "10.1/p", "notices": []}
    refcheck.own_record(cl, meta)
    assert meta["notices"] == [] and "special_issue" not in meta and reporting.record(meta) == []


def test_own_record_offline_changes_nothing():
    def boom(req):
        raise httpx.ConnectError("offline")
    cl, _ = _mock(boom)
    meta = {"doi": "10.1/p", "notices": [], "lookup_errors": ["notices"]}
    assert refcheck.own_record(cl, meta) == {"record": "unavailable"}
    assert meta["lookup_errors"] == ["notices"] and meta["notices"] == []


def test_special_issue_from_crossref_metadata():
    assert refcheck.special_issue_from_crossref({"issue": "Special Issue: Advances in Imaging"})
    assert refcheck.special_issue_from_crossref({"assertion": [{"name": "special_issue", "label": "Special Issue",
                                                                "value": "Tumour Biology"}]}) == "Tumour Biology"
    assert refcheck.special_issue_from_crossref({"issue": "SI"}) == "SI"
    for clean in ({"issue": "7"}, {"issue": "S1"}, {"assertion": [{"name": "received", "value": "1 May 2020"}]}, {}):
        assert refcheck.special_issue_from_crossref(clean) == ""


def test_special_issue_note_and_fast_acceptance():
    slow = reporting.record({"special_issue": "Tumour Biology",
                             "dates": {"received": "2020-01-01", "accepted": "2020-04-01"}})
    assert _checks(slow) == {"record.special_issue"} and slow[0].severity == "info"
    fast = reporting.record({"special_issue": "Tumour Biology",
                             "dates": {"received": "2020-01-01", "accepted": "2020-01-09"}})
    assert fast[0].severity == "low" and "8 day" in fast[0].title
    assert fast[0].kind == "quality"                         # context, never part of the score


def test_special_issue_in_front_matter():
    p = _paper("A study of things. This article belongs to the Special Issue “Cancer Signalling Pathways”. "
               "Received: 3 March 2021; Accepted: 9 March 2021.", "Methods " * 50)
    f = [x for x in reporting.statements(p) if x.check == "record.special_issue"]
    assert f and f[0].data["name"] == "Cancer Signalling Pathways" and f[0].severity == "low"
    # an issue already named by the publisher's record is not reported twice
    assert not [x for x in reporting.statements(p, {"special_issue": "x"}) if x.check == "record.special_issue"]


def test_special_issue_only_in_a_cited_title_is_not_flagged():
    p = _paper("A study of things. " * 40, "Results " * 50,
               "References\n1. Smith J. Editorial: part of the special issue on imaging. J Imaging. 2019;3:1-2.")
    assert "record.special_issue" not in _checks(reporting.statements(p))


# ------------------------------------------------------------------------------------------------ DOI vs title

def test_doi_title_mismatch_fires_only_on_publisher_dois_with_unrelated_titles():
    refs_ = [
        {"DOI": "10.1/a", "doi-asserted-by": "publisher",
         "article-title": "Mitochondrial calcium uptake regulates cardiomyocyte survival"},
        {"DOI": "10.1/b", "doi-asserted-by": "publisher",
         "article-title": "Deep learning for galaxy morphology classification"},      # right DOI
        {"DOI": "10.1/c", "doi-asserted-by": "crossref",
         "article-title": "Soil microbial diversity under drought stress conditions"},  # Crossref's own guess
        {"DOI": "10.1/d", "doi-asserted-by": "publisher",
         "article-title": "Graphene oxide membranes for water desalination"},        # DOI of its correction
    ]
    cr = {"10.1/a": {"title": "Fiscal policy and regional unemployment in European economies", "notices": []},
          "10.1/b": {"title": "Deep learning for galaxy morphology classification.", "notices": []},
          "10.1/c": {"title": "A history of medieval trade routes across Europe", "notices": []},
          "10.1/d": {"title": "Correction to: Graphene oxide membranes", "notices": []}}
    out = refcheck.doi_title_mismatches(refs_, cr, [])
    assert [m["doi"] for m in out] == ["10.1/a"]


def test_doi_title_free_text_only_when_the_style_prints_titles():
    titles = ["Protein folding kinetics in crowded cellular environments", "Neural circuits underlying fear memory",
              "Ocean acidification effects on coral calcification", "Quantum dots for single molecule imaging",
              "Gut microbiome composition in early childhood", "Seismic imaging of the lower crust structure"]
    found = {f"10.1/{i}": {"title": t, "notices": []} for i, t in enumerate(titles)}
    good = [{"DOI": f"10.1/{i}", "unstructured": f"Author A, Author B. {t}. J Sci. 2019;12:100-110."}
            for i, t in enumerate(titles)]
    wrong = dict(good[0], unstructured="Author A. Renal tubular transport of phosphate in rodents. J Sci. "
                                       "2019;12:100-110.")
    assert [m["doi"] for m in refcheck.doi_title_mismatches([wrong] + good[1:], found, [])] == ["10.1/0"]
    # a citation style without titles ("Phys. Rev. Lett. 99, 123") must not make every DOI look wrong
    bare = [{"DOI": f"10.1/{i}", "unstructured": f"A. Author, B. Author and C. Author, Phys. Rev. Lett. {i}, "
                                                 f"123456 (2019), and further remarks"} for i in range(6)]
    assert refcheck.doi_title_mismatches(bare, found, []) == []


# ------------------------------------------------------------------------------------------------ retractions

def _analyse_handler(cr_items):
    def h(req):
        if req.url.host == "api.crossref.org" and req.url.path == "/works" and \
                req.url.params.get("filter", "").startswith("doi:"):
            return httpx.Response(200, json={"message": {"items": cr_items}})
        return httpx.Response(404)
    return h


def _run_analyse(monkeypatch, meta, cr_items, paper=None):
    seen: list[httpx.Request] = []
    h = _analyse_handler(cr_items)

    def handler(req):
        seen.append(req)
        return h(req)
    monkeypatch.setattr(refcheck, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return refcheck.analyse(meta, paper=paper) + (seen,)


def test_reference_cited_after_its_retraction(monkeypatch):
    meta = {"references": [{"DOI": "10.1/old"}, {"DOI": "10.1/new"}, {"DOI": "10.1/fine"}],
            "dates": {"received": "2021-05-01"}, "type": "journal-article"}
    items = [{"DOI": "10.1/old", "title": ["An old claim"], "updated-by": [_rw("10.1/n1", day=(2019, 1, 10))]},
             {"DOI": "10.1/new", "title": ["A later claim"], "updated-by": [_rw("10.1/n2", day=(2022, 6, 1))]},
             {"DOI": "10.1/fine", "title": ["A sound claim"]}]
    findings, stats, seen = _run_analyse(monkeypatch, meta, items)
    by = {f.check: f for f in findings}
    early = by["refs.cited_after_retraction"]
    assert early.severity == "low" and "10.1/old" in early.data["works"][0]["doi"] and "submitted" in early.detail
    later = by["refs.retracted"]
    assert [w["doi"] for w in later.data["works"]] == ["10.1/new"] and "after this paper was written" in later.detail
    assert stats["retracted_before_submission"] == 1
    # DOIs Crossref already knows are not asked about one by one
    assert not any(r.url.host == "doi.org" for r in seen)


def test_recent_retraction_inside_the_grace_period_is_not_called_early(monkeypatch):
    meta = {"references": [{"DOI": "10.1/x"}], "dates": {"received": "2021-05-01"}}
    items = [{"DOI": "10.1/x", "title": ["Claim"], "updated-by": [_rw("10.1/n", day=(2021, 4, 1))]}]
    findings, _, _ = _run_analyse(monkeypatch, meta, items)
    assert "refs.cited_after_retraction" not in _checks(findings) and "refs.retracted" in _checks(findings)


def test_paper_that_discusses_retractions_is_only_noted(monkeypatch):
    meta = {"references": [{"DOI": "10.1/old"}], "dates": {"received": "2021-05-01"}}
    items = [{"DOI": "10.1/old", "title": ["Claim"], "updated-by": [_rw("10.1/n", day=(2015, 1, 1))]}]
    p = _paper("We review studies that were later retracted for image problems. " * 3)
    findings, _, _ = _run_analyse(monkeypatch, meta, items, paper=p)
    f = [x for x in findings if x.check == "refs.cited_after_retraction"]
    assert f and f[0].severity == "info"


def test_no_retractions_no_findings_and_corrections_are_not_retractions(monkeypatch):
    meta = {"references": [{"DOI": "10.1/a"}, {"DOI": "10.1/b"}], "dates": {"received": "2021-05-01"}}
    items = [{"DOI": "10.1/a", "title": ["A"], "updated-by": [_rw("10.1/c", kind="correction")]},
             {"DOI": "10.1/b", "title": ["B"]}]
    findings, stats, _ = _run_analyse(monkeypatch, meta, items)
    assert not _checks(findings) & {"refs.retracted", "refs.cited_after_retraction", "refs.concern"}
    assert stats["retracted"] == 0


def test_expression_of_concern_in_references_is_a_note(monkeypatch):
    meta = {"references": [{"DOI": "10.1/a"}]}
    items = [{"DOI": "10.1/a", "title": ["A worrying study"],
              "updated-by": [_rw("10.1/e", kind="expression_of_concern")]}]
    findings, _, _ = _run_analyse(monkeypatch, meta, items)
    f = [x for x in findings if x.check == "refs.concern"]
    assert f and f[0].severity == "info"


def test_doi_mismatch_through_analyse(monkeypatch):
    meta = {"references": [{"DOI": "10.1/a", "doi-asserted-by": "publisher",
                            "article-title": "Mitochondrial calcium uptake regulates cardiomyocyte survival"}]}
    items = [{"DOI": "10.1/a", "title": ["Fiscal policy and regional unemployment in European economies"]}]
    findings, stats, _ = _run_analyse(monkeypatch, meta, items)
    f = [x for x in findings if x.check == "refs.doi_mismatch"]
    assert f and f[0].severity == "low" and stats["doi_title_mismatches"] == 1


def test_crossref_batch_is_cached_and_busy_is_counted():
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(200, json={"message": {"items": [{"DOI": "10.1/A", "title": ["T"]}]}})
    cl = httpx.Client(transport=httpx.MockTransport(h))
    cache: dict = {}
    got, busy = refcheck.crossref_works(cl, ["10.1/a", "10.1/b"], cache)
    assert set(got) == {"10.1/a"} and busy == 0
    refcheck.crossref_works(cl, ["10.1/a", "10.1/b"], cache)
    assert len(calls) == 1                                   # the second ask comes from the run's cache
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    import paperinspector.refcheck as rc
    old = rc.time.sleep
    rc.time.sleep = lambda s: None
    try:
        assert refcheck.crossref_works(down, ["10.1/z"]) == ({}, 1)
    finally:
        rc.time.sleep = old


def test_mailto_only_when_the_user_set_an_email(monkeypatch):
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"message": {"items": []}})
    cl = httpx.Client(transport=httpx.MockTransport(h))
    refcheck.crossref_works(cl, ["10.1/a"])
    assert "mailto" not in seen[-1].url.params
    monkeypatch.setattr(refcheck, "contact_email", lambda: "reader@example.org")
    refcheck.crossref_works(cl, ["10.1/b"])
    assert seen[-1].url.params["mailto"] == "reader@example.org"


def test_pubpeer_needs_a_key_and_sends_nothing_without_one():
    cl, seen = _mock(lambda req: httpx.Response(200, json={}))
    assert refcheck.pubpeer(cl, ["10.1/p"]) is None and seen == []
    cl, seen = _mock(lambda req: httpx.Response(200, json={"feedbacks": [
        {"id": "10.1/p", "total_comments": 4, "url": "https://pubpeer.com/publications/ABC"}]}))
    assert refcheck.pubpeer(cl, ["10.1/P"], key="k") == {"10.1/p": {"comments": 4,
                                                                   "url": "https://pubpeer.com/publications/ABC"}}
    assert json.loads(seen[0].content)["dois"] == ["10.1/p"]


# ------------------------------------------------------------------------------------------------ sneaked

TITLES = ["Chromatin remodelling during early embryonic development", "Ribosome stalling triggers quality control",
          "Membrane curvature sensing by amphipathic helices", "Circadian control of hepatic glucose output",
          "Synaptic vesicle recycling at central synapses", "Antibiotic tolerance in bacterial persister cells",
          "Phase separation of RNA binding proteins in stress granules", "Telomere length and replicative ageing",
          "Microtubule dynamics in neuronal growth cones", "Lipid droplet biogenesis in adipocytes",
          "DNA mismatch repair in human cancers", "Plant immune receptors recognise fungal effectors"]
EXTRA = ["Optimisation of heuristic scheduling in cloud computing", "A fuzzy controller for wind turbine pitch",
         "Blockchain consensus for supply chain tracking", "Sentiment analysis of product reviews with transformers",
         "Traffic flow prediction with graph networks"]


def _printed_paper(titles, tail_note=""):
    body = "Introduction. " + " ".join(f"Result {i} [{i}]." for i in range(1, len(titles) + 1)) * 5 + tail_note
    lst = "\n".join(f"{i}. Author{i} A, Other B. {t}. J Biol. 20{10 + i % 9}; {i}:{100 + i}-{110 + i}."
                    for i, t in enumerate(titles, 1))
    return _paper(body + "\nReferences\n" + lst)


NAMES = ["Abe", "Baker", "Chen", "Diaz", "Evans", "Fischer", "Garcia", "Hansen", "Ito", "Jones", "Kim", "Lopez",
         "Moreau", "Novak", "Okafor", "Patel", "Quinn"]


def _deposit(titles, author=None, start=0):
    return [{"key": f"r{i}", "author": author or NAMES[(start + i) % len(NAMES)], "year": "2018", "article-title": t}
            for i, t in enumerate(titles, 1)]


def test_sneaked_references_are_listed_by_name():
    meta = {"references": _deposit(TITLES) + _deposit(EXTRA, author="Booster")}
    f = refs.sneaked(_printed_paper(TITLES), meta)
    assert _checks(f) == {"refs.sneaked"} and f[0].severity == "medium" and f[0].data["missing"] == 5
    assert {r["reference"].split(". ")[2] for r in f[0].data["references"]} == set(EXTRA)
    assert "Booster" in f[0].detail                         # who gains from the extra citations
    assert "refs.sneaked" in _checks(refs.check(_printed_paper(TITLES), meta))
    assert "refs.sneaked" not in _checks(refs.check(_printed_paper(TITLES)))     # no metadata, no comparison


def test_sneaked_mixed_into_the_middle_without_a_shared_author():
    deposited = _deposit(TITLES[:6]) + _deposit(EXTRA[:3], start=12) + _deposit(TITLES[6:])
    f = refs.sneaked(_printed_paper(TITLES), {"references": deposited})
    assert f and f[0].severity == "low" and "same first author" not in f[0].detail


def test_matching_lists_and_unreadable_lists_are_quiet():
    paper = _printed_paper(TITLES)
    assert refs.sneaked(paper, {"references": _deposit(TITLES)}) == []
    # PDF hyphenation and line breaks inside titles still match
    broken = _paper(paper.text.replace("remodelling", "remod-\nelling")
                    .replace("Ribosome stalling", "Ribo some\nstalling"))
    assert refs.sneaked(broken, {"references": _deposit(TITLES)}) == []
    # the printed list was cut short by the PDF reader: the misses are the end of the deposit
    cut = _printed_paper(TITLES[:7])
    assert refs.sneaked(cut, {"references": _deposit(TITLES)}) == []
    # the paper says some references are printed only in the supplement
    note = _printed_paper(TITLES, " References 13-17 are cited only in the Supplementary Information.")
    assert refs.sneaked(note, {"references": _deposit(TITLES) + _deposit(EXTRA)}) == []
    # bare DOIs cannot be compared with a list that prints no DOIs
    bare = _deposit(TITLES) + [{"key": f"x{i}", "DOI": f"10.9/extra{i}"} for i in range(5)]
    assert refs.sneaked(paper, {"references": bare}) == []
    # no reference heading at all
    assert refs.sneaked(_paper("Text only. " * 50), {"references": _deposit(TITLES) + _deposit(EXTRA)}) == []


def test_author_year_page_deposits_match_the_printed_list():
    deposited = [{"key": f"r{i}", "author": f"Author{i}", "year": f"20{10 + i % 9}", "first-page": str(100 + i),
                  "volume": str(i), "journal-title": "J Biol"} for i in range(1, 13)]
    assert refs.sneaked(_printed_paper(TITLES), {"references": deposited}) == []


# ------------------------------------------------------------------------------------------------ uploaded PDFs

def test_retractions_for_an_uploaded_pdf_use_its_received_date():
    text = ("Received: 3 March 2021; Accepted: 9 June 2021. Body [1] [2] [3] [4] [5].\nReferences\n" +
            "\n".join(f"{i}. Author A. Title number {i}. J Biol. 2015. doi:10.1234/r{i}" for i in range(1, 6)))
    items = [{"DOI": "10.1234/r2", "title": ["Two"], "updated-by": [_rw("10.1/n", day=(2018, 2, 2))]}]
    cl, seen = _mock(lambda req: httpx.Response(200, json={"message": {"items": items}}))
    found = refs.retractions(_paper(text), client=cl)
    assert _checks(found) == {"refs.cited_after_retraction"} and len(seen) == 1      # one request for all DOIs
    cl, _ = _mock(lambda req: httpx.Response(200, json={"message": {"items": []}}))
    assert refs.retractions(_paper(text), client=cl) == []


# ------------------------------------------------------------------------------------------------ trial numbers

def test_trial_number_formats():
    bad = dict(reporting.trial_id_problems("Registered as NCT0123456 and ISRCTN123456789; ChiCTR180001477; "
                                           "EudraCT 2010-02012-12."))
    assert set(bad) == {"NCT0123456", "ISRCTN123456789", "ChiCTR180001477", "EudraCT 2010-02012-12"}
    good = ("NCT01234567, NCT0123 4567 (split by a line break), ISRCTN12345678, ChiCTR1800014773, "
            "ChiCTR-IOR-17012345, ACTRN12615000123456, DRKS00012345, UMIN000012345, EudraCT 2010-020123-12, "
            "NCTC 8325 bacteria, KCTD10 gene.")
    assert reporting.trial_id_problems(good) == []


def test_statements_report_a_bad_trial_number_and_accept_eudract():
    filler = "Patients were treated and followed up. " * 120
    bad = _paper(filler + "This randomised controlled trial was registered (NCT0123456).")
    f = [x for x in reporting.statements(bad) if x.check == "report.trial_id_format"]
    assert f and f[0].severity == "low" and f[0].kind == "quality"
    eu = _paper(filler + "This randomised controlled trial was registered (EudraCT number 2010-020123-12).")
    checks = _checks(reporting.statements(eu))
    assert "report.no_registration" not in checks and "report.trial_id_format" not in checks


def _ctgov(studies: dict):
    def h(req):
        nct = req.url.path.rsplit("/", 1)[-1]
        if nct not in studies:
            return httpx.Response(404, text=f"NCT number {nct} not found")
        return httpx.Response(200, json={"protocolSection": studies[nct]})
    return h


def _study(registered, start, kind="INTERVENTIONAL"):
    return {"statusModule": {"studyFirstSubmitDate": registered, "startDateStruct": {"date": start}},
            "designModule": {"studyType": kind}, "identificationModule": {"briefTitle": "A trial"}}


TRIAL_TEXT = "Received: 10 January 2021; Accepted: 2 May 2021. Patients were enrolled between March 2018 and " \
             "June 2019. The trial is registered at ClinicalTrials.gov ({id})."


def test_trial_registered_after_submission():
    cl, _ = _mock(_ctgov({"NCT01234567": _study("2021-03-01", "2018-03")}))
    f = reporting.trials(_paper(TRIAL_TEXT.format(id="NCT01234567")), {}, online=True, client=cl)
    assert _checks(f) == {"report.trial_registered_late"} and "submitted" in f[0].title


def test_trial_registered_after_recruitment_ended():
    cl, _ = _mock(_ctgov({"NCT01234567": _study("2020-01-15", "2018-03")}))
    f = reporting.trials(_paper(TRIAL_TEXT.format(id="NCT01234567")), {}, online=True, client=cl)
    assert _checks(f) == {"report.trial_registered_late"} and "recruitment" in f[0].title


def test_trial_registered_retrospectively_and_missing():
    text = "Patients were treated. The trial is registered at ClinicalTrials.gov ({id})."
    cl, _ = _mock(_ctgov({"NCT01234567": _study("2019-06-01", "2018-03")}))
    f = reporting.trials(_paper(text.format(id="NCT01234567")), {}, online=True, client=cl)
    assert _checks(f) == {"report.trial_retrospective"} and f[0].severity == "low"
    f = reporting.trials(_paper(text.format(id="NCT07654321")), {}, online=True, client=cl)
    assert _checks(f) == {"report.trial_not_found"}


def test_properly_registered_trial_is_quiet():
    cl, _ = _mock(_ctgov({"NCT01234567": _study("2018-02-01", "2018-03")}))
    assert reporting.trials(_paper(TRIAL_TEXT.format(id="NCT01234567")), {}, online=True, client=cl) == []
    # an observational study registered late is not held to the trial rule
    cl, _ = _mock(_ctgov({"NCT01234567": _study("2019-06-01", "2018-03", "OBSERVATIONAL")}))
    text = "Patients were followed. Registered at ClinicalTrials.gov (NCT01234567)."
    assert reporting.trials(_paper(text), {}, online=True, client=cl) == []


def test_trials_ask_nothing_when_they_should_not():
    cl, seen = _mock(_ctgov({}))
    p = _paper(TRIAL_TEXT.format(id="NCT01234567"))
    assert reporting.trials(p, {}, online=False, client=cl) == [] and seen == []
    review = _paper("We pooled NCT01111111, NCT02222222, NCT03333333 and NCT04444444, all registered trials.")
    assert reporting.trials(review, {}, online=True, client=cl) == [] and seen == []
    cited = _paper("A previous trial (NCT01234567) found a small effect.")     # not presented as its own
    assert reporting.trials(cited, {}, online=True, client=cl) == [] and seen == []


def test_trial_lookup_offline_is_a_note_not_a_finding():
    def boom(req):
        raise httpx.ConnectError("offline")
    cl, _ = _mock(boom)
    f = reporting.trials(_paper(TRIAL_TEXT.format(id="NCT01234567")), {}, online=True, client=cl)
    assert _checks(f) == {"report.trial_unchecked"} and f[0].severity == "info"


# ------------------------------------------------------------------------------------------------ Cellosaurus

def _cello(entries):
    def h(req):
        return httpx.Response(200, json={"Cellosaurus": {"cell-line-list": entries}})
    return h


def _entry(name, comment=None, synonyms=()):
    e = {"name-list": [{"type": "identifier", "value": name}] + [{"type": "synonym", "value": s} for s in synonyms]}
    if comment:
        e["comment-list"] = [{"category": "Problematic cell line", "value": comment}]
    return e


@pytest.fixture()
def _offline_iclac(monkeypatch):
    monkeypatch.setattr(reporting, "_iclac", lambda online: dict(reporting.MISIDENTIFIED))


def test_cellosaurus_flags_a_contaminated_line(_offline_iclac):
    cl, seen = _mock(_cello([_entry("QX-801/B1", "Contaminated. Parent line is HeLa."),
                             _entry("QX-801", "Contaminated. Shown to be a HeLa derivative (PubMed=1).", ["QX801"])]))
    p = _paper("QX-801 cells were cultured in DMEM. QX-801 cells were transfected.")
    f = reporting.cell_lines(p, online=True, client=cl)
    assert f and f[0].data["lines"] == {"QX-801": "HeLa"} and f[0].data["sources"]["QX-801"] == "Cellosaurus"
    assert len(seen) == 1 and seen[0].url.host == "api.cellosaurus.org"


def test_cellosaurus_quiet_for_clean_derivative_or_declared_lines(_offline_iclac):
    clean, _ = _mock(_cello([_entry("QX-801")]))
    p = _paper("QX-801 cells were cultured in DMEM.")
    assert reporting.cell_lines(p, online=True, client=clean) == []
    only_derivative, _ = _mock(_cello([_entry("QX-801/B1", "Contaminated. Shown to be a HeLa derivative.")]))
    assert reporting.cell_lines(p, online=True, client=only_derivative) == []
    possibly, _ = _mock(_cello([_entry("QX-801", "Possibly contaminated with another line.")]))
    assert reporting.cell_lines(p, online=True, client=possibly) == []
    declared, _ = _mock(_cello([_entry("QX-801", "Contaminated. Shown to be a HeLa derivative.")]))
    said = _paper("QX-801 cells (a HeLa derivative, as STR profiling shows) were cultured.")
    assert reporting.cell_lines(said, online=True, client=declared) == []


def test_cellosaurus_skips_common_lines_and_offline(_offline_iclac):
    cl, seen = _mock(_cello([]))
    p = _paper("HEK293T cells, A549 cells and Figure 2 cells were used. CD4 cells were sorted.")
    assert reporting.cell_lines(p, online=True, client=cl) == [] and seen == []

    def boom(req):
        raise httpx.ConnectError("offline")
    down, seen = _mock(boom)
    p = _paper("QX-801 cells, QY-802 cells, QZ-803 cells and HEp-2 cells were cultured.")
    f = reporting.cell_lines(p, online=True, client=down)
    assert f and set(f[0].data["lines"]) == {"HEp-2"}        # ICLAC still works; Cellosaurus gave up quietly
    assert len(seen) == 2


def test_markup_left_in_deposited_references_does_not_hide_a_match():
    deposited = _deposit(TITLES[:11]) + [{"key": "sw", "unstructured":
        'Su YS. R2jags: Using R to Run JAGS; 2015. Available from: <ext-link xmlns:xlink="http://www.w3.org/1999/'
        'xlink" ext-link-type="uri">http://CRAN.R-project.org/package=R2jags</ext-link>.'}]
    paper = _printed_paper(TITLES[:11] + ["R2jags: Using R to Run JAGS; 2015. Available from: "
                                          "http://CRAN.R-project.org/package=R2jags"])
    v = refs._printed(deposited[-1], refs._squash(paper.text), refs._spaced(paper.text), False)
    assert v is True and refs.sneaked(paper, {"references": deposited}) == []
