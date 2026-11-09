"""Command line: `paperinspector doi 10.1371/...`, `paperinspector scan paper.pdf`, `paperinspector case a.pdf b.pdf`,
`paperinspector batch dois.txt`, `paperinspector list [words]`, `paperinspector export RUN --format pdf`,
`paperinspector serve`, `paperinspector demo`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SEV_MARK = {"critical": "!!", "high": "! ", "medium": "~ ", "low": ". ", "info": "  "}


def _progress(stage: str, frac: float) -> None:
    print(f"\r[{frac:4.0%}] {stage:<48}", end="", file=sys.stderr, flush=True)


def _print(report: dict) -> None:
    print(file=sys.stderr)
    p = report["paper"]
    print(f"{p.get('title') or p['source']}")
    print(f"risk {report['score']}/100 - {report['verdict']}")
    print(f"{len(report['figures'])} figures, {report['panels']} panels, {report['timings'].get('total', 0):.1f}s\n")
    for kind, head in (("integrity", "Integrity signals"), ("quality", "Quality notes")):
        rows = [f for f in report["findings"] if f.get("kind", "integrity") == kind and f["severity"] != "info"]
        if rows:
            print(head)
        for f in rows:
            print(f"  {SEV_MARK[f['severity']]} [{f['severity']:<8}] {f['title']}")
    print(f"\nrun folder: {report['id']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="paperinspector", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="screen one paper (PDF) or one figure (PNG/JPG/TIFF)")
    s.add_argument("path")
    s.add_argument("--json", action="store_true", help="print the full report as JSON")
    s.add_argument("--online", action="store_true", help="also ask Crossref about retracted references")
    s.add_argument("--ai", action="store_true", help="add an AI investigator's review (needs a configured model)")
    s.add_argument("--supplement", "-s", action="append", default=[], help="a supplementary file (repeatable)")
    o = sub.add_parser("doi", help="fetch a paper and its supplementary files by DOI and screen everything")
    o.add_argument("doi")
    o.add_argument("--json", action="store_true")
    o.add_argument("--ai", action="store_true", help="add an AI investigator's review")
    o.add_argument("--no-online", action="store_true",
                   help="skip the reference and publication-record lookups (fetching still uses the network)")
    c = sub.add_parser("case", help="compare images across several papers")
    c.add_argument("paths", nargs="+")
    c.add_argument("--json", action="store_true")
    v = sub.add_parser("serve", help="start the local web app")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--host", default="127.0.0.1")
    b = sub.add_parser("batch", help="screen many papers, one at a time, from a file of DOIs (one per line, or a "
                                     "BibTeX/RIS export)")
    b.add_argument("file", help="a text file, or - to read from standard input")
    b.add_argument("--no-online", action="store_true", help="skip the reference and publication-record lookups")
    b.add_argument("--csv", help="also write a triage table to this CSV file")
    ls = sub.add_parser("list", help="list past scans, optionally only those matching some words or a tag")
    ls.add_argument("words", nargs="*")
    ls.add_argument("--tag", action="append", default=[])
    x = sub.add_parser("export", help="export one scan's findings or evidence")
    x.add_argument("run", help="the run id (see `list`)")
    x.add_argument("--format", "-f", choices=("csv", "xlsx", "pdf", "zip"), default="pdf")
    x.add_argument("--out", "-o", help="file to write (default: paperinspector-RUN.<format> here)")
    x.add_argument("--notes", action="store_true", help="include your private notes on findings")
    d = sub.add_parser("demo", help="write the synthetic demo paper")
    d.add_argument("out", nargs="?", default="demo_paper.pdf")
    a = ap.parse_args(argv)
    if os.environ.get("PAPERINSPECTOR_DEBUG"):
        return _run(a)
    try:
        return _run(a)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError) as e:
        print(f"\npaperinspector: {e}", file=sys.stderr)
        return 2
    except Exception as e:              # network errors and the like: one line, not a traceback
        print(f"\npaperinspector: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    if a.cmd == "scan":
        from .pipeline import scan
        r = scan(a.path, progress=_progress, online=a.online, supplements=a.supplement)
        if a.ai:
            from .agent import investigate
            r = investigate(r["id"], progress=_progress)
        print(json.dumps(r, indent=1)) if a.json else _print(r)
    elif a.cmd == "doi":
        from .pipeline import scan_doi
        r = scan_doi(a.doi, progress=_progress, online=not a.no_online)
        if a.ai:
            from .agent import investigate
            r = investigate(r["id"], progress=_progress)
        print(json.dumps(r, indent=1)) if a.json else _print(r)
    elif a.cmd == "case":
        from .pipeline import scan_case
        r = scan_case(a.paths, progress=_progress)
        print(json.dumps(r, indent=1)) if a.json else _print(r)
    elif a.cmd == "batch":
        return _batch(a)
    elif a.cmd == "list":
        from .library import search
        for r in search(" ".join(a.words), a.tag):
            tags = f"  [{', '.join(r['tags'])}]" if r["tags"] else ""
            print(f"{r['id']}  {r['score']:>3}  {r['title'][:70]}{tags}")
    elif a.cmd == "export":
        return _export(a)
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("paperinspector.server:app", host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "demo":
        from .synth import demo_paper, demo_supplement, to_pdf_file
        to_pdf_file(demo_paper(), a.out)
        supp = Path(a.out).with_name("Supplementary_Data_1.xlsx")
        demo_supplement(supp)
        print(Path(a.out).resolve())
        print(supp.resolve())
    return 0


def _batch(a) -> int:
    from .batchqueue import parse_entries, resolve
    from .pipeline import scan_doi
    text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(errors="replace")
    entries, skipped = parse_entries(text=text)
    for s_ in skipped:
        print(f"skipped {s_['input'][:60]}: {s_['reason']}", file=sys.stderr)
    rows = []
    for k, e in enumerate(entries, 1):
        print(f"\n[{k}/{len(entries)}] {e['input'][:70]}", file=sys.stderr)
        row = {"input": e["input"], "doi": e.get("doi"), "status": "error"}
        try:
            r = scan_doi(resolve(e), progress=_progress, online=not a.no_online)
            p = r.get("paper") or {}
            row.update(status="done", run=r["id"], title=p.get("title") or p.get("source"), score=r.get("score"))
            print(f"\n  risk {r['score']}/100 - {r['verdict']}", file=sys.stderr)
        except KeyboardInterrupt:
            raise
        except Exception as ex:          # noqa: BLE001 - one bad paper must not stop the batch
            row["error"] = str(ex) or type(ex).__name__
            print(f"\n  failed: {row['error']}", file=sys.stderr)
        rows.append(row)
    if a.csv:
        from .exports import batch_csv
        from .pipeline import RUNS
        Path(a.csv).write_bytes(batch_csv(rows, RUNS))
    done = sum(r["status"] == "done" for r in rows)
    print(f"\n{done} of {len(rows)} scanned", file=sys.stderr)
    for r in rows:
        print(f"{r.get('run') or '-':<40} {str(r.get('score', '')):>3}  {r['input'][:60]}")
    return 0 if done == len(rows) else 1


def _export(a) -> int:
    from . import exports
    from .pipeline import RUNS
    run = RUNS / Path(a.run).name
    if not (run / "report.json").is_file():
        raise ValueError(f"no such run: {a.run}")
    out = Path(a.out or f"paperinspector-{run.name}.{a.format}")
    if a.format == "csv":
        out.write_bytes(exports.findings_csv(exports.load(run), include_notes=a.notes))
    elif a.format == "xlsx":
        out.write_bytes(exports.findings_xlsx(exports.load(run)))
    elif a.format == "pdf":
        out.write_bytes(exports.report_pdf(run, include_notes=a.notes))
    else:
        import shutil
        tmp = exports.evidence_zip(run, out.resolve().parent, include_notes=a.notes)
        shutil.move(tmp, out)
    print(out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
