"""Forensics for numbers in tables and supplementary data.

Integrity signals (things fabricated or copied data tends to leave behind):
  duplicate_block   the same run of measurements appears twice, up to a whole column, possibly in another file
  random_formula    values produced by RAND()/RANDBETWEEN() formulas
  terminal_digits   last digits far from uniform in precise measurements
  repeated_values   precise values repeating far more than chance allows
  too_similar       one SD/SE shared by every group
  mixed_fonts       one column typed in two fonts, in separate blocks of rows
Quality notes (honest problems):
  gene_dates        gene symbols turned into dates by Excel (SEPT2 -> 2-Sep)
  impossible        percentages above 100, p-values above 1, negative counts
  hidden            hidden sheets, rows or columns holding data
  derived_column    a column that is an exact transform of another, presented as data
  sort_anomaly      an ID column sorted except for a few rows (rows moved by hand?)
  digit_preference  values rounded to 0 or 5 far more than measurement would give
  generator_digits  values carrying machine precision (pasted from a generator or a calculation)
Added in the second wave (all quality notes unless the integrator lists them in models.INTEGRITY):
  offset_copy       a run of values equal to another run plus a constant, or times a constant (copied and nudged)
  typed_over_formula  typed numbers inside a column that is otherwise one filled-down formula
  calc_order        Excel's calculation chain shows a few formula rows out of the order they were made in
  external_links    the workbook pulls values from other files that were not supplied
  summary_mismatch  a mean or SD column that does not follow from the replicate values beside it
  precision_mix     a block of short, typed-looking values inside a column of full-precision values
  trailing_repeats  values sharing all their decimal digits while the digits before the point differ
  benford           first digits far from Benford's law in data spanning several orders of magnitude
  file_dates        a supplementary file created or saved well after the paper was published
  file_metadata     (info) the program and dates each supplementary file records, set against the paper's dates
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
from scipy import stats as st

from .models import Finding, Location
from .tables import Table, cell_ref, col_name, file_meta, number

WINDOW = 6


@dataclass
class Col:
    t: Table
    j: int
    header: str
    idx: list[int]           # row index of each value
    raw: list[str]           # cell text as stored
    vals: np.ndarray

    @property
    def where(self) -> str:
        return f"{col_name(self.j)}{self.idx[0] + 1}:{col_name(self.j)}{self.idx[-1] + 1}"

    def loc(self, a: int = 0, b: int | None = None) -> Location:
        b = len(self.idx) if b is None else b
        cells = f"{cell_ref(self.idx[a], self.j)}:{cell_ref(self.idx[b - 1], self.j)}"
        return Location(file=self.t.file, sheet=self.t.name, cells=cells)

    @property
    def label(self) -> str:
        h = f" '{self.header}'" if self.header else ""
        f = f"{self.t.file}, " if self.t.file else ""
        return f"{f}{self.t.name}, column {col_name(self.j)}{h}"


def _norm(s: str) -> str:
    """Canonical text of a number as reported: float noise (0.30000000000000004) removed, but every digit a
    person or a generator could have written kept, so long IDs stay distinct and machine precision stays visible."""
    v = number(s)
    if v is None:
        return ""
    return format(v, ".15g")


def columns(t: Table, min_values: int = 5) -> list[Col]:
    nrows, ncols = t.shape
    out = []
    for j in range(ncols):
        cells = t.column(j)
        idx, raw, vals = [], [], []
        header = ""
        for i, c in enumerate(cells):
            v = number(c)
            if v is None:
                if c and not idx and len(c) < 80:
                    header = c.strip()
                continue
            idx.append(i)
            raw.append(_norm(c))
            vals.append(v)
        if len(vals) >= min_values:
            out.append(Col(t, j, header, idx, raw, np.array(vals, float)))
    return out


def _sig_digits(s: str) -> int:
    s = s.lstrip("-").replace(".", "").lstrip("0")
    s = s.split("e")[0].split("E")[0]
    return len(s)


COMPUTED_SIG = 9          # significant digits that a person or an instrument does not record


def _machine(r: str) -> bool:
    return "e" not in r.lower() and "." in r and len(r.split(".")[1]) >= 4 and _sig_digits(r) >= COMPUTED_SIG


def _calculated(c: Col) -> bool:
    """Most values carry 9+ significant digits: a calculation's output, whose last digits follow its arithmetic.
    Only the last-digit test is skipped for these: copies and exact repeats of long values stay checked, since
    formula output pasted as values is where fabricated copies live."""
    return len(c.raw) > 0 and sum(map(_machine, c.raw)) >= 0.8 * len(c.raw)


def _trivial(win: list[str], common: set[str] = frozenset()) -> bool:
    vals = [float(x) for x in win]
    if len(set(win)) < 4:
        return True
    d = np.diff(vals)
    if np.allclose(d, d[0]):
        return True                      # 1,2,3,4 ids, doses, time points
    if all(v != 0 for v in vals):
        r = np.array(vals[1:]) / np.array(vals[:-1])
        if np.allclose(r, r[0]):
            return True                  # dilution series 1000, 500, 250...
    if all(abs(v) < 10 and float(v).is_integer() for v in vals):
        return True                      # small counts repeat by chance
    if all(x in common for x in win):
        return True                      # design levels of a long-format table (time points, doses per subject)
    return False


def _formula_share(c: Col, s: int, ln: int) -> float:
    """Share of the block's cells that are formulas: values linked to another sheet are identical by construction."""
    if not c.t.formulas:
        return 0.0
    return sum(cell_ref(c.idx[k], c.j) in c.t.formulas for k in range(s, s + ln)) / ln


def _design_repeat(c: Col, s: int, p: int, ln: int) -> bool:
    """A column matching itself shifted by p rows over most of its length is a repeated design (time points per
    subject, a standard curve per plate) when one period runs in order; measured values are not sorted."""
    if ln < 0.8 * (len(c.raw) - p):
        return False
    period = c.vals[s:s + p]
    d = np.diff(period)
    return bool(np.all(d >= 0) or np.all(d <= 0))


def check(tables: list[Table], dates: Optional[dict] = None,
          files: Optional[Iterable] = None) -> list[Finding]:
    """All data checks.  `dates` is the paper's record ({"received", "accepted", "published", "issued"}: ISO or
    '4 March 2021' strings); `files` the supplementary files as paths or (path, name) pairs.  Both are optional:
    without dates the file-metadata timeline is skipped, without files it uses the metadata the tables carry."""
    out: list[Finding] = []
    cols = [c for t in tables for c in columns(t)]
    out += _duplicate_blocks(cols)
    out += _duplicate_columns(cols)
    out += _formulas(tables)
    for c in cols:
        if not (_theoretical(c) or _calculated(c)):
            out += _digits(c)             # computed values' last digits follow the arithmetic
        if not _theoretical(c):
            out += _repeats(c)            # theoretical values repeat whenever their inputs do (isomers)
        out += _impossible(c)
        out += _sort_anomaly(c)
    out += _same_spread(cols)
    out += _gene_dates(tables)
    out += _mixed_fonts(tables)
    out += _generator(tables)
    out += _hidden(tables)
    # second wave: each guarded, so one unexpected layout cannot cost the findings above
    out += _guard(_offset_copies, cols)
    out += _guard(_typed_over_formulas, tables)
    out += _guard(_calc_order, tables)
    out += _guard(_external_links, tables)
    out += _guard(_summary_mismatch, tables)
    out += _guard(_precision_mix, cols)
    out += _guard(_trailing_repeats, cols)
    out += _guard(_benford, cols)
    if dates:
        out += _guard(lambda: _file_timeline(_file_metas(tables, files), dates))
    return out


def _guard(fn: Callable, *args) -> list[Finding]:
    try:
        return fn(*args)
    except Exception:                    # noqa: BLE001 - a data check must never stop the scan
        return []


def _duplicate_blocks(cols: list[Col]) -> list[Finding]:
    index: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for ci, c in enumerate(cols):
        common = {v for v, k in Counter(c.raw).items() if k >= 3}
        for s in range(0, len(c.raw) - WINDOW + 1):
            win = c.raw[s:s + WINDOW]
            if "" in win or _trivial(win, common):
                continue
            occ = index[tuple(win)]
            if len(occ) < 20:             # one pattern in many columns must not blow up the pair loop
                occ.append((ci, s))
    # blocks already found, per diagonal (ca, cb, sa - sb): each diagonal is walked once, not once per window
    covered: dict[tuple[int, int, int], list[tuple[int, int]]] = defaultdict(list)
    found = []
    for key, occ in index.items():
        if len(occ) < 2:
            continue
        for x in range(len(occ)):
            for y in range(x + 1, len(occ)):
                (ca, sa), (cb, sb) = occ[x], occ[y]
                if ca == cb and abs(sa - sb) < WINDOW:
                    continue
                diag = covered[(ca, cb, sa - sb)]
                if any(lo <= sa < hi for lo, hi in diag):
                    continue
                # extend backwards/forwards to the maximal block
                a, b = cols[ca], cols[cb]
                s0a, s0b = sa, sb
                while s0a > 0 and s0b > 0 and a.raw[s0a - 1] == b.raw[s0b - 1] and a.raw[s0a - 1]:
                    s0a -= 1
                    s0b -= 1
                ln = 0
                while s0a + ln < len(a.raw) and s0b + ln < len(b.raw) and a.raw[s0a + ln] == b.raw[s0b + ln]:
                    ln += 1
                diag.append((s0a, s0a + ln))
                if ca == cb and _design_repeat(a, min(s0a, s0b), abs(s0b - s0a), ln):
                    continue
                if max(_formula_share(a, s0a, ln), _formula_share(b, s0b, ln)) >= 0.8:
                    continue
                found.append((ca, s0a, cb, s0b, ln))
    # keep the longest few; a duplicated sheet would otherwise produce hundreds
    found.sort(key=lambda r: -r[4])
    out = []
    used = set()
    same_table = _same_table_twice(cols, found)
    for key, pairs in same_table.items():
        used |= {(ca, cb) for ca, cb, _ in pairs}
        a0, b0 = cols[pairs[0][0]], cols[pairs[0][1]]
        heads = [cols[ca].header for ca, _, _ in pairs]
        out.append(Finding(
            "data.duplicate_block", "medium", f"The same table appears in two files ({len(pairs)} columns)",
            f"{len(pairs)} whole columns of {a0.t.file}, '{a0.t.name}' appear again, value for value and under the "
            f"same headings ({', '.join(repr(h) for h in heads[:5])}), in {b0.t.file}, '{b0.t.name}'. Usually the "
            "same data supplied twice (for example a supplementary table and a figure's source data). It is a "
            "problem only if the two files are said to come from different experiments.",
            0.4, [a0.loc(), b0.loc()],
            data={"length": min(len(a0.raw), len(b0.raw)), "columns": heads[:20], "cross_file": True,
                  "same_table": True}))
    for ca, sa, cb, sb, ln in found:
        if (ca, cb) in used:
            continue
        used.add((ca, cb))
        a, b = cols[ca], cols[cb]
        whole = ln >= 0.9 * min(len(a.raw), len(b.raw))
        precise = np.mean([_sig_digits(v) for v in a.raw[sa:sa + ln]]) >= 3
        cross_file = a.t.file != b.t.file
        sev = "high" if (ln >= 10 or precise and ln >= 8) else "medium"
        what = "the whole column" if whole else f"a run of {ln} values"
        relabelled = ""
        if cross_file and a.header and b.header and _head(a.header) != _head(b.header):
            relabelled = f" The two copies carry different labels ('{a.header}' and '{b.header}')."
        out.append(Finding(
            "data.duplicate_block", sev,
            f"Identical run of {ln} values in two places" + (" (different files)" if cross_file else ""),
            f"{what.capitalize()} in {a.label} (rows {a.idx[sa] + 1}-{a.idx[sa + ln - 1] + 1}) is repeated exactly in "
            f"{b.label} (rows {b.idx[sb] + 1}-{b.idx[sb + ln - 1] + 1}).{relabelled} Independent measurements do not "
            "repeat a sequence; a copied block does. Legitimate if both places show the same data on purpose (e.g. "
            "a shared control), which the legend should say.",
            0.8 if precise else 0.6, [a.loc(sa, sa + ln), b.loc(sb, sb + ln)],
            data={"length": ln, "values": a.raw[sa:sa + min(ln, 12)], "cross_file": cross_file}))
        if len(out) >= 8:
            break
    return out


def _head(h: str) -> str:
    return re.sub(r"[\s_.:()\[\]-]+", " ", h or "").strip().lower()


def _same_table_twice(cols: list[Col], found: list[tuple]) -> dict[tuple, list[tuple[int, int, int]]]:
    """Whole columns copied between two files under the same (non-empty) headings, grouped per table pair.  Two
    or more such columns mean the table itself was supplied twice; one finding then says so instead of one high
    'copied block' per column."""
    groups: dict[tuple, list[tuple[int, int, int]]] = defaultdict(list)
    for ca, sa, cb, sb, ln in found:
        a, b = cols[ca], cols[cb]
        if a.t.file == b.t.file or not a.header or _head(a.header) != _head(b.header):
            continue
        if ln < 0.9 * max(len(a.raw), len(b.raw)):
            continue
        key = (id(a.t), id(b.t))
        if all((ca, cb) != (x, y) for x, y, _ in groups[key]):
            groups[key].append((ca, cb, ln))
    return {k: v for k, v in groups.items() if len(v) >= 2}


def _duplicate_columns(cols: list[Col]) -> list[Finding]:
    out = []
    by_len: dict[int, list[Col]] = defaultdict(list)
    for c in cols:
        if len(c.vals) >= 8 and len(set(c.raw)) >= 5:
            by_len[len(c.vals)].append(c)
    reported = set()
    for n, group in by_len.items():
        for x in range(len(group)):
            for y in range(x + 1, len(group)):
                a, b = group[x], group[y]
                if np.array_equal(a.vals, b.vals):
                    continue                      # exact copies are reported by the block check
                A = np.vstack([a.vals, np.ones(n)]).T
                coef, res, *_ = np.linalg.lstsq(A, b.vals, rcond=None)
                fit = A @ coef
                scale = max(np.abs(b.vals).max(), 1e-9)
                if np.max(np.abs(fit - b.vals)) / scale < 1e-6 and abs(coef[0]) not in (0.0,) and \
                        not (abs(coef[0] - 1) < 1e-9 and abs(coef[1]) < 1e-12):
                    k = (id(a), id(b))
                    if k in reported:
                        continue
                    reported.add(k)
                    rel = (f"{b.label} = {a.label.split(', column ')[-1]} × {coef[0]:.6g}"
                           + (f" + {coef[1]:.6g}" if abs(coef[1]) > 1e-9 else ""))
                    out.append(Finding(
                        "data.derived_column", "low", "One column is an exact transform of another",
                        f"Every value of {b.label} equals {a.label} × {coef[0]:.6g}"
                        + (f" + {coef[1]:.6g}" if abs(coef[1]) > 1e-9 else "")
                        + ". Fine for a unit conversion or normalisation; a problem if the two columns are presented "
                        "as separate measurements.", 0.5, [a.loc(), b.loc()], data={"relation": rel}))
                    if len(out) >= 5:
                        return out
    return out


RAND_RE = re.compile(r"\b(RAND|RANDBETWEEN|RANDARRAY)\s*\(", re.I)


def _formulas(tables: list[Table]) -> list[Finding]:
    out = []
    for t in tables:
        hits = [(k, f) for k, f in t.formulas.items() if RAND_RE.search(f)]
        if hits:
            out.append(Finding(
                "data.random_formula", "high", "Values generated by a random-number formula",
                f"{len(hits)} cell(s) in {t.file}, sheet '{t.name}' compute their value with a random-number function "
                f"(e.g. {hits[0][0]}: {hits[0][1]}). Simulations and random allocation use these legitimately; "
                "measured data never should.", 0.75,
                [Location(file=t.file, sheet=t.name, cells=",".join(k for k, _ in hits[:10]))],
                data={"cells": [k for k, _ in hits[:50]], "example": hits[0][1]}))
    return out


def _digits(c: Col) -> list[Finding]:
    raw = [r for r in c.raw if r and "e" not in r.lower()]
    if len(raw) < 60:
        return []
    sig = np.mean([_sig_digits(r) for r in raw])
    if sig < 3:
        return []
    # all values share one count of decimals?  then the last digit is a real measured digit
    last = [r[-1] for r in raw if r[-1].isdigit()]
    if len(last) < 60:
        return []
    counts = np.array([last.count(str(d)) for d in range(10)], float)
    share_05 = (counts[0] + counts[5]) / counts.sum()
    if share_05 > 0.6:
        return [Finding("data.digit_preference", "low", "Values rounded to 0 or 5 much more than expected",
                        f"In {c.label}, {share_05:.0%} of {len(last)} values end in 0 or 5 (about 20% expected). "
                        "Typical of values read off a scale by eye; worth knowing when judging precision.",
                        0.5, [c.loc()], data={"counts": counts.tolist()})]
    nonzero = counts[1:]                 # trailing zeros are often dropped by spreadsheets, so test digits 1-9
    chi, p = st.chisquare(nonzero)
    if p < 1e-5:
        return [Finding("data.terminal_digits", "medium", "Last digits far from uniform",
                        f"The final digits of {len(last)} precise values in {c.label} are not evenly spread "
                        f"(χ² = {chi:.0f}, p = {p:.1e}). In real measurements the last digit is close to random; "
                        "invented numbers often favour some digits. Weak evidence on its own.",
                        0.45, [c.loc()], data={"counts": counts.tolist(), "p": p})]
    return []


def _repeats(c: Col) -> list[Finding]:
    """More exact coincidences than the column's spread and precision allow (a birthday-problem calculation):
    expected equal pairs ≈ n²/2 · resolution / (2σ√π) for roughly bell-shaped data."""
    raw = [r for r in c.raw if r and "e" not in r and _sig_digits(r) >= 3 and r not in ("0", "1", "100")]
    n = len(raw)
    if n < 30:
        return []
    vals = np.array([float(r) for r in raw])
    # printed precision: trailing zeros are dropped ('1.50' -> '1.5'), so take the finest one that is common
    decs = Counter(len(r.split(".")[1]) if "." in r else 0 for r in raw)
    dec = max(d for d, k in decs.items() if k >= 0.1 * n)
    res = _resolution(vals, dec)
    q75, q25 = np.percentile(vals, [75, 25])
    sigma = (q75 - q25) / 1.349
    if sigma <= 5 * res:
        return []                         # values cluster on a few levels; coincidences are expected
    expected = n * (n - 1) / 2 * res / (2 * sigma * math.sqrt(math.pi))
    cnt = Counter(raw)
    observed = sum(k * (k - 1) // 2 for k in cnt.values())
    if observed < 3 or st.poisson.sf(observed - 1, max(expected, 1e-9)) > 1e-4:
        return []
    top = sorted(((k, v) for k, v in cnt.items() if v > 1), key=lambda kv: -kv[1])[:5]
    return [Finding("data.repeated_values", "medium", "Precise values repeat more than chance allows",
                    f"In {c.label}, {observed} pairs of identical values among {n}, where about {expected:.1f} would "
                    f"be expected by chance at this precision and spread; e.g. {', '.join(f'{k} ×{v}' for k, v in top)}. "
                    "Copied cells repeat; independent measurements rarely do.",
                    0.55, [c.loc()], data={"repeats": dict(top), "observed_pairs": observed,
                                           "expected_pairs": round(expected, 2)})]


def _resolution(vals: np.ndarray, dec: int) -> float:
    """The grid the values really sit on.  Values recorded to the nearest 0.5, or means of 5 integer scores (steps of
    0.2), print one decimal but repeat 5 or 2 times as often as a 0.1 grid would allow."""
    base = 10.0 ** -dec
    ints = np.round(vals / base).astype(np.int64)
    u = np.unique(ints)
    g = int(np.gcd.reduce(np.diff(u))) if len(u) > 1 else 1
    if g <= 1:                            # a few values off the grid (typos, pasted duplicates do not do this)
        for k in (5, 4, 2):
            if np.mean((ints - ints.min()) % k == 0) > 0.9:
                g = k
                break
    return base * max(g, 1)


PCT_H = re.compile(r"%|percent|proportion \(%\)", re.I)
# relative measures legitimately leave 0-100: 104% viability, 130% of control, -5% inhibition
PCT_RELATIVE_H = re.compile(r"of control|relative|viab|recover|growth|inhibit|change|increase|decrease|normali[sz]|"
                            r"fold|vs\.?|versus|baseline|survival|efficiency", re.I)
P_H = re.compile(r"^\s*(p|p[\s_.-]?val(ue)?s?|q[\s_.-]?val(ue)?|fdr|padj|p[\s_.-]?adj(usted)?|adj(usted)?[\s_.-]?p"
                 r"([\s_.-]?val(ue)?)?)\s*(\(.*\))?\s*$", re.I)
NONNEG_H = re.compile(r"\b(count|counts|number|no\.|n|age|weight|mass|length|area|volume|concentration|conc|size)\b", re.I)


def _impossible(c: Col) -> list[Finding]:
    h = c.header or ""
    bad = []
    why = ""
    if PCT_H.search(h):
        if PCT_RELATIVE_H.search(h):
            return []
        bad = [i for i, v in enumerate(c.vals) if v > 100.0001 or v < 0]
        why = "percentages outside 0-100"
    elif P_H.search(h) and not re.search(r"log|-log|ratio", h, re.I):
        bad = [i for i, v in enumerate(c.vals) if v > 1 or v < 0]
        why = "p-values outside 0-1"
    elif NONNEG_H.search(h) and not re.search(r"change|diff|delta|log|z", h, re.I):
        # blank-subtracted readings (ELISA concentrations) dip slightly below zero
        floor = -0.05 * (c.vals.max() - c.vals.min())
        bad = [i for i, v in enumerate(c.vals) if v < floor]
        why = "negative values for a quantity that cannot be negative"
    if not bad or len(bad) > 0.3 * len(c.vals):
        return []                       # most values "impossible": the header means something else
    return [Finding("data.impossible", "medium", f"Impossible values: {why}",
                    f"{len(bad)} value(s) in {c.label}: " + ", ".join(c.raw[i] for i in bad[:6]) + ".",
                    0.6, [c.loc(bad[0], bad[0] + 1)], data={"values": [c.raw[i] for i in bad[:20]]})]


def _sort_anomaly(c: Col) -> list[Finding]:
    """An ID-like column in ascending order except a handful of rows (Data Colada's tell in a 2021 case)."""
    v = c.vals
    if len(v) < 25 or c.j > 1 or not np.all(np.mod(v, 1) == 0) or len(set(v.tolist())) != len(v):
        return []
    d = np.diff(v)
    breaks = np.where(d < 0)[0]
    if 0 < len(breaks) <= max(1, int(0.04 * len(v))) and np.mean(d > 0) > 0.95:
        rows = [c.idx[i + 1] + 1 for i in breaks]
        return [Finding("data.sort_anomaly", "low", "IDs in order except for a few rows",
                        f"{c.label} counts up row by row except at row(s) {', '.join(map(str, rows[:8]))}. "
                        "Often an innocent re-sort; rows that sit out of order inside sorted data have also marked "
                        "rows that were moved or edited by hand.", 0.35, [c.loc(int(breaks[0]), int(breaks[0]) + 2)],
                        data={"rows": rows})]
    return []


SPREAD_H = re.compile(r"\b(sd|s\.d\.|sem|s\.e\.m\.|se|s\.e\.|std|stdev|standard (deviation|error))\b", re.I)


def _same_spread(cols: list[Col]) -> list[Finding]:
    out = []
    for c in cols:
        if not SPREAD_H.search(c.header or "") or len(c.raw) < 5:
            continue
        distinct = set(c.raw)
        if len(distinct) <= 2 and len(c.raw) >= 5 and any(float(x) != 0 for x in distinct):
            out.append(Finding("data.too_similar", "medium", "One spread for every group",
                               f"{c.label} lists {len(c.raw)} SD/SE values but only {len(distinct)} distinct "
                               f"one(s): {', '.join(sorted(distinct))}. Independent groups almost never share their "
                               "variability exactly.", 0.6, [c.loc()], data={"values": sorted(distinct)}))
    return out


GENE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}(-[A-Z0-9]{1,4})?$")
GENE_SERIAL_RE = re.compile(r"^(3\d{4}|4[0-9]\d{3})$")          # Excel date serials 1982-2036
GENE_ENOTE_RE = re.compile(r"^\d\.\d+E\+\d+$", re.I)            # RIKEN-style IDs like 2310009E13 -> 2.31E+13
GENE_DATE_RE = re.compile(r"^(DATE:\d{4}-(0[2-9]|1[0-2])-\d{2}|\d{1,2}-(Sep|Mar|Dec|Oct|Nov|Feb|Apr|Jun|Jul|Aug|May|Jan)"
                          r"|(Sep|Mar|Dec|Oct|Nov|Feb|Apr)-\d{2})$", re.I)


def _gene_dates(tables: list[Table]) -> list[Finding]:
    out = []
    for t in tables:
        nrows, ncols = t.shape
        for j in range(ncols):
            cells = [c.strip() for c in t.column(j) if c and c.strip()]
            if len(cells) < 20:
                continue
            genes = sum(bool(GENE_RE.match(c)) for c in cells)
            dates = [c for c in cells if GENE_DATE_RE.match(c) or GENE_SERIAL_RE.match(c) or GENE_ENOTE_RE.match(c)]
            if genes >= 0.5 * len(cells) and dates:
                out.append(Finding(
                    "data.gene_dates", "medium", "Gene names turned into dates by Excel",
                    f"{len(dates)} entries in {t.file}, '{t.name}' column {col_name(j)} look like dates "
                    f"({', '.join(d.replace('DATE:', '') for d in dates[:4])}) in a column of gene symbols. Excel "
                    "silently converts SEPT2, MARCH1, DEC1 and similar to dates; the original symbols are lost. "
                    "An honest, very common error (Ziemann et al. 2016).", 0.8,
                    [Location(file=t.file, sheet=t.name, cells=f"{col_name(j)}:{col_name(j)}")],
                    data={"examples": dates[:10]}))
    return out


def _hidden(tables: list[Table]) -> list[Finding]:
    out = []
    for t in tables:
        data_cells = sum(1 for r in t.rows for c in r if number(c) is not None)
        if t.hidden and data_cells >= 5:
            out.append(Finding("data.hidden", "low", f"Hidden sheet with data: '{t.name}'",
                               f"{t.file} contains a hidden sheet with {data_cells} numeric cells. Often scratch "
                               "work; sometimes the excluded or original data. Worth opening.", 0.5,
                               [Location(file=t.file, sheet=t.name)]))
            continue
        # hiding the unused rows and columns around a table is tidiness; only hidden cells with content matter
        rows = [r for r in t.hidden_rows if 0 < r <= len(t.rows) and any(c.strip() for c in t.rows[r - 1])]
        cols = [k for k in t.hidden_cols if any(c.strip() for c in t.column(_col_index(k)))]
        if rows or cols:
            out.append(Finding("data.hidden", "low", f"Hidden rows or columns in '{t.name}'",
                               f"{t.file}, sheet '{t.name}' hides {len(rows)} row(s) and {len(cols)} column(s) "
                               "that hold data. Excluded data points are sometimes hidden rather than reported.",
                               0.4, [Location(file=t.file, sheet=t.name)], data={"rows": rows[:30], "cols": cols[:30]}))
    return out


def _col_index(letters: str) -> int:
    j = 0
    for ch in letters.upper():
        j = j * 26 + ord(ch) - 64
    return j - 1


def _mixed_fonts(tables: list[Table]) -> list[Finding]:
    """Numbers in one column typed in two fonts, the two fonts in separate row blocks.  Pasting rows from another
    file carries its formatting along; in a 2021 case half of a column was in Calibri and half in Cambria, and the
    Cambria rows turned out to be altered copies."""
    out = []
    for t in tables:
        for j, cells in t.fonts.items():
            if len(cells) < 12:
                continue
            fonts = Counter(f for _, f in cells)
            if len(fonts) < 2:
                continue
            (f1, n1), (f2, n2) = fonts.most_common(2)
            if n2 < max(4, 0.1 * len(cells)):
                continue
            seq = [f for _, f in sorted(cells)]
            switches = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
            if switches > 6:
                continue                  # interleaved fonts: a style choice, not pasted blocks
            out.append(Finding("data.mixed_fonts", "medium", "One data column typed in two different fonts",
                               f"In {t.file}, sheet '{t.name}', column {col_name(j)}: {n1} numbers in {f1} and {n2} in "
                               f"{f2}, in {switches + 1} separate blocks. Rows pasted in from elsewhere keep their "
                               "font; in a documented fraud case the rows in the second font were altered copies. "
                               "Often innocent (merged files), but worth knowing which rows came from where.", 0.5,
                               [Location(file=t.file, sheet=t.name, cells=f"{col_name(j)}:{col_name(j)}")],
                               data={"fonts": dict(fonts.most_common(4)), "blocks": switches + 1}))
    return out


def _generator(tables: list[Table]) -> list[Finding]:
    """Measured values stored with 15-17 significant digits: what a random-number generator or a formula leaves
    behind when its output is pasted as values.  Instruments and people record a few digits."""
    out = []
    for t in tables:
        if not t.file.lower().endswith((".xlsx", ".xlsm", ".csv", ".tsv", ".txt")):
            continue
        for c in columns(t, min_values=20):
            if any(k.startswith(col_name(c.j)) and k[len(col_name(c.j)):].isdigit() for k in t.formulas):
                continue                  # computed columns are expected to carry full precision
            long = [r for r in c.raw if _sig_digits(r) >= 13]
            if len(long) >= 0.8 * len(c.raw) and len(set(long)) >= 0.9 * len(long):
                out.append(Finding("data.generator_digits", "low", "Values carry machine precision",
                                   f"{len(long)} values in {c.label} have 13+ significant digits (e.g. {long[0]}). "
                                   "Real measurements are recorded to a few digits; values pasted from a random "
                                   "number generator or a calculation look like this. Fine for derived values.",
                                   0.4, [c.loc()], data={"example": long[:3]}))
    return out


# ---- second wave ---------------------------------------------------------------------------------------------------

OFF_WINDOW = 8
# headings of columns that are computed from others on purpose: a constant offset or ratio is their definition
DERIVED_H = re.compile(r"norm|\blog|\bln\b|fold|ratio|relative|\brel\b|percent|%|of control|z[\s_-]?score|scaled|"
                       r"correct|subtract|minus|blank|background|\bbg\b|adjust|delta|Δ|change|diff|baseline|mean|"
                       r"average|median|\bsum\b|total|cumul|rank|upper|lower|\bci\b|interval|bound|limit|±|error|"
                       r"\bsd\b|\bsem?\b|start|\bend\b|stop|position|coord|offset|shift|index|\bid\b|expected|"
                       r"predicted|fitted|calc|convert|\bunits?\b|standardi[sz]|"
                       # theoretical values computed from a formula or sequence (m/z ladders, isotope patterns)
                       r"m/z|\bisotop|\bcharge\b|theoretical|monoisotopic|in[\s-]?silico", re.I)
DERIVED_SHEET = re.compile(r"norm|\blog|fold|relative|ratio|%|percent|correct|subtract|adjust|z[\s_-]?score|scaled|"
                           r"standardi[sz]|calc", re.I)


def _decimals(r: str) -> int:
    if "e" in r.lower():
        return 15
    return len(r.split(".")[1]) if "." in r else 0


THEORETICAL = re.compile(r"m/z|\bisotop|\bcharge\b|theoretical|monoisotopic|in[\s-]?silico", re.I)


def _theoretical(c: Col) -> bool:
    """Values computed from a formula or a sequence (m/z ladders, isotope patterns, in-silico digests), by heading."""
    return bool(THEORETICAL.search(c.header or "") or THEORETICAL.search(c.t.name or ""))


def _derived(c: Col) -> bool:
    return bool(DERIVED_H.search(c.header or "") or DERIVED_SHEET.search(c.t.name or ""))


def _offset_index(c: Col) -> list[tuple[int, bytes]]:
    """(start, key) for each usable window of OFF_WINDOW values.  The key is the window's step pattern scaled by its
    largest step, so a run and a copy of it plus or times a constant share it; checked exactly afterwards."""
    n = len(c.vals)
    if n < OFF_WINDOW or _derived(c):
        return []
    from numpy.lib.stride_tricks import sliding_window_view as sw

    vals = c.vals
    sig = np.array([_sig_digits(r) for r in c.raw], float)
    dec = np.array([_decimals(r) for r in c.raw], float)
    res = 10.0 ** -np.minimum(sw(dec, OFF_WINDOW).max(axis=1), 12)
    d = sw(np.diff(vals), OFF_WINDOW - 1)
    absd = np.abs(d)
    big = absd.argmax(axis=1)
    scale = d[np.arange(len(d)), big]
    srt = np.sort(sw(vals, OFF_WINDOW), axis=1)
    distinct = 1 + (np.diff(srt, axis=1) != 0).sum(axis=1)
    # the filters below also cover _trivial's cases: steps, dilution series and small counts are sorted, repeat
    # values or carry too few digits
    ok = ((absd.max(axis=1) >= 50 * res)                     # steps far above the printing grid
          & (sw(sig, OFF_WINDOW).mean(axis=1) >= 3)          # precise values, not small counts
          & (distinct >= OFF_WINDOW - 1)
          & ~np.all(d > 0, axis=1) & ~np.all(d < 0, axis=1))  # sorted runs: coordinates, cumulative values, doses
    starts = np.nonzero(ok)[0]
    if not len(starts):
        return []
    keys = np.round(d[starts] / scale[starts, None] * 1e6).astype(np.int64)
    return [(int(s), k.tobytes()) for s, k in zip(starts, keys)]


def _relation(a: np.ndarray, b: np.ndarray) -> Optional[tuple[str, float]]:
    """('add', c) when b = a + c, ('mul', k) when b = a × k (k > 0), exactly up to float noise; else None."""
    span = max(np.abs(np.diff(a)).max(initial=0.0), np.abs(np.diff(b)).max(initial=0.0))
    tol = 1e-6 * span + 1e-12
    diff = b - a
    c = float(np.median(diff))
    if np.max(np.abs(diff - c)) <= tol:
        return None if abs(c) <= tol else ("add", c)
    if np.all(a != 0):
        k = float(np.median(b / a))
        if k > 0 and abs(k - 1) > 1e-6 and np.max(np.abs(b - k * a)) <= tol * max(1.0, k):
            return ("mul", k)
    return None


def _holds(rel: tuple[str, float], x: float, y: float, tol: float) -> bool:
    kind, v = rel
    return abs((y - x - v) if kind == "add" else (y - v * x)) <= tol * (max(1.0, v) if kind == "mul" else 1.0)


def _print_tol(a: np.ndarray, ra: list[str], b: np.ndarray, rb: list[str], rel: tuple[str, float]) -> float:
    """How far b may sit from the relation and still be 'exact': half a unit in the last printed place of each side
    (a copy scaled and re-rounded), plus float noise.  Values printed to 12 decimals must match to about 1e-12, so a
    ratio that merely drifts slowly (a physical relation, such as m/z at charge 4 against charge 3) is not a copy."""
    dec = lambda raw: min(max((_decimals(r) for r in raw), default=0), 12)
    k = abs(rel[1]) if rel[0] == "mul" else 1.0
    big = max(float(np.abs(a).max(initial=0.0)), float(np.abs(b).max(initial=0.0)))
    return 0.5 * 10.0 ** -dec(rb) + 0.5 * k * 10.0 ** -dec(ra) + 1e-12 * max(1.0, big)


def _offset_copies(cols: list[Col]) -> list[Finding]:
    """A run of 8+ values that equals another run plus a constant, or times a constant: data copied and nudged.
    Exact copies are the block check's job; whole-column transforms are derived_column's.  Columns whose heading says
    they are computed (normalised, log, fold, corrected...) are skipped, and a column made of several such pieces
    of another (normalised per plate or per group) is treated as derived, not copied."""
    index: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
    for ci, c in enumerate(cols):
        for s, key in _offset_index(c):
            occ = index[key]
            if len(occ) < 20:
                occ.append((ci, s))
    covered: dict[tuple[int, int, int], list[tuple[int, int]]] = defaultdict(list)
    runs: dict[tuple[int, int], list[tuple[int, int, int, tuple]]] = defaultdict(list)
    for occ in index.values():
        if len(occ) < 2:
            continue
        for x in range(len(occ)):
            for y in range(x + 1, len(occ)):
                (ca, sa), (cb, sb) = occ[x], occ[y]
                if ca == cb and abs(sa - sb) < OFF_WINDOW:
                    continue
                diag = covered[(ca, cb, sa - sb)]
                if any(lo <= sa < hi for lo, hi in diag):
                    continue
                a, b = cols[ca], cols[cb]
                rel = _relation(a.vals[sa:sa + OFF_WINDOW], b.vals[sb:sb + OFF_WINDOW])
                if rel is None:
                    continue
                span = np.abs(np.diff(a.vals[sa:sa + OFF_WINDOW])).max()
                wa, wb = a.vals[sa:sa + OFF_WINDOW], b.vals[sb:sb + OFF_WINDOW]
                tol = min(1e-6 * span + 1e-12,
                          _print_tol(wa, a.raw[sa:sa + OFF_WINDOW], wb, b.raw[sb:sb + OFF_WINDOW], rel)
                          / (max(1.0, rel[1]) if rel[0] == "mul" else 1.0))
                if not all(_holds(rel, x, y, tol) for x, y in zip(wa, wb)):
                    continue              # near-constant only: not exact at the precision the values are printed
                s0a, s0b = sa, sb
                while s0a > 0 and s0b > 0 and _holds(rel, a.vals[s0a - 1], b.vals[s0b - 1], tol):
                    s0a, s0b = s0a - 1, s0b - 1
                ln = 0
                while s0a + ln < len(a.vals) and s0b + ln < len(b.vals) and \
                        _holds(rel, a.vals[s0a + ln], b.vals[s0b + ln], tol):
                    ln += 1
                if ca == cb:
                    ln = min(ln, abs(s0b - s0a))      # a run may not overlap its own copy
                diag.append((s0a, s0a + max(ln, 1)))
                if ln >= OFF_WINDOW:
                    runs[(ca, cb)].append((s0a, s0b, ln, rel))
    found = []
    for (ca, cb), rs in runs.items():
        a, b = cols[ca], cols[cb]
        cov_a = np.zeros(len(a.vals), bool)
        cov_b = np.zeros(len(b.vals), bool)
        for sa, sb, ln, _ in rs:
            cov_a[sa:sa + ln] = True
            cov_b[sb:sb + ln] = True
        if ca == cb:
            if (cov_a | cov_b).mean() >= 0.6:
                continue                         # a repeating design (per-subject offsets), not a copied run
        elif cov_a.mean() >= 0.6 or cov_b.mean() >= 0.6:
            continue                             # one column derived from the other, possibly piece by piece
        sa, sb, ln, rel = max(rs, key=lambda r: r[2])
        if max(_formula_share(a, sa, ln), _formula_share(b, sb, ln)) >= 0.5:
            continue                             # computed cells: the formula is the explanation
        found.append((ln, ca, sa, cb, sb, rel))
    found.sort(key=lambda r: -r[0])
    out = []
    for ln, ca, sa, cb, sb, (kind, v) in found[:6]:
        a, b = cols[ca], cols[cb]
        precise = np.mean([_sig_digits(r) for r in a.raw[sa:sa + ln]]) >= 4
        how = f"plus {v:.6g}" if kind == "add" else f"multiplied by {v:.6g}"
        out.append(Finding(
            "data.offset_copy", "high" if ln >= 12 and precise else "medium",
            f"Run of {ln} values matches another run {'plus' if kind == 'add' else 'times'} a constant",
            f"{ln} values in {b.label} (rows {b.idx[sb] + 1}-{b.idx[sb + ln - 1] + 1}) equal the values in {a.label} "
            f"(rows {a.idx[sa] + 1}-{a.idx[sa + ln - 1] + 1}) {how}, row for row. Separate measurements do not keep "
            "an exact constant gap; copying a block and then adding to it, or scaling it, does. Fine if one block "
            "is knowingly computed from the other (a unit change or a correction), which the file should say.",
            0.7 if precise else 0.6, [a.loc(sa, sa + ln), b.loc(sb, sb + ln)],
            data={"length": ln, "relation": kind, "constant": round(v, 9), "cross_file": a.t.file != b.t.file,
                  "values": a.raw[sa:sa + min(ln, 8)], "copy": b.raw[sb:sb + min(ln, 8)]}))
    return out


SUMMARY_ROW = re.compile(r"\b(mean|average|avg|total|sum|median|sd|sem|s\.d\.|s\.e\.m\.|std|stdev|subtotal)\b",
                         re.I)
REF_RE = re.compile(r"(?<![A-Za-z!$])\$?([A-Z]{1,3})\$?(\d{1,7})(?![\d(])")


def _shape(cell: str, formula: str) -> str:
    """The formula written relative to its own row ('=B5/C5' in row 5 -> '=B{0}/C{0}'), so filled-down copies match."""
    row = int(re.sub(r"^[A-Z]+", "", cell))
    return REF_RE.sub(lambda m: f"{m.group(1)}{{{int(m.group(2)) - row}}}", formula.replace(" ", "").upper())


def _formula_columns(t: Table) -> dict[str, dict[int, str]]:
    by_col: dict[str, dict[int, str]] = defaultdict(dict)
    for ref, f in t.formulas.items():
        m = re.match(r"^([A-Z]+)(\d+)$", ref)
        if m:
            by_col[m.group(1)][int(m.group(2)) - 1] = f
    return by_col


def _typed_over_formulas(tables: list[Table]) -> list[Finding]:
    """A column filled down with one formula, except a few cells in the middle that hold typed numbers."""
    out = []
    for t in tables:
        if not t.formulas:
            continue
        for letters, rows in _formula_columns(t).items():
            if len(rows) < 10:
                continue
            j = _col_index(letters)
            shapes = Counter(_shape(f"{letters}{i + 1}", f) for i, f in rows.items())
            shape, n_shape = shapes.most_common(1)[0]
            if n_shape < 0.8 * len(rows) or "{" not in shape:
                continue                          # not one filled-down formula
            lo, hi = min(rows), max(rows)
            typed = []
            for i in range(lo + 1, hi):
                if i in rows or i >= len(t.rows) or j >= len(t.rows[i]):
                    continue
                if number(t.rows[i][j]) is None:
                    continue
                # a data row: other cells of the row are filled too, and it is not a mean / total line
                others = [c for k, c in enumerate(t.rows[i]) if k != j and c.strip()]
                if not others or any(SUMMARY_ROW.search(c) for c in others if number(c) is None):
                    continue
                typed.append(i)
            span = hi - lo + 1
            if not typed or len(typed) > max(3, 0.1 * span):
                continue
            example = next(r for r, f in sorted(rows.items()) if _shape(f"{letters}{r + 1}", f) == shape)
            cells = ", ".join(cell_ref(i, j) for i in typed[:8])
            out.append(Finding(
                "data.typed_over_formula", "low", "Typed numbers inside a formula column",
                f"In {t.file}, sheet '{t.name}', column {letters} is calculated by one formula in {n_shape} rows "
                f"(e.g. {cell_ref(example, j)}: {rows[example][:60]}), but {cells} hold typed numbers instead. A number "
                "typed over a formula no longer follows the data it was calculated from. Often a quick manual fix; "
                "it is also how a result can be changed without touching the raw data.", 0.45,
                [Location(file=t.file, sheet=t.name, cells=cells)],
                data={"cells": [cell_ref(i, j) for i in typed[:30]], "formula": rows[example][:120],
                      "values": [t.rows[i][j] for i in typed[:30]]}))
    return out[:6]


def _lis(seq: list[int]) -> list[int]:
    """Indexes of one longest strictly increasing subsequence (patience sorting, O(n log n))."""
    import bisect

    tails: list[int] = []
    tail_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tail_idx.append(i)
        else:
            tails[k] = v
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else -1
    out, i = [], tail_idx[-1] if tail_idx else -1
    while i >= 0:
        out.append(i)
        i = prev[i]
    return out[::-1]


def _calc_order(tables: list[Table]) -> list[Finding]:
    """Rows whose formulas sit out of sequence in Excel's calculation chain.  Formulas filled down a column enter
    the chain in row order; a row cut and pasted to a new place keeps its old place in the chain.  This is how a
    2021 analysis showed rows had been moved between conditions.  Columns whose formulas depend on other formula
    cells are skipped, because Excel reorders the chain to calculate dependencies first."""
    out = []
    for t in tables:
        chain = t.meta.get("calc_chain") or []
        if len(chain) < 20:
            continue
        by_col: dict[str, list[int]] = defaultdict(list)
        for ref in chain:
            m = re.match(r"^([A-Z]+)(\d+)$", ref)
            if m:
                by_col[m.group(1)].append(int(m.group(2)))
        for letters, seq in by_col.items():
            if len(seq) < 20 or len(set(seq)) != len(seq):
                continue
            col_f = [t.formulas.get(f"{letters}{r}", "") for r in seq]
            refs = {f"{m.group(1)}{m.group(2)}" for f in col_f for m in REF_RE.finditer(f.upper()) if "!" not in f}
            if any(r in t.formulas for r in refs):
                continue                          # dependencies: Excel may have reordered the chain itself
            best = max((_lis(seq), False), (_lis([-r for r in seq]), True), key=lambda x: len(x[0]))
            keep = set(best[0])
            moved = [seq[i] for i in range(len(seq)) if i not in keep]
            if not moved or len(moved) > max(1, int(0.04 * len(seq))):
                continue
            cells = ", ".join(f"{letters}{r}" for r in moved[:8])
            out.append(Finding(
                "data.calc_order", "low", "Formula rows out of order in Excel's calculation record",
                f"In {t.file}, sheet '{t.name}', Excel's internal calculation chain lists the formulas of column "
                f"{letters} in row order except {cells}. A filled-down formula keeps its place in this chain when "
                "its row is cut and pasted elsewhere, so these rows may have been moved after the formulas were "
                "made. Re-sorting and ordinary editing can also do this; worth asking for the original file.", 0.3,
                [Location(file=t.file, sheet=t.name, cells=cells)], data={"rows": moved[:30], "chain": len(seq)}))
    return out[:4]


def _external_links(tables: list[Table]) -> list[Finding]:
    out, seen = [], set()
    for t in tables:
        links = t.meta.get("external_links") or []
        if not links or t.file in seen:
            continue
        seen.add(t.file)
        out.append(Finding(
            "data.external_links", "info", "The workbook takes values from other files",
            f"{t.file} links to {len(links)} other workbook(s): {', '.join(links[:5])}. Those files were not "
            "supplied; they may hold the original measurements behind these values.", 0.5,
            [Location(file=t.file)], data={"links": links}))
    return out


MEAN_H = re.compile(r"^\s*(mean|average|avg)\b", re.I)
SD_H = re.compile(r"^\s*(sd|s\.d\.|std|stdev|st\.?\s*dev\.?|standard deviation)\b", re.I)
SEM_H = re.compile(r"^\s*(sem|s\.e\.m\.?|se|s\.e\.|standard error)\b", re.I)
SUMMARY_H = re.compile(r"mean|average|avg|median|\bsd\b|s\.d\.|\bsem?\b|s\.e\.|\bstd|stdev|standard|^\s*n\s*$|count|"
                       r"total|\bsum\b|\bmin|\bmax|range|\bcv\b|%|p[\s_.-]?val", re.I)


def _cell_num(t: Table, i: int, j: int) -> Optional[float]:
    if i >= len(t.rows) or j < 0 or j >= len(t.rows[i]):
        return None
    return number(t.rows[i][j])


def _summary_mismatch(tables: list[Table]) -> list[Finding]:
    """Mean (and SD/SEM) columns recomputed from the replicate columns beside them.  The replicate columns are the
    run of plain numeric columns just before the summary columns, accepted only when they reproduce at least 80% of
    the reported means within rounding; the rows that then disagree are reported.  Cells holding formulas are left
    alone (the formula says what was averaged)."""
    out = []
    for t in tables:
        cols = {c.j: c for c in columns(t)}
        for m in cols.values():
            if not MEAN_H.search(m.header or ""):
                continue
            e = m.j - 1
            while e >= 0 and e in cols and SUMMARY_H.search(cols[e].header or "") and m.j - e <= 3:
                e -= 1
            best = None
            for k in range(2, 9):
                js = list(range(e - k + 1, e + 1))
                if js[0] < 0 or any(j not in cols or SUMMARY_H.search(cols[j].header or "") for j in js):
                    break
                rows = []
                for i, raw in zip(m.idx, m.raw):
                    if cell_ref(i, m.j) in t.formulas:
                        continue
                    xs = [_cell_num(t, i, j) for j in js]
                    if any(x is None for x in xs):
                        continue
                    rows.append((i, float(raw), np.array(xs, float), 0.5 * 10.0 ** -min(_decimals(raw), 12)))
                if len(rows) < 5:
                    continue
                good = sum(abs(xs.mean() - rep) <= tol * 1.01 + 1e-12 for _, rep, xs, tol in rows)
                if good >= 5 and good >= 0.8 * len(rows) and (best is None or good > best[0]):
                    best = (good, js, rows)
            if best is None:
                continue
            good, js, rows = best
            bad = [(i, rep, xs.mean()) for i, rep, xs, tol in rows
                   if abs(xs.mean() - rep) > 3 * tol + 1e-12 and abs(xs.mean() - rep) > 1e-3 * abs(xs.mean())]
            what = f"columns {col_name(js[0])}-{col_name(js[-1])}"
            if bad:
                out.append(_mismatch(t, m.j, "mean", what, bad, good, len(rows)))
            spread = cols.get(m.j + 1)
            if spread is not None and (SD_H.search(spread.header or "") or SEM_H.search(spread.header or "")):
                f = _spread_mismatch(t, spread, [i for i, *_ in rows], js, what)
                if f:
                    out.append(f)
    return out[:8]


def _spread_mismatch(t: Table, s: Col, mean_rows: list[int], js: list[int], what: str) -> Optional[Finding]:
    sem = bool(SEM_H.search(s.header or ""))
    k = len(js)
    rows = []
    keep = set(mean_rows)
    for i, raw in zip(s.idx, s.raw):
        if i not in keep or cell_ref(i, s.j) in t.formulas:
            continue
        got = [_cell_num(t, i, j) for j in js]
        if any(x is None for x in got):
            continue
        xs = np.array(got, float)
        rows.append((i, float(raw), xs, 0.5 * 10.0 ** -min(_decimals(raw), 12)))
    if len(rows) < 5:
        return None
    best = None
    for ddof in (1, 0):
        calc = [(i, rep, xs.std(ddof=ddof) / (math.sqrt(k) if sem else 1), tol) for i, rep, xs, tol in rows]
        good = sum(abs(c - rep) <= tol * 1.01 + 1e-12 for _, rep, c, tol in calc)
        if good >= 5 and good >= 0.8 * len(rows) and (best is None or good > best[0]):
            best = (good, calc)
    if best is None:
        return None
    good, calc = best
    bad = [(i, rep, c) for i, rep, c, tol in calc if abs(c - rep) > 3 * tol + 1e-12 and abs(c - rep) > 1e-3 * abs(c)]
    return _mismatch(t, s.j, "SEM" if sem else "SD", what, bad, good, len(rows)) if bad else None


def _mismatch(t: Table, j: int, stat: str, what: str, bad: list, good: int, n: int) -> Finding:
    ex = "; ".join(f"row {i + 1}: {rep:g} reported, {calc:.4g} from the replicates" for i, rep, calc in bad[:4])
    cells = ", ".join(cell_ref(i, j) for i, *_ in bad[:8])
    return Finding(
        "data.summary_mismatch", "medium", f"{stat} column does not match its replicate values",
        f"In {t.file}, sheet '{t.name}', the {stat} in column {col_name(j)} equals the {stat} of {what} in {good} of "
        f"{n} rows, but not in {len(bad)} ({ex}). A summary that no longer matches its raw values means one of them "
        "was changed afterwards, or a typo. Often innocent (an excluded replicate), which the legend should say.",
        0.5, [Location(file=t.file, sheet=t.name, cells=cells)],
        data={"stat": stat, "rows": [i + 1 for i, *_ in bad[:30]], "matched": good, "checked": n})


def _precision_mix(cols: list[Col]) -> list[Finding]:
    """A block of short values (1-3 decimals) inside a column whose other values carry full machine precision:
    numbers typed in by hand, or pasted from another source, among instrument or calculated exports."""
    out = []
    for c in cols:
        if not c.t.file.lower().endswith((".xlsx", ".xlsm", ".csv", ".tsv", ".txt")) or len(c.raw) < 30:
            continue
        if any(re.match(rf"^{col_name(c.j)}\d+$", k) for k in c.t.formulas):
            continue
        kinds = []
        for r in c.raw:
            if "e" in r.lower() or "." not in r:
                kinds.append("x")              # integers and exponent forms say nothing about precision
            elif _sig_digits(r) >= 9:
                kinds.append("L")
            elif _decimals(r) <= 3 and _sig_digits(r) <= 5:
                kinds.append("S")
            else:
                kinds.append("x")
        n = len(kinds)
        n_long, n_short = kinds.count("L"), kinds.count("S")
        if n_long < 0.6 * n or n_short < 5:
            continue
        # longest run of short values; whole numbers ('5.00' is stored as 5) neither extend nor break a run
        best, start, last, run = (0, 0, 0), 0, 0, 0
        for i, k in enumerate(kinds + ["L"]):
            if k == "S":
                if run == 0:
                    start = i
                run, last = run + 1, i
            elif k == "L":
                if run > best[0]:
                    best = (run, start, last)
                run = 0
        ln, s0, s1 = best
        p = n_short / n
        if ln < 5 or n * p ** ln > 1e-4 or len(set(c.raw[s0:s1 + 1])) < 3:
            continue
        dec = max(_decimals(r) for r in c.raw[s0:s1 + 1])
        out.append(Finding(
            "data.precision_mix", "low", "A block of values with fewer digits than the rest of the column",
            f"In {c.label}, rows {c.idx[s0] + 1}-{c.idx[s1] + 1} hold {ln} values with at most {dec} decimal "
            f"place(s), while {n_long} other values carry full precision (e.g. {next(r for r, k in zip(c.raw, kinds) if k == 'L')}). "
            "Values typed in by hand, or pasted from a different source, look like this. Often harmless (a batch "
            "measured another way), but worth asking where those rows came from.", 0.4,
            [c.loc(s0, s1 + 1)], data={"rows": [c.idx[s0] + 1, c.idx[s1] + 1], "short": n_short,
                                       "long": n_long, "values": c.raw[s0:s0 + min(s1 + 1 - s0, 10)]}))
    return out[:5]


def _on_grid(vals: np.ndarray, dec: int) -> bool:
    """True when the values are (nearly all) whole multiples of one step coarser than the printing grid: unit
    conversions of whole numbers (pounds to kg: steps of 0.4536) share decimals by arithmetic, not by copying."""
    u = np.unique(vals)
    if len(u) < 3:
        return True
    step = np.min(np.diff(u))
    for m in range(1, 13):
        q = step / m
        if q < 10 * 10.0 ** -dec:
            break
        x = (u - u[0]) / q
        if np.mean(np.abs(x - np.round(x)) < 1e-6 * max(1.0, np.abs(x).max())) >= 0.9:
            return True
    return False


def _trailing_repeats(cols: list[Col]) -> list[Finding]:
    """Values whose whole decimal part is identical while the part before the point differs (12.4837 and
    15.4837): what copying a number and editing its leading digits leaves.  By chance two values with d decimals
    share all of them about once in 9 x 10^(d-1) pairs.  Only the full decimal part is compared: computed values
    (ratios, means) have periodic decimal tails, so their last few digits repeat for arithmetic reasons."""
    out = []
    for c in cols:
        if _derived(c):
            continue
        raw = [r for r in c.raw if "e" not in r.lower() and "." in r]
        if len(raw) < 30:
            continue
        decs = Counter(_decimals(r) for r in raw)
        d, k = decs.most_common(1)[0]
        if not 3 <= d <= 8 or k < 30:
            continue
        sel = [r for r in raw if _decimals(r) == d]
        vals = np.array([float(r) for r in sel])
        if _on_grid(vals, d):
            continue
        # fractions with a small denominator end the same way (x/3 -> .3333, x/8 -> .1250)
        frac = np.abs(vals) % 1
        small = np.zeros(len(vals), bool)
        for den in range(2, 17):
            small |= np.abs(frac * den - np.round(frac * den)) < 10.0 ** -(d - 1)
        sel = [r for r, s in zip(sel, small) if not s]
        if len(sel) < 30:
            continue
        whole = np.array([int(abs(float(r))) for r in sel])
        if np.ptp(whole) < 2:
            continue                              # the part before the point hardly varies
        groups: dict[str, set[str]] = defaultdict(set)
        for r in sel:
            groups[r.split(".")[1]].add(r)
        n = len(set(sel))
        observed = sum(len(g) * (len(g) - 1) // 2 for g in groups.values())
        expected = n * (n - 1) / 2 / (9 * 10 ** (d - 1))   # the last digit is never 0 (trailing zeros are dropped)
        if observed < 5 or observed < 3 * expected or st.poisson.sf(observed - 1, max(expected, 1e-9)) > 1e-6:
            continue
        top = sorted((g for g in groups.values() if len(g) > 1), key=len, reverse=True)[:3]
        ex = "; ".join(", ".join(sorted(g)[:3]) for g in top)
        out.append(Finding(
            "data.trailing_repeats", "low", "Values share their decimal digits more than chance allows",
            f"In {c.label}, {observed} pairs of different values have exactly the same {d} decimal digits (e.g. "
            f"{ex}); about {expected:.2f} would be expected by chance. Copying numbers and then changing the digits "
            "before the decimal point leaves this trace. Weak on its own; look at the rows involved.", 0.4,
            [c.loc()], data={"observed_pairs": observed, "expected_pairs": round(expected, 3),
                             "examples": [sorted(g)[:4] for g in top]}))
    return out[:5]


BENFORD = np.log10(1 + 1 / np.arange(1, 10))
BENFORD_SKIP_H = re.compile(r"\bid\b|index|position|start|end|coord|chr|year|date|time|rank|accession|"
                            r"p[\s_.-]?val|\bp\b|\bq\b|fdr|padj|adj|%|percent|ratio|fold|log|z[\s_-]?score", re.I)


def _benford(cols: list[Col]) -> list[Finding]:
    """First digits against Benford's law, only where the law can apply: 300+ positive values spread over at least
    two orders of magnitude (counts, concentrations, intensities).  Bounded scales, p-values, ratios, logs and
    identifiers are skipped.  Many real data sets bend the law a little, so only a large deviation is reported."""
    out = []
    for c in cols:
        if BENFORD_SKIP_H.search(c.header or ""):
            continue
        v = c.vals[c.vals != 0]
        if len(v) < 300 or np.mean(v > 0) < 0.95:
            continue
        v = v[v > 0]
        lo, hi = np.percentile(v, [5, 95])
        if lo <= 0 or hi / lo < 100 or len(np.unique(v)) < 0.5 * len(v):
            continue
        d = np.diff(np.sort(v))
        if np.mean(np.isclose(d, 1)) > 0.5:
            continue                              # running numbers
        first = (v / 10.0 ** np.floor(np.log10(v))).astype(int).clip(1, 9)
        obs = np.bincount(first, minlength=10)[1:10].astype(float)
        exp = BENFORD * obs.sum()
        chi, p = st.chisquare(obs, exp)
        mad = float(np.mean(np.abs(obs / obs.sum() - BENFORD)))
        if p < 1e-6 and mad > 0.022:
            out.append(Finding(
                "data.benford", "low", "First digits far from Benford's law",
                f"The leading digits of {len(v)} values in {c.label}, which span {np.log10(hi / lo):.1f} orders of "
                f"magnitude, differ clearly from the pattern such data usually follow (mean deviation {mad:.3f}; "
                f"χ² = {chi:.0f}, p = {p:.1e}). Many genuine data sets do not follow Benford's law, so treat this as a "
                "reason to look at the values, not as evidence.", 0.3, [c.loc()],
                data={"observed": (obs / obs.sum()).round(4).tolist(), "expected": BENFORD.round(4).tolist(),
                      "mad": round(mad, 4), "p": p}))
    return out[:3]


# ---- supplementary file metadata ------------------------------------------------------------------------------------

def _file_metas(tables: list[Table], files: Optional[Iterable]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    seen = set()
    for f in files or []:
        path, name = (f if isinstance(f, (tuple, list)) else (f, None))
        if path is None:
            continue
        path = Path(path)
        name = name or path.name
        if name in seen:
            continue
        meta = file_meta(path)
        if meta.get("created") or meta.get("modified") or meta.get("app"):
            seen.add(name)
            out.append((name, meta))
    for t in tables:
        if t.file and t.file not in seen and (t.meta.get("created") or t.meta.get("app")):
            seen.add(t.file)
            out.append((t.file, t.meta))
    return out


MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                          "dec"))}


def _day(s, latest: bool = False) -> Optional[date]:
    """A date from '2021-03-04', '2021-03-04T10:00:00Z', '2021-03' / '2021' (first or last day when `latest`),
    '4 March 2021' or 'March 4, 2021'."""
    if isinstance(s, date):
        return s
    s = str(s or "").strip()
    try:
        if re.match(r"^\d{8}", s):                         # 20210304 (PDF style)
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        m = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?(?!\d)", s)
        if m:
            y, mo, d = int(m.group(1)), m.group(2), m.group(3)
            if mo and d:
                return date(y, int(mo), int(d))
            if mo:
                mo = int(mo)
                return (date(y + mo // 12, mo % 12 + 1, 1) - timedelta(days=1)) if latest else date(y, mo, 1)
            return date(y, 12, 31) if latest else date(y, 1, 1)
        m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", s) or \
            re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)
        if m:
            a, b, y = m.groups()
            day, mon = (a, b) if a.isdigit() else (b, a)
            return date(int(y), MONTHS[mon.lower()], int(day))
    except (ValueError, KeyError):
        return None
    return None


def _file_timeline(metas: list[tuple[str, dict]], dates: dict) -> list[Finding]:
    """Each supplementary file's own record of when it was made and saved, against the paper's dates.  Source data
    are routinely prepared during revision or at acceptance, so only dates well after publication are flagged."""
    if not metas:
        return []
    rec = {k: _day(dates.get(k), latest=k in ("published", "issued")) for k in ("received", "accepted", "published",
                                                                               "issued")}
    published = rec["published"] or rec["issued"]
    if not any(rec.values()):
        return []
    rows, late, apps = [], [], set()
    for name, m in metas:
        created, modified = _day(m.get("created")), _day(m.get("modified"))
        app = (m.get("app") or "").strip()
        if app:
            apps.add(re.sub(r"\s*[\d.]+$", "", app).strip().lower())
        notes = []
        if created and rec["received"] and created > rec["received"]:
            notes.append("made after submission")
        if created and rec["accepted"] and created > rec["accepted"]:
            notes = ["made after acceptance"]
        if published:
            # publishers and archives often re-render PDF supplements, so a PDF needs a much later date
            grace = 365 if m.get("kind") == "pdf" else 30
            if created and created > published + timedelta(days=grace):
                notes = ["made after publication"]
                late.append((name, "created", created))
            elif modified and m.get("kind") != "pdf" and modified > published + timedelta(days=90):
                notes.append("saved again after publication")
                late.append((name, "last saved", modified))
        rows.append({"file": name, "app": app, "created": created.isoformat() if created else "",
                     "modified": modified.isoformat() if modified else "", "note": ", ".join(notes)})
    out = []
    for name, what, d in late[:5]:
        out.append(Finding(
            "data.file_dates", "low", f"{name} was {what} after the paper was published",
            f"The file records that it was {what} on {d.isoformat()}, well after the paper appeared "
            f"({published.isoformat()}). A data file made after publication was not the one the reviewers saw. Often "
            "innocent (a correction, or a file re-exported for the journal), but worth asking which version is the "
            "original.", 0.4, [Location(file=name)], data={"date": d.isoformat(), "published": published.isoformat()}))
    shown = "; ".join(f"{r['file']}: {r['app'] or 'unknown program'}"
                      + (f", made {r['created']}" if r["created"] else "")
                      + (f", last saved {r['modified']}" if r["modified"] else "")
                      + (f" ({r['note']})" if r["note"] else "") for r in rows[:8])
    out.append(Finding(
        "data.file_metadata", "info", "When and with what the supplementary files were made",
        f"{shown}. Files made during revision or at acceptance are normal (journals ask for source data late)."
        + (f" The files were made with {len(apps)} different programs." if len(apps) > 1 else ""), 0.5,
        [Location(file=r["file"]) for r in rows[:8]],
        data={"files": rows[:40], "paper_dates": {k: v.isoformat() for k, v in rec.items() if v}}))
    return out
