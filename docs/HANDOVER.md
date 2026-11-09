# Handover

This note is for the next person working on Paper Inspector. It says what the project is, how to run and test it,
where things live, what changed in the last round of work, and what is left. The status list is in
`docs/ROADMAP.md`; this file is the guide.

## What it is

A local research-integrity screen. Paste a DOI (or a PMID, PMCID, arXiv ID, link or title), and the engine fetches
the paper, its figures and its supplementary files from open sources, then checks images, data, statistics, text,
references and the publication record. Every finding carries its evidence so a person can judge it quickly.

- **Engine**: Python package in `engine/paperinspector` (FastAPI server, CLI, all checks).
- **Web app**: `web/` (Vite + React + TypeScript), served by the engine from `web/dist`. Code map in `web/README.md`.
- **Mac app**: `mac/` (SwiftUI package). It starts and talks to the same local engine.

Findings have a `kind`: **integrity** findings (could mean manipulation) feed the 0-100 risk score; **quality**
findings are listed but not scored. The list of scored checks is `models.INTEGRITY`.

## Running it

```
python3 -m venv .venv && .venv/bin/pip install -e .      # first time only
.venv/bin/paperinspector serve                           # web app on http://127.0.0.1:8765
.venv/bin/paperinspector doi 10.1371/journal.pone.0000000
.venv/bin/paperinspector scan paper.pdf -s data.xlsx --json
.venv/bin/paperinspector demo out.pdf                    # writes the synthetic demo paper and workbook
.venv/bin/paperinspector batch dois.txt --csv out.csv
```

Set `PAPERINSPECTOR_HOME` to a scratch folder when testing, so runs, settings and the cache stay out of the real
data folder (default `~/.paperinspector`).

Web: `cd web && npm install && npm run dev` (proxies `/api` to the engine) or `npm run build`.
Mac: `cd mac && swift build && swift test`. Live Swift tests run with `PI_ENGINE_URL` pointing at a running engine.

## Tests and benchmarks

```
.venv/bin/python -m pytest -q                       # from the repo root; 501 pass
.venv/bin/python tools/bench.py --n 4 --seed 99     # held-out image benchmark: 195/212 = 92.0%, 0/40 false alarms
.venv/bin/python tools/bench.py                     # development draw: 149/159 = 93.7%, 0/30
cd web && npm run build                             # type-checks and builds
cd mac && swift test                                # 36 tests, 8 skipped without a live engine
```

Rules the tests rely on: no test touches the network (httpx is mocked), and `tests/conftest.py` sets
`PAPERINSPECTOR_TESTING=1` so the server's loopback-host guard accepts the test client, and points
`PAPERINSPECTOR_HOME` at a throwaway folder so no test writes into the real data folder.

Wave 2 test files, one per area: `test_inputs_wave2.py`, `test_backend_wave2.py`, `test_refs_wave2.py`,
`test_data_wave2.py`, `test_stats_wave2.py`, `test_images_wave2.py`, and `test_integration_wave2.py` (pipeline
wiring, cancel, `/api/resolve`, catalogue coverage).

## How a scan flows

`pipeline.py` is the spine. `scan()` (uploaded PDF), `scan_doi()` and `scan_case()` (several papers) all end in
`_analyse()` / `_finish()`, which write `runs/<id>/report.json`, `figures/` and `evidence/`.

Order inside `_analyse`:

1. Figures are split into panels (`panels.segment`).
2. Core image checks (`_image_findings`: reuse, clones, flat patches, splices), then `imgchecks.check` (mirrored
   clones, band rows, negatives, embedded-image reuse) and `imgchecks.label_bik` (Bik category on every duplicate).
3. Text and statistics (`textcheck.check`, `stats.check`), also on supplementary PDFs and documents.
4. Data (`datacheck.check` with the paper's dates and the supplementary files), then `stats.check_tables`
   (Carlisle baseline balance).
5. References (`refs.check(paper, meta)`, including named sneaked references); online: `refcheck.analyse` (also
   folds the paper's own Crossref record into `meta`) and `history.check`.
6. Reporting and record (`reporting.record`, timeline, statements, cell lines, consistency, `reporting.trials`).

Each optional step is wrapped so a failure becomes an `*.unchecked` info finding instead of losing the scan. A cancel
raises `models.Cancelled`, a `BaseException`, so those wrappers cannot swallow it; `fetch.Cancelled` is a subclass of
it. A scan that does not finish removes its own half-written run folder.

## Where things live (engine)

| File | What it does |
| --- | --- |
| `inputs.py` | Reads pasted identifiers offline (`parse_input`, `parse_batch`) and resolves them (`resolve`, `resolve_batch`) |
| `fetch.py` | Fetches the paper bundle; retries with backoff; per-source status; `summary()`; `user_agent()` |
| `sources.py` | Turns the bundle or an upload into a `Paper`, tables and source files |
| `pdfdoc.py`, `panels.py` | PDF reading, figure and panel segmentation; `image_refs` per figure placement |
| `imgdup.py`, `clone.py`, `forensics.py`, `blotrows.py`, `imgchecks.py`, `evidence.py` | Image checks and evidence images |
| `tables.py`, `datacheck.py` | Reading spreadsheets, CSV, Word and PDF tables; data checks |
| `stats.py`, `textcheck.py` | Statistics recompute and consistency; text checks |
| `refs.py`, `refcheck.py`, `history.py`, `reporting.py` | References, retractions, the publication record, reporting statements and trials |
| `models.py` | Data classes, `INTEGRITY`, score, `Cancelled`, rulings and fingerprint helpers |
| `catalog.py` | Plain explanation of every check (label, meaning, false alarms, what to look at, question for authors) |
| `rulings.py`, `library.py`, `batchqueue.py`, `exports.py`, `letters.py`, `report.py` | Reviewer rulings, search and tags, batch queue, exports, letters, HTML report and PubPeer draft |
| `server.py`, `cli.py`, `settings.py`, `agent.py` | HTTP API, command line, settings, optional AI investigator |
| `synth.py` | Synthetic images and the demo paper used by tests and the benchmark |

The HTTP API is documented in the docstrings of `server.py`. Every POST and DELETE needs the header
`X-Paper-Inspector: 1`, and the server answers only loopback hosts. New in this round: `POST /api/resolve`
`{text}` returns `inputs.resolve()` plus a `label`; `POST /api/doi` now also accepts identifiers that resolve to one
DOI (422 when several papers match, 503 when the lookup service is offline); job and queue scans pass a cancel flag
into the fetch; `report.json` gains `fetch` (what was found, missing or unreachable) and `meta.special_issue`.

## Adding a check

1. Emit `Finding("<category>.<name>", severity, title, detail, confidence, locations, data=...)` from the module of
   its area, behind that module's existing entry function, so the pipeline needs no change.
2. Keep it precise: a conservative threshold, plain words in the title and detail (what was measured, the common
   innocent causes), and tests with a positive case and a clean negative case.
3. Add a catalogue entry in `catalog._C` (a test fails if an emitted id has none) and a short label in
   `web/src/api.ts` `CHECK_NAMES` and `mac/Sources/PaperInspector/Models.swift` `checkNames`.
4. Only add it to `models.INTEGRITY` if it is as specific as the checks already there; run `tools/bench.py` and
   the whole suite before and after.

## House rules

- Precision over recall: real papers must not get new false alarms.
- No new third-party dependencies (engine: PyMuPDF, OpenCV, NumPy, SciPy, FastAPI, httpx, openpyxl, python-docx).
- Network calls: timeouts, one at a time, graceful offline. No email address is sent unless the user set one in
  Settings. API keys are never logged or stored in reports.
- The repo is meant to go public: no personal names, home paths, host names or institution names in code, tests,
  comments or docs. File metadata checks never read or show person names.
- Plain English in all UI strings and finding text.

## State of the work

Everything described here is committed. Last full check: 501 Python tests pass, 36 Swift tests run (8 need a
live engine and are skipped without one), the web build is clean, the held-out image benchmark is 195/212 = 92.0%
with 0/40 clean false alarms, the real-paper run flags the named figure in 4 of 7 image retractions (median score
44 for retracted papers, 0 for controls), and both apps were clicked through on the demo paper (findings, rulings, blink
comparator, exports menu).

Scripts and agents that scan papers must set `PAPERINSPECTOR_HOME` to a scratch folder. Every scan writes a run
folder, so a benchmark run without it fills the real data folder with hundreds of synthetic runs. `tools/bench.py`
and the tests already do this; one-off scripts must too.

## Publishing

The public repository is https://github.com/Micropeptide/paper-inspector (branch `main`, tag `v0.1.0`). To publish
a change:

1. Re-run the checks above and fill in any numbers in `README.md` and `docs/SUBMISSION.md` that changed.
2. Check that no file holds personal names, home paths, host names, institution names or e-mail addresses:
   `git grep -nIiE "/Users/|/home/|[a-z0-9._-]+@[a-z0-9-]+\.[a-z]{2,}"` should show only generic placeholders
   and package metadata.
3. Commit and `git push`. Tag releases (`git tag v0.x.y && git push --tags`).
4. Add the screenshots from `docs/screenshots/` and the demo video to the project page.

History rules: commits keep their real timestamps (never back-date or forward-date them), and the docs make no
claim about when the project started.

## Hackathon submission (Slay Hacks 2026)

The project is being prepared for Slay Hacks 2026 (Devpost, AI theme required). The form asks for: title and
description, the problem, AI implementation details, a demo or video, screenshots, a public repository and team
information. `docs/SUBMISSION.md` has text for each field, written around the judging criteria: innovation,
technical execution, impact, AI integration and responsible use, and UX and presentation. `docs/DEMO_SCRIPT.md` is a
shot list for the video. The rules disqualify false or misleading submission information, so every number in the
write-up must come from a benchmark run in this repository, and anything not built must not be described as
built.

Still to do for the submission: record the video and fill in team information.

## Known issues and next steps

See "Left to do" in `docs/ROADMAP.md`. The most useful next pieces are: page-image PDF segmentation
(the main recall gap on real retracted papers), the compare endpoint's resolution mismatch, demo plants for the new
checks, and the PubPeer key field.
