import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { useKeydown, useReducedMotion, useStored, useZoomPan } from "./lib/hooks";
import { Icon, Segmented, Spinner } from "./ui";

export type CompareMode = "blink" | "swipe" | "onion" | "diff" | "side";

const MODES: { id: CompareMode; label: string; key: string; title: string }[] = [
  { id: "blink", label: "Blink", key: "1", title: "Shows A and B in the same place, one after the other. Identical pictures stand still." },
  { id: "swipe", label: "Swipe", key: "2", title: "Drag the line to uncover B over A." },
  { id: "onion", label: "Onion", key: "3", title: "Lays B over A at the opacity you choose." },
  { id: "diff", label: "Difference", key: "4", title: "Subtracts B from A. Black means the two agree." },
  { id: "side", label: "Side by side", key: "5", title: "A and B next to each other, zoomed together." },
];

interface Prefs {
  mode: CompareMode;
  rate: number;
  gain: number;
  invert: boolean;
  onion: number;
}

const GAP = 16;

/** The blink comparator on the full dark stage.  Two aligned copies are shown in the same place: identical pictures
 *  stand still, different ones shimmer.  Swipe, onion, difference and side by side answer the same question other
 *  ways.  Zoom and pan are shared by every mode. */
export function CompareStage({ a, b, diff, labelA, labelB, aligned, measures, active, toolbarStart, toolbarEnd, onBack }: {
  a: string;
  b: string;
  diff?: string;
  labelA: string;
  labelB: string;
  aligned: boolean | null;
  measures?: ReactNode;
  active: boolean;
  toolbarStart?: ReactNode;
  toolbarEnd?: ReactNode;
  onBack?: () => void;
}) {
  const reduce = useReducedMotion();
  const [prefs, setPrefs] = useStored<Prefs>("pi.blink", { mode: "blink", rate: 2, gain: 4, invert: false, onion: 50 });
  const [mode, setMode] = useState<CompareMode>(prefs.mode);
  const [showB, setShowB] = useState(false);
  const [hold, setHold] = useState<"a" | "b" | null>(null);
  const [playing, setPlaying] = useState(!reduce);
  const [split, setSplit] = useState(50);
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const [err, setErr] = useState(false);
  const [cross, setCross] = useState<{ x: number; y: number } | null>(null);
  const canDiff = aligned !== false;

  useEffect(() => {
    setShowB(false);
    setDims(null);
    setErr(false);
  }, [a, b]);
  useEffect(() => {
    if (aligned === false) setMode("side");
  }, [aligned]);
  useEffect(() => {
    if (reduce) setPlaying(false);
  }, [reduce]);

  const pick = (m: CompareMode) => {
    if (m === "diff" && !canDiff) return;
    setMode(m);
    setPrefs((p) => ({ ...p, mode: m }));
  };

  useEffect(() => {
    if (mode !== "blink" || !playing || hold) return;
    const t = window.setInterval(() => setShowB((v) => !v), 1000 / prefs.rate);
    return () => window.clearInterval(t);
  }, [mode, playing, prefs.rate, hold]);

  const w = dims ? (mode === "side" ? dims.w * 2 + GAP : dims.w) : 0;
  const h = dims?.h ?? 0;
  const zp = useZoomPan(w, h, `${a}|${b}|${mode === "side"}`);
  const { view } = zp;

  useKeydown((e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const m = MODES.find((x) => x.key === e.key);
    if (m) { e.preventDefault(); pick(m.id); return; }
    if (e.key === " " && (mode === "blink" || mode === "onion")) {
      e.preventDefault();
      if (mode === "blink") { setPlaying(false); setShowB((v) => !v); }
      else setPrefs((p) => ({ ...p, onion: p.onion > 50 ? 0 : 100 }));
      return;
    }
    if (e.key === "p" && mode === "blink") { e.preventDefault(); setPlaying((p) => !p); return; }
    if ((e.key === "a" || e.key === "b") && !e.repeat) { e.preventDefault(); setHold(e.key); return; }
    if (e.key === "+" || e.key === "=") { e.preventDefault(); zp.zoomAt(1.25); }
    else if (e.key === "-") { e.preventDefault(); zp.zoomAt(0.8); }
    else if (e.key === "0") { e.preventDefault(); zp.fitTo(); }
    else if (e.key === "!" || (e.shiftKey && e.code === "Digit1")) { e.preventDefault(); zp.setScale(1); }
    else if (e.key === "Escape" && onBack) { e.preventDefault(); onBack(); }
  }, active);

  useEffect(() => {
    if (!hold) return;
    const up = (e: KeyboardEvent) => { if (e.key === hold) setHold(null); };
    window.addEventListener("keyup", up);
    window.addEventListener("blur", () => setHold(null), { once: true });
    return () => window.removeEventListener("keyup", up);
  }, [hold]);

  const bVisible = hold ? hold === "b" : showB;
  const frameColour = mode === "blink" ? (bVisible ? "b" : "a") : hold ?? null;
  const pixelated = view.s >= 1;
  const sx = (x: number) => view.x + x * view.s;
  const sy = (y: number) => view.y + y * view.s;

  const bStyle = useMemo((): React.CSSProperties => {
    if (hold === "a") return { opacity: 0 };
    if (hold === "b") return { opacity: 1 };
    if (mode === "blink") return { opacity: showB ? 1 : 0 };
    if (mode === "swipe") return { clipPath: `inset(0 0 0 ${split}%)` };
    if (mode === "onion") return { opacity: prefs.onion / 100 };
    if (mode === "diff") return { mixBlendMode: "difference" };
    return {};
  }, [mode, showB, split, prefs.onion, hold]);

  const swipeFromPointer = (clientX: number) => {
    const r = zp.vp.current?.getBoundingClientRect();
    if (!r || !dims) return;
    setSplit(Math.max(0, Math.min(100, ((clientX - r.left - view.x) / (dims.w * view.s)) * 100)));
  };

  const frameRect = dims ? { left: sx(0), top: sy(0), width: dims.w * view.s, height: dims.h * view.s } : null;

  return (
    <div className="compare">
      <div className="stage-bar">
        {toolbarStart}
        <Segmented label="Comparison mode" value={mode} onChange={pick} className="dark"
          options={MODES.map((m) => ({ id: m.id, label: m.label, title: `${m.title} Key ${m.key}.`, disabled: m.id === "diff" && !canDiff }))} />
        <span className="ab-chips">
          <button className={`ab-chip a${hold === "a" ? " on" : ""}`} onPointerDown={() => setHold("a")} onPointerUp={() => setHold(null)}
            onPointerLeave={() => setHold((x) => (x === "a" ? null : x))} title="Hold to show only A (or hold the A key)">A <span>{labelA}</span></button>
          <button className={`ab-chip b${hold === "b" ? " on" : ""}`} onPointerDown={() => setHold("b")} onPointerUp={() => setHold(null)}
            onPointerLeave={() => setHold((x) => (x === "b" ? null : x))} title="Hold to show only B (or hold the B key)">B <span>{labelB}</span></button>
        </span>
        <span className="grow" />
        <ZoomGroup s={view.s} onIn={() => zp.zoomAt(1.25)} onOut={() => zp.zoomAt(0.8)} onFit={() => zp.fitTo()} onSet={zp.setScale} />
        {toolbarEnd}
      </div>

      {aligned === false && (
        <div className="stage-banner" role="status">
          No shared content was found under any shift, rotation, mirror or rescale. The panels are shown unaligned.
        </div>
      )}

      <div className={`viewport${pixelated ? " pixelated" : ""}${mode === "swipe" ? " swipe" : ""}`} ref={zp.vp} tabIndex={0}
        aria-label={`Comparison of ${labelA} and ${labelB}, ${MODES.find((m) => m.id === mode)?.label} mode`}
        {...zp.handlers}
        onPointerDown={(e) => {
          if (mode === "swipe" && !e.shiftKey && e.button === 0) {
            (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
            swipeFromPointer(e.clientX);
            return;
          }
          zp.handlers.onPointerDown(e);
        }}
        onPointerMove={(e) => {
          if (mode === "swipe" && e.buttons === 1 && !e.shiftKey) { swipeFromPointer(e.clientX); return; }
          if (mode === "side" && dims) {
            const r = zp.vp.current!.getBoundingClientRect();
            const cx = (e.clientX - r.left - view.x) / view.s, cy = (e.clientY - r.top - view.y) / view.s;
            const lx = cx > dims.w + GAP / 2 ? cx - dims.w - GAP : cx;
            setCross(lx >= 0 && lx <= dims.w && cy >= 0 && cy <= dims.h ? { x: lx, y: cy } : null);
          }
          zp.handlers.onPointerMove(e);
        }}
        onPointerLeave={() => setCross(null)}>
        {!dims && !err && <div className="stage-loading" aria-live="polite"><Spinner /> Loading the two panels…</div>}
        {err && <div className="stage-loading">These panels could not be loaded.</div>}
        <div className="content" style={{ width: w || undefined, height: h || undefined, transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})`, visibility: dims ? "visible" : "hidden" }}>
          {mode === "side" ? (
            <>
              <img src={a} alt={`A: ${labelA}`} draggable={false} style={{ position: "absolute", left: 0, top: 0, width: dims?.w, height: dims?.h }}
                onLoad={(e) => setDims({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })} onError={() => setErr(true)} />
              <img src={b} alt={`B: ${labelB}`} draggable={false} style={{ position: "absolute", left: (dims?.w ?? 0) + GAP, top: 0, width: dims?.w, height: dims?.h }} />
            </>
          ) : (
            <div className={`frame-stack${mode === "diff" ? " diff" : ""}`}
              style={mode === "diff" ? { filter: `brightness(${prefs.gain})${prefs.invert ? " invert(1)" : ""}` } : undefined}>
              <img src={a} alt={`A: ${labelA}`} draggable={false}
                onLoad={(e) => setDims({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })} onError={() => setErr(true)} />
              <img src={b} alt={`B: ${labelB}`} draggable={false} className="over" style={bStyle} />
            </div>
          )}
        </div>
        {dims && frameRect && mode !== "side" && (
          <>
            {frameColour && <div className={`frame-ring ${frameColour}`} style={frameRect} aria-hidden="true" />}
            {mode === "swipe" ? (
              <>
                <span className="frame-tag a" style={{ left: frameRect.left + 6, top: frameRect.top + 6 }}>A</span>
                <span className="frame-tag b" style={{ left: frameRect.left + frameRect.width - 26, top: frameRect.top + 6 }}>B</span>
                <div className="swipe-line" style={{ left: frameRect.left + (frameRect.width * split) / 100, top: frameRect.top, height: frameRect.height }}>
                  <span className="swipe-grip" role="slider" tabIndex={0} aria-label="Swipe position" aria-valuemin={0} aria-valuemax={100}
                    aria-valuenow={Math.round(split)} aria-valuetext={`${Math.round(split)}% of B uncovered from the right`}
                    onKeyDown={(e) => {
                      const st = e.shiftKey ? 10 : 1;
                      if (e.key === "ArrowLeft") { e.preventDefault(); setSplit((x) => Math.max(0, x - st)); }
                      else if (e.key === "ArrowRight") { e.preventDefault(); setSplit((x) => Math.min(100, x + st)); }
                      else if (e.key === "Home") { e.preventDefault(); setSplit(0); }
                      else if (e.key === "End") { e.preventDefault(); setSplit(100); }
                    }} />
                </div>
              </>
            ) : (
              <span className={`frame-tag ${mode === "blink" ? (bVisible ? "b" : "a") : hold ?? "a"}`} style={{ left: frameRect.left + 6, top: frameRect.top + 6 }}>
                {mode === "blink" || hold ? (bVisible ? `B · ${labelB}` : `A · ${labelA}`) :
                  mode === "onion" ? `A + B at ${prefs.onion}%` : mode === "diff" ? `A − B, ×${prefs.gain}` : ""}
              </span>
            )}
          </>
        )}
        {dims && mode === "side" && (
          <>
            <span className="frame-tag a" style={{ left: sx(0) + 6, top: sy(0) + 6 }}>A · {labelA}</span>
            <span className="frame-tag b" style={{ left: sx(dims.w + GAP) + 6, top: sy(0) + 6 }}>B · {labelB}</span>
            {cross && [0, dims.w + GAP].map((off) => (
              <span key={off} aria-hidden="true">
                <span className="cross v" style={{ left: sx(off + cross.x), top: sy(0), height: dims.h * view.s }} />
                <span className="cross h" style={{ left: sx(off), top: sy(cross.y), width: dims.w * view.s }} />
              </span>
            ))}
          </>
        )}
      </div>

      <div className="stage-controls">
        {mode === "blink" && (
          <>
            <button className="sbtn" onClick={() => setPlaying((p) => !p)} title="Play or pause (P)">
              <Icon name={playing ? "pause" : "play"} /> {playing ? "Pause" : "Play"}
            </button>
            <button className="sbtn" onClick={() => { setPlaying(false); setShowB((v) => !v); }} title="Flip once (Space)">
              <Icon name="flip" /> Flip
            </button>
            <label className="slider">
              Speed
              <input type="range" min={1} max={8} step={0.5} value={prefs.rate} aria-valuetext={`${prefs.rate} flips per second`}
                onChange={(e) => setPrefs((p) => ({ ...p, rate: Number(e.target.value) }))} />
              <output>{prefs.rate}/s</output>
            </label>
            {reduce && !playing ? <span className="note">Auto-blink is off because reduced motion is on. Press Space to flip.</span>
              : prefs.rate > 3 ? <span className="note">Fast flicker. Lower the speed if this is uncomfortable.</span>
                : <span className="note">Hold A or B to freeze a frame.</span>}
          </>
        )}
        {mode === "swipe" && <span className="note">Drag across the picture, or focus the handle and use the arrow keys. Shift-drag pans.</span>}
        {mode === "onion" && (
          <label className="slider">
            Opacity of B
            <input type="range" min={0} max={100} step={1} value={prefs.onion} aria-valuetext={`${prefs.onion}%`}
              onChange={(e) => setPrefs((p) => ({ ...p, onion: Number(e.target.value) }))} />
            <output>{prefs.onion}%</output>
          </label>
        )}
        {mode === "diff" && (
          <>
            <label className="slider">
              Gain
              <input type="range" min={1} max={16} step={1} value={prefs.gain} aria-valuetext={`times ${prefs.gain}`}
                onChange={(e) => setPrefs((p) => ({ ...p, gain: Number(e.target.value) }))} />
              <output>×{prefs.gain}</output>
            </label>
            <label className="check">
              <input type="checkbox" checked={prefs.invert} onChange={(e) => setPrefs((p) => ({ ...p, invert: e.target.checked }))} /> Invert
            </label>
            <span className="note">{prefs.invert ? "White" : "Black"} means the two copies agree pixel for pixel. Raise the gain to see faint differences.</span>
            {diff && <a className="sbtn" href={diff} target="_blank" rel="noreferrer" title="The engine's own difference image">Engine image <Icon name="ext" /></a>}
          </>
        )}
        {mode === "side" && <span className="note">Both sides zoom and pan together. The crosshair marks the same spot in each.</span>}
        <span className="grow" />
        {measures && <span className="measures-strip">{measures}</span>}
      </div>
    </div>
  );
}

export function ZoomGroup({ s, onIn, onOut, onFit, onSet }: { s: number; onIn: () => void; onOut: () => void; onFit: () => void; onSet: (s: number) => void }) {
  return (
    <span className="zoom-group" role="group" aria-label="Zoom">
      <button className="sicon" onClick={onOut} aria-label="Zoom out" title="Zoom out (−)"><Icon name="minus" /></button>
      <button className="sbtn zoom-read" onClick={() => onSet(1)} title="Show at 100% (Shift+1)" aria-label={`Zoom ${Math.round(s * 100)}%. Show at 100%`}>
        {Math.round(s * 100)}%
      </button>
      <button className="sicon" onClick={onIn} aria-label="Zoom in" title="Zoom in (+)"><Icon name="plus" /></button>
      <button className="sicon" onClick={onFit} aria-label="Fit to stage" title="Fit (0)"><Icon name="fit" /></button>
    </span>
  );
}

/** A small auto-blinking preview for the inspector (static under reduced motion). */
export function MiniBlink({ a, b, onOpen }: { a: string; b: string; onOpen: () => void }) {
  const reduce = useReducedMotion();
  const [showB, setShowB] = useState(false);
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (reduce) return;
    const t = window.setInterval(() => setShowB((v) => !v), 500);
    return () => window.clearInterval(t);
  }, [reduce]);
  return (
    <button ref={ref} className={`mini-blink ${showB ? "b" : "a"}`} onClick={onOpen} aria-label="Show the comparison on the stage">
      <span className="mb-frame">
        <img src={a} alt="" />
        <img src={b} alt="" style={{ opacity: showB ? 1 : 0 }} />
      </span>
      <span className="mb-tag">{showB ? "B" : "A"}</span>
      <span className="mb-open">Show in stage <kbd className="kbd">↵</kbd></span>
    </button>
  );
}
