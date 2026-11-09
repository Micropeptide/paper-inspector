# Paper Inspector: submission write-up

Text for the hackathon project page. Each heading matches a field the form asks for. Numbers come from the
benchmarks in this repository and can be re-run with the commands in the README.

## Title and tagline

**Paper Inspector**: paste a DOI, get the paper inspected.

## The problem

Every year thousands of scientific papers are retracted for duplicated images, fabricated data or references that
do not exist. That usually happens years after other scientists, doctors and patients have relied on them. Most of
these problems are found by a few volunteer image sleuths. They work by hand, one paper at a time, flipping and
contrast-stretching figures and opening supplementary spreadsheets that reviewers rarely have time to read.

Generative AI makes this worse in three ways. It is cheap to make a plausible paper with invented references. Some
authors now hide instructions to AI reviewers in white or microscopic text. And chatbot text ends up pasted into
the paper itself. Meanwhile editors and reviewers are asked to check more papers than ever.

The people who need a fast, careful first look include peer reviewers, journal editors, research-integrity
officers, students about to build on a paper, and authors checking their own work before they submit.

## What it does

Paste a DOI, PMID, PMCID, arXiv ID, link or title, or drop in a PDF. Paper Inspector fetches the paper and
everything that comes with it from open sources: the PDF, the full-text XML, the original figure files and the
supplementary spreadsheets, Word files and archives. It then checks:

- **Images**: every panel against every other, including copies that were mirrored, rotated, rescaled, stretched,
  cropped or shown as a negative. It also finds regions cloned inside one image, reused band rows between blots,
  splice seams, painted patches with no camera noise, and the same embedded image placed twice in the PDF.
- **Data**: copied runs of values (also shifted by a constant), numbers made with `RAND()`, last digits that are
  not random, typed numbers inside a formula column, rows moved after the fact (from Excel's own calculation
  record), and gene names that Excel turned into dates.
- **Statistics**: p-values recomputed from their test statistics, and means and SDs that are impossible for the
  sample size (GRIM, GRIMMER). Confidence intervals that contradict their p-values, and trial baseline tables that
  are too similar to be random (Carlisle).
- **Text**: hidden prompts aimed at AI reviewers, tortured phrases from paraphrasing tools, and chatbot leftovers.
- **References**: every reference is looked up, so it flags references that cannot be found (a common sign of
  AI-written text), retracted or misattributed DOIs, works cited after their retraction, and citation patterns
  that look coerced.
- **Reporting and record**: misidentified cell lines, trial registrations, data accession numbers that are not
  public, retractions, expressions of concern, and acceptance within days.

Findings come in two kinds. **Integrity signals** could mean manipulation, and only they set the 0–100 risk score.
**Quality notes** are honest mistakes and missing information. Every finding carries its evidence: annotated
figures, a blink comparator that flickers two aligned copies (a reused image stands still), a difference image,
the exact spreadsheet cells, or the quoted text.

The reviewer stays in charge. Each finding can be ruled confirmed, false alarm, needs raw data or not sure, with a
note. Rulings are saved and survive a re-scan. The app exports CSV, Excel and PDF reports, an evidence pack with a
SHA-256 manifest, and polite letter templates that ask authors for raw data.

## How AI is used

AI shows up in two places: as an optional investigator that reviews the detectors' work, and as the subject of
several checks.

**1. The AI investigator (optional, model of your choice).** The detectors are fast but literal. They do not know
that a magnified inset is meant to repeat its parent panel. The investigator is a tool-using agent that works like
an experienced sleuth. It has these tools:

| Tool | What the model can do |
| --- | --- |
| `list_findings` | read every candidate with its measurements |
| `view_evidence` | look at the annotated side-by-side and difference images |
| `view_figure` | see a whole figure with panel outlines, plus its legend |
| `view_panel` | open one panel in original, contrast-stretched, "reveal" or noise view |
| `compare_panels` | ask the engine to align any two panels (mirrored, rotated, rescaled) and score them |
| `search_text` | search the methods and legends ("re-probed", "representative", "inset") |
| `submit_review` | rule on each finding, add anything missed, and draft a cautious public comment |

It reads the legends to tell intended repeats (insets, re-probed membranes, a shared loading control the legend
admits to) from real problems. It rules on each candidate and drafts a comment that describes what is visible and
asks for raw data, never alleging intent. Claude runs through the Anthropic SDK. GPT, Gemini, DeepSeek, Qwen,
Kimi, Mistral and Grok run through any OpenAI-compatible endpoint, and a local Ollama or LM Studio model works with
no key and no data leaving the computer.

**2. Checks for AI-era problems.** Paper Inspector looks for hidden instructions to AI reviewers (white,
microscopic or off-page text, and invisible Unicode tag characters). It also flags references that no index can
find, chatbot boilerplate ("as an AI language model", "certainly, here is"), and a "no AI was used" statement next
to chatbot text.

**3. Classical computer vision does the measuring.** Image matching uses SIFT keypoints over every panel and its
mirror image, RANSAC alignment, and a band-pass "camera noise" correlation. That correlation separates a true copy
from two honest photos of similar tissue. These are deterministic, testable measurements; the language model
reviews and explains them, but it is not the detector.

## Responsible use

- **Candidates, not verdicts.** Every screen shows that a flag needs a person to look at it and that many flags
  have innocent explanations. The score is triage.
- **Precision first.** A detector that cries wolf does harm. The image checks are tuned so that clean papers stay
  clean (0 false alarms on 40 clean synthetic papers in the held-out benchmark), and real papers were used to find
  and fix innocent look-alikes: chart text, colour bars, halftone fills, evenly spaced gel lanes, and the red
  RETRACTED stamp.
- **Prompt-injection defence.** Everything taken from the paper (title, text, legends, quotes) is wrapped in
  tagged blocks that the model is told are untrusted data. Text addressed to "reviewers" or "AI systems" is
  reported as a finding, not obeyed.
- **Careful tone.** Finding text and drafted comments describe what is visible, name common innocent causes, and
  ask for raw data. Nothing says "fraud".
- **Local and private.** The engine runs on the user's own computer and answers only local requests (with a
  required header on every change, to block cross-site requests). Papers, reports and settings stay on that
  computer. API keys are saved only in a local settings file readable by the user alone, never written to reports
  or logs. A local model can be used for a fully offline review.
- **No personal data mining.** File-metadata checks read software names and timestamps, never author or person
  names. No email address is sent to any service unless the user enters one for the polite-pool APIs.
- **Open sources only.** Nothing behind a paywall is bypassed. For a paywalled paper, the user drops in the PDF
  they already have.

## How it was built

- **Engine (Python)**: PyMuPDF for PDFs; OpenCV for panel segmentation, SIFT, RANSAC and noise correlation; SciPy
  for statistics; openpyxl and python-docx for supplements; FastAPI for the local API; optional Tesseract OCR to
  mask text printed inside images.
- **Fetching**: Crossref, OpenAlex, the PubMed Central cloud dataset, Europe PMC, bioRxiv/medRxiv, NCBI
  E-utilities, ClinicalTrials.gov and Cellosaurus. All free and keyless, called politely with retries.
- **Web app**: React, TypeScript and Vite. It has a dark figure stage with zoom, pan and loupe, a full-stage blink
  comparator (blink, swipe, onion skin, difference with gain), keyboard triage, a command palette and deep links.
- **Mac app**: native SwiftUI over the same engine, with a three-column layout, menus and shortcuts, a batch
  queue and exports.
- **Testing**: about 500 automated Python tests and 36 Swift tests. A synthetic benchmark renders realistic fake
  papers (fluorescence, histology, phase contrast, photos, western blots) with one planted manipulation each. A
  real-paper benchmark compares retracted and non-retracted PLOS ONE papers against what each retraction notice
  says.
- **AI coding assistant**: Claude Code was used throughout development.

## Results

- **Synthetic, held-out draw** (`tools/bench.py --n 4 --seed 99`): 92.0% (195/212) of planted image
  manipulations found; 0 false alarms on 40 clean papers.
- **Real papers** (`tools/real_cases.py` + `tools/real_eval.py`): 12 retracted and 12 non-retracted PLOS ONE
  papers, scored against what each retraction notice says. Of the 7 retracted for image problems, the figure the
  notice names was flagged in 4. Median risk score 44 for retracted papers against 0 for non-retracted ones; 7 of 12
  retracted and 3 of 12 non-retracted papers scored 40 or more.
- Real figures are much harder than synthetic ones. The misses are band-level problems inside one blot (flipped
  bands, lane splices) and retracted papers whose PDF is one scanned image per page.
- The real-paper run also found a false alarm, which was then fixed. A theoretical peptide-mass table in one control's
  supplement looked like "copied and scaled" data, because charge-4 m/z is almost exactly ¾ of charge-3 m/z. The
  check now demands that a relation hold to the last printed digit, and skips theoretical m/z and isotope columns.
  Tests pin that fabricated copies in full-precision columns are still caught.

## Challenges

- **False alarms are the whole game.** Each innocent repeat found on real papers needed its own guard: OCR masking,
  line-art detection, an autocorrelation test for halftones, and fetching the original figure files instead of the
  PDF's re-compressed copies.
- **Getting the supplementary files.** Supplements live in many places. The fetcher tries the PMC cloud dataset,
  Europe PMC, bioRxiv, publisher endpoints and the landing page's own metadata, and reports what it found and what
  is missing.
- **Tone.** Writing finding text that is specific and useful without accusing anyone took many passes.

## What's next

- Split scanned-page PDFs into figures, to reach older retracted papers.
- Cross-paper search across an author's other work ("pull the thread").
- A browser extension that runs on any article page.
- A PubPeer integration for users who have a key.

## Try it

See the README for setup. `paperinspector demo demo.pdf` writes a synthetic paper with planted problems, so the
whole flow can be tried offline in a minute.

## Built with

python, opencv, pymupdf, numpy, scipy, fastapi, httpx, openpyxl, python-docx, tesseract, react, typescript, vite,
swiftui, anthropic-claude, openai-compatible APIs, ollama, crossref, openalex, pubmed-central, europe-pmc,
claude-code.
