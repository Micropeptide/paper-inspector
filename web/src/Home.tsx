import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { absTime, api, ApiError, Job, QueueItem, relTime, Resolved, RunSummary, scoreTone, scoreWord, TagCount } from "./api";
import { guess, KIND_LABEL } from "./lib/inputs";
import { Route, useDebounced, useKeydown, usePoll, useReducedMotion } from "./lib/hooks";
import { Command, useCommands } from "./Palette";
import { copyText, Icon, Menu, Segmented, Spinner, useToast } from "./ui";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp,.xlsx,.xls,.csv,.docx,.zip";

type Engine = "checking" | "ready" | "offline";

/** Track a scan from start to finish: a direct job (/api/jobs) or a queue item (/api/queue) for identifiers the
 *  engine resolves itself. */
interface Tracked {
  label: string;
  job?: Job;
  item?: QueueItem;
  started: number;
  retry: () => void;
}

const STEPS: [string, RegExp][] = [
  ["Fetch the paper", /fetch|download|find|look|doi|queued|resolv|metadata|crossref|openalex/i],
  ["Read the pages", /read|pages?|text|pdf|extract|render/i],
  ["Find the panels", /panel|figure|segment|split/i],
  ["Compare the images", /compar|match|image|clone|duplicate|blot/i],
  ["Check data, statistics, text and references", /data|stat|reference|table|spreadsheet|check|record/i],
  ["Write the report", /report|writ|done|sav/i],
];

function stepOf(stage: string, progress: number): number {
  let best = -1;
  STEPS.forEach(([, re], i) => { if (re.test(stage)) best = i; });
  if (best < 0) best = Math.min(STEPS.length - 1, Math.floor(progress * STEPS.length));
  return best;
}

export default function Home({ go, theme, onTheme, onSettings, version }: {
  go: (r: Route) => void; theme: string; onTheme: () => void; onSettings: () => void; version: string;
}) {
  const [engine, setEngine] = useState<Engine>("checking");
  const [text, setText] = useState("");
  const [inputErr, setInputErr] = useState("");
  const [cands, setCands] = useState<Resolved | null>(null);
  const [resolving, setResolving] = useState(false);
  const [track, setTrack] = useState<Tracked | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [paperIdx, setPaperIdx] = useState(0);
  const [separate, setSeparate] = useState(false);
  const [online, setOnline] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const g = guess(text);
  const busy = Boolean(track && (track.job?.status === "running" || (track.item && ["queued", "running"].includes(track.item.status))));

  /* ---------------------------------------------------------------- engine status */

  const ping = useCallback(async () => {
    try {
      await api.health();
      setEngine((e) => {
        if (e === "offline") toast({ text: "Engine connected" });
        return "ready";
      });
    } catch {
      setEngine("offline");
    }
  }, [toast]);
  useEffect(() => { ping(); }, [ping]);
  useEffect(() => {
    if (engine !== "offline") return;
    const t = window.setInterval(ping, 5000);
    return () => window.clearInterval(t);
  }, [engine, ping]);

  useKeydown((e) => {
    if (e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key === "l")) { e.preventDefault(); field.current?.focus(); }
  });

  // A scan started in another tab or before a reload: pick it up again.
  useEffect(() => {
    if (engine !== "ready" || track) return;
    api.jobs().then((js) => {
      const j = js.find((x) => x.status === "running" && x.kind !== "investigate");
      if (j) setTrack({ label: "the scan in progress", job: j, started: (j.started ?? Date.now() / 1000) * 1000, retry: () => undefined });
    }, () => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine]);

  /* ---------------------------------------------------------------- progress */

  usePoll(async () => {
    if (!track) return;
    if (track.job) {
      const j = await api.job(track.job.id);
      setTrack((t) => t && { ...t, job: j });
      if (j.status === "done" && j.run) go({ page: "run", run: j.run });
    } else if (track.item) {
      const it = await api.queueItem(track.item.id);
      setTrack((t) => t && { ...t, item: it });
      if (it.status === "done" && it.run) go({ page: "run", run: it.run });
    }
  }, 500, busy, (e) => setInputErr(`Lost contact with the engine: ${e.message}`));

  const startJob = async (label: string, run: () => Promise<Job>) => {
    setInputErr("");
    setCands(null);
    const retry = () => startJob(label, run);
    try {
      const job = await run();
      setTrack({ label, job, started: Date.now(), retry });
    } catch (e) {
      setInputErr((e as Error).message);
    }
  };

  const cancel = async () => {
    if (!track) return;
    try {
      if (track.job) setTrack({ ...track, job: await api.cancelJob(track.job.id) });
      else if (track.item) setTrack({ ...track, item: await api.cancelItem(track.item.id) });
    } catch (e) {
      toast({ text: `Could not cancel: ${(e as Error).message}`, tone: "error" });
    }
  };

  /* ---------------------------------------------------------------- the smart box */

  const viaQueue = async (input: string) => {
    const r = await api.enqueue({ items: [input], name: input.slice(0, 60) });
    const item = r.added[0];
    if (!item) throw new Error(r.skipped[0]?.reason ? `${r.skipped[0].reason}.` : "That could not be read as a paper identifier.");
    setTrack({ label: input, item, started: Date.now(), retry: () => submit() });
  };

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    setInputErr("");
    setCands(null);
    const gu = guess(text);
    if (gu.kind === "empty") { field.current?.focus(); return; }
    if (gu.kind === "many") {
      try {
        const r = await api.enqueue({ text, name: `Pasted list, ${new Date().toLocaleString()}` });
        toast({ text: `${r.added.length} paper${r.added.length === 1 ? "" : "s"} added to the queue${r.skipped.length ? `, ${r.skipped.length} skipped` : ""}.` });
        go({ page: "queue", batch: r.batch?.id });
      } catch (er) {
        setInputErr((er as Error).message);
      }
      return;
    }
    if (gu.kind === "doi") {
      startJob(gu.value, () => api.doi(gu.value));
      return;
    }
    setResolving(true);
    try {
      const r = await api.resolve(text.trim());
      if (r.doi) startJob(r.doi, () => api.doi(r.doi!));
      else if (r.candidates.length) setCands(r);
      else setInputErr(r.message || "No article was found for that. Paste its DOI instead.");
    } catch (er) {
      // An engine without /api/resolve: let the batch queue resolve it.
      if (er instanceof ApiError && (er.status === 404 || er.status === 405)) {
        try { await viaQueue(text.trim()); } catch (e2) { setInputErr((e2 as Error).message); }
      } else setInputErr((er as Error).message);
    } finally {
      setResolving(false);
    }
  };

  /* ---------------------------------------------------------------- files */

  const addFiles = (list: File[]) => {
    if (!list.length) return;
    setFiles((f) => {
      const next = [...f, ...list.filter((x) => !f.some((y) => y.name === x.name && y.size === x.size))];
      const pi = next.findIndex((x) => /\.pdf$/i.test(x.name));
      setPaperIdx(pi >= 0 ? pi : next.findIndex((x) => /\.(png|jpe?g|tiff?|bmp|webp)$/i.test(x.name)));
      return next;
    });
  };
  const pdfs = files.filter((f) => /\.pdf$/i.test(f.name));
  const inspectFiles = () => {
    if (separate && pdfs.length > 1) {
      startJob(`${pdfs.length} papers`, () => api.scanCase(pdfs));
      return;
    }
    const main = files[paperIdx];
    if (!main || !/\.(pdf|png|jpe?g|tiff?|bmp|webp)$/i.test(main.name)) {
      setInputErr("Add the paper itself as a PDF (or a figure as an image). Spreadsheets and documents can come along as supplementary files.");
      return;
    }
    startJob(main.name, () => api.scan(main, files.filter((f) => f !== main), online));
  };

  useEffect(() => {
    const over = (e: DragEvent) => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); setDragging(true); } };
    const leave = (e: DragEvent) => { if (!e.relatedTarget) setDragging(false); };
    const drop = (e: DragEvent) => {
      if (!e.dataTransfer?.files.length) return;
      e.preventDefault();
      setDragging(false);
      if (!busy) addFiles(Array.from(e.dataTransfer.files));
    };
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, [busy]);

  const offline = engine === "offline";
  const status = track?.job ?? track?.item;
  const failed = status && (status.status === "error" || status.status === "cancelled");

  return (
    <div className="home">
      <header className="home-head">
        <span className="brand-row"><img src="/icon.svg" alt="" width={30} height={30} /> <b>Paper Inspector</b></span>
        <span className="grow" />
        <span className={`engine ${engine}`} role="status">
          <i aria-hidden="true" />{engine === "ready" ? `Engine ready${version ? `, v${version}` : ""}` : engine === "checking" ? "Connecting…" : "Engine offline"}
        </span>
        <button className="btn ghost" onClick={() => go({ page: "queue" })}><Icon name="queue" /> Batch queue</button>
        <button className="icon-btn" onClick={onTheme} aria-label={`Theme: ${theme}. Change theme`} title={`Theme: ${theme}`}>
          <Icon name={theme === "dark" ? "moon" : theme === "light" ? "sun" : "monitor"} />
        </button>
        <button className="btn" onClick={onSettings}><Icon name="gear" /> Settings</button>
      </header>

      <main id="main">
        <section className="hero">
          <div className="hero-copy">
            <h1>Paste a DOI. Get the paper inspected.</h1>
            <p className="lede">
              Paper Inspector fetches the paper with its figures, data and supplementary files, then checks what a careful
              reviewer would: reused or edited images, copied or impossible numbers, references that do not exist, and the
              quieter problems authors often miss. Every flag comes with its evidence.
            </p>

            {offline ? (
              <div className="offline-card">
                <h2>The engine isn't running</h2>
                <p>Start it in a terminal, then this page connects by itself.</p>
                <div className="code-row">
                  <code>paperinspector serve</code>
                  <button className="btn s" onClick={async () => toast((await copyText("paperinspector serve")) ? { text: "Command copied" } : { text: "Copy blocked. Select the command by hand.", tone: "error" })}>Copy</button>
                </div>
                <p className="muted"><Spinner /> Checking again every 5 seconds…</p>
              </div>
            ) : track && !failed ? (
              <ProgressCard track={track} onCancel={cancel} />
            ) : (
              <>
                {failed && status && (
                  <div className="error-card" role="alert">
                    <h2>{status.status === "cancelled" ? "The scan was cancelled" : "The scan stopped"}</h2>
                    {status.error && <pre>{status.error}</pre>}
                    <div className="row">
                      <button className="btn primary" onClick={() => { const r = track!.retry; setTrack(null); r(); }}>Try again</button>
                      <button className="btn" onClick={() => setTrack(null)}>Start over</button>
                    </div>
                  </div>
                )}
                <form className="smart" onSubmit={submit} role="search">
                  <label htmlFor="smart" className="sr">DOI, PubMed ID, PMC ID, arXiv ID, article link or title</label>
                  <div className={`smart-field${inputErr ? " bad" : ""}`}>
                    <Icon name="search" size={18} />
                    {g.kind === "many" ? (
                      <textarea id="smart" value={text} rows={4} onChange={(e) => { setText(e.target.value); setInputErr(""); setCands(null); }}
                        aria-describedby="smart-help" placeholder="One per line" spellCheck={false} />
                    ) : (
                      <input id="smart" ref={field} value={text} autoComplete="off" spellCheck={false} aria-describedby="smart-help smart-err"
                        placeholder="10.1371/journal.pbio.1002106, PMID, arXiv ID, link or title"
                        onChange={(e) => { setText(e.target.value); setInputErr(""); setCands(null); }}
                        onPaste={(e) => {
                          const t = e.clipboardData.getData("text");
                          if (t.includes("\n")) { e.preventDefault(); setText(t.trim()); }
                        }} />
                    )}
                    <button className="btn primary l" disabled={!text.trim() || resolving || engine !== "ready"}>
                      {resolving ? <Spinner /> : null}{g.kind === "many" ? `Queue ${g.lines} papers` : "Inspect"}
                    </button>
                  </div>
                  <p id="smart-help" className="smart-help" aria-live="polite">
                    {g.kind === "empty" ? "A DOI, a PubMed or PMC ID, an arXiv ID, a link to the article, or its title. Paste a list to queue many." :
                      g.kind === "many" ? `${g.lines} lines. Each one is looked up and scanned in turn in the batch queue.` :
                        g.kind === "title" ? "Looks like a title or citation. It is searched in Crossref; you pick the match." :
                          g.kind === "url" ? "An article link. The page is read for its DOI." :
                            `Reads as a ${KIND_LABEL[g.kind]}: ${g.value}`}
                  </p>
                  {inputErr && <p id="smart-err" className="error-text" role="alert">{inputErr}</p>}
                  {cands && (
                    <div className="cands">
                      <p>{cands.message || "Several articles match. Pick one."}</p>
                      <ul>
                        {cands.candidates.map((c) => (
                          <li key={c.doi}>
                            <button className="cand" onClick={() => startJob(c.doi, () => api.doi(c.doi))}>
                              <b>{c.title || c.doi}</b>
                              <small>{[c.first_author, c.journal, c.year, c.doi].filter(Boolean).join(", ")}</small>
                            </button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </form>

                <div className="or" aria-hidden="true"><span>or</span></div>

                <div className={`drop${files.length ? " has" : ""}`} role="button" tabIndex={0} aria-label="Choose files to inspect"
                  onClick={() => !files.length && fileInput.current?.click()}
                  onKeyDown={(e) => { if (!files.length && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); fileInput.current?.click(); } }}>
                  <input ref={fileInput} type="file" multiple hidden accept={ACCEPT} onChange={(e) => { addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
                  {!files.length ? (
                    <>
                      <Icon name="upload" size={22} />
                      <b>Drop the PDF here, with its supplementary files</b>
                      <span>PDF, images, spreadsheets, Word files and zips. Paywalled papers: add the PDF yourself.</span>
                    </>
                  ) : (
                    <div className="staged" onClick={(e) => e.stopPropagation()}>
                      <ul>
                        {files.map((f, i) => (
                          <li key={`${f.name}-${f.size}`}>
                            <span className={`kind-badge${i === paperIdx ? " paper" : ""}`}>{separate && /\.pdf$/i.test(f.name) ? "Paper" : i === paperIdx ? "Paper" : "Supplement"}</span>
                            <span className="fname">{f.name}</span>
                            <small>{(f.size / 1024 / 1024).toFixed(1)} MB</small>
                            {i !== paperIdx && /\.(pdf|png|jpe?g|tiff?|bmp|webp)$/i.test(f.name) && !separate &&
                              <button className="link-btn" onClick={() => setPaperIdx(i)}>Make this the paper</button>}
                            <button className="icon-btn s" aria-label={`Remove ${f.name}`} onClick={() => {
                              setFiles((x) => x.filter((_, j) => j !== i));
                              setPaperIdx((p) => (i < p ? p - 1 : i === p ? 0 : p));
                            }}><Icon name="close" size={14} /></button>
                          </li>
                        ))}
                      </ul>
                      <details className="upload-opts">
                        <summary>Upload options</summary>
                        <label className="check"><input type="checkbox" checked={separate} disabled={pdfs.length < 2} onChange={(e) => setSeparate(e.target.checked)} />
                          <span>Compare as separate papers<small>Each PDF is treated as its own paper, and images are matched across them.</small></span></label>
                        <label className="check"><input type="checkbox" checked={online} onChange={(e) => setOnline(e.target.checked)} />
                          <span>Look up references online<small>Sends reference titles to Crossref and OpenAlex. DOI scans always do this.</small></span></label>
                      </details>
                      <div className="row">
                        <button className="btn primary" disabled={engine !== "ready"} onClick={inspectFiles}>Inspect {files.length} file{files.length === 1 ? "" : "s"}</button>
                        <button className="btn" onClick={() => fileInput.current?.click()}>Add files</button>
                        <button className="btn ghost" onClick={() => setFiles([])}>Clear</button>
                      </div>
                    </div>
                  )}
                </div>
                <p className="demo-line">No paper handy? <button className="link-btn" disabled={engine !== "ready"} onClick={() => startJob("the demo paper", api.demo)}>Try the demo paper</button></p>
              </>
            )}
            <p className="fine"><Icon name="lock" size={14} /> Uploaded files stay on this computer. A DOI is looked up in public databases (Crossref, OpenAlex, PubMed Central); the AI investigator sends figures to the model you choose, only when you ask.</p>
          </div>
          <HeroBlink />
        </section>

        {engine === "ready" && <Library go={go} />}

        <section className="how" aria-labelledby="how-h">
          <h2 id="how-h" className="sr">What it checks</h2>
          {[
            ["Images", "Every panel in the paper and its supplements is matched against every other, mirrored, rotated and rescaled too. Cloned patches, spliced blots and painted-over regions are flagged."],
            ["Data", "Tables and spreadsheets: copied runs of numbers, random-number formulas, odd last digits, identical spreads, impossible values, hidden sheets, gene names that Excel turned into dates."],
            ["Statistics and text", "p-values recomputed from their tests, impossible means and percentages, chatbot leftovers, tortured phrases, and hidden instructions aimed at AI reviewers."],
            ["References", "Every reference looked up: ones that do not exist, retracted ones, heavy self-citation, one outside author cited again and again, references never cited in the text."],
            ["Reporting", "Misidentified cell lines, unregistered trials, missing ethics or data statements, data “on request”, accession numbers that are not public, week-long peer review."],
            ["A second opinion", "An AI investigator opens the evidence, reads the legends, rules on each finding and drafts a careful comment that asks for the raw data."],
          ].map(([h, p]) => <div key={h}><h3>{h}</h3><p>{p}</p></div>)}
        </section>
      </main>
      <footer className="home-foot">
        <span>Paper Inspector{version ? ` ${version}` : ""}</span>
        <span>Press <kbd className="kbd">?</kbd> for keyboard shortcuts, <kbd className="kbd">Cmd/Ctrl K</kbd> to jump anywhere.</span>
      </footer>
      {dragging && <div className="drop-overlay" aria-hidden="true"><span>Drop to add these files</span></div>}
    </div>
  );
}

function ProgressCard({ track, onCancel }: { track: Tracked; onCancel: () => void }) {
  const s = track.job ?? track.item!;
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  const secs = Math.max(0, Math.round((now - track.started) / 1000));
  const cur = stepOf(s.stage, s.progress);
  const cancelling = /cancel/i.test(s.stage);
  return (
    <div className="progress-card" aria-live="polite" aria-busy="true">
      <div className="pc-head">
        <h2>Inspecting {track.label}</h2>
        <span className="elapsed">{Math.floor(secs / 60)}:{String(secs % 60).padStart(2, "0")}</span>
      </div>
      <div className="bar" role="progressbar" aria-valuenow={Math.round(s.progress * 100)} aria-valuemin={0} aria-valuemax={100} aria-label="Scan progress">
        <i style={{ width: `${Math.round(s.progress * 100)}%` }} />
      </div>
      <ol className="stepper">
        {STEPS.map(([label], i) => (
          <li key={label} className={i < cur ? "done" : i === cur ? "now" : ""}>
            <span className="st-mark" aria-hidden="true">{i < cur ? <Icon name="check" size={14} /> : i === cur ? <Spinner size={12} /> : null}</span>
            {label}{i < cur ? <span className="sr"> (done)</span> : i === cur ? <span className="sr"> (in progress)</span> : null}
          </li>
        ))}
      </ol>
      <p className="muted stage-line">{s.stage}</p>
      <div className="row">
        <span className="muted grow">You can leave this page; the scan keeps running.</span>
        <button className="btn" disabled={cancelling} onClick={onCancel}><Icon name="stop" /> {cancelling ? "Cancelling…" : "Cancel"}</button>
      </div>
    </div>
  );
}

function HeroBlink() {
  const reduce = useReducedMotion();
  const [paused, setPaused] = useState(reduce);
  const [b, setB] = useState(false);
  useEffect(() => setPaused(reduce), [reduce]);
  useEffect(() => {
    if (paused) return;
    const t = window.setInterval(() => setB((x) => !x), 650);
    return () => window.clearInterval(t);
  }, [paused]);
  return (
    <figure className="hero-blink" aria-label="A mirrored image caught by blinking">
      <div className={`hb-stage ${b ? "b" : "a"}`}>
        <img src="/hero-a.png" alt="Panel A of a figure" />
        <img src="/hero-b.png" alt="Panel E of the same figure, mirrored back" className="hb-b" style={{ opacity: b ? 1 : 0 }} />
        <span className="hb-tag">{b ? "Panel E, mirrored back" : "Panel A"}</span>
        <button className="icon-btn hb-play" aria-label={paused ? "Play the blink" : "Pause the blink"} onClick={() => setPaused((p) => !p)}>
          <Icon name={paused ? "play" : "pause"} />
        </button>
      </div>
      <figcaption>Panels A and E of one figure, labelled as different treatments. Mirrored back and flickered, only the brightness changes: it is one image.</figcaption>
    </figure>
  );
}

/* ---------------------------------------------------------------- recent scans */

function Library({ go }: { go: (r: Route) => void }) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [tags, setTags] = useState<TagCount[]>([]);
  const [total, setTotal] = useState(0);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<string[]>([]);
  const [sort, setSort] = useState<"created" | "score" | "title">("created");
  const [err, setErr] = useState("");
  const pending = useRef<Record<string, number>>({});
  const dq = useDebounced(q, 200);
  const toast = useToast();

  const load = useCallback(() => {
    setErr("");
    api.search({ q: dq, tags: sel, sort }).then((r) => { setRuns(r.runs); setTotal(r.total); setTags(r.tags); },
      () => api.runs().then((r) => { setRuns(r); setTotal(r.length); }, (e) => setErr((e as Error).message)));
  }, [dq, sel, sort]);
  useEffect(load, [load]);

  const remove = (r: RunSummary) => {
    setRuns((x) => x && x.filter((y) => y.id !== r.id));
    pending.current[r.id] = window.setTimeout(async () => {
      delete pending.current[r.id];
      try {
        await api.remove(r.id);
      } catch (e) {
        toast({ text: `Could not delete: ${(e as Error).message}`, tone: "error" });
        load();
      }
    }, 6000);
    toast({
      text: "Scan deleted.", action: { label: "Undo", run: () => { window.clearTimeout(pending.current[r.id]); delete pending.current[r.id]; load(); } },
    });
  };
  useEffect(() => () => {
    // leaving the page: send the deletes that are still waiting
    Object.entries(pending.current).forEach(([id, t]) => { window.clearTimeout(t); api.remove(id).catch(() => undefined); });
  }, []);

  const commands = useMemo<Command[]>(() => (runs ?? []).slice(0, 40).map((r) => ({
    id: `run-${r.id}`, group: "Recent scans", label: r.title || r.source, hint: `score ${r.score}`, run: () => go({ page: "run", run: r.id }),
  })), [runs, go]);
  useCommands("home-runs", commands);

  if (err) return <section className="recent"><p className="error-text">The list of scans could not be loaded: {err} <button className="link-btn" onClick={load}>Retry</button></p></section>;
  if (runs && !runs.length && !dq && !sel.length) return <section className="recent"><h2>Recent scans</h2><p className="muted">Your scans will appear here.</p></section>;

  return (
    <section className="recent" aria-labelledby="recent-h">
      <div className="recent-head">
        <h2 id="recent-h">Recent scans <span className="count">{total}</span></h2>
        <div className="search-box">
          <Icon name="search" />
          <label className="sr" htmlFor="lib-q">Filter scans</label>
          <input id="lib-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by title, DOI, author, journal or tag" />
        </div>
        <Segmented label="Sort" value={sort} onChange={setSort} options={[{ id: "created", label: "Newest" }, { id: "score", label: "Highest score" }, { id: "title", label: "Title" }]} />
      </div>
      {tags.length > 0 && (
        <div className="chips" role="group" aria-label="Filter by tag">
          {tags.map((t) => (
            <button key={t.tag} className={`chip${sel.includes(t.tag) ? " on" : ""}`} aria-pressed={sel.includes(t.tag)}
              onClick={() => setSel((s) => (s.includes(t.tag) ? s.filter((x) => x !== t.tag) : [...s, t.tag]))}>
              <Icon name="tag" size={12} /> {t.tag} <span>{t.count}</span>
            </button>
          ))}
        </div>
      )}
      {!runs ? (
        <div className="rows">{[0, 1, 2].map((i) => <div key={i} className="skeleton row-skel" />)}</div>
      ) : !runs.length ? (
        <p className="muted">No scans match. <button className="link-btn" onClick={() => { setQ(""); setSel([]); }}>Clear the filter</button></p>
      ) : (
        <ul className="rows">
          {runs.map((r) => {
            const tone = scoreTone(r.score_ai ?? r.score);
            const sc = r.score_ai ?? r.score;
            return (
              <li key={r.id} className="lib-row">
                <a className="lib-main" href={`#/run/${r.id}`} onClick={(e) => { e.preventDefault(); go({ page: "run", run: r.id }); }}>
                  <span className={`score-pill ${tone}`} aria-label={`Score ${sc}, ${scoreWord(sc).toLowerCase()}`}>{sc}</span>
                  <span className="lib-title">
                    <b>{r.title || r.source}</b>
                    <small>{[r.authors?.[0] ? `${r.authors[0]}${(r.authors?.length ?? 0) > 1 ? " et al." : ""}` : "", r.journal, r.doi || r.source].filter(Boolean).join(", ")}</small>
                  </span>
                  <span className="lib-meta">
                    {(r.counts.critical ?? 0) + (r.counts.high ?? 0)} serious, {r.counts.medium ?? 0} to check
                    {r.case ? <span className="chip static">multi-paper</span> : null}
                    {(r.tags ?? []).slice(0, 3).map((t) => <span key={t} className="chip static"><Icon name="tag" size={11} /> {t}</span>)}
                  </span>
                  <span className="lib-date" title={absTime(r.created)}>{relTime(r.created)}</span>
                </a>
                <Menu label={`Actions for ${r.title || r.source}`} buttonClass="icon-btn" button={<Icon name="more" />} items={[
                  { label: "Open", run: () => go({ page: "run", run: r.id }) },
                  { label: "Copy link", icon: "link", run: async () => toast((await copyText(`${window.location.origin}${window.location.pathname}#/run/${r.id}`)) ? { text: "Link copied" } : { text: "Copy blocked", tone: "error" }) },
                  { label: "Download report (PDF)", icon: "download", href: `/api/runs/${r.id}/export/report.pdf`, download: true },
                  { separator: true, label: "" },
                  { label: "Delete", icon: "trash", run: () => remove(r) },
                ]} />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
