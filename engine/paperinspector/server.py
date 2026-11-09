"""Local HTTP API for the web and Mac apps.  Binds to 127.0.0.1; papers never leave the computer unless the user
turns on the AI investigator (sends figures to the chosen model) or the Crossref check (sends cited DOIs).

Any web page the user visits can aim requests at 127.0.0.1, so the API answers only loopback Host names (a DNS
rebinding page arrives under its own name), and every POST or DELETE must carry ``X-Paper-Inspector: 1``: a custom
header forces a CORS preflight that other origins fail, so a cross-site form can neither start jobs nor change
settings.  Files that came from an upload or the web are served so that nothing in them can run on this origin."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import cv2
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import Headers, MutableHeaders

from . import (__version__, batchqueue, catalog, exports, forensics, imgdup, letters, library, pdfdoc, pipeline,
               report as reportmod, rulings, settings)
from .evidence import aligned, crop_valid, difference
from .models import RULINGS, Cancelled, Panel

HOSTS = {"127.0.0.1", "localhost", "[::1]"}
HEADER = "X-Paper-Inspector"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_UPLOAD = 512 * 1024 * 1024         # one request: a paper and all its supplements
MAX_FILES = 60
WORKERS = 2                            # scans are CPU- and memory-heavy; more at once only thrashes
MAX_PENDING = 8                        # queued + running; beyond this a new job gets 429
JOB_TTL = 3600                         # finished jobs are forgotten after an hour...
MAX_JOBS = 200                         # ...or sooner once this many are kept
COMPARE_CACHE = 32
SANDBOX = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
TOO_BIG = f"upload larger than {MAX_UPLOAD // 2 ** 20} MB"
ORPHAN_AGE = 24 * 3600                 # a run folder with no report this old was left by a scan that never finished


def _hostname(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[:host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _allowed_hosts() -> set[str]:
    """TestClient calls itself "testserver"; it is let in only when the test suite says so."""
    return HOSTS | {"testserver"} if os.environ.get("PAPERINSPECTOR_TESTING") == "1" else HOSTS


class Guard:
    """Host allowlist, the custom-header check and a request size cap, ahead of everything (CORS included);
    stamps nosniff on every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        n = headers.get("content-length")
        problem = None
        if _hostname(headers.get("host", "")) not in _allowed_hosts():
            problem = 400, "this server only answers requests addressed to 127.0.0.1 or localhost"
        elif scope["method"] not in SAFE_METHODS and headers.get(HEADER) != "1":
            problem = 403, f"missing {HEADER} header"
        elif n is not None and (not n.isdigit() or int(n) > MAX_UPLOAD):
            problem = 413, TOO_BIG
        if problem:
            resp = JSONResponse({"detail": problem[1]}, problem[0], headers={"X-Content-Type-Options": "nosniff"})
            return await resp(scope, receive, send)

        seen = 0

        async def counted():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > MAX_UPLOAD:  # a chunked upload carries no Content-Length
                    raise HTTPException(413, TOO_BIG)
            return msg

        async def stamped(msg):
            if msg["type"] == "http.response.start":
                MutableHeaders(scope=msg).setdefault("X-Content-Type-Options", "nosniff")
            await send(msg)

        await self.app(scope, counted, stamped)


@asynccontextmanager
async def _lifespan(_app):
    """Start the batch queue (it resumes whatever was waiting when the app last closed) and tidy left-overs."""
    _sweep_orphans(time.time() - ORPHAN_AGE)
    QUEUE.start()
    try:
        yield
    finally:
        QUEUE.stop(timeout=2)


app = FastAPI(title="Paper Inspector", version=__version__, lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", HEADER])
app.add_middleware(Guard)

JOBS: dict[str, dict] = {}
UPLOADS = pipeline.HOME / "uploads"
POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="paperinspector-job")
_JOBS_LOCK = threading.Lock()
BUSY = "too many scans waiting; try again when one finishes"


_CANCEL: dict[str, threading.Event] = {}
_CLEANUP: dict[str, Callable[[], None]] = {}


def _pending() -> int:
    return sum(j["status"] == "running" for j in list(JOBS.values()))


def _prune(now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    done = sorted((j for j in list(JOBS.values()) if j["status"] != "running"), key=lambda j: j["finished"] or 0)
    for j in done:
        if now - (j["finished"] or 0) > JOB_TTL or len(JOBS) > MAX_JOBS:
            JOBS.pop(j["id"], None)


def _admit() -> None:
    """Refuse before an upload is copied, not after, when the queue is already full."""
    if _pending() >= MAX_PENDING:
        raise HTTPException(429, BUSY)


def _job(kind: str, fn, *args, cleanup: Optional[Callable[[], None]] = None, cancellable: bool = False) -> dict:
    """Queue fn on the small worker pool; cleanup (the job's upload folder) runs once it ends or is refused.
    cancellable: fn also takes should_cancel=, polled inside long downloads so a cancel does not wait for them."""
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "kind": kind, "stage": "Queued", "progress": 0.0, "status": "running", "run": None,
           "error": None, "started": time.time(), "finished": None}
    cancel = threading.Event()
    done_once = threading.Lock()

    def tidy():
        if cleanup and done_once.acquire(blocking=False):
            cleanup()

    with _JOBS_LOCK:
        _prune()
        if _pending() >= MAX_PENDING:
            if cleanup:
                cleanup()
            raise HTTPException(429, BUSY)
        JOBS[jid] = job
        _CANCEL[jid] = cancel
        _CLEANUP[jid] = tidy

    def prog(stage, frac):
        if cancel.is_set():
            raise Cancelled()
        job["stage"], job["progress"] = stage, round(float(frac), 3)

    def work():
        with _JOBS_LOCK:
            if job["status"] != "running":      # cancelled while it waited for a worker
                return
            job["began"] = time.time()
        end = {"status": "error"}
        try:
            if cancel.is_set():
                raise Cancelled()
            kw = {"should_cancel": cancel.is_set} if cancellable else {}
            end = {"run": fn(*args, progress=prog, **kw)["id"], "status": "done"}
        except batchqueue.CANCELLED:
            end = {"status": "cancelled", "stage": "Cancelled"}
            _sweep_partial(job["began"], jid)
        except Exception as e:
            end["error"] = str(e) or type(e).__name__
            traceback.print_exc()
        finally:
            tidy()
            _CANCEL.pop(jid, None)
            _CLEANUP.pop(jid, None)
            job.update(finished=time.time(), **end)  # status last: a poller that sees "done" sees everything

    POOL.submit(work)
    return job


def _cancel_job(jid: str) -> dict:
    """A job still waiting for a worker stops at once; a running one at its next step (the step in progress
    finishes first, so this can take a few seconds)."""
    with _JOBS_LOCK:
        job = JOBS.get(jid)
        if job is None:
            raise HTTPException(404, "no such job")
        if job["status"] != "running":
            return job
        ev = _CANCEL.get(jid)
        if ev is not None:
            ev.set()
        if "began" not in job:
            tidy = _CLEANUP.pop(jid, None)
            _CANCEL.pop(jid, None)
            job.update(stage="Cancelled", finished=time.time(), status="cancelled")
        else:
            job["stage"] = "Cancelling..."
            tidy = None
    if tidy:
        tidy()
    return job


def _sweep_partial(since: float, me: str) -> None:
    """Remove the half-written run folder a cancelled scan leaves (no report.json, made after it began).  Only when
    no other scan in this server is running, so a folder that belongs to one is never touched."""
    if any(j["status"] == "running" and j["id"] != me for j in list(JOBS.values())) or QUEUE.list()["running"]:
        return
    _sweep_orphans(None, since)


def _sweep_orphans(before: Optional[float], since: Optional[float] = None) -> None:
    if not pipeline.RUNS.exists():
        return
    for d in pipeline.RUNS.iterdir():
        try:
            if not d.is_dir() or d.is_symlink() or (d / "report.json").exists():
                continue
            mt = d.stat().st_mtime
            if (before is not None and mt < before) or (since is not None and mt >= since - 1):
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def _run_dir(rid: str) -> Path:
    d = pipeline.RUNS / rid
    if "/" in rid or "\\" in rid or ".." in rid or not (d / "report.json").exists():
        raise HTTPException(404, "no such run")
    return d


def _uploads() -> Path:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    return UPLOADS


PAPER_SUFFIXES = {".pdf"} | pdfdoc.IMAGE_SUFFIXES


def _sniff(head: bytes) -> Optional[str]:
    """What the bytes really are, for the few types a browser may show inline; None for anything else."""
    if b"%PDF-" in head[:1024]:
        return "application/pdf"
    for magic, kind in ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                        (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"II*\x00", "image/tiff"),
                        (b"MM\x00*", "image/tiff"), (b"BM", "image/bmp")):
        if head.startswith(magic):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class Uploads:
    """One request's files in one temp folder, deleted when its job ends, under one shared byte budget."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(dir=_uploads()))
        self.left = MAX_UPLOAD
        self.n = 0

    def save(self, f: UploadFile, paper: bool = False) -> Path:
        """paper: the file is read as the article itself, so it must be a real PDF or image, whatever its name says."""
        self.n += 1
        if self.n > MAX_FILES:
            raise HTTPException(413, f"at most {MAX_FILES} files per upload")
        name = Path(f.filename or "upload.pdf").name.strip().lstrip(".") or "upload.pdf"
        if len(name) > 160:
            name = name[:150] + Path(name).suffix[:10]
        suffix = Path(name).suffix.lower()
        if paper and suffix not in PAPER_SUFFIXES:
            raise HTTPException(400, f"{name}: upload the paper as a PDF or an image")
        d = self.dir / str(self.n)
        d.mkdir()
        p = d / name
        with open(p, "wb") as out:
            while chunk := f.file.read(1 << 20):
                self.left -= len(chunk)
                if self.left < 0:
                    raise HTTPException(413, TOO_BIG)
                out.write(chunk)
        if paper:
            with open(p, "rb") as fh:
                kind = _sniff(fh.read(1024))
            if kind is None or (kind == "application/pdf") != (suffix == ".pdf"):
                raise HTTPException(400, f"{name} is not a readable {'PDF' if suffix == '.pdf' else 'image'}")
        return p

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _user_file(p: Path, attachment: bool = False, cache: bool = False) -> FileResponse:
    """A file that came from an upload or the web.  Only bytes that really are a PDF or a raster image go out inline,
    under that type; anything else (HTML, SVG, a script renamed .pdf) downloads as opaque bytes.  The sandbox CSP
    gives even an inline file an opaque origin, so it can never script this API."""
    with open(p, "rb") as fh:
        kind = _sniff(fh.read(1024))
    headers = {"Content-Security-Policy": SANDBOX, "X-Content-Type-Options": "nosniff"}
    if cache:
        headers["Cache-Control"] = "max-age=3600"
    inline = kind is not None and not attachment
    return FileResponse(p, media_type=kind or "application/octet-stream", headers=headers, filename=p.name,
                        content_disposition_type="inline" if inline else "attachment")


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__}


@app.post("/api/scan")
def scan(file: UploadFile = File(...), supplements: list[UploadFile] = File(default=[]),
         online: bool = Query(False)):
    """One paper (PDF or a figure image), optionally with its supplementary files.  Plain def: copying a large
    upload runs in the threadpool instead of stalling the event loop."""
    _admit()
    up = Uploads()
    try:
        path = up.save(file, paper=True)
        supp = [up.save(f) for f in supplements]
    except BaseException:
        up.discard()
        raise
    return _job("scan", lambda p, progress: pipeline.scan(p, progress=progress, online=online, supplements=supp),
                path, cleanup=up.discard)


class DOIRequest(BaseModel):
    doi: str


class ResolveRequest(BaseModel):
    text: str


RESOLVE_MAX = 2000                     # characters: one identifier, link, title or citation
_RESOLVE_LOCK = threading.Lock()       # one lookup at a time, so the app stays polite to the services it asks


def _resolve(text: str) -> dict:
    from . import inputs
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "Paste a DOI, PubMed or PMC ID, arXiv ID, link or title.")
    if len(text) > RESOLVE_MAX:
        raise HTTPException(400, f"That is too long for one paper (over {RESOLVE_MAX} characters). Paste a list "
                                 "into the batch queue instead.")
    parsed = inputs.parse_input(text)
    if parsed.get("kind") in ("doi", "arxiv"):
        r = inputs.resolve(text)       # no network needed
    else:
        with _RESOLVE_LOCK:
            r = inputs.resolve(text)
    r.setdefault("label", parsed.get("label") or "")
    return r


@app.post("/api/resolve")
def resolve(req: ResolveRequest):
    """Turn a pasted DOI, PMID, PMCID, arXiv ID, link, title or citation into a DOI, or a short list to pick from.
    Only DOIs and arXiv IDs are read offline; the rest asks NCBI, Europe PMC, Crossref or the page itself."""
    return _resolve(req.text)


@app.post("/api/doi")
def scan_doi(req: DOIRequest):
    """Paste a DOI (or a PMID, PMCID, arXiv ID or link that leads to one): fetch the paper and its supplementary
    files from open sources, then screen everything."""
    from .fetch import normalize
    try:
        doi = normalize(req.doi)
    except ValueError as e:
        r = _resolve(req.doi) if (req.doi or "").strip() else None
        if r is None:
            raise HTTPException(400, str(e))
        if not r.get("doi"):
            if r.get("error") == "network":
                raise HTTPException(503, r.get("message") or "The lookup service could not be reached.")
            if r.get("candidates"):
                raise HTTPException(422, (r.get("message") or "More than one paper matches.") +
                                    " Pick one from the list (POST /api/resolve returns it).")
            raise HTTPException(400, r.get("message") or str(e))
        doi = r["doi"]
    return _job("doi", lambda d, progress, should_cancel=None:
                pipeline.scan_doi(d, progress=progress, should_cancel=should_cancel), doi, cancellable=True)


@app.post("/api/case")
def case(files: list[UploadFile] = File(...)):
    if len(files) < 2:
        raise HTTPException(400, "a case needs at least two papers")
    _admit()
    up = Uploads()
    try:
        paths = [up.save(f, paper=True) for f in files]
    except BaseException:
        up.discard()
        raise
    return _job("case", lambda ps, progress: pipeline.scan_case(ps, progress=progress), paths, cleanup=up.discard)


@app.post("/api/demo")
def demo():
    from .synth import demo_paper, demo_supplement, to_pdf_file
    _admit()
    up = Uploads()
    p = up.dir / "paperinspector-demo-paper.pdf"
    s = up.dir / "Supplementary_Data_1.xlsx"
    try:
        to_pdf_file(demo_paper(), p)
        demo_supplement(s)
    except BaseException:
        up.discard()
        raise
    return _job("scan", lambda p, progress: pipeline.scan(p, progress=progress, supplements=[s]), p,
                cleanup=up.discard)


@app.get("/api/jobs")
def jobs():
    """Scans started from this app session, newest first (finished ones are kept for an hour)."""
    return sorted(list(JOBS.values()), key=lambda j: j.get("started") or 0, reverse=True)


@app.get("/api/jobs/{jid}")
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "no such job")
    return JOBS[jid]


@app.delete("/api/jobs/{jid}")
def cancel_job(jid: str):
    return _cancel_job(jid)


@app.post("/api/jobs/{jid}/cancel")
def cancel_job_post(jid: str):
    return _cancel_job(jid)


# --- the batch queue ----------------------------------------------------------------------------------------------

def _queue_runner(item: dict, progress) -> dict:
    progress("Finding the DOI", 0.0)
    doi = batchqueue.resolve(item, should_cancel=lambda: QUEUE.cancelling(item["id"]))
    return pipeline.scan_doi(doi, progress=progress, online=bool(item.get("online", True)),
                             should_cancel=lambda: QUEUE.cancelling(item["id"]))


def _queue_cancelled(began: float) -> None:
    if not any(j["status"] == "running" for j in list(JOBS.values())):
        _sweep_orphans(None, began)


QUEUE = batchqueue.BatchQueue(pipeline.HOME / "queue.json", lambda item, progress: _queue_runner(item, progress),
                              may_start=lambda: _pending() < WORKERS, on_cancelled=_queue_cancelled)


class QueueRequest(BaseModel):
    items: list[str] = []
    text: str = ""
    name: str = ""
    online: bool = True


@app.post("/api/queue")
def queue_add(req: QueueRequest):
    """Queue many papers: a list of DOIs (or other identifiers) and/or pasted text, one per line."""
    if len(req.items) > batchqueue.MAX_BATCH or len(req.text) > 2_000_000:
        raise HTTPException(413, f"at most {batchqueue.MAX_BATCH} papers at once")
    entries, skipped = batchqueue.parse_entries(req.items, req.text)
    if not entries:
        raise HTTPException(400, "no DOIs found in what was sent")
    try:
        out = QUEUE.add(entries, req.name, req.online)
    except ValueError as e:
        raise HTTPException(400, str(e))
    QUEUE.start()
    out["skipped"] = skipped + out["skipped"]
    return out


@app.get("/api/queue")
def queue_list(batch: Optional[str] = None):
    QUEUE.start()
    return QUEUE.list(batch)


@app.post("/api/queue/clear")
def queue_clear():
    return {"removed": QUEUE.clear()}


@app.get("/api/queue/batches/{bid}.csv")
def queue_batch_csv(bid: str):
    items = QUEUE.list(bid)["items"]
    if not items:
        raise HTTPException(404, "no such batch")
    return _download(exports.batch_csv(items, pipeline.RUNS), f"paperinspector-batch-{bid}.csv", "text/csv")


@app.get("/api/queue/{qid}")
def queue_item(qid: str):
    it = QUEUE.get(qid)
    if it is None:
        raise HTTPException(404, "no such queue item")
    return it


@app.post("/api/queue/{qid}/cancel")
def queue_cancel(qid: str):
    it = QUEUE.cancel(qid)
    if it is None:
        raise HTTPException(404, "no such queue item")
    return it


@app.post("/api/queue/{qid}/retry")
def queue_retry(qid: str):
    it = QUEUE.retry(qid)
    if it is None:
        raise HTTPException(404, "no such queue item")
    return it


@app.delete("/api/queue/{qid}")
def queue_remove(qid: str):
    if not QUEUE.remove(qid):
        raise HTTPException(404, "no such queue item")
    return {"ok": True}


# --- the library: runs, search, tags --------------------------------------------------------------------------

@app.get("/api/runs")
def runs():
    out = pipeline.list_runs()
    tags = library.tag_map()
    for r in out:
        r["tags"] = tags.get(r["id"], [])
    return out


@app.get("/api/runs/search")
def search_runs(q: str = "", tag: list[str] = Query(default=[]), min_score: Optional[int] = None,
                max_score: Optional[int] = None, sort: str = "created", limit: int = 200):
    """Search past scans: every word of q must appear in the title, DOI, authors, journal, file name or tags."""
    found = library.search(q, tag, min_score, max_score, sort, limit)
    return {"runs": found, "total": len(found), "tags": library.all_tags()}


@app.get("/api/tags")
def all_tags():
    return {"tags": library.all_tags()}


@app.get("/api/runs/{rid}")
def run(rid: str):
    """The report, plus each finding's fingerprint and ruling, a review summary and the run's tags."""
    d = _run_dir(rid)
    rep = rulings.annotate(json.loads((d / "report.json").read_text()), d)
    rep["tags"] = library.tags_of(rid)
    return JSONResponse(rep)


@app.delete("/api/runs/{rid}")
def delete_run(rid: str):
    shutil.rmtree(_run_dir(rid))
    library.forget(rid)
    with _COMPARE_LOCK:
        for key in [k for k in _COMPARE if k[0] == rid]:
            del _COMPARE[key]
    return {"ok": True}


class TagEdit(BaseModel):
    add: list[str] = []
    remove: list[str] = []
    set: Optional[list[str]] = None


@app.get("/api/runs/{rid}/tags")
def run_tags(rid: str):
    _run_dir(rid)
    return {"tags": library.tags_of(rid)}


@app.post("/api/runs/{rid}/tags")
def edit_run_tags(rid: str, req: TagEdit):
    _run_dir(rid)
    try:
        return {"tags": library.edit_tags(rid, req.add, req.remove, req.set)}
    except ValueError as e:
        raise HTTPException(400, str(e))


# --- rulings ----------------------------------------------------------------------------------------------------

def _report(rid: str) -> tuple[Path, dict]:
    d = _run_dir(rid)
    return d, json.loads((d / "report.json").read_text())


@app.get("/api/runs/{rid}/rulings")
def get_rulings(rid: str):
    d, rep = _report(rid)
    return rulings.for_run(rep, d)


class RulingRequest(BaseModel):
    ruling: str
    note: Optional[str] = None


def _rule(rid: str, fid: str, ruling: str, note: Optional[str]) -> dict:
    d, rep = _report(rid)
    try:
        rec = rulings.set_ruling(rep, d, fid, ruling, note)
    except KeyError:
        raise HTTPException(404, "no such finding")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"finding": fid, "fingerprint": rec["fingerprint"],
            "ruling": {k: rec.get(k) for k in ("ruling", "note", "updated")},
            "review": rulings.annotate(rep, d)["review"]}


@app.post("/api/runs/{rid}/findings/{fid}/ruling")
def set_ruling(rid: str, fid: str, req: RulingRequest):
    if req.ruling not in RULINGS:
        raise HTTPException(400, f"ruling must be one of {', '.join(RULINGS)}")
    return _rule(rid, fid, req.ruling, req.note)


@app.delete("/api/runs/{rid}/findings/{fid}/ruling")
def clear_ruling(rid: str, fid: str):
    return _rule(rid, fid, "unreviewed", "")


# --- exports and letters ----------------------------------------------------------------------------------------

def _download(data: bytes, name: str, media: str) -> Response:
    return Response(data, media_type=media, headers={"Content-Disposition": f"attachment; filename={name}",
                                                     "Content-Security-Policy": SANDBOX,
                                                     "X-Content-Type-Options": "nosniff"})


@app.get("/api/runs/{rid}/export/findings.csv")
def export_csv(rid: str, info: bool = False, notes: bool = True):
    d = _run_dir(rid)
    return _download(exports.findings_csv(exports.load(d), info, notes), f"paperinspector-{rid}.csv",
                     "text/csv; charset=utf-8")


@app.get("/api/runs/{rid}/export/findings.xlsx")
def export_xlsx(rid: str, info: bool = False):
    d = _run_dir(rid)
    return _download(exports.findings_xlsx(exports.load(d), info), f"paperinspector-{rid}.xlsx",
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/runs/{rid}/export/report.pdf")
def export_pdf(rid: str, info: bool = False, notes: bool = False):
    d = _run_dir(rid)
    return _download(exports.report_pdf(d, include_info=info, include_notes=notes), f"paperinspector-{rid}.pdf",
                     "application/pdf")


@app.get("/api/runs/{rid}/export/evidence.zip")
def export_zip(rid: str, notes: bool = False):
    d = _run_dir(rid)
    tmp = exports.evidence_zip(d, _uploads(), include_notes=notes)
    return FileResponse(tmp, media_type="application/zip", filename=f"paperinspector-{rid}-evidence.zip",
                        headers={"X-Content-Type-Options": "nosniff"},
                        background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))


@app.get("/api/letters")
def letter_templates():
    return {"templates": letters.templates()}


@app.get("/api/runs/{rid}/letters/{kind}")
def letter(rid: str, kind: str, needs_data: bool = True):
    if kind not in letters.TEMPLATES:
        raise HTTPException(404, "no such letter template")
    d, rep = _report(rid)
    return letters.draft(rulings.annotate(rep, d), kind, needs_data)


# --- the check catalogue ----------------------------------------------------------------------------------------

@app.get("/api/catalog")
def check_catalog():
    from .models import CATEGORIES, RULING_LABELS
    return {"checks": catalog.all_checks(), "categories": CATEGORIES,
            "rulings": [{"id": r, "label": RULING_LABELS[r]} for r in RULINGS]}


@app.get("/api/catalog/{check}")
def check_entry(check: str):
    return catalog.explain(check[:80])


@app.get("/api/runs/{rid}/figures/{fid}.png")
def figure(rid: str, fid: str):
    p = _run_dir(rid) / "figures" / f"{Path(fid).name}.png"
    if not p.exists():
        raise HTTPException(404)
    return _user_file(p, cache=True)


@app.get("/api/runs/{rid}/evidence/{name}")
def evidence_file(rid: str, name: str):
    p = _run_dir(rid) / "evidence" / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return _user_file(p, cache=True)


def _panel(rid: str, pid: str):
    d = _run_dir(rid)
    rep = json.loads((d / "report.json").read_text())
    for f in rep["figures"]:
        for p in f["panels"]:
            if p["id"] == pid:
                img = cv2.imread(str(d / "figures" / f"{Path(f['id']).name}.png"))
                if img is None:
                    raise HTTPException(404, "figure image missing")
                x, y, w, h = (int(v) for v in p["box"])
                return p, img[y:y + h, x:x + w]
    raise HTTPException(404, "no such panel")


def _png(img) -> Response:
    ok, buf = cv2.imencode(".png", img)
    return Response(buf.tobytes(), media_type="image/png")


@app.get("/api/runs/{rid}/panels/{pid}.png")
def panel_view(rid: str, pid: str, mode: str = "original"):
    if mode not in forensics.VIEW_MODES:
        raise HTTPException(400, f"mode must be one of {forensics.VIEW_MODES}")
    _, img = _panel(rid, pid)
    return _png(forensics.view(img, mode))


@app.get("/api/runs/{rid}/figures/{fid}/view.png")
def figure_view(rid: str, fid: str, mode: str = "original"):
    if mode not in forensics.VIEW_MODES:
        raise HTTPException(400)
    img = cv2.imread(str(_run_dir(rid) / "figures" / f"{Path(fid).name}.png"))
    if img is None:
        raise HTTPException(404)
    return _png(forensics.view(img, mode))


_COMPARE: OrderedDict[tuple, dict] = OrderedDict()
_COMPARE_LOCK = threading.Lock()


def _compared(rid: str, a: str, b: str) -> dict:
    """Least-recently-used cache of aligned pairs: each holds two full-size frames, so it stays small."""
    _run_dir(rid)
    key = (rid, a, b)
    with _COMPARE_LOCK:
        if key in _COMPARE:
            _COMPARE.move_to_end(key)
            return _COMPARE[key]
    pa_meta, ia = _panel(rid, a)
    pb_meta, ib = _panel(rid, b)
    fa = imgdup.features(Panel(a, pa_meta["figure"], None, "other", ia))  # type: ignore[arg-type]
    fb = imgdup.features(Panel(b, pb_meta["figure"], None, "other", ib))  # type: ignore[arg-type]
    best = None
    if fa and fb:
        for flip in (False, True):
            m = imgdup.verify(fa, fb, flip)
            if m and (best is None or m.ncc_dog > best.ncc_dog):
                best = m
    if best is None:
        h = 320
        ra = cv2.resize(ia, (int(ia.shape[1] * h / ia.shape[0]), h))
        rb = cv2.resize(ib, (ra.shape[1], h))
        out = {"aligned": False, "_a": ra, "_b": rb}
    else:
        ref, warped, valid = aligned(ia, ib, best.M, best.flip)
        r, w = crop_valid(ref, warped, valid)
        out = {"aligned": True, "inliers": best.inliers, "ncc_texture": round(best.ncc_dog, 3),
               "mirrored": best.flip, "rotation": round(best.geo["rotation"], 1), "scale": round(best.geo["scale"], 3),
               "overlap_a": round(best.frac_a, 3), "overlap_b": round(best.frac_b, 3), "_a": r, "_b": w}
    with _COMPARE_LOCK:
        _COMPARE[key] = out
        while len(_COMPARE) > COMPARE_CACHE:
            _COMPARE.popitem(last=False)
    return out


@app.get("/api/runs/{rid}/compare")
def compare(rid: str, a: str, b: str):
    """The manual blink comparator: align any two panels if they share content.  Frames are fetched separately."""
    return {k: v for k, v in _compared(rid, a, b).items() if not k.startswith("_")}


@app.get("/api/runs/{rid}/compare/{which}.png")
def compare_frame(rid: str, which: str, a: str, b: str):
    if which not in ("a", "b", "diff"):
        raise HTTPException(404)
    c = _compared(rid, a, b)
    if which == "diff":
        return _png(difference(c["_a"], c["_b"]))
    return _png(c["_" + which])


@app.post("/api/runs/{rid}/investigate")
def investigate(rid: str):
    _run_dir(rid)
    from .agent import investigate as inv
    return _job("investigate", lambda r, progress: inv(r, progress=progress), rid)


@app.get("/api/runs/{rid}/pubpeer", response_class=PlainTextResponse)
def pubpeer(rid: str):
    d, rep = _report(rid)
    return reportmod.pubpeer(rulings.annotate(rep, d))


REPORT_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


@app.get("/api/runs/{rid}/report.html", response_class=HTMLResponse)
def report_html(rid: str, download: bool = False):
    """Self-contained (images inlined, no script), so a CSP that allows nothing else costs nothing and stops any
    paper text that slipped through escaping from doing harm."""
    html = reportmod.html_report(_run_dir(rid))
    headers = {"Content-Security-Policy": REPORT_CSP}
    if download:
        headers["Content-Disposition"] = f"attachment; filename=paperinspector-{rid}.html"
    return HTMLResponse(html, headers=headers)


@app.get("/api/runs/{rid}/supplements/{name}")
def supplement(rid: str, name: str):
    p = _run_dir(rid) / "supplements" / Path(name).name
    if not p.is_file():
        raise HTTPException(404)
    return _user_file(p, attachment=True)


@app.get("/api/runs/{rid}/source")
def source(rid: str):
    p = next(_run_dir(rid).glob("source.*"), None)
    if p is None:
        raise HTTPException(404)
    return _user_file(p)


class SettingsPatch(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    vision: Optional[bool] = None
    effort: Optional[str] = None
    online: Optional[bool] = None
    email: Optional[str] = None
    keys: Optional[dict[str, str]] = None


@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.post("/api/settings")
def set_settings(patch: SettingsPatch):
    settings.save({k: v for k, v in patch.model_dump().items() if v is not None})
    return settings.public()


WEB = Path(__file__).resolve().parents[2] / "web" / "dist"
if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
