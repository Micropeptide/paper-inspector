"""The local API's defences: loopback-only Host, the custom header on writes, safe serving of uploaded files,
bounded uploads and a bounded job queue."""
from __future__ import annotations

import importlib
import json
import threading
import time

import pymupdf
import pytest

G = {"X-Paper-Inspector": "1"}
SCRIPT = b"<html><script>fetch('/api/settings')</script></html>"


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
    server.POOL.shutdown(wait=True)


@pytest.fixture()
def client(sv):
    from fastapi.testclient import TestClient
    return TestClient(sv.app)


def _pdf() -> bytes:
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "An honest paper")
    return d.tobytes()


def _run(sv, rid="20260101-000000-abcdef12", files=None):
    d = sv.pipeline.RUNS / rid
    d.mkdir(parents=True)
    (d / "report.json").write_text(json.dumps({"id": rid}))
    for name, data in (files or {}).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
    return rid


def _wait(job, timeout=10.0):
    end = time.time() + timeout
    while job["status"] == "running" and time.time() < end:
        time.sleep(0.02)


# --- Host header (DNS rebinding) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost", "localhost:5173", "[::1]:8765", "127.0.0.1"])
def test_loopback_hosts_answered(client, host):
    r = client.get("/api/health", headers={"Host": host})
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "localhost.evil.example", "127.0.0.1.nip.io",
                                  "192.168.1.5:8765", ""])
def test_rebound_hosts_refused(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 400
    assert client.get("/api/settings", headers={"Host": host}).status_code == 400


def test_testserver_host_only_in_tests(client, monkeypatch):
    assert client.get("/api/health").status_code == 200
    monkeypatch.delenv("PAPERINSPECTOR_TESTING")
    assert client.get("/api/health").status_code == 400


# --- custom header on POST / DELETE (CSRF) ------------------------------------------------------------------------

def test_writes_need_header(client, sv):
    assert client.post("/api/settings", json={"online": True}).status_code == 403
    assert client.post("/api/settings", json={"online": True}, headers={"X-Paper-Inspector": "0"}).status_code == 403
    assert client.post("/api/settings", json={"online": False}, headers=G).status_code == 200
    rid = _run(sv)
    assert client.delete(f"/api/runs/{rid}").status_code == 403
    assert (sv.pipeline.RUNS / rid).exists()
    assert client.delete(f"/api/runs/{rid}", headers=G).status_code == 200
    assert client.get("/api/runs").status_code == 200


def test_cross_site_form_upload_refused_before_saving(client, sv):
    r = client.post("/api/scan", files={"file": ("x.pdf", _pdf(), "application/pdf")},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not sv.UPLOADS.exists() or not any(sv.UPLOADS.iterdir())
    assert not sv.JOBS


def test_preflight_fails_for_other_origins(client):
    ask = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-paper-inspector"}
    evil = client.options("/api/scan", headers={"Origin": "https://evil.example", **ask})
    assert "access-control-allow-origin" not in evil.headers
    dev = client.options("/api/scan", headers={"Origin": "http://localhost:5173", **ask})
    assert dev.headers.get("access-control-allow-origin") == "http://localhost:5173"


# --- serving uploaded and fetched files ---------------------------------------------------------------------------

def test_html_source_never_served_inline(client, sv):
    rid = _run(sv, files={"source.html": SCRIPT})
    r = client.get(f"/api/runs/{rid}/source")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


def test_html_renamed_pdf_not_served_as_pdf(client, sv):
    rid = _run(sv, files={"source.pdf": SCRIPT})
    r = client.get(f"/api/runs/{rid}/source")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")


def test_real_pdf_source_opens_inline(client, sv):
    rid = _run(sv, files={"source.pdf": _pdf()})
    r = client.get(f"/api/runs/{rid}/source")
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_supplements_and_evidence_are_safe(client, sv):
    rid = _run(sv, files={"supplements/notes.html": SCRIPT, "supplements/data.svg": b"<svg onload='x()'/>",
                          "evidence/pair.png": b"\x89PNG\r\n\x1a\n" + b"0" * 32, "evidence/x.html": SCRIPT})
    for path in ("supplements/notes.html", "supplements/data.svg", "evidence/x.html"):
        r = client.get(f"/api/runs/{rid}/{path}")
        assert r.headers["content-type"] == "application/octet-stream", path
        assert r.headers["content-disposition"].startswith("attachment"), path
        assert "sandbox" in r.headers["content-security-policy"]
    r = client.get(f"/api/runs/{rid}/evidence/pair.png")
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"


# --- uploads ------------------------------------------------------------------------------------------------------

def test_paper_upload_must_be_pdf_or_image(client, sv):
    for name, data in (("evil.html", SCRIPT), ("paper.pdf", SCRIPT), ("fig.png", SCRIPT), ("paper.svg", b"<svg/>")):
        r = client.post("/api/scan", files={"file": (name, data, "application/octet-stream")}, headers=G)
        assert r.status_code == 400, name
    assert not any(sv.UPLOADS.iterdir())
    assert not sv.JOBS


def test_honest_upload_accepted_and_cleaned_up(client, sv, monkeypatch):
    seen = {}

    def fake_scan(path, progress=None, online=False, supplements=None):
        seen["paper"] = path.read_bytes()[:5]
        seen["supp"] = [p.name for p in supplements]
        return {"id": "r"}

    monkeypatch.setattr(sv.pipeline, "scan", fake_scan)
    r = client.post("/api/scan", headers=G, files=[("file", ("Paper.PDF", _pdf(), "application/pdf")),
                                                   ("supplements", ("S1.xlsx", b"PK\x03\x04", "application/zip")),
                                                   ("supplements", ("S1.xlsx", b"PK\x03\x04", "application/zip"))])
    assert r.status_code == 200
    job = sv.JOBS[r.json()["id"]]
    _wait(job)
    assert job["status"] == "done"
    assert seen == {"paper": b"%PDF-", "supp": ["S1.xlsx", "S1.xlsx"]}
    assert not any(sv.UPLOADS.iterdir())


def test_oversized_upload_refused(client, sv, monkeypatch):
    monkeypatch.setattr(sv, "MAX_UPLOAD", 1000)
    big = b"%PDF-" + b"0" * 5000
    r = client.post("/api/scan", files={"file": ("p.pdf", big, "application/pdf")}, headers=G)
    assert r.status_code == 413

    def chunks():
        for _ in range(10):
            yield b"0" * 500

    r = client.post("/api/settings", content=chunks(), headers={**G, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert not sv.JOBS


def test_upload_budget_counts_while_copying(sv, monkeypatch):
    import io

    from fastapi import HTTPException, UploadFile
    monkeypatch.setattr(sv, "MAX_UPLOAD", 1000)
    up = sv.Uploads()
    with pytest.raises(HTTPException) as e:
        up.save(UploadFile(io.BytesIO(b"%PDF-" + b"0" * 5000), filename="p.pdf"), paper=True)
    assert e.value.status_code == 413
    up.discard()
    assert not up.dir.exists()


# --- jobs ---------------------------------------------------------------------------------------------------------

def test_job_queue_is_bounded(sv, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(sv, "MAX_PENDING", 2)
    gate = threading.Event()
    cleaned = []

    def slow(x, progress):
        gate.wait(10)
        return {"id": x}

    jobs = [sv._job("t", slow, i, cleanup=lambda i=i: cleaned.append(i)) for i in range(2)]
    with pytest.raises(HTTPException) as e:
        sv._job("t", slow, 9, cleanup=lambda: cleaned.append(9))
    assert e.value.status_code == 429
    assert cleaned == [9]  # a refused job still drops its uploads
    with pytest.raises(HTTPException):
        sv._admit()
    gate.set()
    for j in jobs:
        _wait(j)
    assert [j["status"] for j in jobs] == ["done", "done"]
    assert sorted(cleaned) == [0, 1, 9]
    sv._admit()


def test_finished_jobs_expire(sv, monkeypatch):
    now = time.time()
    sv.JOBS.update({
        "old": {"id": "old", "status": "done", "finished": now - sv.JOB_TTL - 1},
        "fresh": {"id": "fresh", "status": "error", "finished": now - 5},
        "live": {"id": "live", "status": "running", "finished": None},
    })
    sv._prune(now)
    assert set(sv.JOBS) == {"fresh", "live"}
    monkeypatch.setattr(sv, "MAX_JOBS", 3)
    for k in range(5):
        sv.JOBS[f"d{k}"] = {"id": f"d{k}", "status": "done", "finished": now - 100 + k}
    sv._prune(now)
    assert len(sv.JOBS) == 3 and "live" in sv.JOBS and "d4" in sv.JOBS


def test_failed_job_reports_error(sv):
    def boom(x, progress):
        raise RuntimeError("bad file")

    j = sv._job("t", boom, 1)
    _wait(j)
    assert j["status"] == "error" and j["error"] == "bad file" and j["finished"]


# --- compare cache ------------------------------------------------------------------------------------------------

def test_delete_run_drops_cached_comparisons(client, sv):
    rid = _run(sv)
    sv._COMPARE[(rid, "a", "b")] = {"aligned": False}
    sv._COMPARE[("other", "a", "b")] = {"aligned": False}
    assert client.delete(f"/api/runs/{rid}", headers=G).status_code == 200
    assert list(sv._COMPARE) == [("other", "a", "b")]
    assert client.get(f"/api/runs/{rid}/compare?a=a&b=b").status_code == 404
