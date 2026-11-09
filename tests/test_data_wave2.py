"""Second-wave data checks: offset copies, cross-file tables, workbook internals, summary recomputation, precision
and digit patterns, and the supplementary-file timeline.  Each check has a planted positive and honest negatives."""
from __future__ import annotations

import zipfile
from datetime import date, datetime

import docx
import numpy as np
import openpyxl
import pymupdf

from paperinspector import datacheck, tables
from paperinspector.tables import Table

NEW = {"data.offset_copy", "data.typed_over_formula", "data.calc_order", "data.external_links",
       "data.summary_mismatch", "data.precision_mix", "data.trailing_repeats", "data.benford", "data.file_dates",
       "data.file_metadata"}


def _table(cols: dict[str, list], name="S1", file="s.csv", formulas=None, meta=None) -> Table:
    heads = list(cols)
    n = max(len(v) for v in cols.values())
    rows = [heads] + [[str(cols[h][i]) if i < len(cols[h]) else "" for h in heads] for i in range(n)]
    return Table(name, file, rows, formulas=formulas or {}, meta=meta or {})


def _ids(found) -> set[str]:
    return {f.check for f in found}


def _cols(*ts):
    return [c for t in ts for c in datacheck.columns(t)]


# ---- constant offset / constant ratio copies ----------------------------------------------------------------------

def _rand(rng, n, mu=20.0, sd=4.0, dec=3):
    return [f"{x:.{dec}f}" for x in rng.normal(mu, sd, n)]


def test_offset_copy_within_a_table_is_found():
    rng = np.random.default_rng(10)
    a = _rand(rng, 40)
    b = _rand(rng, 40, 30)
    for k in range(12):                          # rows 2-13 of A, copied 8 rows lower into B, plus 0.5
        b[10 + k] = f"{float(a[2 + k]) + 0.5:.3f}"
    t = _table({"Control": a, "Treated": b})
    found = datacheck._offset_copies(_cols(t))
    assert len(found) == 1
    f = found[0]
    assert f.check == "data.offset_copy" and f.data["relation"] == "add" and abs(f.data["constant"] - 0.5) < 1e-9
    assert f.data["length"] == 12 and f.severity in ("medium", "high")
    assert "data.offset_copy" in _ids(datacheck.check([t]))


def test_ratio_copy_across_files_is_found():
    rng = np.random.default_rng(11)
    a = _rand(rng, 30, 50, 10)
    b = _rand(rng, 30, 55, 10, dec=4)
    for k in range(10):
        b[15 + k] = f"{float(a[3 + k]) * 1.1:.4f}"
    found = datacheck._offset_copies(_cols(_table({"Day 3": a}, file="a.csv"), _table({"Day 7": b}, file="b.csv")))
    assert len(found) == 1 and found[0].data["relation"] == "mul" and found[0].data["cross_file"]
    assert abs(found[0].data["constant"] - 1.1) < 1e-6


def test_offset_copy_inside_one_column_is_found():
    rng = np.random.default_rng(12)
    v = _rand(rng, 60)
    for k in range(10):
        v[40 + k] = f"{float(v[5 + k]) + 2.25:.3f}"
    found = datacheck._offset_copies(_cols(_table({"Weight (g)": v})))
    assert found and found[0].data["length"] == 10


def test_honest_and_derived_columns_are_not_offset_copies():
    rng = np.random.default_rng(13)
    a = _rand(rng, 40)
    honest = _table({"A": a, "B": _rand(rng, 40), "C": _rand(rng, 40)})
    assert datacheck._offset_copies(_cols(honest)) == []
    # a whole column computed from another: derived_column's business, not a copied run
    whole = _table({"OD": a, "OD2": [f"{float(x) + 0.5:.3f}" for x in a]})
    assert datacheck._offset_copies(_cols(whole)) == []
    assert "data.derived_column" in _ids(datacheck.check([whole]))
    # a heading that says the column is computed
    part = _rand(rng, 40)
    for k in range(12):
        part[10 + k] = f"{float(a[2 + k]) + 0.5:.3f}"
    assert datacheck._offset_copies(_cols(_table({"Raw": a, "Background-corrected": part}))) == []
    # blank subtraction per plate: a different constant for each block of rows
    plates = [f"{float(x) - (0.045 if i < 20 else 0.061):.3f}" for i, x in enumerate(a)]
    assert datacheck._offset_copies(_cols(_table({"Raw": a, "Net": plates}))) == []
    # an exact copy is the block check's job
    exact = _rand(rng, 40)
    exact[10:22] = a[2:14]
    t = _table({"A": a, "B": exact})
    assert datacheck._offset_copies(_cols(t)) == []
    assert "data.duplicate_block" in _ids(datacheck.check([t]))


def test_long_format_designs_and_sorted_columns_are_not_offset_copies():
    rng = np.random.default_rng(14)
    # per-subject time offsets in long format: a repeating design, not a copy
    pattern = [0.0, 0.75, 2.5, 1.25, 6.0, 3.5, 12.0, 8.25]
    times = [f"{p + 100.0 * s:.2f}" for s in range(10) for p in pattern]
    assert datacheck._offset_copies(_cols(_table({"Clock (h)": times}))) == []
    # sorted genomic windows: end = start + width for a stretch of rows
    start = np.sort(rng.integers(10_000, 9_000_000, 60))
    end = start + rng.integers(200, 900, 60)
    end[10:30] = start[10:30] + 499
    t = _table({"chrom start": start.tolist(), "chrom end": end.tolist()})
    assert datacheck._offset_copies(_cols(t)) == []
    t2 = _table({"a": start.tolist(), "b": end.tolist()})            # sorted, even without telling headings
    assert datacheck._offset_copies(_cols(t2)) == []


# ---- copied columns across files ----------------------------------------------------------------------------------

def test_same_table_in_two_files_is_one_moderate_finding():
    rng = np.random.default_rng(20)
    cols = {"Control": _rand(rng, 25), "Drug A": _rand(rng, 25), "Drug B": _rand(rng, 25)}
    t1 = _table(cols, name="Table S3", file="Supplementary Table 3.xlsx")
    t2 = _table(cols, name="Fig 2b", file="Source Data Fig 2.xlsx")
    dup = [f for f in datacheck.check([t1, t2]) if f.check == "data.duplicate_block"]
    assert len(dup) == 1
    assert dup[0].severity == "medium" and dup[0].data["same_table"] and dup[0].confidence <= 0.4


def test_relabelled_copy_across_files_stays_high():
    rng = np.random.default_rng(21)
    v = _rand(rng, 25)
    t1 = _table({"Control": v, "Other": _rand(rng, 25)}, file="a.xlsx")
    t2 = _table({"Treated": v, "More": _rand(rng, 25)}, file="b.xlsx")
    dup = [f for f in datacheck.check([t1, t2]) if f.check == "data.duplicate_block"]
    assert len(dup) == 1 and dup[0].severity == "high" and dup[0].data["cross_file"]
    assert "'Control'" in dup[0].detail and "'Treated'" in dup[0].detail


# ---- workbook internals -------------------------------------------------------------------------------------------

def _formula_table(n=30, typed=(), summary_row=None):
    rows = [["A", "B", "Ratio"]]
    formulas = {}
    rng = np.random.default_rng(30)
    for i in range(1, n + 1):
        a, b = rng.uniform(1, 10), rng.uniform(1, 10)
        r = i + 1
        if i in typed:
            rows.append([f"{a:.3f}", f"{b:.3f}", "1.234"])
        elif i == summary_row:
            rows.append(["Mean", "", "4.5"])
        else:
            rows.append([f"{a:.3f}", f"{b:.3f}", ""])          # a formula without a cached value reads as blank
            formulas[f"C{r}"] = f"=A{r}/B{r}"
    return Table("Sheet1", "s.xlsx", rows, formulas=formulas)


def test_typed_number_inside_a_formula_column():
    found = datacheck._typed_over_formulas([_formula_table(typed=(14,))])
    assert len(found) == 1 and found[0].data["cells"] == ["C15"]
    assert found[0].check == "data.typed_over_formula"


def test_clean_formula_columns_and_summary_rows_are_quiet():
    assert datacheck._typed_over_formulas([_formula_table()]) == []
    assert datacheck._typed_over_formulas([_formula_table(summary_row=15)]) == []
    many = _formula_table(typed=tuple(range(3, 28, 3)))           # too many typed cells: not a formula column
    assert datacheck._typed_over_formulas([many]) == []


def _chain_table(seq, formula="=A{r}*2", extra_formulas=None):
    formulas = {f"C{r}": formula.format(r=r) for r in seq}
    formulas.update(extra_formulas or {})
    rows = [["x", "", "y"]] + [["1.5", "", ""] for _ in range(max(seq))]
    return Table("Sheet1", "s.xlsx", rows, formulas=formulas, meta={"calc_chain": [f"C{r}" for r in seq]})


def test_calc_chain_shows_a_moved_row():
    seq = list(range(2, 41))
    seq.remove(30)
    seq.insert(4, 30)                            # row 30's formula sits where row 6 was made
    found = datacheck._calc_order([_chain_table(seq)])
    assert len(found) == 1 and found[0].data["rows"] == [30]


def test_calc_chain_in_order_scrambled_or_dependent_is_quiet():
    assert datacheck._calc_order([_chain_table(list(range(2, 41)))]) == []
    assert datacheck._calc_order([_chain_table(list(range(40, 1, -1)))]) == []
    rng = np.random.default_rng(31)
    assert datacheck._calc_order([_chain_table([int(x) for x in rng.permutation(range(2, 41))])]) == []
    seq = list(range(2, 41))
    seq.remove(30)
    seq.insert(4, 30)
    dep = _chain_table(seq, formula="=D{r}*2", extra_formulas={f"D{r}": f"=A{r}+1" for r in range(2, 41)})
    assert datacheck._calc_order([dep]) == []


def _workbook_with_internals(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["x", "y"])
    for i in range(30):
        ws.append([i * 1.5, f"=A{i + 2}*2"])
    wb.properties.creator = "Example Author"
    wb.save(path)
    chain = "".join(f'<c r="B{r}"{" i=\"1\"" if r == 2 else ""}/>' for r in range(2, 32))
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("xl/calcChain.xml", '<?xml version="1.0" encoding="UTF-8"?><calcChain '
                   f'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{chain}</calcChain>')
        z.writestr("xl/externalLinks/_rels/externalLink1.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships/externalLinkPath" Target="file:///C:/data/raw%20plate%202.xlsx" '
                   'TargetMode="External"/></Relationships>')


def test_reader_keeps_calc_chain_links_and_program(tmp_path):
    p = tmp_path / "wb.xlsx"
    _workbook_with_internals(p)
    ts = tables.read_xlsx(p, "wb.xlsx")
    assert ts and ts[0].meta["calc_chain"][:3] == ["B2", "B3", "B4"]
    assert ts[0].meta["external_links"] == ["raw plate 2.xlsx"]       # file name only, never the folder
    assert ts[0].meta.get("app")
    found = datacheck.check(ts)
    links = [f for f in found if f.check == "data.external_links"]
    assert len(links) == 1 and "raw plate 2.xlsx" in links[0].detail and "C:/" not in links[0].detail
    assert "data.calc_order" not in _ids(found)
    assert all("Example Author" not in f.detail for f in found)


def test_reader_without_internals_is_unchanged(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.append([1.5, 2.5])
    p = tmp_path / "plain.xlsx"
    wb.save(p)
    t = tables.read_xlsx(p, "plain.xlsx")[0]
    assert "calc_chain" not in t.meta and "external_links" not in t.meta


# ---- summaries recomputed from replicates -------------------------------------------------------------------------

def _summary_table(bad_mean=(), bad_sd=(), n=20, formulas=False):
    rng = np.random.default_rng(40)
    reps = np.round(rng.normal(10, 2, (n, 3)), 2)
    rows = [["Gene", "Rep 1", "Rep 2", "Rep 3", "Mean", "SD"]]
    f = {}
    for i, r in enumerate(reps):
        m, sd = r.mean(), r.std(ddof=1)
        if i in bad_mean:
            m += 0.8
        if i in bad_sd:
            sd += 0.5
        rows.append([f"G{i}"] + [f"{x:.2f}" for x in r] + [f"{m:.3f}", f"{sd:.3f}"])
        if formulas:
            f[f"E{i + 2}"] = f"=AVERAGE(B{i + 2}:D{i + 2})"
    return Table("S2", "s.xlsx", rows, formulas=f)


def test_mean_and_sd_that_do_not_follow_from_replicates():
    found = datacheck._summary_mismatch([_summary_table(bad_mean=(4, 11), bad_sd=(7,))])
    by = {f.data["stat"]: f for f in found}
    assert by["mean"].data["rows"] == [6, 13]
    assert by["SD"].data["rows"] == [9]
    assert all(f.check == "data.summary_mismatch" for f in found)


def test_correct_summaries_and_formula_means_are_quiet():
    assert datacheck._summary_mismatch([_summary_table()]) == []
    assert [f for f in datacheck._summary_mismatch([_summary_table(bad_mean=(4,), formulas=True)])
            if f.data["stat"] == "mean"] == []
    # a mean that matches no set of neighbouring columns is not checked at all
    rng = np.random.default_rng(41)
    t = _table({"A": _rand(rng, 20), "B": _rand(rng, 20), "Mean": _rand(rng, 20)})
    assert datacheck._summary_mismatch([t]) == []


# ---- precision and digit patterns ---------------------------------------------------------------------------------

def test_typed_block_among_full_precision_values():
    rng = np.random.default_rng(50)
    v = [repr(float(x)) for x in rng.normal(5, 1, 60)]
    for k in range(20, 28):
        v[k] = f"{rng.normal(5, 1):.2f}"
    found = datacheck._precision_mix(_cols(_table({"Signal": v}, file="s.xlsx")))
    assert len(found) == 1 and found[0].data["rows"][1] == 29 and found[0].data["rows"][0] in (22, 23)


def test_full_precision_and_scattered_short_values_are_quiet():
    rng = np.random.default_rng(51)
    v = [repr(float(x)) for x in rng.normal(5, 1, 60)]
    assert datacheck._precision_mix(_cols(_table({"Signal": v}))) == []
    for k in rng.choice(60, 8, replace=False):
        v[int(k)] = f"{rng.normal(5, 1):.2f}"
    assert datacheck._precision_mix(_cols(_table({"Signal": v}))) == []
    short = [f"{x:.2f}" for x in rng.normal(5, 1, 60)]
    assert datacheck._precision_mix(_cols(_table({"Signal": short}))) == []


def _four_dec(rng, n, lo=10, hi=60):
    out = []
    while len(out) < n:
        s = f"{rng.uniform(lo, hi):.4f}"
        if not s.endswith("0"):
            out.append(s)
    return out


def test_values_sharing_trailing_digits():
    rng = np.random.default_rng(60)
    v = _four_dec(rng, 80)
    for k in range(25):                          # copy the decimals, change the leading digits
        src = v[k]
        whole = int(float(src)) + int(rng.integers(1, 9))
        v.insert(int(rng.integers(0, len(v))), f"{whole}.{src.split('.')[1]}")
    found = datacheck._trailing_repeats(_cols(_table({"Area": v})))
    assert len(found) == 1 and found[0].data["observed_pairs"] >= 20


def test_honest_and_arithmetic_trailing_digits_are_quiet():
    rng = np.random.default_rng(61)
    assert datacheck._trailing_repeats(_cols(_table({"Area": _four_dec(rng, 300)}))) == []
    pounds = rng.integers(100, 250, 200)
    kg = [f"{x * 0.4536:.4f}" for x in pounds]                     # unit conversion of whole numbers
    assert datacheck._trailing_repeats(_cols(_table({"Weight (kg)": kg}))) == []
    net = [f"{x - 0.4837:.4f}" for x in rng.integers(20, 900, 200)]  # whole counts minus a blank
    assert datacheck._trailing_repeats(_cols(_table({"Net": net}))) == []
    thirds = [f"{x / 3:.4f}" for x in rng.integers(30, 3000, 200)]
    assert datacheck._trailing_repeats(_cols(_table({"Mean of 3": thirds}))) == []


def test_benford_flags_flat_first_digits_over_several_magnitudes():
    rng = np.random.default_rng(70)
    d = rng.integers(1, 10, 1000)
    v = (d + rng.uniform(0, 0.999, 1000)) * 10.0 ** rng.integers(0, 4, 1000)
    found = datacheck._benford(_cols(_table({"Intensity": [f"{x:.4g}" for x in v]})))
    assert len(found) == 1 and found[0].check == "data.benford" and found[0].severity == "low"
    assert "not as evidence" in found[0].detail


def test_benford_quiet_where_it_does_not_apply():
    rng = np.random.default_rng(71)
    logn = rng.lognormal(3, 2.0, 2000)
    assert datacheck._benford(_cols(_table({"Counts": [f"{x:.5g}" for x in logn]}))) == []
    narrow = rng.normal(50, 5, 2000)
    assert datacheck._benford(_cols(_table({"Weight": [f"{x:.3f}" for x in narrow]}))) == []
    d = rng.integers(1, 10, 1000)
    flat = (d + rng.uniform(0, 0.999, 1000)) * 10.0 ** -rng.integers(1, 5, 1000)
    assert datacheck._benford(_cols(_table({"p value": [f"{x:.4g}" for x in flat]}))) == []
    small = (d[:200] + 0.5) * 10.0 ** rng.integers(0, 4, 200)
    assert datacheck._benford(_cols(_table({"Intensity": [f"{x:.4g}" for x in small]}))) == []


# ---- supplementary file timeline ----------------------------------------------------------------------------------

def _set_modified(path, when: datetime):
    """openpyxl stamps 'modified' with the time of saving; put the date a real file would carry."""
    import re

    with zipfile.ZipFile(path) as z:
        parts = {i.filename: z.read(i) for i in z.infolist()}
    core = parts["docProps/core.xml"].decode()
    core = re.sub(r"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                  lambda m: m.group(1) + when.strftime("%Y-%m-%dT%H:%M:%SZ") + m.group(2), core)
    parts["docProps/core.xml"] = core.encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)


def _dated_files(tmp_path, created: datetime):
    x = tmp_path / "Data S1.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append([1.5, 2.5])
    wb.properties.created = created
    wb.properties.creator = "Example Author"
    wb.save(x)
    _set_modified(x, created)
    w = tmp_path / "Methods S1.docx"
    d = docx.Document()
    d.add_paragraph("Supplementary methods.")
    d.core_properties.created = created
    d.core_properties.modified = created
    d.core_properties.author = "Example Author"
    d.save(w)
    p = tmp_path / "Figures S1.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Supplementary figures")
    doc.set_metadata({"creator": "Word", "creationDate": created.strftime("D:%Y%m%d%H%M%S"), "author": "Example Author"})
    doc.save(p)
    doc.close()
    return [x, w, p]


def test_file_meta_reads_office_and_pdf(tmp_path):
    for f in _dated_files(tmp_path, datetime(2023, 6, 1, 9, 0, 0)):
        m = tables.file_meta(f)
        assert m["created"].startswith("2023-06-01"), (f.name, m)
        assert "Example Author" not in str(m)
    assert tables.file_meta(tmp_path / "missing.xlsx") == {}
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    assert tables.file_meta(tmp_path / "broken.docx") == {}


def test_files_made_after_publication_are_flagged(tmp_path):
    files = _dated_files(tmp_path, datetime(2023, 6, 1))
    found = datacheck.check([], dates={"received": "2020-11-02", "accepted": "2021-02-01", "published": "2021-03"},
                            files=[(f, f.name) for f in files])
    late = [f for f in found if f.check == "data.file_dates"]
    assert {f.locations[0].file for f in late} == {"Data S1.xlsx", "Methods S1.docx", "Figures S1.pdf"}
    summary = [f for f in found if f.check == "data.file_metadata"]
    assert len(summary) == 1 and summary[0].severity == "info"
    assert all("Example Author" not in f.detail and "Example Author" not in str(f.data) for f in found)


def test_file_saved_long_after_publication_is_flagged(tmp_path):
    x = _dated_files(tmp_path, datetime(2021, 1, 10))[0]
    _set_modified(x, datetime(2022, 5, 1))
    found = datacheck.check([], dates={"published": "2021-03-04"}, files=[x])
    late = [f for f in found if f.check == "data.file_dates"]
    assert len(late) == 1 and "last saved" in late[0].title


def test_files_made_during_review_are_only_listed(tmp_path):
    files = _dated_files(tmp_path, datetime(2021, 1, 10))
    found = datacheck.check([], dates={"received": "2020-11-02", "accepted": "2021-02-01",
                                       "published": "4 March 2021"}, files=files)
    assert "data.file_dates" not in _ids(found)
    summary = [f for f in found if f.check == "data.file_metadata"]
    assert summary and "after submission" in summary[0].detail


def test_no_paper_dates_means_no_timeline(tmp_path):
    files = _dated_files(tmp_path, datetime(2023, 6, 1))
    assert _ids(datacheck.check([], files=files)) & NEW == set()
    assert _ids(datacheck.check([], dates={}, files=files)) & NEW == set()


def test_date_parsing():
    assert datacheck._day("2021-03-04T10:00:00Z") == date(2021, 3, 4)
    assert datacheck._day("2021-03", latest=True) == date(2021, 3, 31)
    assert datacheck._day("2021-12", latest=True) == date(2021, 12, 31)
    assert datacheck._day("2021") == date(2021, 1, 1)
    assert datacheck._day("4 March 2021") == date(2021, 3, 4)
    assert datacheck._day("March 4, 2021") == date(2021, 3, 4)
    assert datacheck._day("20210304") == date(2021, 3, 4)
    assert datacheck._day("not a date") is None and datacheck._day(None) is None


# ---- whole-workbook quiet test ------------------------------------------------------------------------------------

def test_realistic_honest_workbook_raises_no_new_flags(tmp_path):
    rng = np.random.default_rng(90)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fig 3a"
    ws.append(["Sample", "Rep 1", "Rep 2", "Rep 3", "Mean", "SD", "Normalized", "log2 FC", "Blank-corrected", "Plate"])
    for i in range(48):
        r = np.round(rng.lognormal(2, 0.4, 3), 3)
        blank = 0.052 if i < 24 else 0.047
        ws.append([f"S{i + 1}", *map(float, r), float(np.round(r.mean(), 4)), float(np.round(r.std(ddof=1), 4)),
                   float(np.round(r.mean() / 7.4, 6)), float(np.round(np.log2(r.mean() / 7.4), 6)),
                   float(np.round(r[0] - blank, 3)), 1 if i < 24 else 2])
    raw = wb.create_sheet("Raw reads")
    raw.append(["Gene", "Count"])
    for i, c in enumerate(rng.negative_binomial(2, 0.002, 600)):
        raw.append([f"g{i}", int(c)])
    p = tmp_path / "Source Data Fig 3.xlsx"
    wb.save(p)
    found = datacheck.check(tables.read_any(p, p.name))
    assert _ids(found) & (NEW - {"data.file_metadata"}) == set(), [(f.check, f.detail) for f in found]


# ---- computed tables and near-constant ratios (false alarms found on a real control paper) ------------------------

def _peptide_table(rng, n=3000):
    """An in-silico digest like a real mass-spectrometry supplement: one row per peptide window, isotope intensities
    and m/z for charges 1-4 and isotope peaks 1-3, all computed from the peptide's mass and stored at Excel's 15
    significant digits.  Charge-4 m/z is about 3/4 of charge-3 m/z (not exactly), windows that share a prefix differ
    by a fixed mass, similar windows sit together, and isomers (same composition) share a mass."""
    mass = np.round(rng.uniform(700, 4000, n), 8)
    mass[2000:2300] = np.round(2930 + rng.uniform(-3, 3, 300), 8)   # similar windows: the ratio barely drifts
    mass[1200:1212] = np.round(mass[400:412] + 1296.64962126, 8)    # the same windows with a longer prefix
    iso = rng.choice(n, 300, replace=False)
    mass[iso] = mass[rng.choice(n, 300)]                             # isomers, scattered through the table
    g = lambda x: format(float(x), ".15g")
    cols = {"peptide": [f"PEP{i}" for i in range(n)],
            "relative intensity (isotope peak 2)": [g(round(0.00055 * m - 0.02, 6)) for m in mass],
            "relative intensity (isotope peak 3)": [g(round(1.6e-7 * m * m, 6)) for m in mass]}
    for z in (1, 2, 3, 4):
        for i in (1, 2, 3):
            cols[f"m/z (charge = {z}, isotope peak {i})"] = [g((m + z * 1.00727646688 + (i - 1) * 1.00335483) / z)
                                                            for m in mass]
    return _table(cols, name="gag.fasta.out", file="s012.xlsx")


DATA_INTEGRITY = {"data.offset_copy", "data.terminal_digits", "data.repeated_values", "data.duplicate_block"}


def test_computed_mass_table_raises_no_data_integrity_flags():
    found = _ids(datacheck.check([_peptide_table(np.random.default_rng(21))]))
    assert not found & DATA_INTEGRITY, found


def test_a_ratio_that_drifts_is_not_a_constant():
    for seed in range(8):
        rng = np.random.default_rng(100 + seed)
        a = rng.normal(20.0, 4.0, 60)
        drift = 0.75 * (1 + 2e-8 * (a - 20.0))              # a smooth physical relation, not a copy: the ratio
        b = rng.normal(15.0, 3.0, 60)                       # moves by ~1e-7, far more than 6-decimal rounding
        b[20:40] = a[5:25] * drift[5:25]
        t = _table({"A": [f"{x:.6f}" for x in a], "B": [f"{x:.6f}" for x in b]})
        assert datacheck._offset_copies(_cols(t)) == [], seed


# Formula output pasted as values carries 15 significant digits; copies made there must stay visible.
_full = lambda xs: [repr(float(x)) for x in xs]


def test_copies_in_full_precision_columns_are_still_found():
    rng = np.random.default_rng(31)
    a, b = rng.normal(20, 4, 40), rng.normal(26, 4, 40)
    b[10:22] = a[2:14]                                     # block copied between two NORMINV(RAND())-style columns
    assert "data.duplicate_block" in _ids(datacheck.check([_table({"Control": _full(a), "Treated": _full(b)})]))
    c = rng.normal(26, 4, 40)
    a3 = np.array([float(f"{x:.3f}") for x in a])
    c[10:22] = a3[2:14] * 1.0837                            # copied from typed data, scaled by a formula
    t = _table({"Control": [f"{x:.3f}" for x in a3], "Treated": _full(c)})
    assert "data.offset_copy" in _ids(datacheck.check([t]))


def test_raw_columns_next_to_many_derived_columns_are_still_checked():
    rng = np.random.default_rng(32)
    sig, bg = rng.normal(800, 120, 48), rng.normal(120, 20, 48)
    s1 = np.round(sig, 1)
    s1[30:42] = s1[2:14] + 25                               # a raw block copied and nudged
    cols = {"Signal": [f"{x:.1f}" for x in s1], "Background": [f"{x:.1f}" for x in bg]}
    for j in range(6):
        cols[f"derived {j}"] = _full(s1 / (bg + j) * (1 + rng.normal(0, 0.01, 48)))
    assert "data.offset_copy" in _ids(datacheck.check([_table(cols)]))


def test_exact_repeats_of_long_values_are_still_found():
    rng = np.random.default_rng(33)
    c = rng.normal(50, 10, 200)
    for i in range(0, 40, 2):
        c[100 + i] = c[i]                                   # single cells copied over others
    assert "data.repeated_values" in _ids(datacheck.check([_table({"Value": _full(c)})]))
