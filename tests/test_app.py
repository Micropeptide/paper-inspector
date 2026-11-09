"""End to end: the demo paper through the pipeline and the HTTP API."""
from __future__ import annotations

import time

import pytest


@pytest.fixture(scope="module")
def client(tmp_path_factory, monkeypatch_module):
    home = tmp_path_factory.mktemp("home")
    monkeypatch_module.setenv("PAPERINSPECTOR_HOME", str(home))
    import importlib

    import paperinspector.pipeline as pl
    importlib.reload(pl)
    import paperinspector.settings as st
    importlib.reload(st)
    import paperinspector.server as sv
    importlib.reload(sv)
    from fastapi.testclient import TestClient
    return TestClient(sv.app, headers={"X-Paper-Inspector": "1"})


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def demo_run(client):
    job = client.post("/api/demo").json()
    for _ in range(240):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] != "running":
            break
        time.sleep(0.25)
    assert j["status"] == "done", j
    return j["run"]


def test_demo_finds_every_plant(client, demo_run):
    r = client.get(f"/api/runs/{demo_run}").json()
    pairs = {tuple(sorted(l["panel"] for l in f["locations"])) for f in r["findings"] if f["check"] == "image.duplicate"}
    assert {("F1.a", "F1.e"), ("F2.b", "F2.d"), ("F3.b", "F3.e"), ("F4.b", "F4.e")} <= pairs
    checks = {f["check"] for f in r["findings"]}
    for c in ("image.clone", "image.flat", "image.splice", "text.ai_boilerplate", "text.ai_statement", "text.tortured",
              "stats.recompute", "stats.grim", "stats.percent", "stats.repeated_se", "refs.uncited",
              "refs.duplicate", "data.duplicate_block", "data.random_formula", "data.gene_dates"):
        assert c in checks, c
    assert r["score"] >= 70


def test_api_views_and_compare(client, demo_run):
    assert client.get(f"/api/runs/{demo_run}/panels/F1.a.png?mode=reveal").status_code == 200
    c = client.get(f"/api/runs/{demo_run}/compare?a=F1.a&b=F1.e").json()
    assert c["aligned"] and c["mirrored"]
    assert client.get(f"/api/runs/{demo_run}/compare?a=F1.a&b=F1.b").json()["aligned"] is False
    assert client.get(f"/api/runs/{demo_run}/compare/diff.png?a=F1.a&b=F1.e").status_code == 200
    assert "raw" in client.get(f"/api/runs/{demo_run}/pubpeer").text or "original" in \
        client.get(f"/api/runs/{demo_run}/pubpeer").text
    assert client.get(f"/api/runs/{demo_run}/report.html").text.startswith("<!doctype html>")
    assert any(x["id"] == demo_run for x in client.get("/api/runs").json())


def test_settings_never_echo_keys(client):
    s = client.post("/api/settings", json={"provider": "deepseek", "keys": {"deepseek": "sk-test-123"}}).json()
    assert s["key_set"]["deepseek"] is True
    assert "sk-test-123" not in str(s)
    client.post("/api/settings", json={"provider": "anthropic", "keys": {"deepseek": ""}})


def test_path_traversal_rejected(client):
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404
