import { forwardRef, ReactNode, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Catalog, checkLabel, FigureInfo, Finding, num, pairOf, panelLabel, RULING_KEY, RULING_LABEL, RulingId, url, VERDICT } from "./api";
import { MiniBlink } from "./Blink";
import { useStored } from "./lib/hooks";
import { Icon, Popover, SeverityBadge, Spinner } from "./ui";

const RULINGS: RulingId[] = ["unreviewed", "confirmed", "false_positive", "needs_raw_data"];
const RULING_ICON: Record<RulingId, string> = { unreviewed: "○", confirmed: "●", false_positive: "⊘", needs_raw_data: "◐" };

export function RulingMark({ r }: { r?: RulingId }) {
  const v = r ?? "unreviewed";
  return <span className={`ruling-mark ${v}`} title={RULING_LABEL[v]} aria-label={RULING_LABEL[v]}>{RULING_ICON[v]}</span>;
}

/** "What this means": the catalogue entry for a check, in plain words. */
export function CheckHelp({ check, catalog, children }: { check: string; catalog: Catalog | null; children?: ReactNode }) {
  const info = catalog?.checks.find((c) => c.id === check);
  return (
    <Popover label={`What “${checkLabel(check, catalog)}” means`} buttonClass="link-btn help-btn" title="What this means"
      button={children ?? <><Icon name="info" size={14} /> What this means</>}>
      {() => info ? (
        <div className="help-pop">
          <h3>{info.label}</h3>
          <p className="help-kind">{info.category_label}. {info.scored ? "An integrity signal: it counts towards the score." : "A quality note: listed, not scored."}</p>
          <h4>What was measured</h4><p>{info.what}</p>
          <h4>Common false alarms</h4><p>{info.false_alarms}</p>
          <h4>What to look at</h4><p>{info.look_at}</p>
          <h4>A question for the authors</h4><p className="ask">{info.ask}</p>
        </div>
      ) : <div className="help-pop"><p>{catalog ? "No description for this check yet." : "Loading the check catalogue…"}</p></div>}
    </Popover>
  );
}

export interface InspectorHandle {
  focusNote: () => void;
}

export const Inspector = forwardRef<InspectorHandle, {
  run: string;
  f: Finding;
  figures: FigureInfo[];
  catalog: Catalog | null;
  saving: boolean;
  onRuling: (r: RulingId, note?: string) => void;
  onFigure: (id: string) => void;
  onCompare: () => void;
  onPrev: () => void;
  onNext: () => void;
  onCopyLink: () => void;
  position: string;
}>(function Inspector({ run, f, figures, catalog, saving, onRuling, onFigure, onCompare, onPrev, onNext, onCopyLink, position }, ref) {
  const pair = pairOf(f);
  const figs = Array.from(new Set(f.locations.map((l) => l.figure).filter(Boolean))) as string[];
  const ruling = f.ruling?.ruling ?? "unreviewed";
  const [note, setNote] = useState(f.ruling?.note ?? "");
  const [noteOpen, setNoteOpen] = useState(Boolean(f.ruling?.note));
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const [closed, setClosed] = useStored<Record<string, boolean>>("pi.sections", {});
  const d = f.data as Record<string, unknown>;

  useEffect(() => {
    setNote(f.ruling?.note ?? "");
    setNoteOpen(Boolean(f.ruling?.note));
  }, [f.id, f.ruling?.note]);

  useImperativeHandle(ref, () => ({
    focusNote: () => {
      setNoteOpen(true);
      window.setTimeout(() => noteRef.current?.focus(), 0);
    },
  }));

  const saveNote = () => {
    if ((f.ruling?.note ?? "") !== note) onRuling(ruling, note.slice(0, 4000));
  };

  const gif = f.evidence.find((e) => e.endsWith(".gif"));
  const stills = f.evidence.filter((e) => /\.(png|jpe?g)$/i.test(e) && !(pair && e.includes("-diff")));
  const quotes = f.locations.filter((l) => l.quote);
  const files = f.locations.filter((l) => l.file || l.sheet);

  const sec = (id: string, title: string, children: ReactNode) => (
    <section className="insp-sec" key={id}>
      <h3>
        <button aria-expanded={!closed[id]} onClick={() => setClosed((c) => ({ ...c, [id]: !c[id] }))}>
          <Icon name={closed[id] ? "chevR" : "chevD"} size={14} /> {title}
        </button>
      </h3>
      {!closed[id] && <div className="sec-body">{children}</div>}
    </section>
  );

  return (
    <div className="insp">
      <div className="insp-head">
        <div className="insp-meta">
          <SeverityBadge s={f.severity} />
          <span className="insp-check">{checkLabel(f.check, catalog)}</span>
          <span className="conf" title="How sure the detector is">
            <span className="conf-bar"><i style={{ width: `${Math.round(f.confidence * 100)}%` }} /></span>
            {Math.round(f.confidence * 100)}%
          </span>
          <span className="grow" />
          <span className="insp-pos">{position}</span>
          <button className="icon-btn s" aria-label="Previous finding" title="Previous (k)" onClick={onPrev}><Icon name="chevL" /></button>
          <button className="icon-btn s" aria-label="Next finding" title="Next (j)" onClick={onNext}><Icon name="chevR" /></button>
        </div>
        <h2>{f.title}</h2>
        <div className="insp-sub">
          <span className={`kind-pill ${f.kind}`}>{f.kind === "integrity" ? "Integrity signal" : "Quality note"}</span>
          <CheckHelp check={f.check} catalog={catalog} />
          <button className="link-btn" onClick={onCopyLink} title="Copy a link to this finding"><Icon name="link" size={14} /> Copy link</button>
        </div>
        {figs.length > 1 && (
          <div className="fig-jump">
            {figs.map((x) => <button key={x} className="chip" onClick={() => onFigure(x)}>{figures.find((y) => y.id === x)?.label ?? x}</button>)}
          </div>
        )}
      </div>

      <div className="triage" aria-busy={saving}>
        <div className="triage-row" role="radiogroup" aria-label="Your ruling on this finding">
          {RULINGS.map((r) => (
            <button key={r} role="radio" aria-checked={ruling === r} className={`rule-btn ${r}${ruling === r ? " on" : ""}`}
              title={`${RULING_LABEL[r]} (${RULING_KEY[r]})`} onClick={() => onRuling(r)}>
              <span aria-hidden="true">{RULING_ICON[r]}</span> {RULING_LABEL[r]} <kbd className="kbd">{RULING_KEY[r]}</kbd>
            </button>
          ))}
          {saving && <Spinner />}
        </div>
        {noteOpen ? (
          <div className="note-box">
            <label htmlFor={`note-${f.id}`}>Your note</label>
            <textarea id={`note-${f.id}`} ref={noteRef} value={note} maxLength={4000} rows={3}
              placeholder="What you checked, and what you would ask the authors."
              onChange={(e) => setNote(e.target.value)} onBlur={saveNote}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveNote(); noteRef.current?.blur(); }
                if (e.key === "Escape") { e.preventDefault(); setNote(f.ruling?.note ?? ""); noteRef.current?.blur(); }
              }} />
            <small>Saved when you leave the box, or with {navigator.platform.includes("Mac") ? "Cmd" : "Ctrl"}+Enter. Notes stay on this computer.</small>
          </div>
        ) : (
          <button className="link-btn" onClick={() => { setNoteOpen(true); window.setTimeout(() => noteRef.current?.focus(), 0); }}>
            <Icon name="note" size={14} /> Add a note <kbd className="kbd">n</kbd>
          </button>
        )}
        {f.ruling?.updated && ruling !== "unreviewed" && <small className="muted">Ruled {new Date(f.ruling.updated).toLocaleString()}</small>}
      </div>

      {sec("what", "What we found", <>
        <p className="detail">{f.detail}</p>
      </>)}

      {pair && (
        sec("cmp", "Comparison", <>
          <MiniBlink a={url.frame(run, "a", pair[0], pair[1])} b={url.frame(run, "b", pair[0], pair[1])} onOpen={onCompare} />
          <p className="muted">A is {panelLabel(pair[0], figures)}, B is {panelLabel(pair[1], figures)}, aligned onto A.</p>
        </>)
      )}

      {pair && (
        sec("measures", "Measurements", <>
          <Measures d={d} />
        </>)
      )}

      {f.ai && (
        sec("ai", "AI opinion", <>
          <p className={`ai-note ${f.ai.verdict}`}><b>{VERDICT[f.ai.verdict]}.</b> {f.ai.reason}</p>
        </>)
      )}

      {(stills.length > 0 || (!pair && gif)) && (
        sec("evidence", "Evidence", <>
          <div className="evidence-list">
            {!pair && gif && <a href={url.evidence(run, gif)} target="_blank" rel="noreferrer"><img className="evidence" src={url.evidence(run, gif)} alt={`Animation flicking between the two marked regions for ${checkLabel(f.check, catalog)}`} /></a>}
            {stills.map((e) => (
              <a key={e} href={url.evidence(run, e)} target="_blank" rel="noreferrer" title="Open full size">
                <img className="evidence" src={url.evidence(run, e)} alt={`${checkLabel(f.check, catalog)} evidence${figs[0] ? `, ${figures.find((x) => x.id === figs[0])?.label ?? figs[0]}` : ""}`} />
              </a>
            ))}
          </div>
        </>)
      )}

      {(quotes.length > 0 || files.length > 0) && (
        sec("where", "Where", <>
          {quotes.map((l, i) => (
            <blockquote key={i}>
              {l.quote}
              {l.page ? <a className="page-link" href={`${url.source(run)}#page=${l.page}`} target="_blank" rel="noreferrer">p. {l.page} <Icon name="ext" size={12} /></a> : null}
            </blockquote>
          ))}
          {files.length > 0 && (
            <ul className="locs">
              {files.map((l, i) => (
                <li key={i}>
                  {l.file ? <a href={url.supplement(run, l.file)} title="Downloads the file">{l.file}</a> : "The article"}
                  {l.sheet && <>, {/^(page |Table)/.test(l.sheet) ? "" : "sheet "}“{l.sheet}”</>}
                  {l.cells && <>, cells {l.cells}</>}
                </li>
              ))}
            </ul>
          )}
        </>)
      )}

      <DataList data={f.data} />
    </div>
  );
});

function Measures({ d }: { d: Record<string, unknown> }) {
  const tex = Number(d.ncc_texture);
  const transform = [d.mirrored ? "mirrored" : "", Math.abs(Number(d.rotation)) > 3 ? `rotated ${Math.round(Number(d.rotation))}°` : "",
    Number.isFinite(Number(d.scale)) && Math.abs(Number(d.scale) - 1) > 0.05 ? `scaled ×${Number(d.scale).toFixed(2)}` : ""].filter(Boolean).join(", ");
  return (
    <dl className="measures">
      <dt>Texture match <span className="hint-i" title="How well the fine grain (camera noise, speckle) agrees after alignment. Copies usually score above 0.5; unrelated images score near 0.">?</span></dt>
      <dd>
        {num(tex, 2)}
        {Number.isFinite(tex) && <span className="tbar" aria-hidden="true"><i style={{ width: `${Math.max(0, Math.min(1, tex)) * 100}%` }} /><b style={{ left: "50%" }} /></span>}
      </dd>
      <dt>Matching keypoints <span className="hint-i" title="Distinct points found in both panels that agree on one geometric transform. A few dozen is already strong.">?</span></dt>
      <dd>{num(d.inliers)}</dd>
      <dt>Shared area</dt>
      <dd>{Number.isFinite(Number(d.overlap_a)) ? `${Math.round(Number(d.overlap_a) * 100)}% of A, ${Math.round(Number(d.overlap_b) * 100)}% of B` : "–"}</dd>
      <dt>Transform</dt>
      <dd>{transform || "none"}</dd>
    </dl>
  );
}

const LIST_KEYS: [string, string][] = [
  ["values", "Values"], ["examples", "Examples"], ["references", "References"], ["titles", "Works"],
  ["dois", "DOIs"], ["missing", "Not found"], ["uncited", "Uncited reference numbers"],
];

/** Lists carried in a finding's data (values, references, titles) shown as plain lists. */
function DataList({ data }: { data: Record<string, unknown> }) {
  const [all, setAll] = useState<Record<string, boolean>>({});
  const lists = LIST_KEYS.map(([k, label]) => [label, data?.[k]] as const)
    .filter(([, v]) => Array.isArray(v) && (v as unknown[]).length) as [string, unknown[]][];
  const works = Array.isArray(data?.works) ? (data.works as { title?: string; doi?: string }[]) : [];
  if (!lists.length && !works.length) return null;
  return (
    <section className="insp-sec datalist">
      <h3><span>Raw data</span></h3>
      {lists.map(([label, v]) => (
        <details key={label} open={v.length <= 8}>
          <summary>{label} ({v.length})</summary>
          <ul>{(all[label] ? v : v.slice(0, 40)).map((x, i) => <li key={i}>{typeof x === "string" ? x : JSON.stringify(x)}</li>)}</ul>
          {v.length > 40 && !all[label] && <button className="link-btn" onClick={() => setAll((a) => ({ ...a, [label]: true }))}>Show all {v.length}</button>}
        </details>
      ))}
      {works.length > 0 && (
        <details open>
          <summary>Works ({works.length})</summary>
          <ul>{works.slice(0, 20).map((w, i) => <li key={i}>{w.doi ? <a href={w.doi.startsWith("http") ? w.doi : `https://doi.org/${w.doi}`} target="_blank" rel="noreferrer">{w.title || w.doi} ↗</a> : w.title}</li>)}</ul>
        </details>
      )}
    </section>
  );
}

/** The inspector when two panels were picked by hand. */
export function ManualSummary({ a, b, figures, res, err, onClear, onRetry }: {
  a: string; b: string; figures: FigureInfo[];
  res: { aligned: boolean; inliers?: number; ncc_texture?: number; mirrored?: boolean } | null;
  err: string; onClear: () => void; onRetry: () => void;
}) {
  return (
    <div className="insp">
      <div className="insp-head">
        <div className="insp-meta"><span className="manual-pill">Your comparison</span></div>
        <h2>{panelLabel(a, figures)} against {panelLabel(b, figures)}</h2>
      </div>
      <div aria-live="polite">
        {err ? (
          <div className="inline-error">
            <p>The panels could not be compared: {err}</p>
            <button className="btn" onClick={onRetry}>Retry</button>
          </div>
        ) : !res ? (
          <p className="muted"><Spinner /> Aligning the panels…</p>
        ) : res.aligned ? (
          <p className="detail">These panels line up: {num(res.inliers)} keypoints agree{res.mirrored ? " after mirroring" : ""}, and the fine texture correlates at {num(res.ncc_texture, 2)} (copies usually score above 0.5, unrelated images near 0).</p>
        ) : (
          <p className="detail">No shared content found: the panels do not line up under any shift, rotation, mirror or rescale. The stage shows them side by side, unaligned.</p>
        )}
      </div>
      <div className="row"><button className="btn" onClick={onClear}>Clear comparison</button></div>
    </div>
  );
}
