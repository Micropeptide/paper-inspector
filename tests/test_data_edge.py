"""Edge cases for the number, table and text checks: honest layouts stay quiet, real problems are still caught."""
from __future__ import annotations

import codecs
import re
import time
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
import openpyxl
import pymupdf
import pytest

from paperinspector import datacheck, models, refs, reporting, stats, tables, textcheck
from paperinspector.models import INTEGRITY, Finding, Paper, risk_score
from paperinspector.tables import Table

ENGINE = Path(__file__).resolve().parents[1] / "engine" / "paperinspector"


def _paper(text: str) -> Paper:
    return Paper(source="t.pdf", pages=[text], page_count=1)


def _checks(found) -> set[str]:
    return {f.check for f in found}


# ---- statistics ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "t(28) = 5.20, p = 1.6 × 10-5", "t(28) = 5.20, p = 1.6e-5", "t(28) = 5.20, P = 1.6 x 10-5",
    "t(28) = 5.20, p = 1.6 × 10^-5", "t(28) = 5.20, p < 1 × 10-4", "t(28) = 5.20, p = 2e-5",
])
def test_scientific_p_values_are_read_whole(text):
    assert stats.recompute(text) == []


def test_scientific_p_value_through_check_with_unicode_minus():
    assert _checks(stats.check(_paper("Treated mice differed, t(28) = 5.20, P = 1.6 × 10−5."))) == set()
    bad = stats.recompute("t(28) = 1.20, p = 1.5 × 10-5")
    assert bad and bad[0]["decision"] and bad[0]["reported"] == "p = 1.5 × 10-5"


def test_p_below_05_is_a_significance_claim():
    bad = stats.recompute("t(20) = 1.50, p < .05")
    assert bad and bad[0]["decision"]
    assert stats.recompute("t(20) = 1.50, p > .05") == []
    assert stats.recompute("t(10) = 2.23, p = 0.05.") == []


def test_one_tailed_test_is_accepted_when_stated():
    assert stats.recompute("A one-tailed test gave t(15) = 1.90, p = .038.") == []
    assert stats.recompute("t(15) = 1.90, p = .038.")


def test_statistic_without_leading_zero():
    assert stats.recompute("r(28) = .45, p = .80")
    assert stats.recompute("r(28) = .45, p = .013") == []


@pytest.mark.parametrize("text", ["P = 2.3e-5", "p = 3.1 × 10-4", "p = 1.5 × 10-5", "p = 2.3 × 10^-5",
                                  "cells at passage P = 3", "p = 1.0", "P = 1.5 kPa", "p = 0.05."])
def test_impossible_quiet_on_valid_p(text):
    assert stats.impossible(text) == []


def test_impossible_still_flags():
    assert stats.impossible("the effect was significant (p = 1.5, n = 4)")
    assert stats.impossible("p = 0.000")


@pytest.mark.parametrize("n", [4, 8, 16])
def test_grim_accepts_half_up_and_half_even(n):
    for k in range(0, 5 * n):
        for mode in (ROUND_HALF_UP, ROUND_HALF_EVEN):
            m = (Decimal(k) / Decimal(n)).quantize(Decimal("0.01"), rounding=mode)
            assert stats.grim(f"The mean number of lesions per mouse was {m} (n = {n}).") == [], (m, n)


def test_grim_positive_and_closest_is_half_up():
    hit = stats.grim("The mean number of lesions per mouse was 2.47 (n = 8).")
    assert hit and hit[0]["closest"] == "2.50"
    assert stats.grim("The mean Likert scale value across items was 2.47 (n = 8).") == []


def test_percent_has_no_catastrophic_backtracking():
    t = time.perf_counter()
    stats.percent("Fragment 1 of 2 " + "ACGT" * 1000)
    assert time.perf_counter() - t < 1.0
    assert stats.percent("12 of 40 mice(35%)")
    assert stats.percent("5 of 10 treated animals (40%)")
    assert stats.percent("3/4 (75 %)") == []


def test_repeated_spread_ignores_settings_and_tolerances():
    assert stats.repeated_spread("HEPES pH 7.40 ± 0.05, Tris pH 8.00 ± 0.05, MES pH 6.20 ± 0.05, "
                                 "acetate pH 5.00 ± 0.05") == []
    assert stats.repeated_spread("Doses were 100 ± 10, 200 ± 10, 300 ± 10, 400 ± 10 mg") == []
    assert stats.repeated_spread("incubated at 37 ± 0.25 °C, 42 ± 0.25 °C, 30 ± 0.25 °C, 25 ± 0.25 °C") == []
    assert stats.repeated_spread("A 102.4 ± 3.24, B 61.7 ± 3.24, C 74.2 ± 3.24, D 99.8 ± 3.24")


# ---- spreadsheets -------------------------------------------------------------------------------------------------

def _table(cols: dict[str, list], name="S1", file="s.csv", formulas=None) -> Table:
    heads = list(cols)
    n = max(len(v) for v in cols.values())
    rows = [heads] + [[str(cols[h][i]) if i < len(cols[h]) else "" for h in heads] for i in range(n)]
    return Table(name, file, rows, formulas=formulas or {})


def test_long_format_and_repeated_standard_curves_are_not_copies():
    rng = np.random.default_rng(1)
    times = [0, 5, 15, 30, 60, 120] * 20
    long = _table({"Subject": [i // 6 + 1 for i in range(120)], "Time (min)": times,
                   "Value": [f"{v:.3f}" for v in rng.normal(10, 2, 120)]})
    for reps in (2, 4):
        curve = [1000, 500, 250, 125, 62.5, 31.25, 15.625] * reps
        plate = _table({"Standard (pg/mL)": curve, "OD": [f"{v:.3f}" for v in rng.uniform(0.05, 2, len(curve))]},
                       name=f"plate{reps}")
        assert "data.duplicate_block" not in _checks(datacheck.check([plate])), reps
    assert _checks(datacheck.check([long])) & INTEGRITY == set()


def test_copy_within_one_column_still_found():
    rng = np.random.default_rng(2)
    v = [f"{x:.3f}" for x in rng.normal(50, 8, 40)]
    v[30:40] = v[2:12]
    dup = [f for f in datacheck.check([_table({"Weight (g)": v})]) if f.check == "data.duplicate_block"]
    assert dup and dup[0].data["length"] == 10


def test_formula_linked_cells_are_not_copies():
    rng = np.random.default_rng(3)
    v = [f"{x:.3f}" for x in rng.normal(20, 3, 30)]
    raw = _table({"Raw": v}, name="Raw", file="s.xlsx")
    linked = _table({"Fig2a": v}, name="Fig2a", file="s.xlsx",
                    formulas={f"A{i + 2}": f"=Raw!A{i + 2}" for i in range(30)})
    assert "data.duplicate_block" not in _checks(datacheck.check([raw, linked]))
    pasted = _table({"Fig2a": v}, name="Fig2a", file="s.xlsx")
    assert "data.duplicate_block" in _checks(datacheck.check([raw, pasted]))


def test_identical_long_columns_are_linear_time():
    rng = np.random.default_rng(4)
    v = [f"{x:.4f}" for x in rng.uniform(0, 100, 8000)]
    cols = datacheck.columns(_table({"a": v, "b": v}))
    t = time.perf_counter()
    found = datacheck._duplicate_blocks(cols)
    assert time.perf_counter() - t < 2.0
    assert len(found) == 1 and found[0].data["length"] == 8000


def test_repeated_values_respect_the_real_grid():
    rng = np.random.default_rng(5)
    half = np.round(rng.normal(120, 15, 200) * 2) / 2
    means5 = rng.integers(0, 11, (200, 5)).mean(axis=1) + 20
    for vals in (half, means5):
        t = _table({"Tumour size (mm)": [f"{x:g}" for x in vals]})
        assert "data.repeated_values" not in _checks(datacheck.check([t]))
    tenth = np.round(rng.normal(50, 20, 200), 1)
    tenth[100:160] = tenth[0:60]
    t = _table({"Glucose (mM)": [f"{x:.1f}" for x in tenth]})
    assert "data.repeated_values" in _checks(datacheck.check([t]))


def test_generator_digits_and_long_ids(tmp_path):
    rng = np.random.default_rng(6)
    p = tmp_path / "weights.csv"
    p.write_text("Weight\n" + "\n".join(repr(float(x)) for x in rng.random(40) * 100))
    assert "data.generator_digits" in _checks(datacheck.check(tables.read_any(p, p.name)))
    ids = [str(1700000000000 + 37 * i) for i in range(40)]
    assert len(set(datacheck._norm(x) for x in ids)) == 40


def test_relative_percentages_and_blank_subtracted_concentrations():
    rng = np.random.default_rng(7)
    viab = list(np.round(rng.normal(90, 6, 30), 1)) + [104.2, 107.9]
    assert "data.impossible" not in _checks(datacheck.check([_table({"% viability": viab})]))
    conc = list(np.round(rng.uniform(5, 200, 30), 2)) + [-0.8, -1.2]
    assert "data.impossible" not in _checks(datacheck.check([_table({"Concentration (pg/mL)": conc})]))
    share = list(np.round(rng.uniform(10, 90, 30), 1)) + [130.0]
    assert "data.impossible" in _checks(datacheck.check([_table({"% of total": share})]))


def test_hidden_empty_columns_are_tidy_but_hidden_data_is_not(tmp_path):
    rng = np.random.default_rng(8)

    def book(hide_data_row: bool) -> Path:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["ID", "Weight (g)", None, "Length (cm)"])
        for i in range(30):
            ws.append([i + 1, float(np.round(rng.normal(25, 2), 2)), None, float(np.round(rng.normal(9, 1), 2))])
        ws.column_dimensions["C"].hidden = True            # an empty spacer column
        ws.column_dimensions.group("F", "Z", hidden=True)  # the unused columns to the right
        if hide_data_row:
            ws.row_dimensions[5].hidden = True
        p = tmp_path / f"h{int(hide_data_row)}.xlsx"
        wb.save(p)
        return p
    t = tables.read_any(book(False), "tidy.xlsx")
    assert t[0].hidden_cols == ["C"] and "data.hidden" not in _checks(datacheck.check(t))
    t = tables.read_any(book(True), "hidden.xlsx")
    hid = [f for f in datacheck.check(t) if f.check == "data.hidden"]
    assert hid and hid[0].data["rows"] == [5] and hid[0].data["cols"] == []


# ---- table readers ------------------------------------------------------------------------------------------------

def test_number_edge_cases():
    assert tables.number("0,125") is None             # a decimal comma, not 125
    assert tables.number("1,234") == 1234
    assert tables.number("1,234.5") == 1234.5
    assert tables.number("﻿1.2") == 1.2
    assert tables.number("−3.2") == -3.2
    assert tables._cell_str(float("nan")) == "" and tables._cell_str(float("inf")) == ""


def test_xlsx_is_streamed_and_bounded(tmp_path, monkeypatch):
    wb = openpyxl.Workbook()
    ws = wb.active
    for i in range(25000):
        ws.append([i, i * 0.5])
    ws["D2"] = "=RAND()"
    p = tmp_path / "big.xlsx"
    wb.save(p)
    t = tables.read_xlsx(p, "big.xlsx")
    assert len(t[0].rows) == tables.MAX_ROWS and t[0].shape[1] == 2 and "D2" in t[0].formulas
    monkeypatch.setattr(tables, "MAX_XML", 1000)
    assert tables.read_xlsx(p, "big.xlsx") == []


def test_delimited_encodings_and_decimal_commas(tmp_path):
    body = "gene\tvalue\nA\t1.5\nB\t2.25\nC\t3.75\n"
    p = tmp_path / "u16.txt"
    p.write_bytes(codecs.BOM_UTF16_LE + body.encode("utf-16-le"))
    assert [tables.number(c) for c in tables.read_any(p, p.name)[0].column(1)[1:]] == [1.5, 2.25, 3.75]
    p = tmp_path / "cp.csv"
    p.write_bytes("name;µg\nwt;1.5\nko;2.5\n".encode("cp1252"))
    assert tables.read_any(p, p.name)[0].rows[0][1] == "µg"
    eu = "probe;p value;mean\n" + "".join(f"P{i};0,{120 + i * 7};{i},5\n" for i in range(12))
    p = tmp_path / "eu.csv"
    p.write_text(eu)
    t = tables.read_any(p, p.name)[0]
    assert t.meta.get("decimal") == "," and tables.number(t.rows[1][1]) == 0.12 and tables.number(t.rows[2][2]) == 1.5
    us = "id,amount\n" + "".join(f"{i},\"1,{200 + i:03d}\"\n" for i in range(12))
    p = tmp_path / "us.csv"
    p.write_text(us)
    assert tables.number(tables.read_any(p, p.name)[0].rows[1][1]) == 1200


def test_jats_spans_are_parsed_defensively():
    xml = ("<article><table-wrap><label>Table 1</label><table>"
           "<tr><th>Group</th><th>Mean</th><th>SD</th></tr>"
           "<tr><td rowspan='2'>Control</td><td>1.1</td><td>0.2</td></tr>"
           "<tr><td>1.3</td><td>0.3</td></tr>"
           "<tr><td colspan='2.0'>x</td><td colspan='inf'>y</td><td colspan='20000000'>z</td></tr>"
           "</table></table-wrap></article>")
    t = tables.read_jats_tables(xml)[0]
    assert t.rows[2] == ["", "1.3", "0.3"]
    assert t.rows[3][:3] == ["x", "", "y"] and len(t.rows[3]) <= tables.MAX_COLS


def test_docx_merged_cells_are_not_duplicated(tmp_path):
    import docx

    d = docx.Document()
    tb = d.add_table(rows=2, cols=3)
    tb.cell(0, 0).merge(tb.cell(0, 1)).text = "Merged"
    tb.cell(1, 0).text, tb.cell(1, 1).text, tb.cell(1, 2).text = "1.5", "2.5", "3.5"
    p = tmp_path / "t.docx"
    d.save(p)
    rows = tables.read_docx(p, p.name)[0][0].rows
    assert rows[0][:2] == ["Merged", ""] and rows[1] == ["1.5", "2.5", "3.5"]


# ---- reporting ----------------------------------------------------------------------------------------------------

def test_us_style_dates():
    tl, info = reporting.timeline(_paper("Received 5/1/2021; Accepted 6/1/2021."), {})
    assert tl == [] and info.get("date_format_ambiguous")
    tl, info = reporting.timeline(_paper("Received 12/20/2021; Accepted 12/24/2021."), {})
    assert info["days_to_accept"] == 4 and tl[0].check == "record.fast_acceptance"
    meta = {"dates": {"received": "2021-05-01", "accepted": "2021-06-01"}}
    tl, info = reporting.timeline(_paper("Received 5/1/2021; Accepted 6/1/2021."), meta)
    assert tl == [] and info["days_to_accept"] == 31
    tl, info = reporting.timeline(_paper("Received: 3 March 2021; Accepted: 6 March 2021."), {})
    assert info["days_to_accept"] == 3 and tl[0].check == "record.fast_acceptance"


def test_ethics_not_silenced_by_licence_footer():
    animal = "Methods. Twelve mice were used and the mice were weighed weekly. " * 60
    footer = " This article is licensed under a Creative Commons Attribution 4.0 International License."
    assert "report.no_ethics" in _checks(reporting.statements(_paper(animal + footer)))
    ok = animal + " All procedures were approved by the institutional animal ethics committee." + footer
    assert "report.no_ethics" not in _checks(reporting.statements(_paper(ok)))
    model = "We simulate diffusion on graphs. " * 120 + "Such models may one day help patients."
    assert "report.no_ethics" not in _checks(reporting.statements(_paper(model)))


def test_figure_count_ignores_supplementary_figures():
    p = _paper("See Figure 2, Fig. 4 and Supplementary Figure 9, and Extended Data Fig. 8.")
    assert reporting.consistency(p, 4, 0) == []
    assert _checks(reporting.consistency(_paper("As shown in Fig. 8."), 7, 0)) == {"report.figure_count"}


def test_record_notice_types():
    def one(t):
        return reporting.record({"notices": [{"type": t, "date": "2024-01-02"}]})
    assert one("withdrawal")[0].check == "record.retracted" and one("withdrawal")[0].severity == "critical"
    assert one("partial_retraction")[0].severity == "high"
    assert one("Expression-of-Concern")[0].check == "record.concern"
    assert one("clarification")[0].check == "record.correction"
    assert reporting.record({"notices": [{"type": "new_version"}]}) == []


# ---- references and text -------------------------------------------------------------------------------------------

def test_reference_list_keeps_longest_run():
    refs_text = "\n".join(f"{i}. Author {i}. A study of topic {i}. J Test. 2020;{i}:1-10." for i in range(1, 41))
    legends = "\n".join(f"{i}. Step {i} of the protocol was done at room temperature." for i in range(1, 7))
    _, entries = refs.split("Body text.\nReferences\n" + refs_text + "\nSupplementary Methods\n" + legends)
    assert len(entries) == 40


def test_superscript_paper_is_not_called_padded():
    body = ("Previous work12,13 showed this5. Values lay in [0, 1] and equation [2] holds; see step [5]. " * 3
            + "\nReferences\n")
    entries = "\n".join(f"{i}. Author {i}. A study of topic {i} in mice. J Test. 2020;{i}:1-10." for i in range(1, 41))
    assert "refs.uncited" not in _checks(refs.check(_paper(body + entries)))


def test_white_label_on_image_is_not_hidden(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200), 0)
    pix.clear_with(0)
    page.insert_image(pymupdf.Rect(72, 72, 400, 400), pixmap=pix)
    for k in range(6):
        page.insert_text((90, 100 + 40 * k), "50 µm scale bar", fontsize=9, color=(1, 1, 1))
    page.insert_text((72, 450), "A normal sentence about cells.", fontsize=11)
    path = tmp_path / "label.pdf"
    doc.save(path)
    assert textcheck.hidden_text(path) == []


def test_response_to_reviewers_is_not_chatbot_text():
    letter = ("Response to reviewers. Reviewer #1 asked for more detail. Here is the revised figure with error bars. "
              "Reviewer #2: we hope this helps. Here is the revised version of the methods.")
    assert textcheck.check(_paper(letter)) == []
    assert _checks(textcheck.check(_paper("As an AI language model, I cannot verify this."))) == {"text.ai_boilerplate"}


# ---- scoring -------------------------------------------------------------------------------------------------------

def test_integrity_set_matches_emitted_checks():
    src = "\n".join(p.read_text() for p in ENGINE.glob("*.py"))
    emitted = set(re.findall(r"Finding\(\s*\"([a-z]+\.[a-z_]+)\"", src))
    assert INTEGRITY <= emitted, INTEGRITY - emitted
    assert models.kind_of("refs.bad_doi") == "quality"


def test_repeats_of_one_check_are_capped():
    one = [Finding("data.repeated_values", "medium", "t", "d", 0.55)]
    many = one * 20
    assert risk_score(many) <= 2 * risk_score(one) + 1
    high = [Finding("data.repeated_values", "high", "t", "d", 0.55)]
    # repeats are discounted across severities too: at most twice the strongest hit
    assert risk_score(high + one * 10) <= round(100 * (1 - np.exp(-2 * 22 * 0.55 / 45)))
