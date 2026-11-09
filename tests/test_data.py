"""Data forensics on spreadsheets: planted problems are found, honest data stays quiet."""
from __future__ import annotations

import numpy as np
import openpyxl

from paperinspector import datacheck, reporting, synth, tables
from paperinspector.models import Paper


def _checks(path, label="S1.xlsx"):
    return {f.check for f in datacheck.check(tables.read_any(path, label))}


def test_demo_supplement_planted_problems(tmp_path):
    p = tmp_path / "s.xlsx"
    synth.demo_supplement(p)
    found = _checks(p)
    assert {"data.duplicate_block", "data.random_formula", "data.too_similar", "data.mixed_fonts",
            "data.gene_dates", "data.hidden"} <= found


def test_honest_spreadsheet_is_quiet(tmp_path):
    rng = np.random.default_rng(4)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ID", "Weight (g)", "Glucose (mM)", "Insulin (ng/mL)", "p value"])
    for i in range(80):
        ws.append([i + 1, float(np.round(rng.normal(25, 2), 2)), float(np.round(rng.normal(7.1, 1.2), 2)),
                   float(np.round(rng.lognormal(0, 0.5), 3)), float(np.round(rng.uniform(0, 1), 4))])
    p = tmp_path / "clean.xlsx"
    wb.save(p)
    assert _checks(p) == set()


def test_csv_and_copied_block_across_files(tmp_path):
    rng = np.random.default_rng(5)
    a = np.round(rng.normal(10, 2, 30), 3)
    (tmp_path / "a.csv").write_text("x\n" + "\n".join(map(str, a)))
    (tmp_path / "b.csv").write_text("y\n" + "\n".join(map(str, np.r_[np.round(rng.normal(5, 1, 10), 3), a[5:20]])))
    t = tables.read_any(tmp_path / "a.csv", "a.csv") + tables.read_any(tmp_path / "b.csv", "b.csv")
    dup = [f for f in datacheck.check(t) if f.check == "data.duplicate_block"]
    assert dup and dup[0].data["cross_file"]


def test_misidentified_cell_lines_offline():
    p = Paper(source="x", pages=["Cells: HEp-2 and SGC-7901 cell lines were cultured. Mice were used."])
    f = reporting.cell_lines(p, online=False)
    assert f and "HEp-2" in f[0].data["lines"]
    honest = Paper(source="x", pages=["MCF-7, A549, HEK293T and HeLa cells were cultured."])
    assert reporting.cell_lines(honest, online=False) == []


def test_statements_and_timeline():
    body = ("Methods. Twelve mice were used. " * 100 + "Data are available from the corresponding author upon "
            "reasonable request. Received: 3 March 2021; Accepted: 6 March 2021.")
    p = Paper(source="x", pages=[body])
    checks = {f.check for f in reporting.statements(p)}
    assert {"report.data_on_request", "report.no_ethics"} <= checks
    tl, info = reporting.timeline(p, {})
    assert info["days_to_accept"] == 3 and tl[0].check == "record.fast_acceptance"
