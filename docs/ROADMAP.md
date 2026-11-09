# Improvement plan and status

A full audit (design, engine bugs, security, tests, and ~290 feature ideas) was run first (notes in
`docs/audit/*.json`). Work is done in waves; each wave's agents own separate files. For a guide to picking the work
up, read `docs/HANDOVER.md`.

## Done

- **Wave 1 — bugs and security** (commit "Wave 1: …"): every verified engine and server finding fixed, with ~150
  new regression tests. The local server requires the `X-Paper-Inspector: 1` header on POST and DELETE and only
  answers localhost hosts.
- **Mac redesign** merged into `main` (three-column layout, Settings scene, menus and shortcuts, queue, exporter).
- **Wave 2 — new checks** (all wired into the pipeline):
  - Statistics: one extractor for t, F, r, chi-square and z in all common layouts; GRIMMER; confidence interval
    against p-value; estimate outside its interval; df against stated n; SD/SEM mix-ups; out-of-range r, R², alpha
    and AUC; Carlisle baseline balance on trial tables. More chatbot and tortured phrases.
  - Images: mirrored and 180° clones inside one image; reused band rows (loading controls) between blots;
    copies shown as a negative; one embedded PDF image placed twice; Bik categories I–III on every duplicate.
  - Data: copied-and-shifted runs (plus or times a constant); one finding for a table supplied twice; typed values
    over a formula column; Excel calculation-chain order; linked workbooks; mean/SD columns recomputed from
    replicates; mixed precision; repeated decimals; Benford (strict); supplementary-file timeline from the files'
    own metadata (no person names read).
  - References and record: cited DOI pointing to an unrelated paper; works cited after their retraction;
    expressions of concern; batched Crossref/Retraction Watch lookups; the paper's own Crossref record; named
    sneaked references; special-issue flag; trial IDs (format offline, ClinicalTrials.gov online); Cellosaurus.
  - Scored (integrity) additions: `data.offset_copy`, `stats.grimmer`, `stats.ci_p`, `stats.carlisle`. The rest
    are quality notes.
- **Wave 2 — back end**: saved rulings per finding with stable fingerprints, notes and history; cancel for scans
  (also inside a download); persistent batch queue; CSV, XLSX and PDF exports; evidence pack with SHA-256 manifest;
  letter templates; run search and tags; check catalogue for every emitted check; smart input (DOI, PMID, PMCID,
  arXiv, links, titles, BibTeX/RIS) with `POST /api/resolve`; retries with backoff; per-source fetch status in the
  report; optional contact email (sent only when the user sets one).
- **Wave 3 — web redesign**: dark figure stage with zoom, pan and loupe; full-stage blink comparator (blink,
  swipe, onion, difference with gain, side by side); visible panel outlines; keyboard triage with rulings; command
  palette; deep links; resizable panes; export menu and letters; batch dashboard; catalogue popovers;
  accessibility pass; no third-party fonts loaded.
- **Mac app, wave 2**: rulings in the inspector with menu shortcuts, catalogue section, export menu, letters,
  batch queue in the sidebar, cancel through the engine, contact email in Settings.
- **Verification**: 501 Python tests, 36 Swift tests (8 need a live engine), web build clean. Image
  benchmark held out (`--n 4 --seed 99`): 195/212 = 92.0% recall, 0/40 clean false alarms; development draw
  (default): 149/159 = 93.7%, 0/30. Real papers (12 retracted, 12 controls): named figure flagged in 4/7 image
  retractions, median score 44 vs 0. That run found a data false alarm on a theoretical m/z table, now fixed (exact
  relations to the last printed digit; theoretical m/z and isotope columns skipped by the copy, repeat and digit checks).

## Left to do

1. **Page-image PDFs**: many retracted papers are one raster per page; `panels.segment` does not split them and blot
   panels there are classed as charts, so image checks skip them. This is the biggest remaining recall gap.
2. **Manual compare endpoint** (`GET /api/runs/{rid}/compare`) aligns panels cut from the saved figure PNG, which
   can be lower resolution than the panel images the scan used, so a pair the scan matched (the demo's F3.b/F3.e,
   rotated 180°) can come back `aligned: false`. Save panel crops at scan resolution or re-read them from the PDF.
3. **Demo paper**: `synth.demo_paper()` does not yet plant the wave 2 checks (mirrored clone, band-row reuse,
   offset copy, GRIMMER, CI/p). Add plants and update `docs/DEMO_SCRIPT.md`.
4. **PubPeer**: needs a key (no keyless API). `refcheck.pubpeer` exists but the Settings UI has no field for it,
   and its parsing has not been checked against a live keyed answer.
5. **Fonts**: the web app uses Atkinson Hyperlegible Next and Newsreader only when they are installed on the
   computer, and falls back to system fonts otherwise. Both are under the SIL Open Font License; bundle their
   `.woff2` files in `web/public/fonts/` and add `url()` sources to the `@font-face` rules if wanted.
6. **Mac app**: tag editing and the search/library view are read-only; the app was not clicked through by hand.
7. **Smaller ideas left out for precision**: Mann-Whitney/Wilcoxon recompute, SPRITE, p-curve, colour negatives,
   OCR of blot labels, authorship from file metadata (deliberately not built: false alarms and personal data),
   Cellosaurus "not found", registries other than ClinicalTrials.gov, retraction reasons.
8. **Submission**: the repository is public; the demo video and team information are still to do (see
   `docs/HANDOVER.md`).
