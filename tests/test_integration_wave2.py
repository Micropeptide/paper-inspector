"""Wave 2 integration: the new checks are wired into the pipeline, cancels stop a scan and leave no half-written run,
the server resolves pasted identifiers, and the catalogue knows every check the engine can emit.
No test touches the network."""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import cv2
import numpy as np
import pytest

from paperinspector import catalog, fetch, models, synth
from paperinspector.models import INTEGRITY, Cancelled, Finding

G = {"X-Paper-Inspector": "1"}
ENGINE = Path(__file__).resolve().parents[1] / "engine" / "paperinspector"


@pytest.fixture()
def pl(monkeypatch, tmp_path):
    import paperinspector.pipeline as pl
    monkeypatch.setattr(pl, "HOME", tmp_path)
    monkeypatch.setattr(pl, "RUNS", tmp_path / "runs")
    return pl


def _bundle(root: Path, dates: dict | None = None) -> fetch.Bundle:
    d = root / "bundle"
    (d / "pmc").mkdir(parents=True)
    (d / "supplements").mkdir()
    meta = {"doi": "10.1234/abc", "title": "A paper about cells", "references": [], "authors": [], "notices": [],
            "lookup_errors": [], "issued": "2020-05-01", "dates": dates or {}}
    b = fetch.Bundle(doi="10.1234/abc", dir=d, meta=meta)
    img = synth.histology(np.random.default_rng(5), 360, 360)
    cv2.imwrite(str(d / "pmc" / "fig1.png"), img)
    cv2.imwrite(str(d / "pmc" / "fig2.png"), synth.flip_h(img))
    b.figures = [{"file": "pmc/fig1.png", "label": "Figure 1", "caption": "Figure 1 Control."},
                 {"file": "pmc/fig2.png", "label": "Figure 2", "caption": "Figure 2 Treated."}]
    synth.demo_supplement(d / "supplements" / "Data_S1.xlsx")
    b.supplements = [{"file": "supplements/Data_S1.xlsx", "name": "Data_S1.xlsx", "label": "S1 Data",
                      "source": "PMC", "status": "downloaded"}]
    return b


# ------------------------------------------------------------------ one class for every cancel

def test_fetch_cancel_is_the_models_cancel():
    assert issubclass(fetch.Cancelled, models.Cancelled)
    assert not issubclass(fetch.Cancelled, Exception)   # `except Exception` around optional steps cannot swallow it


def test_fetch_cleans_up_when_the_callers_progress_raises(monkeypatch, tmp_path):
    def stop(*a, **k):
        raise Cancelled()
    monkeypatch.setattr(fetch, "_metadata", stop)
    with pytest.raises(Cancelled):
        fetch.fetch("10.1234/abc", tmp_path)
    assert not [p for p in tmp_path.iterdir() if p.is_dir()]      # the half-filled bundle folder is gone


# ------------------------------------------------------------------ pipeline wiring

def test_scan_doi_passes_cancel_and_reports_the_fetch(pl, monkeypatch, tmp_path):
    b = _bundle(tmp_path)
    seen = {}

    def fake_fetch(doi, root, progress=None, should_cancel=None):
        seen["cancel"] = should_cancel
        return b
    monkeypatch.setattr(pl.fetch, "fetch", fake_fetch)
    flag = lambda: False   # noqa: E731
    r = pl.scan_doi("10.1234/abc", online=False, should_cancel=flag)
    assert seen["cancel"] is flag
    assert set(r["fetch"]) >= {"found", "missing", "sources"}
    # the new image pass ran: the mirrored copy carries a Bik category
    dup = [f for f in r["findings"] if f["check"] == "image.duplicate" and f["severity"] != "info"]
    assert dup and all(f["data"].get("bik") in ("I", "II", "III") for f in dup)


def test_scan_doi_accepts_an_arxiv_id_offline(pl, monkeypatch, tmp_path):
    b = _bundle(tmp_path)
    got = {}

    def fake_fetch(doi, root, progress=None, should_cancel=None):
        got["doi"] = doi
        return b
    monkeypatch.setattr(pl.fetch, "fetch", fake_fetch)
    pl.scan_doi("arXiv:2101.00001", online=False)
    assert got["doi"] == "10.48550/arxiv.2101.00001"


def test_scan_doi_refuses_a_link_without_network(pl, monkeypatch):
    monkeypatch.setattr(pl.fetch, "fetch", lambda *a, **k: pytest.fail("must not fetch"))
    with pytest.raises(ValueError):
        pl.scan_doi("https://www.example.org/articles/some-paper", online=False)


def test_a_cancelled_scan_leaves_no_run_folder(pl, monkeypatch, tmp_path):
    b = _bundle(tmp_path)
    monkeypatch.setattr(pl.fetch, "fetch", lambda *a, **k: b)

    def prog(stage, frac):
        if frac > 0.3:
            raise Cancelled()
    with pytest.raises(Cancelled):
        pl.scan_doi("10.1234/abc", progress=prog, online=False)
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


def test_a_failed_upload_scan_leaves_no_run_folder(pl, monkeypatch, tmp_path):
    p = tmp_path / "demo.pdf"
    synth.to_pdf_file(synth.demo_paper(), p)
    monkeypatch.setattr(pl, "_analyse", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        pl.scan(p)
    assert not any((tmp_path / "runs").iterdir())


def test_datacheck_gets_the_paper_dates_and_supplement_files(pl, monkeypatch, tmp_path):
    b = _bundle(tmp_path, dates={"received": "2019-01-02", "accepted": "2019-06-01"})
    monkeypatch.setattr(pl.fetch, "fetch", lambda *a, **k: b)
    calls = {}
    real = pl.datacheck.check

    def spy(tables, dates=None, files=None):
        calls["dates"], calls["files"] = dates, list(files or [])
        return real(tables, dates=dates, files=files)
    monkeypatch.setattr(pl.datacheck, "check", spy)
    pl.scan_doi("10.1234/abc", online=False)
    assert calls["dates"]["received"] == "2019-01-02" and calls["dates"]["issued"] == "2020-05-01"
    names = [n for _, n in calls["files"]]
    assert names and all(not n.lower().endswith(".pdf") or "article" not in n.lower() for n in names)


def test_paper_dates_fall_back_to_the_pdf_text(pl):
    assert pl._paper_dates({}, {"received": "2020-01-01", "days_to_accept": 3}) == {"received": "2020-01-01"}
    assert pl._paper_dates({}, {}) is None


def test_baseline_check_and_trials_run_on_every_scan(pl, monkeypatch, tmp_path):
    b = _bundle(tmp_path)
    monkeypatch.setattr(pl.fetch, "fetch", lambda *a, **k: b)
    hit = {}
    monkeypatch.setattr(pl.stats, "check_tables", lambda tables, paper=None: hit.setdefault("carlisle", True) and [])
    monkeypatch.setattr(pl.reporting, "trials", lambda paper, meta=None, online=False: hit.setdefault("trials", online)
                        and [])
    pl.scan_doi("10.1234/abc", online=False)
    assert hit == {"carlisle": True, "trials": False}


def test_a_failing_new_step_does_not_sink_the_scan(pl, monkeypatch, tmp_path):
    b = _bundle(tmp_path)
    monkeypatch.setattr(pl.fetch, "fetch", lambda *a, **k: b)
    monkeypatch.setattr(pl.imgchecks, "check", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(pl.stats, "check_tables", lambda *a, **k: 1 / 0)
    r = pl.scan_doi("10.1234/abc", online=False)
    checks = {f["check"] for f in r["findings"]}
    assert {"image.unchecked", "stats.unchecked"} <= checks
    assert all(f["severity"] == "info" for f in r["findings"] if f["check"].endswith(".unchecked"))


def test_sneaked_count_is_not_counted_twice(pl, monkeypatch, tmp_path):
    b = _bundle(tmp_path)
    monkeypatch.setattr(pl.fetch, "fetch", lambda *a, **k: b)
    named = Finding("refs.sneaked", "medium", "2 deposited references are not in the printed list", "x", 0.6,
                    data={"references": [{"key": "a"}]})
    count = Finding("refs.sneaked", "medium", "2 more references deposited than printed", "y", 0.5)
    monkeypatch.setattr(pl.refs, "check", lambda paper, meta=None: [named])
    monkeypatch.setattr(pl.refcheck, "analyse", lambda meta, progress=None, paper=None: ([], {}))
    monkeypatch.setattr(pl.history, "check", lambda meta, paper, progress=None: ([count], {}))
    r = pl.scan_doi("10.1234/abc", online=True)
    sneaked = [f for f in r["findings"] if f["check"] == "refs.sneaked"]
    assert len(sneaked) == 1 and "references" in sneaked[0]["data"]


# ------------------------------------------------------------------ scoring and catalogue

def test_new_strong_checks_are_scored():
    assert {"data.offset_copy", "stats.grimmer", "stats.ci_p", "stats.carlisle"} <= INTEGRITY
    for weak in ("stats.df_n", "stats.sd_sem", "data.benford", "data.file_dates", "refs.doi_mismatch"):
        assert weak not in INTEGRITY


def test_catalogue_knows_every_emitted_check():
    ids = set()
    for f in ENGINE.glob("*.py"):
        text = f.read_text()
        ids |= set(re.findall(r'Finding\(\s*"([a-z]+\.[a-z_]+)"', text))
        ids |= set(re.findall(r'_skipped\("([a-z]+\.[a-z_]+)"', text))
    missing = sorted(i for i in ids if not catalog.explain(i)["known"])
    assert not missing, missing


def test_pubpeer_draft_uses_catalogue_questions():
    from paperinspector import report
    r = {"paper": {"title": "T"}, "findings": [
        {"id": "001", "check": "stats.carlisle", "severity": "medium", "kind": "integrity", "category": "stats",
         "title": "Baseline", "detail": "Too similar.", "locations": [{"page": 3}], "data": {}}]}
    assert "randomisation" in report.pubpeer(r)


# ------------------------------------------------------------------ contact address

def test_user_agent_carries_an_address_only_when_set(monkeypatch):
    import paperinspector.settings as st
    monkeypatch.setattr(fetch, "CONTACT", "")
    monkeypatch.setattr(st, "contact_email", lambda: "")
    assert "mailto" not in fetch.user_agent()
    monkeypatch.setattr(st, "contact_email", lambda: "someone@example.org")
    assert "mailto:someone@example.org" in fetch.user_agent()


# ------------------------------------------------------------------ server

@pytest.fixture()
def sv(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERINSPECTOR_HOME", str(tmp_path))
    import paperinspector.pipeline as pl_
    importlib.reload(pl_)
    import paperinspector.settings as st
    importlib.reload(st)
    import paperinspector.server as server
    importlib.reload(server)
    yield server
    server.QUEUE.stop(timeout=5)
    server.POOL.shutdown(wait=True)


@pytest.fixture()
def client(sv):
    from fastapi.testclient import TestClient
    return TestClient(sv.app)


def test_resolve_route_reads_dois_offline(client, monkeypatch):
    import paperinspector.inputs as inputs
    monkeypatch.setattr(inputs, "_Client", lambda *a, **k: pytest.fail("no network for a DOI"))
    r = client.post("/api/resolve", json={"text": "https://doi.org/10.1371/journal.pone.0000001."}, headers=G)
    assert r.status_code == 200
    body = r.json()
    assert body["doi"] == "10.1371/journal.pone.0000001" and body["kind"] == "doi" and "label" in body


def test_resolve_route_refuses_empty_and_huge_text(client):
    assert client.post("/api/resolve", json={"text": "  "}, headers=G).status_code == 400
    assert client.post("/api/resolve", json={"text": "x " * 1500}, headers=G).status_code == 400
    assert client.post("/api/resolve", json={"text": "10.1/x"}).status_code == 403     # guard header required


def test_doi_route_resolves_other_identifiers(client, sv, monkeypatch):
    import paperinspector.inputs as inputs
    monkeypatch.setattr(inputs, "resolve", lambda text, **k: {"kind": "pmid", "input": text, "value": "123456",
                                                              "doi": "10.1234/abc", "candidates": [], "message": ""})
    started = {}

    def fake_scan_doi(doi, progress=None, online=True, should_cancel=None):
        started["doi"], started["cancel"] = doi, should_cancel
        return {"id": "r1"}
    monkeypatch.setattr(sv.pipeline, "scan_doi", fake_scan_doi)
    job = client.post("/api/doi", json={"doi": "PMID: 123456"}, headers=G).json()
    import time
    for _ in range(100):
        if client.get(f"/api/jobs/{job['id']}").json()["status"] != "running":
            break
        time.sleep(0.05)
    assert started["doi"] == "10.1234/abc" and callable(started["cancel"])


def test_doi_route_explains_ambiguous_and_offline_lookups(client, monkeypatch):
    import paperinspector.inputs as inputs
    monkeypatch.setattr(inputs, "resolve", lambda text, **k: {
        "kind": "title", "input": text, "value": text, "doi": None, "message": "Two papers match.",
        "candidates": [{"doi": "10.1/a"}, {"doi": "10.1/b"}]})
    r = client.post("/api/doi", json={"doi": "a study of cells in mice"}, headers=G)
    assert r.status_code == 422 and "Two papers match" in r.json()["detail"]
    monkeypatch.setattr(inputs, "resolve", lambda text, **k: {
        "kind": "pmid", "input": text, "value": "1", "doi": None, "message": "Offline.", "candidates": [],
        "error": "network"})
    assert client.post("/api/doi", json={"doi": "PMID 12345"}, headers=G).status_code == 503


def test_report_json_round_trip_keeps_new_fields(pl, monkeypatch, tmp_path):
    b = _bundle(tmp_path)
    monkeypatch.setattr(pl.fetch, "fetch", lambda *a, **k: b)
    r = pl.scan_doi("10.1234/abc", online=False)
    saved = json.loads((pl.RUNS / r["id"] / "report.json").read_text())
    assert "fetch" in saved and "special_issue" in saved["meta"]
