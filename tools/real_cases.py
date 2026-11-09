"""Real-paper benchmark: retracted open-access papers against non-retracted ones from the same journal.

    python tools/real_cases.py --n 12 --out real_cases.json

Retracted papers are not all image cases, and controls are not guaranteed clean, so read the result as a
separation (do retracted papers score higher?) and a false-alarm audit (open every control flagged high).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from paperinspector import pipeline  # noqa: E402
from paperinspector.models import risk_score  # noqa: E402

PLOS_ONE = "S202381698"


def sample(retracted: bool, n: int, seed: int) -> list[str]:
    r = httpx.get("https://api.openalex.org/works", params={
        "filter": f"is_retracted:{str(retracted).lower()},primary_location.source.id:{PLOS_ONE},"
                  "publication_year:2012-2020,type:article",
        "sample": n, "seed": seed, "per-page": n, "select": "doi"}, timeout=60)
    return [w["doi"].replace("https://doi.org/", "") for w in r.json().get("results", []) if w.get("doi")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="real_cases.json")
    ap.add_argument("--dois", default=str(Path(__file__).with_name("real_cases_dois.json")),
                    help="fixed DOI list (JSON with 'retracted' and 'control'); use --sample to draw new ones")
    ap.add_argument("--sample", action="store_true")
    a = ap.parse_args()
    fixed = json.loads(Path(a.dois).read_text()) if not a.sample and Path(a.dois).exists() else None
    rows = []
    for label, retracted in (("retracted", True), ("control", False)):
        for doi in (fixed[label][:a.n] if fixed else sample(retracted, a.n, a.seed)):
            t = time.time()
            try:
                r = pipeline.scan_doi(doi)
                top = [f"{f['severity']}:{f['check']}:{f['title'][:70]}" for f in r["findings"]
                       if not f["check"].startswith("record.")
                       if f["kind"] == "integrity" and f["severity"] in ("critical", "high", "medium")][:6]
                content = [f for f in r["findings"] if not f["check"].startswith("record.")]
                rows.append({"group": label, "doi": doi, "score": risk_score(content), "score_all": r["score"],
                             "run": r["id"],
                             "figures": len(r["figures"]), "panels": r["panels"], "kinds": r["kinds"],
                             "sources": len(r.get("sources", [])), "top": top, "seconds": round(time.time() - t, 1)})
            except Exception as e:
                rows.append({"group": label, "doi": doi, "error": f"{type(e).__name__}: {e}"})
            print(json.dumps(rows[-1]), flush=True)
    Path(a.out).write_text(json.dumps(rows, indent=1))
    for g in ("retracted", "control"):
        s = [r["score"] for r in rows if r["group"] == g and "score" in r]
        hi = sum(x >= 40 for x in s)
        print(f"{g} (score without the retraction notice): n={len(s)} median score {sorted(s)[len(s) // 2] if s else '-'}; flagged (score ≥ 40): {hi}")


if __name__ == "__main__":
    main()
