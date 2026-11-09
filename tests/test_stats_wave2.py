"""Second wave of statistics checks: GRIMMER, CIs against p-values, df against n, SD against SEM, more test formats,
out-of-range coefficients, Carlisle's baseline test, and new chatbot and tortured phrases.  Every check has a case
it must catch and an honest case it must leave alone."""
from __future__ import annotations

import json

import numpy as np
import pytest

from paperinspector import stats, textcheck
from paperinspector.models import Paper
from paperinspector.tables import Table


def _paper(text: str) -> Paper:
    return Paper(source="t.pdf", pages=[text], page_count=1)


def _checks(found) -> set[str]:
    return {f.check for f in found}


# ---- more test-statistic formats ----------------------------------------------------------------------------------

@pytest.mark.parametrize("good,bad", [
    ("t = 2.20, df = 28, p = .036", "t = 2.20, df = 28, p = .36"),
    ("t(df = 28) = 2.20, p = .036", "t(df = 28) = 2.20, p = .36"),
    ("t[28] = 2.20, p = .036", "t[28] = 2.20, p = .36"),
    ("t(28) = 2.20 (p = .036)", "t(28) = 2.20 (p = .36)"),
    ("F = 3.21, df = 2, 45, p = .05", "F = 3.21, df = 2, 45, p = .005"),
    ("F1,45 = 4.10, p = .049", "F1,45 = 4.10, p = .49"),
    ("F(2/45) = 3.21, p = .05", "F(2/45) = 3.21, p = .005"),
    ("r = .45, df = 28, p = .013", "r = .45, df = 28, p = .13"),
    ("r = .45, n = 30, p = .013", "r = .45, n = 30, p = .13"),
    ("χ2 = 4.20, df = 1, p = .04", "χ2 = 4.20, df = 1, p = .4"),
    ("chi-squared(1) = 4.20, p = .04", "chi-squared(1) = 4.20, p = .4"),
    ("χ 2 (1) = 4.20, p = .04", "χ 2 (1) = 4.20, p = .4"),
    ("Chi2(1, N = 90) = 4.2, p = .04", "Chi2(1, N = 90) = 4.2, p = .4"),
    ("z = 2.10 (p = .036)", "Z = 2.10, P = 0.36"),
])
def test_recompute_reads_common_formats(good, bad):
    assert stats.recompute(good) == [], good
    hit = stats.recompute(bad)
    assert len(hit) == 1 and hit[0]["text"] in bad


def test_rank_correlations_are_not_recomputed_as_pearson():
    assert stats.recompute("Spearman r = .45, n = 30, p = .13") == []


def test_one_hit_per_p_value():
    # a sentence matched by two patterns is reported once
    assert len(stats.tests_in("t(28) = 2.20, p = .036")) == 1


def test_tiny_threshold_slips_are_not_reported():
    assert stats.recompute("F(3, 12) = 15.15, p < 0.0002") == []      # p = 0.00022
    assert stats.recompute("F(3, 12) = 15.15, p < 0.0001")            # off by more than a rounding slip


def test_original_formats_unchanged():
    assert stats.recompute("The groups differed, t(14) = 2.10, p < 0.001.")
    assert stats.recompute("The groups differed, t(28) = 2.20, p = .036.") == []
    assert stats.recompute("F(2, 45) = 3.21, p = .05") == []
    assert stats.recompute("A one-tailed test gave t(15) = 1.90, p = .038.") == []


# ---- GRIMMER -------------------------------------------------------------------------------------------------------

def test_grimmer_catches_impossible_sd():
    # 5 whole numbers summing to 15: possible sample SDs are 0, 0.71, 1.00, 1.22, ... - never 0.85
    hit = stats.grimmer("The mean number of lesions per mouse was 3.00 ± 0.85 SD (n = 5).")
    assert hit and hit[0]["sd"] == "0.85" and hit[0]["text"].startswith("3.00 ± 0.85")
    assert stats.grimmer("Colony counts: M = 3.00, SD = 0.85, n = 5.")
    f = [x for x in stats.check(_paper("The mean number of lesions per mouse was 3.00 ± 0.85 SD (n = 5)."))
         if x.check == "stats.grimmer"]
    assert f and "3.00 ± 0.85" in f[0].detail and f[0].locations[0].quote


@pytest.mark.parametrize("text", [
    "The mean number of lesions per mouse was 3.00 ± 0.71 SD (n = 5).",      # 3,3,3,3,4... gives 0.71
    "Colony counts: M = 3.00, SD = 1.00, n = 5.",
    "The mean number of lesions per mouse was 3.00 ± 0.85 SEM (n = 5).",     # an SEM, not an SD
    "The mean Likert item score was 3.00 ± 0.85 SD (n = 5).",                # an average of items
    "The mean number of lesions per mouse was 3.00 ± 0.85 (n = 5).",         # spread never called an SD
])
def test_grimmer_leaves_possible_or_unclear_sds_alone(text):
    assert stats.grimmer(text) == []


def test_grimmer_accepts_every_real_sample():
    rng = np.random.default_rng(4)
    for _ in range(400):
        n = int(rng.integers(3, 30))
        x = rng.integers(0, 12, n)
        mean, sd = f"{x.mean():.2f}", f"{x.std(ddof=1):.2f}"
        if float(sd) == 0:
            continue
        assert stats._grimmer_possible(mean, sd, n) is not False, (list(x), mean, sd)


# ---- confidence intervals ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The odds ratio was 2.10 (95% CI 1.20-3.60), p = 0.40.",
    "OR 1.30 (95% CI 0.80–2.10), p < 0.001.",
    "The mean difference was 3.2 (95% CI -1.1 to 7.3; P = .003).",
    "The mean difference was 3.2 (95% CI 1.1 to 5.3; P = .25).",
])
def test_ci_contradicting_p_is_caught(text):
    conflicts, _ = stats.ci_checks(text)
    assert len(conflicts) == 1 and conflicts[0]["text"] in text
    assert "stats.ci_p" in _checks(stats.check(_paper(text)))


@pytest.mark.parametrize("text", [
    "The odds ratio was 2.10 (95% CI 1.20-3.60), p = 0.007.",
    "OR 1.30 (95% CI 0.80–2.10), p = 0.29.",
    "HR = 0.75, 95% CI: 0.60-0.94, P = .01",
    "The mean difference was 3.2 (95% CI 1.1 to 5.3; P = .003).",
    "β = -0.25, 95% CI -0.40 to -0.10, p = .001",
    # near the line, a profile-likelihood CI and a Wald p can honestly disagree
    "OR 2.10 (95% CI 1.01-4.37), p = 0.06",
    "HR (95% CI) 0.75 (0.60-0.94), p = .031",
])
def test_ci_agreeing_with_p_is_quiet(text):
    assert stats.ci_checks(text) == ([], [])


def test_estimate_outside_its_ci():
    _, outside = stats.ci_checks("The mean difference was 3.2 (95% CI 4.1 to 5.3; P = .003).")
    assert outside and outside[0]["estimate"] == "3.2"
    assert "stats.ci_estimate" in _checks(stats.check(_paper("OR 0.62 (95% CI 0.70-0.95), p = .01")))
    # an estimate on the rounded edge of its interval is fine
    assert stats.ci_checks("OR 0.70 (95% CI 0.70-0.95), p = .03")[1] == []


# ---- degrees of freedom against n ----------------------------------------------------------------------------------

def test_df_larger_than_the_groups_allow():
    hit = stats.df_vs_n("Treated mice (n = 8) gained more weight than controls (n = 7), t(28) = 2.50, p = .019.")
    assert hit and hit[0]["max_df"] == 13 and hit[0]["text"].startswith("t(28)")
    assert stats.df_vs_n("Across all participants (N = 40), F(2, 57) = 4.10, p = .022.")
    assert stats.df_vs_n("Height and weight were correlated (n = 20), r(38) = .45, p = .004.")
    assert "stats.df_n" in _checks(stats.check(_paper(
        "Treated mice (n = 8) gained more weight than controls (n = 7), t(28) = 2.50, p = .019.")))


@pytest.mark.parametrize("text", [
    "Treated mice (n = 8) gained more weight than controls (n = 7), t(13) = 2.50, p = .027.",
    "Treated mice (n = 8) gained more weight than controls (n = 7), t(11.2) = 2.50, p = .029.",   # Welch
    "Treated mice (n = 8) and controls (n = 7) differed, t(10) = 2.50, p = .031.",                # dropped animals
    "Cells from treated mice (n = 8) and controls (n = 7) differed, t(58) = 2.50, p = .015.",     # cells, not mice
    "With n = 8 per group, Tukey's test gave t(21) = 2.50, p = .021.",                            # pooled ANOVA error
    "Knockdown (n = 3) and control (n = 3) wells over technical replicates gave t(16) = 2.50, p = .024.",
    "Across all participants (N = 40), F(2, 37) = 4.10, p = .025.",
    "A repeated-measures ANOVA on N = 10 subjects gave F(3, 27) = 4.10, p = .016.",
    "Height and weight were correlated (n = 40), r(38) = .45, p = .004.",
])
def test_df_that_fit_or_are_explained_are_quiet(text):
    assert stats.df_vs_n(text) == []


# ---- SD against SEM ------------------------------------------------------------------------------------------------

def test_spread_label_that_does_not_fit_its_ci():
    # SD 3.1, n = 16: SEM 0.78, so the 95% CI is about ± 1.65; ± 6.6 only fits if 3.1 were the SEM
    hit = stats.sd_vs_sem("Colony count was 12.4 ± 3.1 SD (n = 16; 95% CI 5.8–19.0).")
    assert hit and hit[0]["label"] == "SD" and hit[0]["fits"] == "SEM"
    hit = stats.sd_vs_sem("Colony count was 12.4 ± 3.1 SEM (n = 16; 95% CI 10.7–14.1).")
    assert hit and hit[0]["label"] == "SEM" and hit[0]["fits"] == "SD"
    assert "stats.sd_sem" in _checks(stats.check(_paper("Colony count was 12.4 ± 3.1 SEM (n = 16; 95% CI 10.7–14.1).")))


@pytest.mark.parametrize("text", [
    "Colony count was 12.4 ± 3.1 SD (n = 16; 95% CI 10.7–14.1).",
    "Colony count was 12.4 ± 0.8 SEM (n = 16; 95% CI 10.7–14.1).",
    "Colony count was 12.4 ± 3.1 SD (n = 16; 95% CI 1.1–3.4 for the difference).",   # not this mean's CI
    "Colony count was 12.4 ± 3.1 SD (n = 3; 95% CI 4.7–20.1).",                       # n too small to tell
])
def test_spread_labels_that_fit_are_quiet(text):
    assert stats.sd_vs_sem(text) == []


# ---- out-of-range coefficients -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Pearson correlation between age and score was strong (r = 1.20, p < .001).",
    "r(28) = 1.45, p = .01",
    "The linear fit was good (R² = 1.12).",
    "Cronbach's α = 1.08",
    "The ROC analysis gave an AUC of 1.07.",
])
def test_out_of_range_coefficients(text):
    assert stats.out_of_range(text), text


@pytest.mark.parametrize("text", [
    "Pearson correlation between age and score was strong (r = 0.82, p < .001).",
    "The linear fit was good (R² = 0.92).",
    "R2 = 1.5 mg was added to the linear model",
    "Cronbach's alpha was 0.86",
    "The plasma AUC was 4.5 µg·h/mL.",                     # a pharmacokinetic AUC has no upper limit
    "The universal gas constant R = 8.314 J/(mol K) was used.",
    "a circle of radius r = 1.5 cm with correlation",
])
def test_in_range_or_unrelated_values_are_quiet(text):
    assert stats.out_of_range(text) == []


# ---- Carlisle baseline balance -------------------------------------------------------------------------------------

RCT = _paper("Patients were randomised 1:1 to drug or placebo. Table 1. Baseline characteristics of the participants.")


def _baseline(rng, rows=10, n=(50, 50), fake=None, dec=1, fmt="pm", total=False):
    head = ["Characteristic"] + [f"Arm {k + 1} (n = {v})" for k, v in enumerate(n)]
    if total:
        head.append(f"Total (n = {sum(n)})")
    out = [head]
    for i in range(rows):
        mu = rng.uniform(10, 100)
        sd = mu * rng.uniform(0.05, 0.4)
        cells = []
        for j, nn in enumerate(n):
            x = rng.normal(mu, sd, nn)
            m, s = x.mean(), x.std(ddof=1)
            if fake == "similar" and j:
                m, s = float(cells[0][0]) + rng.normal(0, sd * 0.01), float(cells[0][1]) * (1 + rng.normal(0, 0.01))
            if fake == "different" and j:
                m += sd
            cells.append((f"{m:.{dec}f}", f"{s:.{dec}f}"))
        fmtd = [f"{a} ± {b}" if fmt == "pm" else f"{a} ({b})" for a, b in cells]
        out.append([f"Variable {i + 1}" + (", mean (SD)" if fmt == "paren" else "")] + fmtd +
                   (["50.0 ± 10.0"] if total else []))
    return Table("Table 1", "", out)


def test_carlisle_flags_arms_that_are_too_similar():
    f = stats.check_tables([_baseline(np.random.default_rng(1), rows=12, fake="similar")], RCT)
    assert len(f) == 1 and f[0].check == "stats.carlisle" and f[0].data["side"] == "similar"
    assert "Variable 1:" in f[0].detail and f[0].locations[0].quote
    json.dumps(f[0].public())


def test_carlisle_flags_arms_that_are_too_different():
    f = stats.check_tables([_baseline(np.random.default_rng(2), fake="different")], RCT)
    assert len(f) == 1 and f[0].data["side"] == "different" and "SEs printed as SDs" in f[0].detail


def test_carlisle_reads_paren_cells_and_skips_total_columns():
    t = _baseline(np.random.default_rng(3), rows=12, n=(40, 40, 40), fake="similar", fmt="paren", total=True)
    res = stats.baseline_balance(t)
    assert res and res["arms"] == [40, 40, 40]
    assert stats.check_tables([t], RCT)


def test_carlisle_quiet_on_honest_randomised_tables():
    rng = np.random.default_rng(5)
    for n in ((50, 50), (12, 14), (200, 200, 200)):
        for dec in (1, 2):
            for _ in range(150):
                assert stats.check_tables([_baseline(rng, rows=int(rng.integers(8, 20)), n=n, dec=dec)], RCT) == []


def test_carlisle_needs_a_randomised_baseline_table_with_enough_rows():
    rng = np.random.default_rng(6)
    similar = _baseline(rng, rows=12, fake="similar")
    cohort = _paper("A propensity-matched cohort. Table 1. Baseline characteristics of the participants.")
    observational = _paper("Table 1. Baseline characteristics of the participants in the cohort.")
    assert stats.check_tables([similar], cohort) == []
    assert stats.check_tables([similar], observational) == []
    assert stats.check_tables([_baseline(rng, rows=6, fake="similar")], RCT) == []
    results = Table("Table 3", "", similar.rows)
    assert stats.check_tables([results], _paper("Patients were randomised. Table 3. Outcomes at 12 weeks.")) == []


def test_carlisle_reads_arm_headings_that_span_two_columns():
    rng = np.random.default_rng(8)
    t = _baseline(rng, rows=10, fake="similar")
    rows = [["Variable", "Drug (n = 50)", "", "Placebo (n = 50)", ""], ["", "n (%)", "Mean ± SD", "n (%)", "Mean ± SD"],
            ["Male sex", "23 (46.0)", "", "24 (48.0)", ""]]
    rows += [[r[0], "", r[1], "", r[2]] for r in t.rows[1:]]
    res = stats.baseline_balance(Table("Table 1", "", rows))
    assert res and len(res["rows"]) == 10 and res["arms"] == [50, 50]


def test_carlisle_skips_time_points_nested_over_arms():
    rng = np.random.default_rng(9)
    t = _baseline(rng, rows=10, n=(50, 50, 50, 50), fake="different")
    rows = [["", "Baseline (N = 100)", "", "Week 12 (N = 100)", ""],
            ["Variable", "Intervention", "Control", "Intervention", "Control"]] + t.rows[1:]
    assert stats.baseline_balance(Table("Table 1", "", rows)) is None


def test_carlisle_skips_count_percent_rows_and_sem_tables():
    rng = np.random.default_rng(7)
    t = _baseline(rng, rows=12, fake="similar", fmt="paren")
    counts = [t.rows[0]] + [[f"Male sex {i}", "23 (46.0)", "23 (46.0)"] for i in range(12)]
    assert stats.baseline_balance(Table("Table 1", "", counts), "mean (SD)") is None
    pm = _baseline(rng, rows=12, fake="similar")
    sem = Table("Table 1", "", [pm.rows[0] + ["values are mean ± SEM"]] + pm.rows[1:])
    assert stats.baseline_balance(Table("Table 1", "", [pm.rows[0] + ["values are mean ± SD"]] + pm.rows[1:]))
    assert stats.baseline_balance(sem) is None


# ---- whole-paper behaviour -----------------------------------------------------------------------------------------

def test_new_checks_in_one_paragraph():
    text = ("The mean number of lesions per mouse was 3.00 ± 0.85 SD (n = 5). OR 1.30 (95% CI 0.80–2.10), "
            "p < 0.001. Treated mice (n = 8) gained more weight than controls (n = 7), t(28) = 2.50, p = .019. "
            "Colony count was 12.4 ± 3.1 SEM (n = 16; 95% CI 10.7–14.1). Cronbach's α = 1.08. "
            "OR 0.62 (95% CI 0.70-0.95), p = .01.")
    found = stats.check(_paper(text))
    assert _checks(found) == {"stats.grimmer", "stats.ci_p", "stats.ci_estimate", "stats.df_n", "stats.sd_sem",
                              "stats.out_of_range"}
    for f in found:
        assert f.locations and f.locations[0].quote and f.locations[0].quote in text, f.check
        assert f.locations[0].page == 1
        json.dumps(f.public())


def test_clean_results_section_raises_nothing_new():
    text = ("Mice were randomly assigned to groups (n = 8 and n = 7). Treated mice gained more weight, "
            "t(13) = 2.50, p = .027, and the odds ratio for tumours was 2.10 (95% CI 1.20-3.60), p = 0.009. "
            "Body weight was 24.1 ± 2.3 SD (n = 15). The correlation between weight and tumour size was "
            "moderate (r = 0.45, n = 30, p = .013). A two-way ANOVA gave F(1, 26) = 5.12, p = .032. "
            "Cronbach's alpha was 0.86 and the model R² = 0.41. The ROC AUC was 0.78 (95% CI 0.66-0.90). "
            "Incubations were at 37 ± 0.5 °C and pH 7.40 ± 0.05.")
    assert stats.check(_paper(text)) == []


# ---- text: chatbot boilerplate and tortured phrases ----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "As of my last update, the gene had no known function.",
    "I'm sorry, but I can't assist with that request.",
    "Certainly! Below is a revised abstract.",
    "Corresponding author: [Your Name], [Your Institution].",
])
def test_new_chatbot_phrases(text):
    assert "text.ai_boilerplate" in _checks(textcheck.check(_paper(text)))


@pytest.mark.parametrize("text", [
    "We used GPT-4 as an AI model to label images.",
    "A participant said: I'm sorry, but I was late to every session.",
    "Corresponding author: see the title page.",
])
def test_ordinary_sentences_are_not_chatbot_text(text):
    assert textcheck.check(_paper(text)) == []


def test_new_tortured_phrases():
    f = textcheck.check(_paper("We trained an irregular backwoods classifier and a profound neural system."))
    assert "text.tortured" in _checks(f)
    assert textcheck.check(_paper("We trained a random forest classifier and a deep neural network.")) == []
    assert textcheck.check(_paper("The neural system of the fly was imaged.")) == []
