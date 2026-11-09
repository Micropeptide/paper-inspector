"""Score a real_cases.py run against what the retraction notices actually say.

    python tools/real_eval.py --cases real_cases.json --home <PAPERINSPECTOR_HOME used for the run>

Hit: an integrity image finding of medium or worse that touches a figure the notice names.
False-alarm load: integrity image findings (high or worse) in papers whose problem was not an image problem, and in
non-retracted controls.  Controls are not guaranteed clean, so every control hit deserves a look rather than a
count.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TRUTH = Path(__file__).with_name("real_cases_truth.json")


def fig_numbers(f: dict) -> set[str]:
    out = set()
    for loc in f.get("locations", []):
        m = re.match(r"(?:\d+_)?F(S?\d+)", loc.get("figure") or loc.get("panel") or "")
        if m:
            out.add(m.group(1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--home", required=True)
    a = ap.parse_args()
    truth = json.loads(TRUTH.read_text())
    rows = json.loads(Path(a.cases).read_text())
    hits = total = 0
    load = {"non-image retraction": [], "control": []}
    for r in rows:
        if "run" not in r:
            continue
        rep = json.loads((Path(a.home) / "runs" / r["run"] / "report.json").read_text())
        img = [f for f in rep["findings"] if f["check"].startswith("image.") and f.get("kind") == "integrity"]
        t = truth.get(r["doi"])
        if t and t["image"]:
            total += 1
            named = set(t["image"])
            ok = [f for f in img if f["severity"] in ("critical", "high", "medium") and fig_numbers(f) & named]
            hits += bool(ok)
            print(f"{'HIT ' if ok else 'MISS'} {r['doi']}  named Fig {', '.join(sorted(named))}: "
                  + ("; ".join(f"{f['severity']} {f['title'][:70]}" for f in ok[:2]) or t["kind"][:80]))
        else:
            group = "control" if r["group"] == "control" else "non-image retraction"
            strong = [f for f in img if f["severity"] in ("critical", "high")]
            load[group].append((r["doi"], len(strong)))
    print(f"\nimage retractions: the named figure was flagged in {hits}/{total}")
    for g, v in load.items():
        if v:
            flagged = sum(1 for _, n in v if n)
            print(f"{g}: {flagged}/{len(v)} papers with a high/critical image flag; "
                  + ", ".join(f"{d[-7:]}:{n}" for d, n in v))


if __name__ == "__main__":
    main()
