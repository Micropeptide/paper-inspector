import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { FigureInfo, Finding, Severity, SEVERITY_ORDER, url } from "./api";
import { ZoomGroup } from "./Blink";
import { useKeydown, useReducedMotion, useStored, useZoomPan } from "./lib/hooks";
import { Icon, Spinner } from "./ui";

export const VIEWS = [
  ["original", "Original", "The figure as printed."],
  ["levels", "Levels", "Stretches contrast to show faint backgrounds and edits."],
  ["reveal", "Reveal", "Exaggerates tonal steps to expose pasted or painted areas."],
  ["noise", "Noise", "Shows the noise pattern. Pasted areas often have different noise."],
  ["edges", "Edges", "Traces edges so straight splice lines stand out."],
  ["false", "False colour", "Maps brightness to colour to separate near-identical greys."],
] as const;

export type Outlines = "always" | "hover" | "off";

interface Mark {
  key: number;
  x: number;
  y: number;
  w: number;
  h: number;
  tone: "a" | "b";
  tag: string;
}

export function marksFor(f: Finding | null, fig: FigureInfo): Mark[] {
  if (!f) return [];
  const out: Mark[] = [];
  f.locations.forEach((l, i) => {
    if (l.figure !== fig.id) return;
    let box: number[] | null = null;
    if (l.panel) {
      const p = fig.panels.find((x) => x.id === l.panel);
      if (!p) return;
      const [px, py, pw, ph] = p.box;
      const b = l.box ?? [0, 0, pw, ph];
      box = [px + b[0], py + b[1], b[2], b[3]];
    } else if (l.box && l.box.length === 4) {
      box = l.box;
    }
    if (!box) return;
    const n = out.length;
    out.push({ key: i, x: box[0], y: box[1], w: box[2], h: box[3], tone: n % 2 ? "b" : "a", tag: n === 0 ? "A" : n === 1 ? "B" : String(n + 1) });
  });
  return out;
}

/** The figure on the dark stage: zoom, pan, a loupe, panel outlines you can pick, and the finding's marks. */
export function FigureStage({ run, fig, figures, finding, pick, onPanel, onFigure, view, setView, worst, active, modeSwitch, toolbarEnd }: {
  run: string;
  fig: FigureInfo;
  figures: FigureInfo[];
  finding: Finding | null;
  pick: string[];
  onPanel: (id: string) => void;
  onFigure: (id: string) => void;
  view: string;
  setView: (v: string) => void;
  worst: Record<string, { s: Severity; n: number }>;
  active: boolean;
  modeSwitch: ReactNode;
  toolbarEnd?: ReactNode;
}) {
  const reduce = useReducedMotion();
  const [outlines, setOutlines] = useStored<Outlines>("pi.outlines", "always");
  const [showMarks, setShowMarks] = useStored("pi.marks", true);
  const [strip, setStrip] = useStored("pi.filmstrip", true);
  const [loupeOn, setLoupeOn] = useStored("pi.loupe", false);
  const [legendOpen, setLegendOpen] = useState(false);
  const [lens, setLens] = useState<{ x: number; y: number; cx: number; cy: number } | null>(null);
  const [shown, setShown] = useState(() => url.figure(run, fig.id, view));
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [hint, setHint] = useStored("pi.hints.compare", true);
  const zp = useZoomPan(fig.width, fig.height, `${run}/${fig.id}`);
  const v = zp.view;
  const marks = useMemo(() => marksFor(finding, fig), [finding, fig]);
  const idx = figures.findIndex((x) => x.id === fig.id);

  // Load the next view in the background; swap when it has decoded so the stage never flashes empty.
  useEffect(() => {
    const src = url.figure(run, fig.id, view);
    setFailed(false);
    if (src === shown) return;
    setLoading(true);
    const im = new Image();
    im.onload = () => { setShown(src); setLoading(false); };
    im.onerror = () => { setLoading(false); setFailed(true); };
    im.src = src;
    return () => { im.onload = null; im.onerror = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, fig.id, view]);

  // A newly selected finding: fit its marks, unless the user just zoomed by hand.
  useEffect(() => {
    if (!marks.length || Date.now() - zp.touched.current < 2000) return;
    const x0 = Math.min(...marks.map((m) => m.x)), y0 = Math.min(...marks.map((m) => m.y));
    const x1 = Math.max(...marks.map((m) => m.x + m.w)), y1 = Math.max(...marks.map((m) => m.y + m.h));
    const t = window.setTimeout(() => zp.fitTo([x0, y0, x1 - x0, y1 - y0], 0.15), reduce ? 0 : 30);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finding?.id, fig.id]);

  const cycleOutlines = () => setOutlines((o) => (o === "always" ? "hover" : o === "hover" ? "off" : "always"));

  useKeydown((e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const vi = VIEWS.findIndex((_, i) => String(i + 1) === e.key);
    if (vi >= 0 && !e.shiftKey) { e.preventDefault(); setView(VIEWS[vi][0]); return; }
    switch (e.key) {
      case ",": if (idx > 0) { e.preventDefault(); onFigure(figures[idx - 1].id); } break;
      case ".": if (idx < figures.length - 1) { e.preventDefault(); onFigure(figures[idx + 1].id); } break;
      case "+": case "=": e.preventDefault(); zp.zoomAt(1.25); break;
      case "-": e.preventDefault(); zp.zoomAt(0.8); break;
      case "0": e.preventDefault(); zp.fitTo(); break;
      case "!": e.preventDefault(); zp.setScale(1); break;
      case "o": e.preventDefault(); cycleOutlines(); break;
      case "m": e.preventDefault(); setShowMarks((x) => !x); break;
      case "l": e.preventDefault(); setLoupeOn((x) => !x); break;
      case "F": e.preventDefault(); setStrip((x) => !x); break;
      default:
        if (document.activeElement === zp.vp.current && e.key.startsWith("Arrow")) {
          e.preventDefault();
          const st = e.shiftKey ? 256 : 64;
          zp.panBy(e.key === "ArrowLeft" ? st : e.key === "ArrowRight" ? -st : 0, e.key === "ArrowUp" ? st : e.key === "ArrowDown" ? -st : 0);
        }
    }
  }, active);

  const panels = useMemo(() => [...fig.panels].sort((p, q) => (p.box[1] - q.box[1]) || (p.box[0] - q.box[0])), [fig]);
  const showLabels = v.s >= 0.4 || panels.length <= 24;
  const L = Math.max(2, v.s * 3);

  return (
    <div className="figure-stage">
      <div className="stage-bar">
        {modeSwitch}
        <span className="fig-nav">
          <button className="sicon" aria-label="Previous figure" title="Previous figure (,)" disabled={idx <= 0} onClick={() => onFigure(figures[idx - 1].id)}><Icon name="chevL" /></button>
          <label className="sr" htmlFor="fig-pick">Figure</label>
          <select id="fig-pick" className="sselect" value={fig.id} onChange={(e) => onFigure(e.target.value)}>
            {figures.map((f) => <option key={f.id} value={f.id}>{f.label}{worst[f.id] ? ` (${worst[f.id].n})` : ""}</option>)}
          </select>
          <button className="sicon" aria-label="Next figure" title="Next figure (.)" disabled={idx >= figures.length - 1} onClick={() => onFigure(figures[idx + 1].id)}><Icon name="chevR" /></button>
          {fig.page ? <span className="muted">p. {fig.page}</span> : null}
        </span>
        <span className="view-modes seg dark" role="radiogroup" aria-label="Image view">
          {VIEWS.map(([id, label, tip], i) => (
            <button key={id} role="radio" aria-checked={view === id} className={view === id ? "on" : ""} title={`${tip} Key ${i + 1}.`} onClick={() => setView(id)}>
              {label}{view === id && loading ? <Spinner size={10} /> : null}
            </button>
          ))}
        </span>
        <span className="grow" />
        <ZoomGroup s={v.s} onIn={() => zp.zoomAt(1.25)} onOut={() => zp.zoomAt(0.8)} onFit={() => zp.fitTo()} onSet={zp.setScale} />
        <button className={`sicon${loupeOn ? " on" : ""}`} aria-pressed={loupeOn} aria-label="Loupe" title="Loupe: magnify under the pointer (L)" onClick={() => setLoupeOn((x) => !x)}><Icon name="lens" /></button>
        <button className={`sicon${outlines !== "off" ? " on" : ""}`} aria-label={`Panel outlines: ${outlines === "always" ? "always shown" : outlines === "hover" ? "on hover only" : "off"}`}
          title={`Panel outlines: ${outlines === "always" ? "always" : outlines === "hover" ? "on hover" : "off"} (O)`} onClick={cycleOutlines}><Icon name="outline" /></button>
        <button className={`sicon${showMarks ? " on" : ""}`} aria-pressed={showMarks} aria-label="Finding marks" title="Show the finding's marks (M)" onClick={() => setShowMarks((x) => !x)}><Icon name="marks" /></button>
        {toolbarEnd}
      </div>

      <div className={`viewport outlines-${outlines}${v.s >= 2 ? " pixelated" : ""}${loupeOn ? " loupe-on" : ""}`} ref={zp.vp} tabIndex={0}
        aria-label={`${fig.label}. Drag to pan, Ctrl or Cmd and scroll to zoom.`}
        {...zp.handlers}
        onPointerMove={(e) => {
          zp.handlers.onPointerMove(e);
          if (!loupeOn) return;
          const r = zp.vp.current!.getBoundingClientRect();
          const x = e.clientX - r.left, y = e.clientY - r.top;
          const cx = (x - v.x) / v.s, cy = (y - v.y) / v.s;
          setLens(cx >= 0 && cy >= 0 && cx <= fig.width && cy <= fig.height ? { x, y, cx, cy } : null);
        }}
        onPointerLeave={() => setLens(null)}>
        <div className="content" style={{ width: fig.width, height: fig.height, transform: `translate(${v.x}px, ${v.y}px) scale(${v.s})` }}>
          <img src={shown} alt={`${fig.label}${fig.caption ? `. ${fig.caption.slice(0, 160)}` : ""}`} draggable={false} width={fig.width} height={fig.height}
            onError={() => setFailed(true)} />
          <svg viewBox={`0 0 ${fig.width} ${fig.height}`} width={fig.width} height={fig.height}>
            {panels.map((p) => {
              const [x, y, w, h] = p.box;
              const pi = pick.indexOf(p.id);
              const name = p.id.split(".").pop()!.toUpperCase();
              return (
                <g key={p.id} className={`pan${pi === 0 ? " pick-a" : pi === 1 ? " pick-b" : ""}`} role="button" tabIndex={0}
                  aria-pressed={pi >= 0} aria-label={`Panel ${fig.label.replace(/^Figure\s*/i, "")}${name}. ${pi >= 0 ? `Picked as ${pi ? "B" : "A"}.` : "Press Enter to pick it for comparison."}`}
                  onClick={() => { if (!zp.wasDrag()) onPanel(p.id); }}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPanel(p.id); } }}>
                  <rect className="halo" x={x} y={y} width={w} height={h} />
                  <rect className="line" x={x} y={y} width={w} height={h} />
                </g>
              );
            })}
            {showMarks && marks.map((m) => (
              <g key={m.key} className={`mark ${m.tone}`} aria-hidden="true">
                <rect className="halo" x={m.x} y={m.y} width={m.w} height={m.h} />
                <rect className="line" x={m.x} y={m.y} width={m.w} height={m.h} />
              </g>
            ))}
          </svg>
        </div>
        <div className="label-layer" aria-hidden="true">
          {showLabels && outlines !== "off" && panels.map((p) => {
            const pi = pick.indexOf(p.id);
            return (
              <span key={p.id} className={`pan-label${pi === 0 ? " a" : pi === 1 ? " b" : ""}`}
                style={{ left: v.x + p.box[0] * v.s + 4, top: v.y + (p.box[1] + p.box[3]) * v.s - 22 }}>
                {p.id.split(".").pop()!.toUpperCase()}{pi >= 0 ? `, picked as ${pi ? "B" : "A"}` : ""}
              </span>
            );
          })}
          {showMarks && marks.map((m) => (
            <span key={m.key} className={`mark-tag ${m.tone}`} style={{ left: v.x + m.x * v.s, top: v.y + m.y * v.s - 20 }}>{m.tag}</span>
          ))}
        </div>
        {loupeOn && lens && (
          <div className="loupe" aria-hidden="true" style={{
            left: lens.x - 90, top: lens.y - 90,
            backgroundImage: `url("${shown}")`,
            backgroundSize: `${fig.width * L}px ${fig.height * L}px`,
            backgroundPosition: `${-(lens.cx * L - 90)}px ${-(lens.cy * L - 90)}px`,
          }}><span>×{(L).toFixed(1)}</span></div>
        )}
        {failed && (
          <div className="stage-loading">
            This figure could not be loaded.
            <button className="sbtn" onClick={() => { setFailed(false); setShown(`${url.figure(run, fig.id, view)}${url.figure(run, fig.id, view).includes("?") ? "&" : "?"}r=${Date.now()}`); }}>Retry</button>
          </div>
        )}
        {hint && pick.length === 0 && (
          <div className="stage-hint">
            Click two panels to blink them against each other.
            <button className="sicon" aria-label="Dismiss hint" onClick={() => setHint(false)}><Icon name="close" size={14} /></button>
          </div>
        )}
        {pick.length === 1 && <div className="stage-hint">Panel {pick[0].split(".").pop()!.toUpperCase()} picked as A. Click a second panel, in any figure.</div>}
      </div>

      {fig.caption && (
        <div className={`legend${legendOpen ? " open" : ""}`}>
          <p><b>{fig.label}.</b> {fig.caption.replace(/^Fig(ure)?\.?\s*\d+[a-z]?\.?\s*/i, "")}</p>
          <button className="link-btn" aria-expanded={legendOpen} onClick={() => setLegendOpen((x) => !x)}>{legendOpen ? "Show less" : "Show full legend"}</button>
        </div>
      )}

      {figures.length > 1 && (
        <div className={`filmstrip${strip ? "" : " closed"}`}>
          <button className="sicon strip-toggle" aria-expanded={strip} aria-label={strip ? "Hide figure strip" : "Show figure strip"} title="Figure strip (Shift+F)" onClick={() => setStrip((x) => !x)}>
            <Icon name={strip ? "chevD" : "chevR"} />
          </button>
          {strip && (
            <div className="strip-items" role="listbox" aria-label="Figures" aria-orientation="horizontal">
              {figures.map((f) => (
                <button key={f.id} role="option" aria-selected={f.id === fig.id} className={`thumb${f.id === fig.id ? " on" : ""}`} onClick={() => onFigure(f.id)}
                  onKeyDown={(e) => {
                    const i = figures.indexOf(f);
                    if (e.key === "ArrowRight" && i < figures.length - 1) { e.preventDefault(); onFigure(figures[i + 1].id); }
                    if (e.key === "ArrowLeft" && i > 0) { e.preventDefault(); onFigure(figures[i - 1].id); }
                  }}>
                  <img src={url.figure(run, f.id)} alt="" loading="lazy" />
                  <span>{f.label.replace(/^Figure/i, "Fig.")}</span>
                  {worst[f.id] && <i className={`thumb-badge ${worst[f.id].s}`} aria-label={`${worst[f.id].n} findings`}>{worst[f.id].n}</i>}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Worst severity and count of non-note findings per figure, for badges. */
export function worstByFigure(findings: Finding[]): Record<string, { s: Severity; n: number }> {
  const out: Record<string, { s: Severity; n: number }> = {};
  for (const f of findings) {
    if (f.severity === "info") continue;
    for (const fig of new Set(f.locations.map((l) => l.figure).filter(Boolean) as string[])) {
      const cur = out[fig];
      if (!cur) out[fig] = { s: f.severity, n: 1 };
      else out[fig] = { s: SEVERITY_ORDER.indexOf(f.severity) < SEVERITY_ORDER.indexOf(cur.s) ? f.severity : cur.s, n: cur.n + 1 };
    }
  }
  return out;
}

/** Shown on the stage when the selected finding is about text, data or the record. */
export function NonFigureCard({ run, f, onBrowse }: { run: string; f: Finding | null; onBrowse: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  if (!f) {
    return (
      <div className="nonfig"><div className="nonfig-card">
        <h2>Nothing on the stage yet</h2>
        <p>Pick a finding, or browse the figures and click two panels to compare them.</p>
        <button className="sbtn" onClick={onBrowse}>Browse figures</button>
      </div></div>
    );
  }
  const what: Record<string, [string, string]> = {
    data: ["table", "the data"], stats: ["text", "the statistics in the text"], text: ["text", "the text"], refs: ["refs", "the references"],
    report: ["doc", "what the paper reports"], record: ["record", "the publication record"], image: ["doc", "an image that is not shown as a figure"],
  };
  const [icon, noun] = what[f.category] ?? ["doc", "the paper"];
  const file = f.locations.find((l) => l.file);
  const page = f.locations.find((l) => l.page)?.page;
  const parts = f.locations.filter((l) => l.file || l.sheet || l.cells).slice(0, 3)
    .map((l) => [l.file, l.sheet && `sheet “${l.sheet}”`, l.cells && `cells ${l.cells}`].filter(Boolean).join(", "));
  return (
    <div className="nonfig" ref={ref}>
      <div className="nonfig-card">
        <Icon name={icon} size={28} />
        <h2>This finding is about {noun}, not a figure.</h2>
        {parts.length > 0 ? <ul>{parts.map((p, i) => <li key={i}>{p}</li>)}</ul> : page ? <p>Page {page} of the paper.</p> : !f.locations.length ? <p>This finding has no location in the paper.</p> : null}
        <div className="row">
          {file?.file && <a className="sbtn" href={url.supplement(run, file.file)}>Download {file.file} <Icon name="download" /></a>}
          {page && <a className="sbtn" href={`${url.source(run)}#page=${page}`} target="_blank" rel="noreferrer">Open the PDF at page {page} <Icon name="ext" /></a>}
          <button className="sbtn" onClick={onBrowse}>Browse figures</button>
        </div>
      </div>
    </div>
  );
}
