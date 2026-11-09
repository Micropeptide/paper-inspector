"""A persistent scan queue for many papers at once: paste a list of DOIs, they run one at a time, and the list
survives a restart of the app (a scan that was running when the app closed starts again from the beginning).

The queue is saved to <data dir>/queue.json after every change of status.  Progress within a scan is kept in memory
only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Callable, Optional

from .models import Cancelled

try:                                    # the fetcher's own stop signal (raised when its should_cancel() turns true)
    from .fetch import Cancelled as _FetchCancelled
    CANCELLED: tuple = (Cancelled, _FetchCancelled)
except Exception:                       # noqa: BLE001
    CANCELLED = (Cancelled,)

MAX_ITEMS = 1000          # everything the queue remembers, finished items included
MAX_BATCH = 300           # one request
FINISHED = ("done", "error", "cancelled")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>{},;]+", re.I)
# identifiers the input module may turn into a DOI (PubMed, PMC, arXiv ids, article links)
OTHER_ID = re.compile(r"^(?:\d{5,9}|PMC\d{4,10}|arxiv:\s*\S+|\d{4}\.\d{4,5}(?:v\d+)?|https?://\S+)$", re.I)


def _inputs():
    """The input module (DOI, PMID, PMC, arXiv, link -> DOI) when it is installed; None otherwise."""
    try:
        from . import inputs  # type: ignore[attr-defined]
        return inputs
    except Exception:          # noqa: BLE001 - written in parallel; absent or broken means "DOIs only"
        return None


def _doi_of(x) -> Optional[str]:
    """A DOI out of whatever the input module returns: a string, a dict ({"doi": ...}, or {"kind": "doi",
    "value": ...} from inputs.parse_input) or an object with a .doi."""
    if isinstance(x, str):
        m = DOI_RE.search(x)
        return m.group(0) if m else None
    if isinstance(x, dict):
        v = x.get("doi") or (x.get("value") if x.get("kind") == "doi" else None)
    else:
        v = getattr(x, "doi", None)
    return v if isinstance(v, str) and v.startswith("10.") else None


def _normalize(doi: str) -> str:
    from .fetch import normalize
    return normalize(doi)


def parse_entries(items: Optional[list[str]] = None, text: str = "") -> tuple[list[dict], list[dict]]:
    """Read a pasted list (one per line, or a reference-manager export) into entries.  Returns (entries, skipped):
    entries are {"input", "doi"} with doi None when it must be looked up first (a PubMed id, say); skipped are
    {"input", "reason"}.  Lines without a DOI are taken only if they look like another identifier, so the other
    lines of a BibTeX or RIS file are ignored rather than searched for."""
    lines = [str(s) for s in items or []] + (text or "").splitlines()
    mod = _inputs()
    if mod is not None and hasattr(mod, "parse_batch"):
        try:
            return _from_parse_batch(mod, lines)
        except Exception:      # noqa: BLE001 - fall back to reading DOIs only
            pass
    entries, skipped, seen = [], [], set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        found = DOI_RE.findall(line)
        cands = found if found else [line]
        for raw in cands:
            doi = None
            if found:
                try:
                    doi = _normalize(raw)
                except ValueError:
                    skipped.append({"input": raw[:200], "reason": "not a valid DOI"})
                    continue
            elif mod is not None and OTHER_ID.match(raw):
                parsed = raw if hasattr(mod, "resolve") else None
                for fn in ("parse_input", "parse"):
                    if hasattr(mod, fn):
                        try:
                            parsed = getattr(mod, fn)(raw)
                        except Exception:          # noqa: BLE001
                            parsed = None
                        break
                doi = _doi_of(parsed) if parsed is not None else None
                if parsed is None:
                    skipped.append({"input": raw[:200], "reason": "not a DOI or a known identifier"})
                    continue
            else:
                skipped.append({"input": raw[:200], "reason": "not a DOI" if mod is None
                                else "not a DOI or a known identifier"})
                continue
            k = doi or raw.lower()
            if k in seen:
                skipped.append({"input": raw[:200], "reason": "listed twice"})
                continue
            seen.add(k)
            entries.append({"input": raw[:500], "doi": doi})
    return entries, skipped


def _from_parse_batch(mod, lines: list[str]) -> tuple[list[dict], list[dict]]:
    """Use the input module's batch reader: it knows PubMed, PMC and arXiv ids, links, titles, and BibTeX/RIS."""
    entries, skipped, seen = [], [], set()
    for it in mod.parse_batch("\n".join(lines), MAX_BATCH):
        raw = str(it.get("input") or it.get("value") or "").strip()
        kind = it.get("kind")
        if kind in ("empty", "unknown"):
            if raw:
                skipped.append({"input": raw[:200], "reason": it.get("message") or "not recognised"})
            continue
        doi = _doi_of(it)
        if doi:
            try:
                doi = _normalize(doi)
            except ValueError:
                skipped.append({"input": raw[:200], "reason": "not a valid DOI"})
                continue
        k = doi or f"{kind}:{str(it.get('value', raw)).lower()}"
        if k in seen:
            skipped.append({"input": raw[:200], "reason": "listed twice"})
            continue
        seen.add(k)
        entries.append({"input": (raw or str(it.get("value", "")))[:500], "doi": doi})
    return entries, skipped


def resolve(entry: dict, should_cancel: Optional[Callable[[], bool]] = None) -> str:
    """The DOI to scan.  Looking up a non-DOI identifier may use the network, so it happens in the worker; the
    input module explains in plain words when it cannot find one."""
    if entry.get("doi"):
        return entry["doi"]
    mod = _inputs()
    if mod is None:
        raise ValueError(f"'{entry.get('input', '')}' is not a DOI")
    raw = entry["input"]
    parsed = mod.parse_input(raw) if hasattr(mod, "parse_input") else raw
    doi = _doi_of(parsed)
    res = None
    if not doi and hasattr(mod, "resolve"):
        try:
            res = mod.resolve(raw, should_cancel=should_cancel) if should_cancel else mod.resolve(raw)
        except TypeError:           # an older resolve() that takes what parse_input returned
            res = mod.resolve(parsed)
        doi = _doi_of(res)
    if not doi:
        why = res.get("message") if isinstance(res, dict) else ""
        raise ValueError(why or f"no DOI found for '{raw}'")
    return _normalize(doi)


Runner = Callable[[dict, Callable[[str, float], None]], dict]


class BatchQueue:
    """runner(item, progress) scans one item and returns its report (with "id").  may_start() says whether the
    machine is free enough to begin the next one (the server holds back while interactive scans run)."""

    def __init__(self, path: Path, runner: Runner, may_start: Callable[[], bool] = lambda: True, poll: float = 1.0,
                 on_cancelled: Optional[Callable[[float], None]] = None):
        self.path = Path(path)
        self.on_cancelled = on_cancelled      # called with the time the cancelled scan began (to tidy its folder)
        self.runner = runner
        self.may_start = may_start
        self.poll = poll
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._cancel: Optional[threading.Event] = None
        self._current: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self.items: list[dict] = []
        self.batches: dict[str, dict] = {}
        self._load()

    # --- storage ---------------------------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        self.items = [i for i in d.get("items") or [] if isinstance(i, dict) and i.get("id")]
        self.batches = {k: v for k, v in (d.get("batches") or {}).items() if isinstance(v, dict)}
        for it in self.items:
            if it.get("status") == "running":   # the app closed mid-scan: start that one again
                it.update(status="queued", stage="Waiting (restarted after the app closed)", progress=0.0,
                          started=None)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".queue.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"items": self.items, "batches": self.batches}, fh, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # --- reading ---------------------------------------------------------------------------------------------
    def get(self, qid: str) -> Optional[dict]:
        with self._lock:
            it = next((i for i in self.items if i["id"] == qid), None)
            return dict(it) if it else None

    def list(self, batch: Optional[str] = None) -> dict:
        with self._lock:
            rows = [dict(i) for i in self.items if batch is None or i.get("batch") == batch]
            counts = {s: sum(i["status"] == s for i in rows) for s in ("queued", "running", *FINISHED)}
            return {"items": rows, "counts": counts, "running": self._current,
                    "batches": sorted(self.batches.values(), key=lambda b: b.get("created", ""), reverse=True)}

    # --- changing ----------------------------------------------------------------------------------------------
    def add(self, entries: list[dict], name: str = "", online: bool = True) -> dict:
        """Queue entries from parse_entries.  A DOI already waiting or running is not queued twice."""
        if len(entries) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} papers at once")
        with self._lock:
            active = {i.get("doi") or i.get("input", "").lower() for i in self.items if i["status"] in
                      ("queued", "running")}
            fresh, skipped = [], []
            for e in entries:
                k = e.get("doi") or e["input"].lower()
                if k in active:
                    skipped.append({"input": e["input"], "reason": "already in the queue"})
                    continue
                active.add(k)
                fresh.append(e)
            room = MAX_ITEMS - len(self.items)
            if len(fresh) > room:
                self._drop_finished(len(fresh) - room)
                room = MAX_ITEMS - len(self.items)
            if len(fresh) > room:
                raise ValueError("the queue is full; clear finished items first")
            bid = uuid.uuid4().hex[:10]
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            batch = {"id": bid, "name": (" ".join(str(name).split())[:80] or f"Batch of {len(fresh)}"),
                     "created": now, "count": len(fresh)}
            added = []
            for e in fresh:
                it = {"id": uuid.uuid4().hex[:12], "batch": bid, "input": e["input"], "doi": e.get("doi"),
                      "online": bool(online), "status": "queued", "stage": "Waiting", "progress": 0.0,
                      "run": None, "error": None, "added": now, "started": None, "finished": None,
                      "title": None, "score": None}
                self.items.append(it)
                added.append(dict(it))
            if added:
                self.batches[bid] = batch
                self._save()
                self._wake.notify_all()
        return {"batch": batch if added else None, "added": added, "skipped": skipped}

    def _drop_finished(self, n: int) -> None:
        old = sorted((i for i in self.items if i["status"] in FINISHED), key=lambda i: i.get("finished") or "")[:n]
        ids = {i["id"] for i in old}
        self.items = [i for i in self.items if i["id"] not in ids]

    def cancel(self, qid: str) -> Optional[dict]:
        """A waiting item is cancelled at once; a running one stops at its next step."""
        with self._lock:
            it = next((i for i in self.items if i["id"] == qid), None)
            if it is None:
                return None
            if it["status"] == "queued":
                it.update(status="cancelled", stage="Cancelled", finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
                self._save()
            elif it["status"] == "running" and self._current == qid and self._cancel is not None:
                self._cancel.set()
                it["stage"] = "Cancelling..."
            return dict(it)

    def cancelling(self, qid: str) -> bool:
        """For long lookups to poll: has the user asked to stop this item?"""
        ev = self._cancel
        return self._current == qid and ev is not None and ev.is_set()

    def retry(self, qid: str) -> Optional[dict]:
        with self._lock:
            it = next((i for i in self.items if i["id"] == qid), None)
            if it is None or it["status"] not in ("error", "cancelled"):
                return dict(it) if it else None
            it.update(status="queued", stage="Waiting", progress=0.0, error=None, started=None, finished=None)
            self._save()
            self._wake.notify_all()
            return dict(it)

    def remove(self, qid: str) -> bool:
        with self._lock:
            it = next((i for i in self.items if i["id"] == qid), None)
            if it is None:
                return False
            if it["status"] == "running":
                self.cancel(qid)
                it["remove"] = True             # the worker drops it once the scan has stopped
            else:
                self.items.remove(it)
            self._save()
            return True

    def clear(self) -> int:
        """Forget finished items (their scans stay in the library)."""
        with self._lock:
            n = len(self.items)
            self.items = [i for i in self.items if i["status"] not in FINISHED]
            live = {i.get("batch") for i in self.items}
            self.batches = {k: v for k, v in self.batches.items() if k in live}
            self._save()
            return n - len(self.items)

    # --- the worker --------------------------------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="paperinspector-queue", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._stop = True
            if self._cancel is not None:
                self._cancel.set()
            self._wake.notify_all()
            t = self._thread
        if t is not None:
            t.join(timeout)

    def _next(self) -> Optional[dict]:
        return next((i for i in self.items if i["status"] == "queued"), None)

    def _loop(self) -> None:
        while True:
            with self._lock:
                while not self._stop and (self._next() is None or not self._safe_may_start()):
                    self._wake.wait(self.poll)
                if self._stop:
                    return
                it = self._next()
                if it is None:
                    continue
                ev = threading.Event()
                self._cancel, self._current = ev, it["id"]
                it.update(status="running", stage="Starting", progress=0.0, error=None,
                          started=time.strftime("%Y-%m-%dT%H:%M:%S"))
                self._save()
            self._run_one(it, ev)

    def _safe_may_start(self) -> bool:
        try:
            return bool(self.may_start())
        except Exception:      # noqa: BLE001
            return True

    def _run_one(self, it: dict, ev: threading.Event) -> None:
        def progress(stage: str, frac: float) -> None:
            if ev.is_set():
                raise Cancelled()
            it["stage"], it["progress"] = stage, round(float(frac), 3)

        end: dict = {"status": "error"}
        began = time.time()
        try:
            if ev.is_set():
                raise Cancelled()
            rep = self.runner(dict(it), progress)
            p = rep.get("paper") or {}
            end = {"status": "done", "run": rep.get("id"), "title": p.get("title") or p.get("source"),
                   "score": rep.get("score"), "doi": it.get("doi") or p.get("doi") or None, "stage": "Done",
                   "progress": 1.0}
        except CANCELLED:
            end = {"status": "cancelled", "stage": "Cancelled"}
            if self.on_cancelled is not None:
                try:
                    self.on_cancelled(began)
                except Exception:      # noqa: BLE001
                    pass
        except Exception as e:      # noqa: BLE001 - one bad paper must not stop the queue
            end = {"status": "error", "error": (str(e) or type(e).__name__)[:500], "stage": "Failed"}
            traceback.print_exc()
        with self._lock:
            it.update(finished=time.strftime("%Y-%m-%dT%H:%M:%S"), **end)
            if it.pop("remove", False) and it in self.items:
                self.items.remove(it)
            self._cancel, self._current = None, None
            self._save()
