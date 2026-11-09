"""Benchmark the image checks on synthetic papers with one planted manipulation each, plus clean controls.

    python tools/bench.py --n 4          # 4 papers per (manipulation, image type)
    python tools/bench.py --n 4 --seed 99  # a held-out draw

Recall counts a plant as found when a finding of the right check names the planted panel(s).  The false-alarm
rate counts clean control papers with any image finding of medium or worse.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import zlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from paperinspector import synth  # noqa: E402

KINDS = ["fluorescence", "histology", "phase", "photo", "blot"]


def _copy_with(t):
    def f(rng, src):
        return t(src, rng)
    return f


MANIP = {
    "copy+contrast": lambda src, rng: synth.adjust(src, 1.2, -10),
    "mirror": lambda src, rng: synth.flip_h(src),
    "rotate90": lambda src, rng: synth.rotate(src, 90),
    "rotate180": lambda src, rng: synth.rotate(src, 180),
    "rotate7": lambda src, rng: synth.rotate(src, 7),
    "rescale1.4": lambda src, rng: synth.rescale(synth.crop(src, 0.1, 0.1, 0.8, 0.8), 1.4),
    "stretch": lambda src, rng: synth.rescale(src, 1.25, 1.0),
    "overlap": None,   # two crops of one field
    "jpeg-copy": lambda src, rng: synth.jpeg(synth.adjust(src, 0.9, 12), 70),
    "clone": None,
    "splice": None,
    "paint": None,
}
CHECK = {"clone": "image.clone", "splice": "image.splice", "paint": "image.flat"}


def make(kind: str, manip: str | None, seed: int):
    rng = np.random.default_rng(seed)
    gen = synth.GENERATORS[kind]
    panels = [gen(rng) for _ in range(6)]
    tags: list[str] = []
    if manip == "overlap":
        big = synth.blot(rng, lanes=10) if kind == "blot" else gen_big(kind, rng)
        panels[1] = synth.crop(big, 0.0, 0.0, 0.62, 1.0 if kind == "blot" else 0.75)
        panels[4] = synth.crop(big, 0.38, 0.0 if kind == "blot" else 0.25, 1.0, 1.0)
        tags = ["F1.b", "F1.e"]
    elif manip == "clone":
        panels[2], _ = synth.clone_patch(panels[2], rng, 0.28 if kind == "blot" else 0.24)
        tags = ["F1.c"]
    elif manip == "splice":
        if kind != "blot":
            return None
        lw = 42
        a, b = synth.blot(rng, lanes=6, lane_w=lw), synth.blot(rng, lanes=6, lane_w=lw)
        panels[3] = synth.splice_lanes(a, b, lw, int(rng.integers(2, 5)))
        tags = ["F1.d"]
    elif manip == "paint":
        if kind in ("histology", "photo", "phase"):
            return None
        panels[3] = synth.paint_patch(panels[3], rng, value=(40, 40, 40) if kind == "blot" else (12, 10, 8))
        tags = ["F1.d"]
    elif manip:
        panels[4] = MANIP[manip](panels[0], rng)
        tags = ["F1.a", "F1.e"]
    cols = 2 if kind == "blot" else 3
    spec = synth.PaperSpec(f"Synthetic {kind} study {seed}", "Synthetic abstract.",
                           ["Methods. Synthetic data generated for benchmarking."],
                           [synth.FigureSpec(1, f"Synthetic {kind} panels.", panels, cols=cols)])
    return spec, tags


def gen_big(kind, rng):
    if kind == "fluorescence":
        return synth.fluorescence(rng, 420, 560)
    if kind == "histology":
        return synth.histology(rng, 420, 560)
    if kind == "phase":
        return synth.phase_contrast(rng, 420, 560)
    return synth.photo(rng, 420, 560)


def run_one(args):
    kind, manip, seed, home = args
    import os
    os.environ["PAPERINSPECTOR_HOME"] = home
    from paperinspector import pipeline
    made = make(kind, manip, seed)
    if made is None:
        return None
    spec, tags = made
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"{kind}-{manip}-{seed}.pdf"
        synth.to_pdf_file(spec, p)
        rep = pipeline.scan(p)
    img = [f for f in rep["findings"] if f["check"].startswith("image.") and f["severity"] in ("critical", "high", "medium")]
    want = CHECK.get(manip or "", "image.duplicate")
    hit = False
    if manip:
        for f in img:
            panels = {l.get("panel") for l in f["locations"]}
            if f["check"] == want and set(tags) <= panels:
                hit = True
    wrong = [f for f in img if not (manip and set(tags) & {l.get("panel") for l in f["locations"]})]
    return {"kind": kind, "manip": manip or "control", "hit": hit, "false": len(wrong),
            "false_titles": [f["title"] for f in wrong][:3], "seconds": rep["timings"]["total"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    home = tempfile.mkdtemp(prefix="blinkbench-")
    import os
    os.environ["PAPERINSPECTOR_HOME"] = home  # workers inherit it; never write into the real data folder
    jobs = []
    for kind in KINDS:
        for manip in list(MANIP) + [None]:
            for i in range(a.n * (2 if manip is None else 1)):
                jobs.append((kind, manip, a.seed + i * 7919 + zlib.crc32(f"{kind}{manip}".encode()) % 1000, home))
    t0 = time.time()
    with ProcessPoolExecutor(a.workers) as ex:
        res = [r for r in ex.map(run_one, jobs) if r]
    by = defaultdict(lambda: [0, 0])
    byk = defaultdict(lambda: [0, 0])
    fa_ctrl, n_ctrl, fa_any = 0, 0, 0
    for r in res:
        if r["manip"] == "control":
            n_ctrl += 1
            fa_ctrl += r["false"] > 0
            if r["false"]:
                print("FALSE ALARM", r["kind"], r["false_titles"])
            continue
        by[r["manip"]][0] += r["hit"]
        by[r["manip"]][1] += 1
        byk[r["kind"]][0] += r["hit"]
        byk[r["kind"]][1] += 1
        fa_any += r["false"] > 0
        if not r["hit"]:
            print("MISS", r["kind"], r["manip"])
        if r["false"]:
            print("EXTRA", r["kind"], r["manip"], r["false_titles"])
    print("\nrecall by manipulation")
    for m, (h, n) in by.items():
        print(f"  {m:<14} {h}/{n}  {h / n:.0%}")
    print("recall by image type")
    for k, (h, n) in byk.items():
        print(f"  {k:<14} {h}/{n}  {h / n:.0%}")
    tot_h = sum(v[0] for v in by.values())
    tot_n = sum(v[1] for v in by.values())
    print(f"\noverall recall {tot_h}/{tot_n} = {tot_h / tot_n:.1%}")
    print(f"clean papers with a false image alarm: {fa_ctrl}/{n_ctrl} = {fa_ctrl / max(1, n_ctrl):.1%}")
    print(f"manipulated papers with an extra wrong alarm: {fa_any}/{tot_n}")
    print(f"{len(res)} papers in {time.time() - t0:.0f}s")
    if a.out:
        Path(a.out).write_text(json.dumps({"by_manip": by, "by_kind": byk, "recall": tot_h / tot_n,
                                           "control_false": fa_ctrl / max(1, n_ctrl)}, indent=1))


if __name__ == "__main__":
    main()
