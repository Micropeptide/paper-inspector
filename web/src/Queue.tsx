import { useCallback, useEffect, useMemo, useState } from "react";
import { absTime, api, QueueAdd, QueueItem, QueueState, QueueStatus, relTime, scoreTone, url } from "./api";
import { Route, usePoll } from "./lib/hooks";
import { Icon, Spinner, useToast } from "./ui";

const STATUS_LABEL: Record<QueueStatus, string> = { queued: "Waiting", running: "Scanning", done: "Done", error: "Stopped", cancelled: "Cancelled" };
const ORDER: QueueStatus[] = ["running", "queued", "error", "cancelled", "done"];

/** The batch dashboard: paste many identifiers, watch them scan one at a time, open or export the results. */
export default function Queue({ batch, go }: { batch?: string; go: (r: Route, replace?: boolean) => void }) {
  const [state, setState] = useState<QueueState | null>(null);
  const [err, setErr] = useState("");
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [online, setOnline] = useState(true);
  const [adding, setAdding] = useState(false);
  const [last, setLast] = useState<QueueAdd | null>(null);
  const [filter, setFilter] = useState<QueueStatus | "all">("all");
  const toast = useToast();

  const load = useCallback(() => api.queue(batch).then((s) => { setState(s); setErr(""); }, (e) => setErr((e as Error).message)), [batch]);
  useEffect(() => { load(); }, [load]);
  const active = Boolean(state && (state.counts.queued || state.counts.running));
  usePoll(load, 1500, active, (e) => setErr(e.message));

  useEffect(() => {
    document.title = "Batch queue · Paper Inspector";
    return () => { document.title = "Paper Inspector"; };
  }, []);

  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean).length;

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setAdding(true);
    try {
      const r = await api.enqueue({ text, name: name.trim() || undefined, online });
      setLast(r);
      setText("");
      setName("");
      toast({ text: `${r.added.length} added to the queue${r.skipped.length ? `, ${r.skipped.length} skipped` : ""}.` });
      if (r.batch) go({ page: "queue", batch: r.batch.id }, true);
      else load();
    } catch (er) {
      toast({ text: (er as Error).message, tone: "error" });
    } finally {
      setAdding(false);
    }
  };

  const act = async (fn: () => Promise<unknown>, done: string) => {
    try {
      await fn();
      toast({ text: done });
      load();
    } catch (e) {
      toast({ text: (e as Error).message, tone: "error" });
    }
  };

  const items = useMemo(() => {
    const list = (state?.items ?? []).filter((i) => filter === "all" || i.status === filter);
    return [...list].sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status) || a.added.localeCompare(b.added));
  }, [state, filter]);
  const cur = state?.batches.find((b) => b.id === batch);
  const c = state?.counts;
  const totalN = c ? c.queued + c.running + c.done + c.error + c.cancelled : 0;

  return (
    <div className="queue-page">
      <header className="home-head">
        <a className="brand-row" href="#/" onClick={(e) => { e.preventDefault(); go({ page: "home" }); }}><img src="/icon.svg" alt="" width={30} height={30} /> <b>Paper Inspector</b></a>
        <span className="grow" />
        <button className="btn ghost" onClick={() => go({ page: "home" })}><Icon name="chevL" /> All scans</button>
      </header>
      <main id="main" className="queue-main">
        <h1>Batch queue</h1>
        <p className="lede">Paste DOIs, PubMed or PMC IDs, arXiv IDs, links or a reference-manager export. Papers are scanned one at a time, in order. You can close this page; the queue keeps going while the engine runs.</p>

        <div className="queue-grid">
          <form className="card queue-add" onSubmit={add}>
            <h2>Add papers</h2>
            <label className="field">List, one per line
              <textarea value={text} onChange={(e) => setText(e.target.value)} rows={8} spellCheck={false}
                placeholder={"10.1371/journal.pbio.1002106\nPMID: 12345678\nPMC1234567\narXiv:2101.12345"} />
            </label>
            <label className="field">Batch name (optional)<input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} placeholder="e.g. Journal issue 12" /></label>
            <label className="check"><input type="checkbox" checked={online} onChange={(e) => setOnline(e.target.checked)} />
              <span>Check references online<small>Looks up every reference in Crossref and OpenAlex. Slower, but finds missing and retracted references.</small></span></label>
            <div className="row">
              <button className="btn primary" disabled={!lines || adding}>{adding ? <Spinner /> : null} Queue {lines || ""} paper{lines === 1 ? "" : "s"}</button>
              <span className="muted">At most 300 at a time.</span>
            </div>
            {last && last.skipped.length > 0 && (
              <details className="skipped">
                <summary>{last.skipped.length} line{last.skipped.length === 1 ? " was" : "s were"} skipped</summary>
                <ul>{last.skipped.map((s, i) => <li key={i}><code>{s.input}</code>: {s.reason}</li>)}</ul>
              </details>
            )}
          </form>

          <aside className="card batches">
            <h2>Batches</h2>
            <ul>
              <li><button className={!batch ? "on" : ""} onClick={() => go({ page: "queue" }, true)}>Everything</button></li>
              {(state?.batches ?? []).map((b) => (
                <li key={b.id}>
                  <button className={batch === b.id ? "on" : ""} onClick={() => go({ page: "queue", batch: b.id }, true)}>
                    <b>{b.name || "Untitled batch"}</b><small title={absTime(b.created)}>{b.count} papers, {relTime(b.created)}</small>
                  </button>
                </li>
              ))}
            </ul>
          </aside>
        </div>

        <section className="queue-list" aria-labelledby="ql-h">
          <div className="ql-head">
            <h2 id="ql-h">{cur ? cur.name || "Untitled batch" : "All queued papers"}</h2>
            {c && totalN > 0 && (
              <div className="ql-progress" aria-label={`${c.done} of ${totalN} done`}>
                <span className="stacked">
                  {(["done", "running", "error", "cancelled", "queued"] as QueueStatus[]).map((s) => c[s] ? <i key={s} className={`q-${s}`} style={{ flex: c[s] }} title={`${STATUS_LABEL[s]}: ${c[s]}`} /> : null)}
                </span>
                <span className="muted">{c.done} of {totalN} done{c.error ? `, ${c.error} stopped` : ""}</span>
              </div>
            )}
            <span className="grow" />
            {cur && <a className="btn" href={url.batchCsv(cur.id)} download><Icon name="download" /> Results (CSV)</a>}
            <button className="btn ghost" onClick={() => act(api.clearQueue, "Finished items cleared")} disabled={!c || !(c.done + c.error + c.cancelled)}>Clear finished</button>
          </div>
          <div className="chips" role="group" aria-label="Filter by status">
            {(["all", ...ORDER] as const).map((s) => (
              <button key={s} className={`chip${filter === s ? " on" : ""}`} aria-pressed={filter === s} onClick={() => setFilter(s)}>
                {s === "all" ? "All" : STATUS_LABEL[s]} {s !== "all" && c ? <span>{c[s]}</span> : null}
              </button>
            ))}
          </div>
          {err && <p className="error-text" role="alert">The queue could not be loaded: {err} <button className="link-btn" onClick={load}>Retry</button></p>}
          {!state && !err ? <div className="rows">{[0, 1, 2].map((i) => <div key={i} className="skeleton row-skel" />)}</div> :
            items.length === 0 ? <p className="muted empty-q">{filter === "all" ? "Nothing in the queue. Paste a list above to start." : "No papers with this status."}</p> : (
              <table className="qtable">
                <thead><tr><th scope="col">Paper</th><th scope="col">Status</th><th scope="col">Score</th><th scope="col">Added</th><th scope="col"><span className="sr">Actions</span></th></tr></thead>
                <tbody>
                  {items.map((it) => <Row key={it.id} it={it} go={go} act={act} />)}
                </tbody>
              </table>
            )}
        </section>
      </main>
    </div>
  );
}

function Row({ it, go, act }: { it: QueueItem; go: (r: Route) => void; act: (fn: () => Promise<unknown>, done: string) => void }) {
  return (
    <tr className={`q-row ${it.status}`}>
      <td>
        <b className="q-title">{it.title || it.doi || it.input}</b>
        <small>{it.title ? it.doi ?? it.input : it.doi && it.doi !== it.input ? it.input : ""}</small>
        {it.error && <small className="error-text">{it.error}</small>}
      </td>
      <td>
        <span className={`q-status ${it.status}`}>{it.status === "running" ? <Spinner size={10} /> : <i aria-hidden="true" />} {STATUS_LABEL[it.status]}</span>
        {it.status === "running" && (
          <>
            <span className="bar mini" role="progressbar" aria-valuenow={Math.round(it.progress * 100)} aria-valuemin={0} aria-valuemax={100} aria-label="Progress"><i style={{ width: `${Math.round(it.progress * 100)}%` }} /></span>
            <small>{it.stage}</small>
          </>
        )}
      </td>
      <td>{it.score !== null && it.score !== undefined ? <span className={`score-pill ${scoreTone(it.score)}`}>{it.score}</span> : <span className="muted">–</span>}</td>
      <td><span title={absTime(it.added)}>{relTime(it.added)}</span></td>
      <td className="q-acts">
        {it.run && <button className="btn s" onClick={() => go({ page: "run", run: it.run! })}>Open</button>}
        {(it.status === "queued" || it.status === "running") && <button className="btn s" disabled={/cancel/i.test(it.stage)} onClick={() => act(() => api.cancelItem(it.id), "Cancelled")}>Cancel</button>}
        {(it.status === "error" || it.status === "cancelled") && <button className="btn s" onClick={() => act(() => api.retryItem(it.id), "Queued again")}><Icon name="refresh" /> Retry</button>}
        {it.status !== "running" && <button className="icon-btn s" aria-label={`Remove ${it.input} from the queue`} onClick={() => act(() => api.removeItem(it.id), "Removed")}><Icon name="close" size={14} /></button>}
      </td>
    </tr>
  );
}
