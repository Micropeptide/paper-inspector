import { createContext, ReactNode, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { SEV_GLYPH, SEV_LABEL, Severity } from "./api";

/* ---------------------------------------------------------------- icons (24px line icons, currentColor) */

const PATHS: Record<string, string> = {
  search: "M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zm9 16-4.2-4.2",
  chevL: "M15 5l-7 7 7 7",
  chevR: "M9 5l7 7-7 7",
  chevD: "M6 9l6 6 6-6",
  plus: "M12 5v14M5 12h14",
  minus: "M5 12h14",
  close: "M6 6l12 12M18 6 6 18",
  sun: "M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z",
  monitor: "M3 5h18v11H3zM8 20h8M12 16v4",
  gear: "M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  download: "M12 4v11M7 10l5 5 5-5M5 20h14",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  ext: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  check: "M5 12.5l4.5 4.5L19 7.5",
  info: "M12 8h.01M11 12h1v5h1M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z",
  sparkle: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  play: "M7 5l12 7-12 7z",
  pause: "M8 5v14M16 5v14",
  flip: "M4 8h13l-3-3M20 16H7l3 3",
  fit: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  outline: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  lens: "M10 4a6 6 0 1 1 0 12 6 6 0 0 1 0-12zm4.5 10.5L20 20",
  marks: "M4 4h10v10H4zM10 10h10v10H10z",
  note: "M5 4h10l4 4v12H5zM14 4v5h5M8 13h8M8 17h5",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
  queue: "M4 6h16M4 12h16M4 18h10",
  doc: "M6 3h9l4 4v14H6zM14 3v5h5",
  table: "M4 5h16v14H4zM4 10h16M4 15h16M10 5v14",
  text: "M5 6h14M5 10h14M5 14h10M5 18h7",
  refs: "M6 4h12v16l-6-4-6 4z",
  record: "M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 7v5l3 2",
  keyboard: "M3 7h18v10H3zM7 11h.01M11 11h.01M15 11h.01M7 14h10",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6",
  stop: "M7 7h10v10H7z",
  tag: "M3 12V4h8l10 10-8 8zM7.5 7.5h.01",
  upload: "M12 16V5M7 10l5-5 5 5M5 20h14",
  lock: "M6 11h12v9H6zM8 11V8a4 4 0 0 1 8 0v3",
};

export function Icon({ name, size = 16, className }: { name: keyof typeof PATHS | string; size?: number; className?: string }) {
  return (
    <svg className={`icon${className ? ` ${className}` : ""}`} width={size} height={size} viewBox="0 0 24 24" aria-hidden="true"
      fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
      <path d={PATHS[name] ?? PATHS.info} />
    </svg>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return <span className="spinner" style={{ width: size, height: size }} aria-hidden="true" />;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function SeverityBadge({ s, solid = false }: { s: Severity; solid?: boolean }) {
  return (
    <span className={`sev-badge ${s}${solid ? " solid" : ""}`}>
      <span aria-hidden="true">{SEV_GLYPH[s]}</span> {SEV_LABEL[s]}
    </span>
  );
}

/* ---------------------------------------------------------------- toasts */

interface Toast {
  id: number;
  text: string;
  tone?: "error" | "ok";
  action?: { label: string; run: () => void };
  ms: number;
}

const ToastCtx = createContext<(t: Omit<Toast, "id" | "ms"> & { ms?: number }) => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const n = useRef(0);
  const push = useCallback((t: Omit<Toast, "id" | "ms"> & { ms?: number }) => {
    const id = ++n.current;
    setItems((x) => [...x.slice(-2), { ...t, id, ms: t.ms ?? (t.action ? 6000 : 4000) }]);
  }, []);
  const drop = useCallback((id: number) => setItems((x) => x.filter((y) => y.id !== id)), []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => <ToastItem key={t.id} t={t} onDone={() => drop(t.id)} />)}
      </div>
    </ToastCtx.Provider>
  );
}

function ToastItem({ t, onDone }: { t: Toast; onDone: () => void }) {
  const [hold, setHold] = useState(false);
  useEffect(() => {
    if (hold) return;
    const h = window.setTimeout(onDone, t.ms);
    return () => window.clearTimeout(h);
  }, [hold, t.ms, onDone]);
  return (
    <div className={`toast${t.tone ? ` ${t.tone}` : ""}`} onMouseEnter={() => setHold(true)} onMouseLeave={() => setHold(false)}
      onFocus={() => setHold(true)} onBlur={() => setHold(false)}>
      <span>{t.text}</span>
      {t.action && <button className="link-btn" onClick={() => { t.action!.run(); onDone(); }}>{t.action.label}</button>}
      <button className="icon-btn s" aria-label="Dismiss" onClick={onDone}><Icon name="close" size={14} /></button>
    </div>
  );
}

/* ---------------------------------------------------------------- popover and menu */

/** A button that opens a floating panel.  Closes on Esc, outside click, or when an item calls close(). */
export function Popover({ label, button, children, className, align = "start", title, buttonClass }: {
  label: string;
  button: ReactNode;
  children: (close: () => void) => ReactNode;
  className?: string;
  align?: "start" | "end";
  title?: string;
  buttonClass?: string;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
        btn.current?.focus();
      }
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [open]);
  const close = useCallback(() => {
    setOpen(false);
    btn.current?.focus();
  }, []);
  return (
    <div className={`pop-wrap${className ? ` ${className}` : ""}`} ref={wrap}>
      <button ref={btn} className={buttonClass ?? "btn"} aria-label={label} title={title} aria-expanded={open} aria-controls={id}
        onClick={() => setOpen((o) => !o)}>
        {button}
      </button>
      {open && (
        <div id={id} className={`popover ${align}`} role="dialog" aria-label={label}>
          {children(close)}
        </div>
      )}
    </div>
  );
}

export interface MenuItem {
  label: string;
  hint?: string;
  icon?: string;
  href?: string;
  download?: boolean;
  newTab?: boolean;
  run?: () => void;
  disabled?: boolean;
  separator?: boolean;
}

/** A menu button (APG pattern): arrow keys move, Enter runs, Esc closes. */
export function Menu({ label, button, items, align = "end", buttonClass }: {
  label: string; button: ReactNode; items: MenuItem[]; align?: "start" | "end"; buttonClass?: string;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const live = items.filter((i) => !i.separator);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.setTimeout(() => (list.current?.querySelector("[role=menuitem]:not([aria-disabled=true])") as HTMLElement | null)?.focus(), 0);
    return () => window.removeEventListener("pointerdown", onDown);
  }, [open]);
  const onKey = (e: React.KeyboardEvent) => {
    const els = Array.from(list.current?.querySelectorAll<HTMLElement>("[role=menuitem]") ?? []);
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === "ArrowDown") { e.preventDefault(); els[(i + 1) % els.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); els[(i - 1 + els.length) % els.length]?.focus(); }
    else if (e.key === "Home") { e.preventDefault(); els[0]?.focus(); }
    else if (e.key === "End") { e.preventDefault(); els[els.length - 1]?.focus(); }
    else if (e.key === "Escape" || e.key === "Tab") {
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      btn.current?.focus();
    }
  };
  return (
    <div className="pop-wrap" ref={wrap}>
      <button ref={btn} className={buttonClass ?? "btn"} aria-haspopup="menu" aria-expanded={open} aria-label={label}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => { if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); } }}>
        {button}
      </button>
      {open && live.length > 0 && (
        <div className={`popover menu ${align}`} role="menu" aria-label={label} ref={list} onKeyDown={onKey}>
          {items.map((it, i) =>
            it.separator ? <div key={i} className="menu-sep" role="separator" /> :
              it.href && !it.disabled ? (
                <a key={i} role="menuitem" tabIndex={-1} className="menu-item" href={it.href}
                  {...(it.download ? { download: "" } : {})} {...(it.newTab ? { target: "_blank", rel: "noreferrer" } : {})}
                  onClick={() => setOpen(false)}>
                  {it.icon && <Icon name={it.icon} />}<span>{it.label}</span>{it.hint && <small>{it.hint}</small>}
                </a>
              ) : (
                <button key={i} role="menuitem" tabIndex={-1} className="menu-item" aria-disabled={it.disabled || undefined}
                  onClick={() => { if (it.disabled) return; setOpen(false); it.run?.(); }}>
                  {it.icon && <Icon name={it.icon} />}<span>{it.label}</span>{it.hint && <small>{it.hint}</small>}
                </button>
              ))}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- dialog */

/** Native <dialog> shown modally; Esc and the close button call onClose; focus returns to the opener. */
export function Modal({ title, onClose, children, className, footer }: {
  title: string; onClose: () => void; children: ReactNode; className?: string; footer?: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef<Element | null>(null);
  const id = useId();
  useLayoutEffect(() => {
    opener.current = document.activeElement;
    const d = ref.current;
    if (d && !d.open) {
      try { d.showModal(); } catch { d.setAttribute("open", ""); }
    }
    return () => {
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, []);
  return (
    <dialog ref={ref} className={`modal${className ? ` ${className}` : ""}`} aria-labelledby={id}
      onCancel={(e) => { e.preventDefault(); onClose(); }}
      onClick={(e) => { if (e.target === ref.current) onClose(); }}>
      <div className="modal-body">
        <header className="modal-head">
          <h2 id={id}>{title}</h2>
          <button className="icon-btn" aria-label="Close" onClick={onClose}><Icon name="close" /></button>
        </header>
        {children}
        {footer && <footer className="modal-foot">{footer}</footer>}
      </div>
    </dialog>
  );
}

/* ---------------------------------------------------------------- segmented control */

export function Segmented<T extends string>({ value, options, onChange, label, className }: {
  value: T;
  options: { id: T; label: ReactNode; title?: string; disabled?: boolean }[];
  onChange: (v: T) => void;
  label: string;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const onKey = (e: React.KeyboardEvent) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const live = options.filter((o) => !o.disabled);
    const i = live.findIndex((o) => o.id === value);
    const n = e.key === "Home" ? 0 : e.key === "End" ? live.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + live.length) % live.length;
    onChange(live[n].id);
    window.setTimeout(() => (ref.current?.querySelector("[aria-checked=true]") as HTMLElement | null)?.focus(), 0);
  };
  return (
    <div ref={ref} className={`seg${className ? ` ${className}` : ""}`} role="radiogroup" aria-label={label} onKeyDown={onKey}>
      {options.map((o) => (
        <button key={o.id} role="radio" aria-checked={o.id === value} tabIndex={o.id === value ? 0 : -1} title={o.title}
          disabled={o.disabled} className={o.id === value ? "on" : ""} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- resizer */

/** A vertical splitter between panes (role=separator); drag, arrow keys, double-click to reset. */
export function Resizer({ value, min, max, onChange, onReset, label, side }: {
  value: number; min: number; max: number; onChange: (v: number) => void; onReset: () => void; label: string; side: "left" | "right";
}) {
  const start = useRef<{ x: number; v: number } | null>(null);
  const clamp = (v: number) => Math.max(min, Math.min(max, v));
  return (
    <div className="resizer" role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0}
      aria-valuenow={Math.round(value)} aria-valuemin={min} aria-valuemax={max}
      onPointerDown={(e) => {
        start.current = { x: e.clientX, v: value };
        (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (!start.current) return;
        const d = e.clientX - start.current.x;
        onChange(clamp(start.current.v + (side === "left" ? d : -d)));
      }}
      onPointerUp={() => { start.current = null; }}
      onDoubleClick={onReset}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
          e.preventDefault();
          const d = (e.key === "ArrowRight" ? 16 : -16) * (side === "left" ? 1 : -1);
          onChange(clamp(value + d));
        }
      }} />
  );
}

export function Meter({ value, max, label }: { value: number; max: number; label: string }) {
  const pct = max ? Math.round((value / max) * 100) : 0;
  return (
    <span className="meter" role="meter" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max} aria-label={label} title={label}>
      <i style={{ width: `${pct}%` }} />
    </span>
  );
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
