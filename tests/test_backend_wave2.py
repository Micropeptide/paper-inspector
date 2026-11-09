"""Back end, wave 2: rulings that survive rescans, cancelling scans, the persistent batch queue, exports (CSV, XLSX,
PDF, evidence ZIP), letters, run search and tags, the check catalogue and the contact-email setting.
No test touches the network."""
from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import threading
import time
import zipfile

import cv2
import numpy as np
import pytest

G = {"X-Paper-Inspector": "1"}


@pytest.fixture()
def sv(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERINSPECTOR_HOME", str(tmp_path))
    import paperinspector.pipeline as pl
    importlib.reload(pl)
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


def _dup(fid, a="F1.a", b="F2.b", ncc=0.81, sev="high", conf=0.8, title=None):
    return {"id": fid, "check": "image.duplicate", "severity": sev, "kind": "integrity", "category": "image",
            "title": title or f"{a} and {b} appear to show the same image", "detail": "Texture matches.",
            "confidence": conf, "locations": [{"figure": a.split(".")[0], "panel": a, "box": [1, 2, 30, 40]},
                                               {"figure": b.split(".")[0], "panel": b, "box": [5, 6, 30, 40]}],
            "evidence": ["dup-pair.png"], "data": {"ncc_texture": ncc, "mirrored": False}}


def _grim(fid, quote="mean 3.47 (n = 12)"):
    return {"id": fid, "check": "stats.grim", "severity": "medium", "kind": "integrity", "category": "stats",
            "title": "A mean that cannot come from whole numbers", "detail": "GRIM fails.", "confidence": 0.6,
            "locations": [{"page": 4, "quote": quote}], "evidence": [], "data": {}}


def _note(fid):
    return {"id": fid, "check": "report.data_on_request", "severity": "low", "kind": "quality", "category": "report",
            "title": "Data “available on request”", "detail": "=HYPERLINK(\"http://x\")", "confidence": 0.6,
            "locations": [{"page": 9, "quote": "available upon request"}], "evidence": [], "data": {}}


def _mk_run(sv, rid, findings, doi="10.1234/abc.5678", title="A study of things", source=b"%PDF-1.4 same paper",
            meta=None, evidence=True):
    d = sv.pipeline.RUNS / rid
    (d / "evidence").mkdir(parents=True)
    (d / "figures").mkdir()
    if source is not None:
        (d / "source.pdf").write_bytes(source)
    if evidence:
        img = np.zeros((60, 120, 3), np.uint8)
        img[:, :60] = (40, 160, 220)
        cv2.imwrite(str(d / "evidence" / "dup-pair.png"), img)
    from paperinspector.models import risk_score
    created = f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}T{rid[9:11]}:{rid[11:13]}:{rid[13:15]}"
    rep = {"id": rid, "version": "0.1.0", "created": created,
           "paper": {"source": "paper.pdf", "title": title, "doi": doi, "pages": 10},
           "figures": [], "findings": findings, "score": risk_score(findings), "verdict": "Worth a closer look.",
           "counts": {s: sum(f["severity"] == s for f in findings) for s in ("critical", "high", "medium", "low", "info")},
           "panels": 0, "meta": meta or {"journal": "Journal of Examples",
                                         "authors": [{"given": "Test", "family": "Firstauthor"}]}}
    (d / "report.json").write_text(json.dumps(rep))
    return rid


# --- fingerprints -----------------------------------------------------------------------------------------------

def test_fingerprint_survives_renumbering_rewording_and_new_measurements():
    from paperinspector.models import fingerprints
    a = [_dup("001"), _grim("002"), _note("003")]
    b = [_note("001"), _grim("002", "Mean 3.47  (n = 12)"), _dup("003", "F2.b", "F1.a", ncc=0.79, conf=0.7,
                                                                   title="reworded by a newer engine")]
    fa, fb = fingerprints(a), fingerprints(b)
    assert fa[0] == fb[2] and fa[1] == fb[1] and fa[2] == fb[0]


def test_fingerprint_tells_neighbours_apart():
    from paperinspector.models import fingerprints
    fps = fingerprints([_dup("1"), _dup("2", "F1.a", "F3.c"), _grim("3", "mean 2.10 (n = 7)"), _grim("4")])
    assert len(set(fps)) == 4
    twins = fingerprints([_grim("1"), _grim("2")])
    assert twins[1] == twins[0] + "-2"



def test_boxed_fingerprints_match_within_tolerance():
    from paperinspector.models import fingerprints, fp_near, match_fingerprint
    flat = lambda x, y=10: {"check": "image.flat", "locations": [{"panel": "F1.a", "box": [x, y, 6, 6]}]}  # noqa: E731
    a, far = fingerprints([flat(10), flat(200)])
    assert a != far and not fp_near(a, far)
    moved = fingerprints([flat(13, 12)])[0]          # the same patch, found again a few pixels away
    assert moved != a and fp_near(a, moved)
    assert match_fingerprint(moved, {a: 1, far: 2}) == a
    assert match_fingerprint(moved, {far: 2}) is None
    other = fingerprints([{"check": "image.flat", "locations": [{"panel": "F2.a", "box": [13, 12, 6, 6]}]}])[0]
    assert not fp_near(a, other)


def test_cancelled_is_not_swallowed_by_except_exception():
    from paperinspector.models import Cancelled
    with pytest.raises(Cancelled):
        try:
            raise Cancelled()
        except Exception:      # noqa: BLE001 - this is what the pipeline does around optional steps
            pass


# --- rulings ------------------------------------------------------------------------------------------------------

def test_ruling_set_shown_and_kept_after_rescan(client, sv):
    r1 = _mk_run(sv, "20261101-100000-aaaa-000001", [_dup("001"), _grim("002"), _note("003")])
    assert client.post(f"/api/runs/{r1}/findings/001/ruling", json={"ruling": "confirmed"}).status_code == 403
    r = client.post(f"/api/runs/{r1}/findings/001/ruling", json={"ruling": "confirmed", "note": "seen by eye"},
                    headers=G)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ruling"]["ruling"] == "confirmed" and body["review"]["reviewed"] == 1
    client.post(f"/api/runs/{r1}/findings/002/ruling", json={"ruling": "false_positive"}, headers=G)
    rep = client.get(f"/api/runs/{r1}").json()
    by = {f["id"]: f for f in rep["findings"]}
    assert by["001"]["ruling"] == {"ruling": "confirmed", "note": "seen by eye", "updated": by["001"]["ruling"]["updated"]}
    assert by["003"]["ruling"]["ruling"] == "unreviewed" and by["001"]["fingerprint"]
    assert rep["review"]["counts"]["false_positive"] == 1
    assert rep["review"]["score_reviewed"] < rep["score"]

    # a later scan of the same DOI: new run id, findings renumbered and reordered, measurements moved a little
    r2 = _mk_run(sv, "20261105-100000-bbbb-000002", [_note("001"), _grim("002"), _dup("003", ncc=0.78)],
                 source=b"%PDF-1.4 refetched")
    rep2 = client.get(f"/api/runs/{r2}").json()
    by2 = {f["id"]: f for f in rep2["findings"]}
    assert by2["003"]["ruling"]["ruling"] == "confirmed" and by2["003"]["ruling"]["note"] == "seen by eye"
    assert by2["002"]["ruling"]["ruling"] == "false_positive"
    assert by2["001"]["ruling"]["ruling"] == "unreviewed"
    got = client.get(f"/api/runs/{r2}/rulings").json()
    assert got["by_finding"]["003"] == by["001"]["fingerprint"]
    assert got["rulings"][by["001"]["fingerprint"]]["check"] == "image.duplicate"

    assert client.delete(f"/api/runs/{r2}/findings/003/ruling", headers=G).status_code == 200
    assert client.get(f"/api/runs/{r1}").json()["findings"][0]["ruling"]["ruling"] == "unreviewed"


def test_ruling_errors(client, sv):
    rid = _mk_run(sv, "20261101-100000-aaaa-000003", [_dup("001")])
    assert client.post(f"/api/runs/{rid}/findings/001/ruling", json={"ruling": "guilty"}, headers=G).status_code == 400
    assert client.post(f"/api/runs/{rid}/findings/999/ruling", json={"ruling": "confirmed"},
                       headers=G).status_code == 404
    assert client.post("/api/runs/nope/findings/001/ruling", json={"ruling": "confirmed"}, headers=G).status_code == 404
    long = "x" * 10000
    r = client.post(f"/api/runs/{rid}/findings/001/ruling", json={"ruling": "needs_raw_data", "note": long}, headers=G)
    assert len(r.json()["ruling"]["note"]) == 4000


def test_rulings_without_doi_follow_the_file_bytes(client, sv):
    a = _mk_run(sv, "20261101-100000-aaaa-000004", [_dup("001")], doi="", source=b"%PDF-1.4 upload one")
    b = _mk_run(sv, "20261101-100001-aaaa-000005", [_dup("001")], doi="", source=b"%PDF-1.4 upload one")
    c = _mk_run(sv, "20261101-100002-aaaa-000006", [_dup("001")], doi="", source=b"%PDF-1.4 a different paper")
    client.post(f"/api/runs/{a}/findings/001/ruling", json={"ruling": "confirmed"}, headers=G)
    assert client.get(f"/api/runs/{b}").json()["findings"][0]["ruling"]["ruling"] == "confirmed"
    assert client.get(f"/api/runs/{c}").json()["findings"][0]["ruling"]["ruling"] == "unreviewed"


def test_pubpeer_and_html_honour_human_false_positive(client, sv):
    rid = _mk_run(sv, "20261101-100000-aaaa-000007", [_dup("001")])
    assert "F1.a" in client.get(f"/api/runs/{rid}/pubpeer").text
    client.post(f"/api/runs/{rid}/findings/001/ruling", json={"ruling": "false_positive", "note": "secret"}, headers=G)
    assert "No issues worth raising" in client.get(f"/api/runs/{rid}/pubpeer").text
    html = client.get(f"/api/runs/{rid}/report.html").text
    assert "False positive" in html and "secret" not in html


# --- cancelling ---------------------------------------------------------------------------------------------------

def _wait(pred, timeout=10.0):
    end = time.time() + timeout
    while not pred() and time.time() < end:
        time.sleep(0.02)
    return pred()


def test_cancel_running_scan_stops_it_and_removes_half_written_run(client, sv):
    started = threading.Event()
    partial = sv.pipeline.RUNS / "20261101-100000-half-000000"

    def slow(x, progress):
        partial.mkdir(parents=True)
        (partial / "figures").mkdir()
        started.set()
        for _ in range(1000):
            progress("Comparing", 0.5)
            time.sleep(0.01)
        return {"id": "never"}

    job = sv._job("scan", slow, 1)
    assert started.wait(5)
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 403
    r = client.delete(f"/api/jobs/{job['id']}", headers=G)
    assert r.status_code == 200
    assert _wait(lambda: job["status"] != "running")
    assert job["status"] == "cancelled" and job["finished"] and job["run"] is None
    assert not partial.exists()
    assert client.post("/api/jobs/nope/cancel", headers=G).status_code == 404


def test_cancel_waiting_scan_never_starts_it(client, sv, monkeypatch):
    gate = threading.Event()
    ran, cleaned = [], []

    def hold(x, progress):
        gate.wait(10)
        return {"id": f"r{x}"}

    busy = [sv._job("t", hold, i) for i in range(sv.WORKERS)]
    waiting = sv._job("t", lambda x, progress: ran.append(x) or {"id": "x"}, 7, cleanup=lambda: cleaned.append(7))
    r = client.post(f"/api/jobs/{waiting['id']}/cancel", headers=G)
    assert r.json()["status"] == "cancelled" and cleaned == [7]
    gate.set()
    assert _wait(lambda: all(j["status"] == "done" for j in busy))
    time.sleep(0.1)
    assert ran == [] and cleaned == [7] and waiting["status"] == "cancelled"


def test_finished_job_cancel_is_harmless(client, sv):
    job = sv._job("t", lambda x, progress: {"id": "ok"}, 1)
    assert _wait(lambda: job["status"] == "done")
    assert client.delete(f"/api/jobs/{job['id']}", headers=G).json()["status"] == "done"
    assert any(j["id"] == job["id"] for j in client.get("/api/jobs").json())


# --- the batch queue ----------------------------------------------------------------------------------------------

def test_parse_entries_reads_lists_and_reference_exports(monkeypatch):
    from paperinspector import batchqueue
    monkeypatch.setattr(batchqueue, "_inputs", lambda: None)
    text = """10.1371/journal.pone.0001
https://doi.org/10.1038/S41586-020-2649-2
  doi = {10.1016/j.cell.2020.01.001},
DO  - 10.1126/science.abc1234
TY  - JOUR
just some words
10.1371/journal.pone.0001
"""
    entries, skipped = batchqueue.parse_entries(["10.5555/xyz.9"], text)
    dois = [e["doi"] for e in entries]
    assert dois == ["10.5555/xyz.9", "10.1371/journal.pone.0001", "10.1038/s41586-020-2649-2",
                    "10.1016/j.cell.2020.01.001", "10.1126/science.abc1234"]
    reasons = {s["input"]: s["reason"] for s in skipped}
    assert reasons["10.1371/journal.pone.0001"] == "listed twice"
    assert "TY  - JOUR" in reasons and "just some words" in reasons


def test_parse_entries_uses_input_module_for_other_ids(monkeypatch):
    from paperinspector import batchqueue

    class Fake:
        @staticmethod
        def parse_input(s):
            return {"kind": "pmid", "value": s} if s.isdigit() else None

        @staticmethod
        def resolve(parsed):
            return {"doi": "10.9999/from.pmid"}

    monkeypatch.setattr(batchqueue, "_inputs", lambda: Fake)
    entries, skipped = batchqueue.parse_entries(text="12345678\nTY  - JOUR\n")
    assert entries == [{"input": "12345678", "doi": None}]
    assert [s["input"] for s in skipped] == ["TY  - JOUR"]
    assert batchqueue.resolve(entries[0]) == "10.9999/from.pmid"
    monkeypatch.setattr(batchqueue, "_inputs", lambda: None)
    with pytest.raises(ValueError):
        batchqueue.resolve(entries[0])


def test_queue_runs_one_at_a_time_and_reports_each_item(tmp_path):
    from paperinspector.batchqueue import BatchQueue
    live, peak, lock = [0], [0], threading.Lock()

    def runner(item, progress):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        try:
            progress("Working", 0.5)
            time.sleep(0.05)
            if item["doi"].endswith("bad"):
                raise RuntimeError("could not fetch")
            return {"id": "run-" + item["doi"][-1], "score": 12, "paper": {"title": "T " + item["doi"]}}
        finally:
            with lock:
                live[0] -= 1

    q = BatchQueue(tmp_path / "queue.json", runner, poll=0.05)
    out = q.add([{"input": d, "doi": d} for d in ("10.1/a", "10.1/bad", "10.1/c")], "Issue 7")
    assert out["batch"]["name"] == "Issue 7" and len(out["added"]) == 3
    again = q.add([{"input": "10.1/a", "doi": "10.1/a"}])
    assert again["added"] == [] and again["skipped"][0]["reason"] == "already in the queue"
    q.start()
    try:
        assert _wait(lambda: q.list()["counts"]["queued"] == 0 and q.list()["running"] is None)
    finally:
        q.stop()
    items = {i["doi"]: i for i in q.list(out["batch"]["id"])["items"]}
    assert peak[0] == 1
    assert items["10.1/a"]["status"] == "done" and items["10.1/a"]["run"] == "run-a" and items["10.1/a"]["score"] == 12
    assert items["10.1/bad"]["status"] == "error" and "could not fetch" in items["10.1/bad"]["error"]
    assert q.retry(items["10.1/bad"]["id"])["status"] == "queued"
    assert q.clear() == 2


def test_queue_survives_restart(tmp_path):
    from paperinspector.batchqueue import BatchQueue
    gate = threading.Event()
    seen = []

    def runner(item, progress):
        seen.append(item["doi"])
        while not gate.is_set():
            progress("Working", 0.1)
            time.sleep(0.01)
        return {"id": "r-" + item["doi"]}

    q = BatchQueue(tmp_path / "queue.json", runner, poll=0.05)
    q.add([{"input": d, "doi": d} for d in ("10.1/x", "10.1/y")])
    q.start()
    assert _wait(lambda: q.list()["running"] is not None)
    # the app quits while 10.1/x is running: what is on disk says "running"
    on_disk = json.loads((tmp_path / "queue.json").read_text())
    assert [i["status"] for i in on_disk["items"]] == ["running", "queued"]
    q.stop()            # stopping cancels the running scan in memory; the file is what a fresh start reads

    (tmp_path / "queue.json").write_text(json.dumps(on_disk))      # as if the process had died right there
    q2 = BatchQueue(tmp_path / "queue.json", runner, poll=0.05)
    assert [i["status"] for i in q2.items] == ["queued", "queued"]
    assert "restarted" in q2.items[0]["stage"]
    gate.set()
    q2.start()
    try:
        assert _wait(lambda: all(i["status"] == "done" for i in q2.list()["items"]))
    finally:
        q2.stop()
    assert seen[-2:] == ["10.1/x", "10.1/y"]


def test_queue_cancel_waiting_and_running(tmp_path):
    from paperinspector.batchqueue import BatchQueue
    started = threading.Event()

    def runner(item, progress):
        started.set()
        for _ in range(1000):
            progress("Working", 0.2)
            time.sleep(0.01)
        return {"id": "never"}

    tidied = []
    q = BatchQueue(tmp_path / "queue.json", runner, poll=0.05, on_cancelled=tidied.append)
    out = q.add([{"input": d, "doi": d} for d in ("10.1/run", "10.1/wait")])
    a, b = (i["id"] for i in out["added"])
    q.start()
    try:
        assert started.wait(5)
        assert q.cancel(b)["status"] == "cancelled"
        q.cancel(a)
        assert _wait(lambda: q.get(a)["status"] == "cancelled")
    finally:
        q.stop()
    assert q.get(b)["status"] == "cancelled" and q.get(a)["run"] is None
    assert len(tidied) == 1          # only the scan that had started had a folder to tidy


def test_queue_endpoints(client, sv, monkeypatch):
    scanned = []

    def fake_scan_doi(doi, progress=None, online=True, should_cancel=None):
        progress("Fetching", 0.1)
        scanned.append((doi, online))
        return {"id": f"run-{len(scanned)}", "score": 3, "paper": {"title": doi}}

    monkeypatch.setattr(sv.pipeline, "scan_doi", fake_scan_doi)
    monkeypatch.setattr(sv.batchqueue, "_inputs", lambda: None)
    assert client.post("/api/queue", json={"items": ["10.1000/a"]}).status_code == 403
    assert client.post("/api/queue", json={"text": "nothing here"}, headers=G).status_code == 400
    r = client.post("/api/queue", json={"items": ["10.1000/a"], "text": "10.1000/b\nhello", "name": "Batch A",
                                        "online": False}, headers=G)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["added"]) == 2 and body["skipped"][0]["input"] == "hello"
    bid = body["batch"]["id"]
    assert _wait(lambda: client.get("/api/queue").json()["counts"]["done"] == 2)
    assert scanned == [("10.1000/a", False), ("10.1000/b", False)]
    lst = client.get(f"/api/queue?batch={bid}").json()
    assert [i["status"] for i in lst["items"]] == ["done", "done"] and lst["batches"][0]["name"] == "Batch A"
    one = lst["items"][0]
    assert client.get(f"/api/queue/{one['id']}").json()["run"] == "run-1"
    csv_ = client.get(f"/api/queue/batches/{bid}.csv")
    assert csv_.status_code == 200 and "10.1000/a" in csv_.text and csv_.headers["content-disposition"].startswith("attachment")
    assert client.delete(f"/api/queue/{one['id']}", headers=G).json() == {"ok": True}
    assert client.get(f"/api/queue/{one['id']}").status_code == 404
    assert client.post("/api/queue/clear", headers=G).json()["removed"] == 1


# --- exports ------------------------------------------------------------------------------------------------------

def test_csv_and_xlsx_exports_escape_formulas(client, sv):
    rid = _mk_run(sv, "20261101-100000-aaaa-000010", [_dup("001"), _note("002")], title="=cmd|' /C calc'!A0")
    client.post(f"/api/runs/{rid}/findings/001/ruling", json={"ruling": "confirmed", "note": "@SUM(1)"}, headers=G)
    r = client.get(f"/api/runs/{rid}/export/findings.csv")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert [x["id"] for x in rows] == ["001", "002"]
    assert rows[0]["ruling"] == "Confirmed" and rows[0]["note"] == "'@SUM(1)" and rows[0]["check_label"] == "Reused image"
    assert rows[1]["detail"].startswith("'=")
    nonotes = list(csv.DictReader(io.StringIO(client.get(f"/api/runs/{rid}/export/findings.csv?notes=false")
                                              .content.decode("utf-8-sig"))))
    assert nonotes[0]["note"] == ""

    from openpyxl import load_workbook
    x = client.get(f"/api/runs/{rid}/export/findings.xlsx")
    wb = load_workbook(io.BytesIO(x.content))
    ws = wb["Findings"]
    hdr = [c.value for c in ws[1]]
    detail = ws.cell(3, hdr.index("detail") + 1)
    assert detail.data_type == "s" and detail.value.startswith("'=")
    title = [r_[1].value for r_ in wb["Paper"].iter_rows() if r_[0].value == "Title"][0]
    assert title.startswith("'=")


def test_pdf_report(client, sv):
    import pymupdf
    rid = _mk_run(sv, "20261101-100000-aaaa-000011", [_dup("001"), _grim("002")], title="Tiny mice × 3 “quoted”")
    client.post(f"/api/runs/{rid}/findings/001/ruling", json={"ruling": "confirmed", "note": "private"}, headers=G)
    r = client.get(f"/api/runs/{rid}/export/report.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    doc = pymupdf.open("pdf", r.content)
    text = "".join(p.get_text(flags=0) for p in doc)       # flags=0: "fi" not as a ligature
    assert "Tiny mice" in text and "Your review: Confirmed" in text and "page 1 of" in text
    assert "private" not in text
    assert sum(len(p.get_images()) for p in doc) >= 1
    with_notes = pymupdf.open("pdf", client.get(f"/api/runs/{rid}/export/report.pdf?notes=true").content)
    assert "private" in "".join(p.get_text(flags=0) for p in with_notes)


def test_pdf_report_with_no_findings_or_evidence(sv):
    import pymupdf
    from paperinspector import exports
    rid = _mk_run(sv, "20261101-100000-aaaa-000012", [], evidence=False)
    doc = pymupdf.open("pdf", exports.report_pdf(sv.pipeline.RUNS / rid))
    assert "No findings." in doc[0].get_text(flags=0)


def test_evidence_zip_manifest_hashes_every_file(client, sv):
    rid = _mk_run(sv, "20261101-100000-aaaa-000013", [_dup("001")])
    d = sv.pipeline.RUNS / rid
    (d / "supplements").mkdir()
    (d / "supplements" / "S1.xlsx").write_bytes(b"PK\x03\x04 data")
    (d / ".report.abc.tmp").write_text("half")
    client.post(f"/api/runs/{rid}/findings/001/ruling", json={"ruling": "confirmed", "note": "private"}, headers=G)
    r = client.get(f"/api/runs/{rid}/export/evidence.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    top = f"paperinspector-{rid}/"
    names = {n[len(top):] for n in z.namelist()}
    assert {"report.json", "source.pdf", "evidence/dup-pair.png", "supplements/S1.xlsx", "report.pdf", "report.html",
            "findings.csv", "rulings.json", "manifest.json", "SHA256SUMS.txt", "README.txt"} <= names
    assert ".report.abc.tmp" not in names
    man = json.loads(z.read(top + "manifest.json"))
    listed = {f["path"]: f["sha256"] for f in man["files"]}
    for n in names - {"manifest.json", "SHA256SUMS.txt"}:
        assert listed[n] == hashlib.sha256(z.read(top + n)).hexdigest(), n
    sums = dict(reversed(line.split("  ", 1)) for line in z.read(top + "SHA256SUMS.txt").decode().splitlines())
    assert sums["manifest.json"] == hashlib.sha256(z.read(top + "manifest.json")).hexdigest()
    assert b"private" not in z.read(top + "rulings.json") and b"private" not in z.read(top + "findings.csv")
    assert not list(sv.UPLOADS.glob(".evidence-*"))      # the temporary file is gone once sent


# --- letters ------------------------------------------------------------------------------------------------------

def test_letters_list_confirmed_findings_in_neutral_words(client, sv):
    from paperinspector.letters import NEVER
    rid = _mk_run(sv, "20261101-100000-aaaa-000020", [_dup("001"), _grim("002"), _note("003")])
    for kind in ("editor", "authors"):
        empty = client.get(f"/api/runs/{rid}/letters/{kind}").json()
        assert empty["empty"] and "No findings are marked as confirmed" in empty["body"]
    client.post(f"/api/runs/{rid}/findings/001/ruling", json={"ruling": "confirmed", "note": "mine"}, headers=G)
    client.post(f"/api/runs/{rid}/findings/002/ruling", json={"ruling": "needs_raw_data"}, headers=G)
    client.post(f"/api/runs/{rid}/findings/003/ruling", json={"ruling": "false_positive"}, headers=G)
    for kind in ("editor", "authors"):
        L = client.get(f"/api/runs/{rid}/letters/{kind}").json()
        assert not L["empty"] and L["included"] == ["001", "002"]
        body = L["body"]
        assert "F1.a, F2.b" in body and "0.81" in body and "original" in body
        assert "could not decide" in body and "“available on request”" not in body and "mine" not in body
        assert "10.1234/abc.5678" in body and "A study of things" in L["subject"]
        low = (body + L["subject"]).lower()
        assert not [w for w in NEVER if w in low]
    no_unsure = client.get(f"/api/runs/{rid}/letters/editor?needs_data=false").json()
    assert no_unsure["included"] == ["001"]
    assert client.get(f"/api/runs/{rid}/letters/police").status_code == 404
    assert {t["id"] for t in client.get("/api/letters").json()["templates"]} == {"editor", "authors"}


# --- search and tags ----------------------------------------------------------------------------------------------

def test_tags_and_search(client, sv):
    a = _mk_run(sv, "20261101-100000-aaaa-000030", [_dup("001")], title="Mitochondria in yeast",
                doi="10.1111/yeast.1", meta={"journal": "Cell Things", "authors": [{"given": "Test", "family": "Firstauthor"}]})
    b = _mk_run(sv, "20261102-100000-aaaa-000031", [_note("001")], title="Root growth in plants",
                doi="10.2222/root.2", meta={"journal": "Plant Notes", "authors": [{"given": "Test", "family": "Secondauthor"}]})
    assert client.post(f"/api/runs/{a}/tags", json={"add": ["Issue 7"]}).status_code == 403
    r = client.post(f"/api/runs/{a}/tags", json={"add": ["Issue 7", "issue 7", " to\x00check  "]}, headers=G)
    assert r.json()["tags"] == ["Issue 7", "tocheck"]
    assert client.post(f"/api/runs/{a}/tags", json={"remove": ["TOCHECK"]}, headers=G).json()["tags"] == ["Issue 7"]
    client.post(f"/api/runs/{b}/tags", json={"set": ["issue 7", "plants"]}, headers=G)
    assert client.get(f"/api/runs/{b}/tags").json()["tags"] == ["issue 7", "plants"]

    def ids(**q):
        return [x["id"] for x in client.get("/api/runs/search", params=q).json()["runs"]]

    assert ids() == [b, a]                       # newest first
    assert ids(q="yeast") == [a]
    assert ids(q="10.2222/root") == [b]
    assert ids(q="firstauthor") == [a]
    assert ids(q="plant notes") == [b]
    assert ids(tag="Issue 7") == [b, a]
    assert ids(tag="plants") == [b]
    assert ids(q="nothing-like-this") == []
    assert ids(sort="score")[0] == a
    tags = {t["tag"].lower(): t["count"] for t in client.get("/api/tags").json()["tags"]}
    assert tags == {"issue 7": 2, "plants": 1}
    assert client.get(f"/api/runs/{a}").json()["tags"] == ["Issue 7"]
    assert {r_["id"]: r_["tags"] for r_ in client.get("/api/runs").json()}[a] == ["Issue 7"]
    client.delete(f"/api/runs/{a}", headers=G)
    assert ids(tag="Issue 7") == [b]
    assert client.post("/api/runs/nope/tags", json={"add": ["x"]}, headers=G).status_code == 404


# --- catalogue ----------------------------------------------------------------------------------------------------

EMITTED = """image.duplicate image.duplicate_chart image.clone image.splice image.flat image.metadata data.duplicate_block
data.derived_column data.random_formula data.terminal_digits data.digit_preference data.repeated_values data.impossible
data.sort_anomaly data.too_similar data.gene_dates data.hidden data.mixed_fonts data.generator_digits stats.recompute
stats.grim stats.percent stats.repeated_se stats.impossible text.ai_boilerplate text.ai_statement text.tortured
text.prompt_injection text.hidden_text text.hidden_tags text.invisible_chars refs.bad_doi refs.unverified
refs.self_citation refs.citation_pushing refs.journal_self refs.off_topic refs.sneaked refs.uncited refs.missing
refs.duplicate refs.retracted refs.unchecked report.timeline report.missing_data report.missing_coi
report.missing_funding report.data_on_request report.no_ethics report.no_registration report.data_not_shown
report.sem_small_n report.misidentified_cells report.figure_count report.abstract_numbers report.accession_missing
record.retracted record.concern record.correction record.fast_acceptance record.duplicate_title
record.author_retractions record.unchecked""".split()


def test_catalog_explains_every_check(client):
    from paperinspector.models import INTEGRITY
    body = client.get("/api/catalog").json()
    known = {c["id"]: c for c in body["checks"]}
    assert set(EMITTED) | INTEGRITY <= set(known)
    for c in known.values():
        assert c["label"] and c["what"] and c["false_alarms"] and c["look_at"], c["id"]
        assert c["scored"] == (c["id"] in INTEGRITY) and c["kind"] in ("integrity", "quality")
    assert {r["id"] for r in body["rulings"]} == {"unreviewed", "confirmed", "false_positive", "needs_raw_data"}
    one = client.get("/api/catalog/image.clone").json()
    assert one["label"] == "Cloned region" and one["known"]
    new = client.get("/api/catalog/image.brand_new_check").json()
    assert not new["known"] and new["category"] == "image" and new["what"]


# --- settings -----------------------------------------------------------------------------------------------------

def test_contact_email_is_opt_in_and_validated(client, sv):
    assert client.get("/api/settings").json()["email"] == ""
    assert sv.settings.contact_email() == ""
    client.post("/api/settings", json={"email": "not an address"}, headers=G)
    assert sv.settings.contact_email() == ""
    client.post("/api/settings", json={"email": " someone@example.org "}, headers=G)
    assert client.get("/api/settings").json()["email"] == "someone@example.org"
    client.post("/api/settings", json={"email": "x <a@b.co>"}, headers=G)
    assert sv.settings.contact_email() == "someone@example.org"
    client.post("/api/settings", json={"email": ""}, headers=G)
    assert sv.settings.contact_email() == ""


# --- command line -------------------------------------------------------------------------------------------------

def test_cli_list_and_export(sv, tmp_path, capsys):
    from paperinspector import cli
    rid = _mk_run(sv, "20261101-100000-aaaa-000040", [_dup("001")], title="A paper about leaves")
    assert cli.main(["list", "leaves"]) == 0
    assert rid in capsys.readouterr().out
    for fmt in ("csv", "xlsx", "pdf", "zip"):
        out = tmp_path / f"x.{fmt}"
        assert cli.main(["export", rid, "--format", fmt, "-o", str(out)]) == 0
        assert out.stat().st_size > 100, fmt
    assert zipfile.ZipFile(tmp_path / "x.zip").testzip() is None
    assert cli.main(["export", "no-such-run"]) == 2


def test_parse_entries_with_the_real_input_module(monkeypatch):
    inputs = pytest.importorskip("paperinspector.inputs")
    from paperinspector import batchqueue
    text = """10.1371/journal.pone.0001
PMID: 12345678
@article{key2020,
  title = {A title line that must not be searched},
  doi = {10.1016/j.cell.2020.01.001},
}
https://doi.org/10.1371/JOURNAL.PONE.0001
"""
    entries, skipped = batchqueue.parse_entries(text=text)
    assert [e["doi"] for e in entries] == ["10.1371/journal.pone.0001", None, "10.1016/j.cell.2020.01.001"]
    assert "12345678" in entries[1]["input"]
    calls = []

    def fake_resolve(t, client=None, should_cancel=None):
        calls.append(t)
        return {"doi": "10.5555/pmid.found", "message": ""}

    monkeypatch.setattr(inputs, "resolve", fake_resolve)
    assert batchqueue.resolve(entries[1], should_cancel=lambda: False) == "10.5555/pmid.found"
    monkeypatch.setattr(inputs, "resolve", lambda t, client=None, should_cancel=None:
                        {"doi": None, "message": "PubMed has no record of this ID."})
    with pytest.raises(ValueError, match="PubMed has no record"):
        batchqueue.resolve(entries[1])
    assert len(calls) == 1
