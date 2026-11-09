"""Numbers that cannot be right.

- recompute: a test statistic with its degrees of freedom fixes the p-value; the reported one must agree (the idea
  behind statcheck, Nuijten et al. 2016).
- grim: a mean of n whole numbers can only take certain values (Brown & Heathers 2017).
- percent: "12 of 40 (35%)" - 12/40 is 30%.
- repeated_se: the same SD/SE after every mean in a table; honest data almost never repeats its spread exactly
  (Sholto David's tell for fabricated tables).
- impossible: p > 1, p = 0.000, negative SDs.
- grimmer: the SD of n whole numbers is as constrained as their mean (Anaya 2016).
- ci_p / ci_estimate: a 95% CI fixes which side of 0.05 the p falls, and must contain its estimate
  (Altman & Bland 2011).
- df_n: degrees of freedom larger than the stated sample sizes allow.
- sd_sem: a spread labelled SD whose CI only fits an SEM, or the reverse.
- out_of_range: |r| > 1, R², Cronbach's alpha or ROC AUC above 1.
- carlisle (check_tables): baseline rows of a randomised trial that are too alike, or too different, to come from
  random allocation (Carlisle 2017).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from scipy import stats as st

from .models import Finding, Location, Paper
from .tables import Table

NUM = r"(-?\d*\.?\d+)"
# exponent forms first, and a terminator, so "p = 1.5 × 10-5" is not read as "p = 1"
EXP = r"\s?(?:[eE]\s?|[×xX*·]\s?10\s?\^?\s?)[-−–]?\s?\d+"
PV = r"[pP]\s*([<>=≤≥]|&lt;|&gt;)\s*(\d(?:\.\d+)?" + EXP + r"|0?\.\d+|1(?:\.0+)?)(?!\.?\d)"
EXP_RE = re.compile(r"(\d(?:\.\d+)?)\s?(?:[eE]\s?|[×xX*·]\s?10\s?\^?\s?)[-−–]?\s?(\d+)$")
ONE_TAILED = re.compile(r"one[- ]?(?:tailed|sided)|directional", re.I)
DASH = r"\s*[,;]?\s*"
# the same p-value pattern with named groups, for the many-format test patterns below
PVN = (r"(?<![A-Za-z])[pP]\s*(?P<op>[<>=≤≥]|&lt;|&gt;)\s*(?P<p>\d(?:\.\d+)?" + EXP +
       r"|0?\.\d+|1(?:\.0+)?)(?!\.?\d)")
_V = r"(?P<v>-?\d*\.?\d+)"
_DF = r"(?P<df>\d+(?:\.\d+)?)"
_DF12 = r"(?P<df1>\d+)\s*[,/;]\s*(?P<df2>\d+(?:\.\d+)?)"
_O, _C = r"\s*[(\[]\s*", r"\s*[)\]]\s*"
_DFEQ = r"(?:df|d\.f\.)\s*=\s*"
_CHI = r"(?<![A-Za-z])(?:χ\s?2|χ²|Χ\s?2|Χ²|[Cc]hi\s?2|[Cc]hi²|[Cc]hi[- ]squared?|X2)"
# between the statistic and its p: "t(28) = 2.20, p", "; p", " (p", ", and p"
_SEP = r"\s*[,;]?\s*(?:and\s+)?\(?\s*"
# Every common way of writing t, F, r, chi-square and z with its degrees of freedom, as (test, form, pattern).
# Groups: v = statistic, df / df1 + df2 = degrees of freedom, n = sample size (r only: df = n - 2).
TEST_FORMS = [
    ("t", "t(df)", rf"\bt{_O}(?:{_DFEQ})?{_DF}{_C}=\s*{_V}"),
    ("t", "t, df", rf"\bt\s*=\s*{_V}\s*[,;]?\s*\(?\s*{_DFEQ}{_DF}\s*\)?"),
    ("F", "F(df1, df2)", rf"\bF{_O}(?:{_DFEQ})?{_DF12}{_C}=\s*{_V}"),
    ("F", "F, df", rf"\bF\s*=\s*{_V}\s*[,;]?\s*\(?\s*{_DFEQ}(?P<df1>\d+)\s*[,/]\s*(?P<df2>\d+(?:\.\d+)?)\s*\)?"),
    # subscripts lost in PDF extraction: "F1,45 = 4.10"
    ("F", "F df1,df2", rf"\bF\s?(?P<df1>\d{{1,2}})\s?,\s?(?P<df2>\d{{1,5}})\s*=\s*{_V}"),
    ("r", "r(df)", rf"\br{_O}(?:{_DFEQ})?{_DF}{_C}=\s*{_V}"),
    ("r", "r, df", rf"\br\s*=\s*{_V}\s*[,;]?\s*\(?\s*{_DFEQ}{_DF}\s*\)?"),
    ("r", "r, n", rf"\br\s*=\s*{_V}\s*[,;]\s*[nN]\s*=\s*(?P<n>\d{{1,6}})"),
    ("chi2", "chi2(df)", rf"{_CHI}{_O}(?:{_DFEQ})?{_DF}(?:\s*,\s*[nN]\s*=\s*\d+)?{_C}=\s*{_V}"),
    ("chi2", "chi2, df", rf"{_CHI}\s*=\s*{_V}\s*[,;]?\s*\(?\s*{_DFEQ}{_DF}\s*\)?"),
    ("z", "z", rf"\b[zZ]\s*=\s*{_V}"),
]
TEST_RES = [(test, form, re.compile(rx + _SEP + PVN)) for test, form, rx in TEST_FORMS]
# kept for callers that iterate the original five patterns
TESTS = [
    ("t", re.compile(r"\bt\s*\(\s*(\d+(?:\.\d+)?)\s*\)\s*=\s*" + NUM + DASH + PV)),
    ("F", re.compile(r"\bF\s*\(\s*(\d+)\s*,\s*(\d+(?:\.\d+)?)\s*\)\s*=\s*" + NUM + DASH + PV)),
    ("r", re.compile(r"\br\s*\(\s*(\d+)\s*\)\s*=\s*" + NUM + DASH + PV)),
    ("chi2", re.compile(r"(?:χ2|χ²|chi2|chi-square|X2)\s*\(\s*(\d+)(?:\s*,\s*[nN]\s*=\s*\d+)?\s*\)\s*=\s*" + NUM + DASH + PV)),
    ("z", re.compile(r"\b[zZ]\s*=\s*" + NUM + DASH + PV)),
]
SPEARMAN = re.compile(r"spearman|\brho\b|ρ|\br\s?s\b|kendall|τ", re.I)


def tests_in(text: str) -> list[dict]:
    """Every reported test with its p-value, in reading order.  `groups` is in the order `_p` expects:
    t, r, chi2: (df, value); F: (df1, df2, value); z: (value,)."""
    seen: set[int] = set()
    out = []
    for test, form, rx in TEST_RES:
        for m in rx.finditer(text):
            if m.start("p") in seen:
                continue
            d = m.groupdict()
            if test == "F":
                groups = (d["df1"], d["df2"], d["v"])
            elif test == "z":
                groups = (d["v"],)
            elif form == "r, n":
                n = int(d["n"])
                # a rank correlation's p is not the t-approximation's at small n
                if n < 4 or SPEARMAN.search(text[max(0, m.start() - 60):m.start() + 3]):
                    continue
                groups = (str(n - 2), d["v"])
            else:
                groups = (d["df"], d["v"])
            seen.add(m.start("p"))
            out.append({"test": test, "form": form, "groups": groups, "op": d["op"], "p": d["p"],
                        "start": m.start(), "end": m.end(), "text": m.group(0).rstrip(" ,;(")})
    out.sort(key=lambda x: x["start"])
    return out


def _p(test: str, g: tuple) -> tuple[float, float] | None:
    """(computed two-tailed p, test value) from regex groups."""
    try:
        if test == "t":
            df, v = float(g[0]), float(g[1])
            return 2 * st.t.sf(abs(v), df), v
        if test == "F":
            d1, d2, v = float(g[0]), float(g[1]), float(g[2])
            return st.f.sf(v, d1, d2), v
        if test == "r":
            df, v = float(g[0]), float(g[1])
            if abs(v) >= 1:
                return None
            t = v * math.sqrt(df / (1 - v * v))
            return 2 * st.t.sf(abs(t), df), v
        if test == "chi2":
            df, v = float(g[0]), float(g[1])
            return st.chi2.sf(v, df), v
        if test == "z":
            v = float(g[0])
            return 2 * st.norm.sf(abs(v)), v
    except (ValueError, OverflowError):
        return None
    return None


def _decimals(s: str) -> int:
    return len(s.split(".")[1]) if "." in s else 0


def _pvalue(s: str) -> tuple[float, float] | None:
    """(value, half a unit of its last printed digit) of a reported p: '.036', '1e-5', '1.5 × 10-5'."""
    m = EXP_RE.match(s.strip())
    if m:
        # "10 5" is a superscript minus lost in extraction: a p-value is never 10^5
        e = -int(m[2])
        return float(m[1]) * 10.0 ** e, 0.5 * 10.0 ** (e - _decimals(m[1]))
    try:
        return float(s), 0.5 * 10 ** -_decimals(s)
    except ValueError:
        return None


def _consistent(op: str, rep: float, dp: float, comp: float, val_s: str, test: str, groups,
                scale: float = 1.0) -> bool:
    """Allow for rounding of both the test statistic and the p-value.  `scale` 0.5 tests a one-tailed reading."""
    op = {"&lt;": "<", "&gt;": ">", "≤": "<", "≥": ">"}.get(op, op)
    # range of p over the rounding interval of the reported statistic
    dv = 0.5 * 10 ** -_decimals(val_s)
    lo_hi = []
    for delta in (-dv, dv):
        g = list(groups)
        idx = {"t": 1, "F": 2, "r": 1, "chi2": 1, "z": 0}[test]
        g[idx] = str(float(g[idx]) + delta)
        r = _p(test, tuple(g))
        if r:
            lo_hi.append(r[0])
    pmin = min(lo_hi + [comp]) * scale
    pmax = max(lo_hi + [comp]) * scale
    if op == "<":
        return pmin < rep + 1e-12
    if op == ">":
        return pmax > rep - 1e-12
    return pmin <= rep + dp + 1e-12 and pmax >= rep - dp - 1e-12


def recompute(text: str) -> list[dict]:
    out = []
    for x in tests_in(text):
        test, stat_groups, op, rep_s = x["test"], x["groups"], x["op"], x["p"]
        r = _p(test, stat_groups)
        if r is None:
            continue
        comp, _ = r
        pv = _pvalue(rep_s)
        if pv is None:
            continue
        rep, dp = pv
        val_s = stat_groups[-1]
        if _consistent(op, rep, dp, comp, val_s, test, stat_groups):
            continue
        # a stated one-tailed test halves the p of t, z and r
        if test in ("t", "z", "r") and ONE_TAILED.search(text[max(0, x["start"] - 150):x["end"] + 150]) and \
                _consistent(op, rep, dp, comp, val_s, test, stat_groups, scale=0.5):
            continue
        opn = {"&lt;": "<", "&gt;": ">", "≤": "<", "≥": ">"}.get(op, op)
        # "F(3, 12) = 15.15, p < 0.0002" when p is 0.00022: a threshold rounded at the far end of the scale, which
        # says nothing about the result; not worth a reader's time
        if opn == "<" and rep < 0.001 and comp < 1.25 * rep:
            continue
        # "p < .05" claims significance just as "p = .03" does
        rep_sig = rep <= 0.05 if opn == "<" else rep < 0.05 if opn == "=" else False
        decision = rep_sig != (comp < 0.05)
        out.append({"test": test, "text": x["text"], "reported": f"p {opn} {rep_s}", "computed": comp,
                    "decision": decision, "start": x["start"]})
    return out


# ---- the sentence around a hit ------------------------------------------------------------------------------------

SENT_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _sentence(text: str, start: int, end: int, reach: int = 320) -> str:
    """The sentence holding text[start:end] (whitespace-collapsed text)."""
    a, b = max(0, start - reach), min(len(text), end + reach)
    s, e = a, b
    for m in SENT_END.finditer(text, a, b):
        if m.end() <= start:
            s = m.end()
        elif m.start() >= end:
            e = m.start()
            break
    return text[s:e]


def _half(s: str) -> float:
    """Half a unit of the last printed digit: the rounding slack of a reported number."""
    return 0.5 * 10 ** -_decimals(s)


# ---- degrees of freedom against the stated n ----------------------------------------------------------------------

# Sentences where the df legitimately exceed the number of animals or people: repeated measures, cells counted as
# observations, models, and post-hoc tests that borrow the pooled error term of a larger ANOVA.
DF_EXCUSE = re.compile(
    r"\beach\b|\bper\b|cells|neurons|fields|images|wells|plates|slices|sections|measurements|observations|trials|"
    r"sessions|replicates?|experiments|technical|samples|"
    r"repeated|within|mixed|model|regression|GEE|multilevel|hierarchical|nested|longitudinal|time ?points|"
    r"post[- ]?hoc|Tukey|Bonferroni|Dunnett|Sidak|Šidák|Holm|LSD|pooled|Satterthwaite|Kenward|weighted|imput", re.I)
N_RE = re.compile(r"(?<![A-Za-z])([nN])\s?(?:[12](?![\d.]))?\s*=\s*(\d{1,6})(?![\d.,]\d)")


def df_vs_n(text: str) -> list[dict]:
    """Degrees of freedom larger than any test on the stated sample sizes allows.

    Only flags what no design can explain: a t-test on two groups of n1 and n2 has at most n1 + n2 - 2 df; a
    between-groups ANOVA on N subjects has df1 + df2 <= N - 1; a correlation on n pairs has n - 2 df.  Fewer df than
    expected is never flagged (dropped animals are common and honest)."""
    out = []
    for x in tests_in(text):
        if x["test"] not in ("t", "F", "r") or x["form"] == "r, n":
            continue
        sent = _sentence(text, x["start"], x["end"])
        if DF_EXCUSE.search(sent):
            continue
        ns = [(c, int(v)) for c, v in N_RE.findall(sent)]
        if not ns or any(v < 2 for _, v in ns):
            continue
        small = [v for c, v in ns if c == "n"]
        big = [v for c, v in ns if c == "N"]
        g = [float(v) for v in x["groups"][:-1]]
        limit = None
        if x["test"] == "t":
            if len(small) == 2 and not big:
                limit, basis = small[0] + small[1] - 2, f"groups of n = {small[0]} and n = {small[1]}"
            elif len(big) == 1 and not small:
                limit, basis = big[0] - 1, f"N = {big[0]}"
            df = g[0]
        elif x["test"] == "F":
            df = g[0] + g[1]
            if len(big) == 1 and not small:
                limit, basis = big[0] - 1, f"N = {big[0]}"
            elif len(small) == int(g[0]) + 1 and not big:
                tot = sum(small)
                limit, basis = tot - 1, f"{len(small)} groups totalling {tot}"
        else:
            df = g[0]
            if len(ns) == 1:
                limit, basis = ns[0][1] - 2, f"{ns[0][0]} = {ns[0][1]}"
        if limit is None or limit < 1 or df <= limit + 1e-9:
            continue
        out.append({"test": x["test"], "text": x["text"], "df": df, "max_df": limit, "basis": basis,
                    "start": x["start"]})
    return out


# ---- confidence intervals against p-values and estimates -----------------------------------------------------------

_RATIO_LAB = (r"(?:(?i:adjusted |crude |unadjusted )?(?i:incidence rate ratio|odds ratio|hazard ratio|risk ratio|"
              r"relative risk|rate ratio)s?|a?ORs?|a?HRs?|a?RRs?|IRR|sHR)")
_DIFF_LAB = r"(?:(?i:standardi[sz]ed mean difference|mean difference|difference|regression coefficient)|MD|SMD|β|beta)"
_CI = r"95\s?%\s?(?:CI|C\.I\.|(?i:confidence interval))"
_RNG = r"(?P<lo>-?\d*\.?\d+)\s*(?:–|—|-|to|,|;)\s*(?P<hi>-?\d*\.?\d+)"
_N = r"(?P<est>-?\d*\.?\d+)"
CI_RES = [
    # "OR 2.10 (95% CI 1.20-3.60), p = 0.01", "mean difference was 3.2 (95% CI: 1.1 to 5.3; P = .003)"
    re.compile(rf"(?<![A-Za-z])(?P<lab>{_RATIO_LAB}|{_DIFF_LAB})\s*(?:of|was|were|is|=|:|,)?\s*{_N}\s*[,;]?\s*"
               rf"[(\[]?\s*{_CI}\s*[:=,]?\s*(?:of\s*)?[(\[]?\s*{_RNG}\s*[)\]]?[\s)\],;]{{0,6}}(?:and\s+)?{PVN}"),
    # "HR (95% CI) 0.75 (0.60-0.94), p = .01"
    re.compile(rf"(?<![A-Za-z])(?P<lab>{_RATIO_LAB}|{_DIFF_LAB})\s*\(\s*{_CI}\s*\)\s*[:=,]?\s*{_N}\s*[(\[]\s*{_RNG}"
               rf"\s*[)\]][\s,;]{{0,4}}{PVN}"),
]
RATIO_RE = re.compile(_RATIO_LAB + r"$")
Z975 = st.norm.ppf(0.975)


def _ci_p_range(est: str, lo: str, hi: str, ratio: bool) -> tuple[float, float] | None:
    """Smallest and largest two-sided p an estimate and its 95% CI allow (Altman & Bland 2011), over the rounding of
    all three numbers and over the estimate versus the centre of the interval."""
    ps = []
    for e in (float(est) - _half(est), float(est) + _half(est), None):
        for l in (float(lo) - _half(lo), float(lo) + _half(lo)):
            for h in (float(hi) - _half(hi), float(hi) + _half(hi)):
                if ratio:
                    l2, h2 = math.log(max(l, 1e-9)), math.log(max(h, 1e-9))
                    e2 = (l2 + h2) / 2 if e is None else math.log(max(e, 1e-9))
                else:
                    l2, h2 = l, h
                    e2 = (l2 + h2) / 2 if e is None else e
                se = (h2 - l2) / (2 * Z975)
                if se > 0:
                    ps.append(2 * st.norm.sf(abs(e2) / se))
    return (min(ps), max(ps)) if ps else None


def ci_checks(text: str) -> tuple[list[dict], list[dict]]:
    """(p-values that contradict their 95% CI, estimates that lie outside their own CI).

    A 95% CI that excludes the null value (0 for a difference, 1 for a ratio) means p < 0.05, and one that includes it
    means p > 0.05.  Different methods for the CI and the p can disagree near the line, so a contradiction is only
    reported when the interval puts p clearly on the other side (below 0.01 or above 0.10)."""
    conflicts, outside = [], []
    seen: set[int] = set()
    for rx in CI_RES:
        for m in rx.finditer(text):
            if m.start("p") in seen:
                continue
            seen.add(m.start("p"))
            lab, est_s, lo_s, hi_s = m["lab"], m["est"], m["lo"], m["hi"]
            est, lo, hi = float(est_s), float(lo_s), float(hi_s)
            ratio = bool(RATIO_RE.match(lab.strip()))
            if lo >= hi or (ratio and min(est, lo) <= 0):
                continue
            quote = m.group(0).rstrip(" ,;(")
            slack = max(_half(est_s), _half(lo_s), _half(hi_s))
            if est < lo - slack - 1e-12 or est > hi + slack + 1e-12:
                outside.append({"text": quote, "estimate": est_s, "ci": [lo_s, hi_s], "start": m.start()})
                continue
            pv = _pvalue(m["p"])
            rng = _ci_p_range(est_s, lo_s, hi_s, ratio)
            if pv is None or rng is None:
                continue
            rep, _ = pv
            op = {"&lt;": "<", "&gt;": ">", "≤": "<", "≥": ">"}.get(m["op"], m["op"])
            null = 1.0 if ratio else 0.0
            excludes = hi + _half(hi_s) < null or lo - _half(lo_s) > null
            includes = lo + _half(lo_s) < null < hi - _half(hi_s)
            says_sig = rep <= 0.05 if op in ("<", "=") else False
            says_ns = (op == "=" and rep > 0.05) or (op == ">" and rep >= 0.05)
            why = ""
            if says_sig and includes and rng[0] > 0.10:
                why = f"the 95% CI includes {null:g} (no effect), so p should be above 0.05"
            elif says_ns and excludes and rng[1] < 0.01:
                why = f"the 95% CI excludes {null:g} (no effect), so p should be below 0.05"
            if why:
                conflicts.append({"text": quote, "reported": f"p {op} {m['p']}", "ci_p": [float(rng[0]), float(rng[1])],
                                  "why": why, "ratio": ratio, "start": m.start()})
    return conflicts, outside


# ---- SD or SEM: does the spread fit its own confidence interval? ---------------------------------------------------

SPREAD_RE = re.compile(r"(?P<m>-?\d+(?:\.\d+)?)\s*(?:±|\+/-|\+-)\s*(?P<s>\d+(?:\.\d+)?)\s*[(\[,;]?\s*"
                       r"(?P<lab>SD|SEM|SE|S\.D\.|S\.E\.M\.|S\.E\.|s\.d\.|s\.e\.m\.|s\.e\.)(?![A-Za-z])")
CI_AFTER = re.compile(_CI + r"\s*[:=,]?\s*(?:of\s*)?[(\[]?\s*" + _RNG)


def _overlap(a: tuple[float, float], b: tuple[float, float], slack: float = 0.1) -> bool:
    return a[0] * (1 - slack) <= b[1] and b[0] <= a[1] * (1 + slack)


def sd_vs_sem(text: str) -> list[dict]:
    """A spread labelled SD whose 95% CI is only as wide as an SEM would give, or the reverse.

    For a mean of n values the 95% CI is mean ± t × SD/√n = mean ± t × SEM.  When the stated n, the spread and the
    CI agree only if the label is swapped, one of the labels is wrong.  Needs n >= 4 so that the two readings differ
    at least two-fold."""
    out = []
    for m in SPREAD_RE.finditer(text):
        sent = _sentence(text, m.start(), m.end())
        tail = text[m.end():m.end() + 140]
        c = CI_AFTER.search(tail)
        if not c or c.start() > 100:
            continue
        ns = {int(v) for k, v in N_RE.findall(sent) if k == "n"}
        if len(ns) != 1:
            continue
        n = ns.pop()
        if n < 4:
            continue
        mean_s, s_s, lo_s, hi_s = m["m"], m["s"], c["lo"], c["hi"]
        mean, s, lo, hi = float(mean_s), float(s_s), float(lo_s), float(hi_s)
        if s <= 0 or lo >= hi:
            continue
        h_ci = max(_half(lo_s), _half(hi_s))
        # the interval must be centred on this mean, or it belongs to something else
        if abs((lo + hi) / 2 - mean) > _half(mean_s) + h_ci + 0.02 * (hi - lo):
            continue
        half = (hi - lo) / 2
        tc = float(st.t.ppf(0.975, n - 1))
        implied = (max(half - h_ci, 0.0) / tc, (half + h_ci) / Z975)        # the SE the CI implies
        hs = _half(s_s)
        as_se = (max(s - hs, 0.0), s + hs)
        as_sd = (max(s - hs, 0.0) / math.sqrt(n), (s + hs) / math.sqrt(n))
        is_sd = m["lab"].upper().startswith("S.D") or m["lab"].upper() == "SD"
        stated, other = (as_sd, as_se) if is_sd else (as_se, as_sd)
        if _overlap(stated, implied) or not _overlap(other, implied):
            continue
        out.append({"text": " ".join(text[m.start():m.end() + c.end()].split()), "label": "SD" if is_sd else "SEM",
                    "fits": "SEM" if is_sd else "SD", "n": n, "start": m.start()})
    return out


# whole-number data only: multi-item scale and Likert averages are not means of n integers
GRIM_CTX = re.compile(r"(number of|count|lesions|colonies|score|per (?:mouse|animal|patient|rat|well)|"
                      r"rating|episodes|events)", re.I)
GRIM_RE = re.compile(r"(?:mean|average)[^.;()]{0,60}?\b(\d+\.\d{2})\b[^.;]{0,25}?\(\s*n\s*=\s*(\d{1,3})\s*\)", re.I)


def grim(text: str) -> list[dict]:
    out = []
    for m in GRIM_RE.finditer(text):
        ctx = text[max(0, m.start() - 80):m.end()]
        if not GRIM_CTX.search(ctx):
            continue
        mean_s, n = m.group(1), int(m.group(2))
        if n < 2 or n > 99:
            continue
        mean = float(mean_s)
        d = _decimals(mean_s)
        # within half a unit of the last digit: accepts half-up and half-even rounding of an exact tie (17/8 = 2.125)
        tol = 0.5 * 10 ** -d + 1e-9
        ks = (math.floor(mean * n), math.ceil(mean * n))
        if not any(abs(k / n - mean) <= tol for k in ks):
            k = min(ks, key=lambda k: abs(k / n - mean))
            closest = str((Decimal(k) / Decimal(n)).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP))
            out.append({"mean": mean_s, "n": n, "closest": closest, "text": " ".join(m.group(0).split()),
                        "start": m.start()})
    return out


# ---- GRIMMER: the SD of n whole numbers is just as constrained as their mean ------------------------------------

# "2.50 ± 1.07 (n = 8)", "2.50 ± 1.07 SD (n = 8)", "M = 2.50, SD = 1.07, n = 8"
GRIMMER_RES = [
    re.compile(r"(?P<m>\d+\.\d+)\s*(?:±|\+/-|\+-)\s*(?P<s>\d+\.\d+)\s*(?:\(?\s*(?:SD|S\.D\.|s\.d\.)\s*\)?\s*)?"
               r"[^.;±]{0,25}?\(\s*n\s*=\s*(?P<n>\d{1,3})\s*\)"),
    re.compile(r"\bM\s*=\s*(?P<m>\d+\.\d+)\s*,\s*SD\s*=\s*(?P<s>\d+\.\d+)\s*,\s*n\s*=\s*(?P<n>\d{1,3})\b"),
]
SD_WORD = re.compile(r"\bSDs?\b|S\.D\.|s\.d\.|standard deviation", re.I)
SE_WORD = re.compile(r"\bSEM?s?\b|S\.E\.|s\.e\.|standard error|\bCI\b|confidence", re.I)
# averages of several items or of transformed values are not means of whole numbers
NOT_WHOLE = re.compile(r"\bitems?\b|subscale|likert|averaged|composite|index|normali[sz]ed|log|ratio|percent|%|"
                       r"per (?:day|week|month|year|hour|field|cm|mm|µm|um)|rate|VAS|visual analog|raters|judges", re.I)


def _grimmer_possible(mean_s: str, sd_s: str, n: int) -> bool | None:
    """Can n whole numbers have this mean and this SD, both as rounded?  None when the mean itself fails GRIM.

    Necessary conditions only (Anaya 2016): the sum of squares S is a whole number, S has the same parity as the sum
    (x² and x are both odd or both even), and S is at least what the most even split of the sum gives.  Failing any
    of them proves impossibility; passing them all does not prove the data exist, so this never over-reports.  Both
    the n - 1 (sample) and the n (population) SD are allowed."""
    mean, h = Fraction(mean_s), Fraction(1, 2 * 10 ** _decimals(mean_s))
    sd, hs = Fraction(sd_s), Fraction(1, 2 * 10 ** _decimals(sd_s))
    sums = [k for k in range(math.floor((mean - h) * n), math.ceil((mean + h) * n) + 1)
            if abs(Fraction(k, n) - mean) <= h]
    if not sums:
        return None
    lo_sd, hi_sd = max(sd - hs, Fraction(0)), sd + hs
    for k in sums:
        q, r = divmod(k, n)
        s_min = r * (q + 1) ** 2 + (n - r) * q ** 2
        for denom in (n - 1, n):
            a = max(math.ceil(denom * lo_sd ** 2 + Fraction(k * k, n)), s_min)
            b = math.floor(denom * hi_sd ** 2 + Fraction(k * k, n))
            if b > a or (b == a and (a - k) % 2 == 0):
                return True
    return False


def grimmer(text: str) -> list[dict]:
    out = []
    seen: set[int] = set()
    for rx in GRIMMER_RES:
        for m in rx.finditer(text):
            if m.start() in seen:
                continue
            seen.add(m.start())
            mean_s, sd_s, n = m["m"], m["s"], int(m["n"])
            if not 2 <= n <= 99 or _decimals(sd_s) < 2 or float(sd_s) <= 0:
                continue
            before = text[max(0, m.start() - 100):m.start()]
            ctx = before + m.group(0)
            # whole-number data, the spread stated to be an SD, and nothing that makes it an average of averages
            if not GRIM_CTX.search(ctx) or NOT_WHOLE.search(ctx):
                continue
            # the spread is named in its own sentence ("mean ± SD") and no SE or CI there competes with it
            around = _sentence(text, m.start(), m.end())
            if not SD_WORD.search(around) or SE_WORD.search(around):
                continue
            if _grimmer_possible(mean_s, sd_s, n) is False:
                out.append({"mean": mean_s, "sd": sd_s, "n": n, "text": " ".join(m.group(0).split()),
                            "start": m.start()})
    return out


# each word is matched whole ((?![a-z])), so a long run of letters cannot be split into words in O(n³) ways
PCT_RE = re.compile(r"\b(\d{1,5})\s*(?:/|of|out of)\s*(\d{1,5})\s*(?:[a-z]+(?![a-z])\s*){0,3}"
                    r"\(\s*(\d{1,3}(?:\.\d+)?)\s*%\s*\)", re.I)


def percent(text: str) -> list[dict]:
    out = []
    for m in PCT_RE.finditer(text):
        k, n, p_s = int(m.group(1)), int(m.group(2)), m.group(3)
        if n == 0 or k > n:
            continue
        d = _decimals(p_s)
        true = 100.0 * k / n
        tol = 0.5 * 10 ** -d + 1e-9
        # some authors truncate instead of rounding: accept either
        if abs(true - float(p_s)) <= tol or abs(math.floor(true * 10 ** d) / 10 ** d - float(p_s)) < 1e-9:
            continue
        out.append({"k": k, "n": n, "reported": p_s, "true": round(true, 2), "text": " ".join(m.group(0).split()),
                    "start": m.start()})
    return out


PM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:±|\+/-|\+-)\s*(\d+(?:\.\d+)?)")
# a setting with its tolerance ("pH 7.40 ± 0.05", "37 ± 0.5 °C", "10 ± 2 ppm") is not a measured mean with its SD
SPEC_BEFORE = re.compile(r"\b(?:pH|at|to|set to|adjusted to|tolerance|temperature)\s*[:=]?\s*$", re.I)
SPEC_AFTER = re.compile(r"^\s*(?:°\s?[CF]|º\s?C|℃|rpm|[x×]\s?g\b|ppm|k?Da\b|k?V\b|[kM]?Hz\b)")


def _nominal(means: set[str]) -> bool:
    """Round settings in equal steps (doses 100, 200, 300, 400): planned levels, not measured group means."""
    v = sorted(float(m) for m in means)
    d = [b - a for a, b in zip(v, v[1:])]
    return all(x.is_integer() for x in v) and all(abs(x - d[0]) < 1e-9 for x in d)


def repeated_spread(text: str) -> list[dict]:
    pairs = [(m.group(1), m.group(2), m.start()) for m in PM_RE.finditer(text)
             if not SPEC_BEFORE.search(text[max(0, m.start() - 16):m.start()])
             and not SPEC_AFTER.match(text[m.end():m.end() + 8])]
    if len(pairs) < 4:
        return []
    out = []
    # within any window of 12 consecutive "mean ± spread" values
    seen_spans = set()
    for i in range(0, max(1, len(pairs) - 3)):
        win = pairs[i:i + 12]
        c = Counter(s for _, s, _ in win if _decimals(s) >= 2 or float(s) >= 10)
        for spread, cnt in c.items():
            means = {m for m, s, _ in win if s == spread}
            if cnt >= 4 and len(means) >= 4 and spread not in seen_spans and not _nominal(means):
                seen_spans.add(spread)
                out.append({"spread": spread, "count": cnt, "means": sorted(means)[:8], "start": win[0][2]})
    return out


# p above 1 or exactly 0; not a mantissa ("p = 2.3e-5"), a percentage, a pressure, or a passage number ("P = 3")
IMPOSSIBLE_RE = re.compile(r"\b[pP]\s*=\s*(0?\.0+|[1-9]\d*\.\d*[1-9]\d*)(?!\.?\d)"
                           r"(?!\s?(?:[eE]\s?[-−–+]?\d|[×xX*·]\s?10))"
                           r"(?!\s?(?:%|[kKM]?Pa\b|atm\b|bar\b|psi\b|mm\s?Hg|torr\b))")


def impossible(text: str) -> list[dict]:
    out = []
    for m in IMPOSSIBLE_RE.finditer(text):
        v = float(m.group(1))
        if v == 0 or v > 1:
            out.append({"text": m.group(0), "value": m.group(1), "start": m.start()})
    return out


# ---- values outside the range their statistic allows ----------------------------------------------------------------

_UNIT_AFTER = r"(?!\.?\d)(?!\s?(?:%|[cmµun]?m\b|Å|k?Da\b|[kM]?Hz\b|°|mg|kg|g\b|s\b|h\b|min\b|[kM]?Ω|ohm))"
RANGE_RES = [
    # a correlation coefficient, recognised by its test or by the word correlation just before it
    ("correlation", 1.0, re.compile(r"(?<![A-Za-z])r\s*\(\s*\d+\s*\)\s*=\s*(?P<v>-?\d*\.\d+)" + _UNIT_AFTER)),
    ("correlation", 1.0, re.compile(r"(?i:correlat\w*|pearson'?s?|spearman'?s?)[^.;]{0,40}?(?<![A-Za-z])"
                                    r"(?:r|rho|ρ|r\s?s)\s*=\s*(?P<v>-?\d*\.\d+)" + _UNIT_AFTER)),
    ("R²", 1.0, re.compile(r"(?<![A-Za-z])R\s?(?:2|²)\s*=\s*(?P<v>\d\.\d+)" + _UNIT_AFTER)),
    ("Cronbach's alpha", 1.0, re.compile(r"(?i:cronbach'?s?)\s*(?:α|alpha|(?i:alpha))\s*(?:=|of|was)?\s*"
                                         r"(?P<v>\d*\.\d+)" + _UNIT_AFTER)),
    ("ROC AUC", 1.0, re.compile(r"(?i:ROC)[^.;]{0,60}?\b(?:AUC|AUROC|area under the curve)\s*(?:=|of|was|:)?\s*"
                                r"(?P<v>\d*\.\d+)" + _UNIT_AFTER)),
]


FIT_CTX = re.compile(r"regress|fit|model|linear|explain|variance|determination|correlat", re.I)


def out_of_range(text: str) -> list[dict]:
    """A correlation beyond ±1, R² or Cronbach's alpha above 1, an ROC AUC above 1.  (Adjusted R² and alpha can be
    negative, so only the top of the range is checked for them.)"""
    out = []
    for what, top, rx in RANGE_RES:
        for m in rx.finditer(text):
            v = float(m["v"])
            if what == "R²" and not FIT_CTX.search(_sentence(text, m.start(), m.end())):
                continue
            if abs(v) > top + 1e-9 if what == "correlation" else v > top + 1e-9:
                out.append({"what": what, "value": m["v"], "text": " ".join(m.group(0).split())[-80:],
                            "start": m.start("v")})
    return out


# ---- Carlisle: baseline balance in randomised trials --------------------------------------------------------------

BASELINE_RE = re.compile(r"baseline|demographic|characteristics of (?:the )?(?:study )?(?:participants|patients|"
                         r"subjects|population|children|women|men)|(?:patient|participant|subject)s?'? characteristics",
                         re.I)
RANDOMISED_RE = re.compile(r"randomi[sz](?:ed|ation)|randomly (?:assigned|allocated)", re.I)
# groups made alike on purpose, or compared because they differ: no randomisation to test
NOT_RANDOM_RE = re.compile(r"propensity|matched (?:cohort|controls?|pairs?|groups?)|case[- ]control|"
                           r"1\s?:\s?1 match|responders? (?:vs\.?|versus|and) non-?responders?", re.I)
CELL_PM = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:±|\+/-|\+-)\s*(\d+(?:\.\d+)?)\s*$")
CELL_PAREN = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*\(\s*(\d+(?:\.\d+)?)\s*\)\s*$")
HEAD_N = re.compile(r"(?<![A-Za-z])[nN]\s*=\s*(\d{1,6})(?!\d)")
TOTAL_COL = re.compile(r"total|overall|all (?:patients|participants|subjects)|combined|p[- ]?value|^\s*p\s*$", re.I)
SKIP_ROW = re.compile(r"median|iqr|range|%|\bno\.|(?<![A-Za-z])n\s*\(", re.I)
FORMAT_ONLY = re.compile(r"^\s*(?:mean|n|no\.?|median)?\s*(?:[±(\[]|\+/-)?\s*(?:sd|s\.d\.|sem|se|%|n|iqr)?\s*[)\]]?\s*$",
                         re.I)
SD_DECLARED = re.compile(r"mean\s*\(\s*SD\s*\)|\(\s*SD\s*\)|mean\s*\(\s*s\.d\.\s*\)|mean\s*±\s*SD|±\s*SD", re.I)
SEM_DECLARED = re.compile(r"\bSEM\b|\bS\.E\.M?\.|standard error", re.I)


def _anova_p(means: list[float], sds: list[float], ns: list[int]) -> float:
    """One-way ANOVA from summary statistics (equal to the pooled t-test for two groups)."""
    N, k = sum(ns), len(ns)
    grand = sum(n * m for n, m in zip(ns, means)) / N
    ssb = sum(n * (m - grand) ** 2 for n, m in zip(ns, means))
    ssw = sum((n - 1) * s * s for n, s in zip(ns, sds))
    if ssw <= 0:
        return 0.0 if ssb > 0 else 1.0
    f = (ssb / (k - 1)) / (ssw / (N - k))
    return float(st.f.sf(f, k - 1, N - k))


def _row_p_range(cells: list[tuple[str, str]], ns: list[int]) -> tuple[float, float]:
    """Smallest and largest p this row allows once every mean and SD is rounding-shifted the most favourable way:
    for the smallest p, means pushed apart and SDs shrunk; for the largest, means pulled together and SDs grown."""
    means = [float(m) for m, _ in cells]
    hm = [_half(m) for m, _ in cells]
    sds = [float(s) for _, s in cells]
    hs = [_half(s) for _, s in cells]
    grand = sum(n * m for n, m in zip(ns, means)) / sum(ns)
    apart, together = [], []
    for i, (m, h) in enumerate(zip(means, hm)):
        d = m - grand
        sign = (1 if d > 0 else -1) if abs(d) > 1e-12 else (1 if i % 2 == 0 else -1)
        apart.append(m + sign * h)
        together.append(grand if abs(d) <= h else m - math.copysign(h, d))
    pmin = _anova_p(apart, [max(s - h, 1e-9) for s, h in zip(sds, hs)], ns)
    pmax = _anova_p(together, [s + h for s, h in zip(sds, hs)], ns)
    return pmin, pmax


def _table_text(t: Table, rows: int = 4) -> str:
    return " ".join([t.name] + [" ".join(r) for r in t.rows[:rows]])


def baseline_balance(t: Table, context: str = "") -> dict | None:
    """Carlisle's test (Carlisle 2017) on one table of baseline means and SDs by randomised arm.

    Each row gives a p-value for the difference between arms; under randomisation these are uniform between 0 and 1.
    Stouffer's method combines them; arms far more alike than chance allows (combined p for similarity < 1 in 10,000)
    or far more different are both flagged.  Rounding is allowed for by giving every row the benefit of the doubt.
    Needs at least 8 continuous rows with the n of each arm in the header."""
    if len(t.rows) < 9 or not t.rows or max(len(r) for r in t.rows) < 3:
        return None
    alltext = " ".join(" ".join(r) for r in t.rows)
    if SEM_DECLARED.search(alltext) and not SD_DECLARED.search(alltext):
        return None
    paren_ok = bool(SD_DECLARED.search(alltext + " " + context[:4000]))
    # the n of each arm, from one header row; n in two header rows means groups nested in groups (time points over
    # arms) and is not a plain baseline table
    arm_n: dict[int, int] = {}
    n_row = -1
    for i, r in enumerate(t.rows[:4]):
        hits = {j: int(HEAD_N.search(c)[1]) for j, c in enumerate(r) if j > 0 and c and HEAD_N.search(c)}
        if hits:
            if arm_n:
                return None
            arm_n, n_row = hits, i
    if not arm_n:
        return None
    is_cell = lambda c: bool(CELL_PM.match(c or "") or CELL_PAREN.match(c or ""))  # noqa: E731
    first = next((i for i in range(n_row + 1, len(t.rows)) if any(is_cell(c) for c in t.rows[i][1:])), None)
    if first is None:
        return None
    left = min(arm_n)
    # header rows under the n row may only name the format ("Mean ± SD", "n (%)"), not split the arms further
    for i in range(n_row + 1, first):
        if any((c or "").strip() and not FORMAT_ONLY.match(c) for c in t.rows[i][left:]):
            return None
    # a "Total" heading may sit in the row above its "(n = 100)"
    total = {j for j in arm_n if any(j < len(t.rows[i]) and TOTAL_COL.search(t.rows[i][j] or "")
                                     for i in range(n_row + 1))}
    cols = sorted(j for j in arm_n if j not in total and arm_n[j] >= 2)
    if len(cols) < 2 or len(cols) > 6:
        return None
    width = max(len(r) for r in t.rows)
    edges = sorted(arm_n) + [width]
    # an arm heading may span several columns ("n (%)" and "Mean ± SD" under one "Drug (n = 44)")
    spans = {j: range(j, edges[edges.index(j) + 1]) for j in cols}
    ns = [arm_n[j] for j in cols]
    rows, seen = [], set()
    for i, r in enumerate(t.rows[first:], start=first):
        label = (r[0] if r else "").strip()
        if SKIP_ROW.search(label):
            continue
        cells = []
        for j in cols:
            got = None
            for jj in spans[j]:
                c = r[jj] if jj < len(r) else ""
                m = CELL_PM.match(c or "")
                if not m and paren_ok:
                    m = CELL_PAREN.match(c or "")
                    # "23 (46.0)" in a paren table can still be a count with its percentage
                    if m and abs(100.0 * float(m[1]) / arm_n[j] - float(m[2])) <= 0.6:
                        m = None
                if m:
                    got = m
                    break
            if not got or float(got[2]) <= 0:
                break
            cells.append((got[1], got[2]))
        if len(cells) != len(cols):
            continue
        key = tuple(cells)
        if key in seen:
            continue
        seen.add(key)
        pmin, pmax = _row_p_range(cells, ns)
        rows.append({"row": i, "label": label[:60], "cells": [f"{a} ± {b}" for a, b in cells],
                     "p_min": pmin, "p_max": pmax})
    if len(rows) < 8:
        return None
    k = len(rows)
    clamp = lambda p: min(max(p, 1e-15), 1 - 1e-15)  # noqa: E731
    z_sim = sum(st.norm.ppf(clamp(r["p_min"])) for r in rows) / math.sqrt(k)
    z_diff = sum(st.norm.ppf(clamp(r["p_max"])) for r in rows) / math.sqrt(k)
    p_similar, p_different = float(st.norm.sf(z_sim)), float(st.norm.cdf(z_diff))
    return {"table": t.name, "file": t.file, "rows": rows, "arms": ns, "p_similar": p_similar,
            "p_different": p_different, "page": t.meta.get("page", 0) if isinstance(t.meta, dict) else 0}


CARLISLE_P = 1e-4


def _is_baseline(t: Table, paper: Paper | None) -> bool:
    if BASELINE_RE.search(_table_text(t)):
        return True
    if paper is None:
        return False
    page = t.meta.get("page") if isinstance(t.meta, dict) else None
    if page and 0 < page <= len(paper.pages) and BASELINE_RE.search(paper.pages[page - 1]) and not t.file:
        return True
    lab = re.match(r"(?:Supplementary )?Table\s*S?\d+", t.name, re.I)
    if lab and not t.file:
        cap = re.search(re.escape(lab.group(0)) + r"\b[.:]?\s*[^.]{0,60}", paper.text, re.I)
        return bool(cap and BASELINE_RE.search(cap.group(0)))
    return False


def check_tables(tables: list[Table], paper: Paper | None = None) -> list[Finding]:
    """Table-level statistics checks (Carlisle baseline balance).  Runs only when the paper says it is randomised and
    does not describe matched or case-control groups."""
    text = paper.text if paper is not None else ""
    out: list[Finding] = []
    for t in tables:
        own = " ".join(" ".join(r) for r in t.rows[:60])
        ctx = text or own
        if not RANDOMISED_RE.search(ctx + " " + t.name) or NOT_RANDOM_RE.search(ctx + " " + own):
            continue
        if not _is_baseline(t, paper):
            continue
        res = baseline_balance(t, text)
        if res is None:
            continue
        side = "similar" if res["p_similar"] < CARLISLE_P else "different" if res["p_different"] < CARLISLE_P else ""
        if not side:
            continue
        p = res["p_similar"] if side == "similar" else res["p_different"]
        ex = res["rows"][:3]
        quote = "; ".join(f"{r['label'] or 'row ' + str(r['row'] + 1)}: {' vs '.join(r['cells'])}" for r in ex)
        where = f"{t.file}, {t.name}" if t.file else t.name
        if side == "similar":
            why = ("The arms are more alike than random allocation produces. Copied or invented rows are one cause; "
                   "stratified randomisation on these variables, or a table that was not of randomised arms, are "
                   "innocent ones.")
        else:
            why = ("The arms differ more than random allocation produces. Common innocent causes are SEs printed as "
                   "SDs, typing errors, or groups that were not formed by randomisation.")
        out.append(Finding(
            "stats.carlisle", "medium",
            f"Baseline groups are too {side} for a randomised trial",
            f"{where}: {len(res['rows'])} baseline rows compared across {len(res['arms'])} arms "
            f"(n = {', '.join(str(n) for n in res['arms'])}). The chance of rows this {side} by luck is "
            f"{_odds(p)} (Carlisle's method). {why} Examples: {quote}.",
            confidence=0.65 if p < 1e-6 else 0.55,
            locations=[Location(page=res["page"], file=t.file, sheet=t.name, quote=quote[:200])],
            data={"side": side, "combined_p": p, "arms": res["arms"],
                  "rows": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}
                           for r in res["rows"]]}))
    return out


def _page(paper: Paper, pos: int, text: str = "") -> int:
    """Page holding the text around `pos` in the whitespace-collapsed text."""
    snippet = text[pos:pos + 30]
    for i, p in enumerate(paper.pages):
        if snippet and snippet in " ".join(p.replace("\u2212", "-").split()):
            return i + 1
    return 0


def check(paper: Paper) -> list[Finding]:
    text = " ".join(paper.text.replace("−", "-").split())
    out: list[Finding] = []
    bad = recompute(text)
    if bad:
        dec = [b for b in bad if b["decision"]]
        sev = "high" if dec else "medium"
        out.append(Finding(
            "stats.recompute", sev, "Reported p-values do not match their test statistics",
            f"{len(bad)} test(s) where the p-value cannot follow from the reported statistic and degrees of "
            f"freedom{f'; {len(dec)} change the conclusion at p = 0.05' if dec else ''}. "
            + " ".join(f"\"{b['text']}\" gives p = {b['computed']:.3g}." for b in bad[:3]),
            confidence=0.85 if dec else 0.7,
            locations=[Location(page=_page(paper, b["start"], text), quote=b["text"]) for b in bad[:8]],
            data={"tests": [{k: v for k, v in b.items() if k != "start"} for b in bad]}))
    g = grim(text)
    if g:
        out.append(Finding(
            "stats.grim", "medium", "Means that are impossible for the sample size (GRIM)",
            f"{len(g)} mean(s) of whole-number data cannot come from the stated n: " +
            " ".join(f"{x['mean']} with n = {x['n']} (closest possible: {x['closest']})." for x in g[:3]),
            confidence=0.65, locations=[Location(page=_page(paper, x["start"], text), quote=x["text"]) for x in g[:6]],
            data={"items": [{k: v for k, v in x.items() if k != "start"} for x in g]}))
    pc = percent(text)
    if pc:
        out.append(Finding(
            "stats.percent", "medium" if len(pc) >= 2 or any(abs(x["true"] - float(x["reported"])) > 2 for x in pc)
            else "low",
            "Percentages that do not match their counts",
            " ".join(f"{x['k']}/{x['n']} is {x['true']}%, reported {x['reported']}%." for x in pc[:4]),
            confidence=0.7, locations=[Location(page=_page(paper, x["start"], text), quote=x["text"]) for x in pc[:6]],
            data={"items": [{k: v for k, v in x.items() if k != "start"} for x in pc]}))
    rs = repeated_spread(text)
    if rs:
        out.append(Finding(
            "stats.repeated_se", "medium", "The same spread repeated after different means",
            " ".join(f"± {x['spread']} appears {x['count']} times after different means ({', '.join(x['means'][:5])})."
                     for x in rs[:3]) + " Independent groups almost never share an SD/SE to the last digit.",
            confidence=0.6, locations=[Location(page=_page(paper, x["start"], text)) for x in rs[:4]],
            data={"items": [{k: v for k, v in x.items() if k != "start"} for x in rs]}))
    imp = impossible(text)
    if imp:
        out.append(Finding("stats.impossible", "low", "Impossible p-values",
                           ", ".join(x["text"] for x in imp[:5]) + ". A p-value is never exactly 0 and never above 1.",
                           confidence=0.6, locations=[Location(page=_page(paper, x["start"], text), quote=x["text"])
                                                      for x in imp[:5]]))
    out += _new_checks(paper, text)
    return out


def _odds(p: float) -> str:
    """'about 1 in 40,000' - a small probability in words."""
    if p < 1e-9:
        return "less than 1 in a billion"
    return f"about 1 in {int(float(f'{1 / p:.2g}')):,}"


def _items(xs: list[dict]) -> list[dict]:
    return [{k: v for k, v in x.items() if k != "start"} for x in xs]


def _new_checks(paper: Paper, text: str) -> list[Finding]:
    """GRIMMER, CI against p and estimate, df against n, SD against SEM, out-of-range coefficients."""
    out: list[Finding] = []
    loc = lambda x: Location(page=_page(paper, x["start"], text), quote=x["text"][:200])  # noqa: E731
    gm = grimmer(text)
    if gm:
        out.append(Finding(
            "stats.grimmer", "medium", "SDs that are impossible for whole-number data (GRIMMER)",
            f"{len(gm)} SD(s) cannot come from whole numbers with the stated mean and n. " +
            " ".join(f"\"{x['text']}\": no set of {x['n']} whole numbers with mean {x['mean']} has an SD that "
                     f"rounds to {x['sd']}." for x in gm[:3]) +
            " Typing errors are possible; otherwise the summary does not describe real counts.",
            confidence=0.6, locations=[loc(x) for x in gm[:6]], data={"items": _items(gm)}))
    conflicts, outside = ci_checks(text)
    if conflicts:
        out.append(Finding(
            "stats.ci_p", "medium", "p-values that contradict their confidence intervals",
            f"{len(conflicts)} result(s) where the 95% CI and the p-value disagree about significance. " +
            " ".join(f"\"{x['text']}\": {x['why']}." for x in conflicts[:3]) +
            " A wrong p, a wrong interval, or numbers from two different analyses.",
            confidence=0.7, locations=[loc(x) for x in conflicts[:6]], data={"items": _items(conflicts)}))
    if outside:
        out.append(Finding(
            "stats.ci_estimate", "low", "Estimates outside their own confidence interval",
            " ".join(f"\"{x['text']}\": {x['estimate']} is not between {x['ci'][0]} and {x['ci'][1]}."
                     for x in outside[:3]) + " An estimate always lies inside its interval; one of the numbers is "
            "mistyped.",
            confidence=0.65, locations=[loc(x) for x in outside[:6]], data={"items": _items(outside)}))
    dfn = df_vs_n(text)
    if dfn:
        out.append(Finding(
            "stats.df_n", "low", "Degrees of freedom larger than the sample allows",
            " ".join(f"\"{x['text']}\" has {x['df']:g} df, but with {x['basis']} the most possible is {x['max_df']}."
                     for x in dfn[:3]) + " The test may have counted repeated measurements as independent, or the "
            "stated n is wrong.",
            confidence=0.5, locations=[loc(x) for x in dfn[:6]], data={"items": _items(dfn)}))
    ss = sd_vs_sem(text)
    if ss:
        out.append(Finding(
            "stats.sd_sem", "low", "SD and SEM labels that do not fit the confidence interval",
            " ".join(f"\"{x['text']}\": with n = {x['n']}, the 95% CI fits this spread only if it is the {x['fits']}, "
                     f"not the {x['label']}." for x in ss[:3]) +
            " Either the label is wrong or the interval is not a CI of the mean.",
            confidence=0.55, locations=[loc(x) for x in ss[:6]], data={"items": _items(ss)}))
    rg = out_of_range(text)
    if rg:
        out.append(Finding(
            "stats.out_of_range", "low", "Coefficients outside their possible range",
            " ".join(f"\"{x['text']}\": a {x['what']} cannot be {x['value']}." for x in rg[:4]) +
            " Correlations lie between -1 and 1; R², Cronbach's alpha and ROC AUC are at most 1.",
            confidence=0.6, locations=[loc(x) for x in rg[:6]], data={"items": _items(rg)}))
    return out
