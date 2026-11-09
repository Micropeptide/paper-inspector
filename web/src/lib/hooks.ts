import { useCallback, useEffect, useRef, useState } from "react";

/* ---------------------------------------------------------------- storage */

export function readStore<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writeStore(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private window or storage full: the setting just is not remembered */
  }
}

/** useState that remembers its value in localStorage (per browser, a convenience only). */
export function useStored<T>(key: string, fallback: T): [T, (v: T | ((p: T) => T)) => void] {
  const [v, setV] = useState<T>(() => readStore(key, fallback));
  const set = useCallback((n: T | ((p: T) => T)) => {
    setV((p) => {
      const next = typeof n === "function" ? (n as (p: T) => T)(p) : n;
      writeStore(key, next);
      return next;
    });
  }, [key]);
  return [v, set];
}

/* ---------------------------------------------------------------- motion */

export function useReducedMotion(): boolean {
  const q = "(prefers-reduced-motion: reduce)";
  const [r, setR] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setR(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return r;
}

/* ---------------------------------------------------------------- keyboard */

export function isEditable(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable) return true;
  if (tag === "INPUT") {
    const type = (el as HTMLInputElement).type;
    return !["checkbox", "radio", "button", "range", "submit"].includes(type);
  }
  return false;
}

/** One window-level key handler; the callback sees every keydown that is not typed into a field (Esc always). */
export function useKeydown(fn: (e: KeyboardEvent) => void, active = true): void {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const on = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      // dialogs (settings, palette, letters) handle their own keys
      if ((e.target as HTMLElement | null)?.closest?.("dialog[open]")) return;
      if (e.key !== "Escape" && isEditable(e.target)) return;
      ref.current(e);
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [active]);
}

/* ---------------------------------------------------------------- hash route */

export interface Route {
  page: "home" | "run" | "queue";
  run?: string;
  finding?: string;
  figure?: string;
  cmp?: [string, string];
  tab?: string;
  batch?: string;
}

export function parseHash(h: string): Route {
  const [path, query = ""] = h.replace(/^#/, "").split("?");
  const q = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean).map((p) => {
    try { return decodeURIComponent(p); } catch { return p; }
  });
  if (parts[0] === "queue") return { page: "queue", batch: parts[1] };
  if (parts[0] === "run" && parts[1]) {
    const r: Route = { page: "run", run: parts[1], tab: q.get("tab") ?? undefined };
    if (parts[2] === "f" && parts[3]) r.finding = parts[3];
    if (parts[2] === "fig" && parts[3]) r.figure = parts[3];
    if (parts[2] === "cmp" && parts[3] && parts[4]) r.cmp = [parts[3], parts[4]];
    return r;
  }
  return { page: "home" };
}

export function buildHash(r: Route): string {
  const e = encodeURIComponent;
  if (r.page === "queue") return r.batch ? `#/queue/${e(r.batch)}` : "#/queue";
  if (r.page !== "run" || !r.run) return "#/";
  let p = `#/run/${e(r.run)}`;
  if (r.cmp) p += `/cmp/${e(r.cmp[0])}/${e(r.cmp[1])}`;
  else if (r.finding) p += `/f/${e(r.finding)}`;
  else if (r.figure) p += `/fig/${e(r.figure)}`;
  if (r.tab && r.tab !== "findings") p += `?tab=${e(r.tab)}`;
  return p;
}

export function useHashRoute(): [Route, (r: Route, replace?: boolean) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", on);
    window.addEventListener("popstate", on);
    return () => {
      window.removeEventListener("hashchange", on);
      window.removeEventListener("popstate", on);
    };
  }, []);
  const go = useCallback((r: Route, replace = false) => {
    const h = buildHash(r);
    if (h === window.location.hash || (h === "#/" && !window.location.hash)) {
      setRoute(r);
      return;
    }
    if (replace) window.history.replaceState(null, "", h);
    else window.history.pushState(null, "", h);
    setRoute(parseHash(h));
  }, []);
  return [route, go];
}

/* ---------------------------------------------------------------- zoom and pan */

export interface View {
  s: number;
  x: number;
  y: number;
}

export const ZOOM_MIN = 0.1;
export const ZOOM_MAX = 16;

/** Zoom and pan of a content box (w x h in content pixels) inside a viewport element.  Ctrl/Cmd + wheel or a
 *  trackpad pinch zooms about the pointer; plain wheel and drag pan; double-click zooms in. */
export function useZoomPan(w: number, h: number, key: string) {
  const vp = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>({ s: 1, x: 0, y: 0 });
  const touched = useRef(0);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean; id: number } | null>(null);
  const moved = useRef(false);

  const fitTo = useCallback((box?: number[], pad = 0.06) => {
    const el = vp.current;
    if (!el || !w || !h) return;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const [bx, by, bw, bh] = box ?? [0, 0, w, h];
    const pw = bw * (1 + pad * 2), ph = bh * (1 + pad * 2);
    let s = Math.min(r.width / pw, r.height / ph);
    if (box) s = Math.min(s, 4);
    s = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, s));
    setView({ s, x: r.width / 2 - (bx + bw / 2) * s, y: r.height / 2 - (by + bh / 2) * s });
  }, [w, h]);

  // fit whenever the content changes
  useEffect(() => {
    fitTo();
  }, [fitTo, key]);

  useEffect(() => {
    const el = vp.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      if (Date.now() - touched.current > 400) fitTo();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [fitTo]);

  const zoomAt = useCallback((factor: number, cx?: number, cy?: number) => {
    const el = vp.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const px = cx ?? r.width / 2, py = cy ?? r.height / 2;
    touched.current = Date.now();
    setView((v) => {
      const s = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, v.s * factor));
      const k = s / v.s;
      return { s, x: px - (px - v.x) * k, y: py - (py - v.y) * k };
    });
  }, []);

  const setScale = useCallback((s: number) => {
    const el = vp.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    touched.current = Date.now();
    setView((v) => {
      const k = s / v.s;
      return { s, x: r.width / 2 - (r.width / 2 - v.x) * k, y: r.height / 2 - (r.height / 2 - v.y) * k };
    });
  }, []);

  const panBy = useCallback((dx: number, dy: number) => {
    touched.current = Date.now();
    setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }));
  }, []);

  // wheel needs a non-passive listener to stop the page from zooming
  useEffect(() => {
    const el = vp.current;
    if (!el) return;
    const on = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) {
        const f = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0025));
        zoomAt(f, e.clientX - r.left, e.clientY - r.top);
      } else {
        const m = e.deltaMode === 1 ? 16 : 1;
        panBy(-e.deltaX * m, -e.deltaY * m);
      }
    };
    el.addEventListener("wheel", on, { passive: false });
    return () => el.removeEventListener("wheel", on);
  }, [zoomAt, panBy]);

  const handlers = {
    onPointerDown: (e: React.PointerEvent) => {
      if (e.button !== 0) return;
      drag.current = { x: e.clientX, y: e.clientY, vx: 0, vy: 0, moved: false, id: e.pointerId };
      moved.current = false;
    },
    onPointerMove: (e: React.PointerEvent) => {
      const d = drag.current;
      if (!d || d.id !== e.pointerId) return;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      if (!d.moved && Math.hypot(dx, dy) < 4) return;
      if (!d.moved) {
        d.moved = true;
        moved.current = true;
        try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* ignore */ }
      }
      d.x = e.clientX;
      d.y = e.clientY;
      panBy(dx, dy);
    },
    onPointerUp: (e: React.PointerEvent) => {
      if (drag.current?.id === e.pointerId) drag.current = null;
    },
    onPointerCancel: () => { drag.current = null; },
    onDoubleClick: (e: React.MouseEvent) => {
      const r = vp.current?.getBoundingClientRect();
      if (!r) return;
      zoomAt(e.altKey ? 0.5 : 2, e.clientX - r.left, e.clientY - r.top);
    },
  };

  /** True right after a drag, so the click that ends it does not pick a panel. */
  const wasDrag = () => moved.current;

  return { vp, view, setView, fitTo, zoomAt, setScale, panBy, handlers, wasDrag, touched };
}

/* ---------------------------------------------------------------- polling */

/** Run fn every `ms` while `active`; stops after three failures in a row and reports them. */
export function usePoll(fn: () => Promise<unknown>, ms: number, active: boolean, onFail?: (e: Error) => void) {
  const ref = useRef(fn);
  ref.current = fn;
  const fail = useRef(onFail);
  fail.current = onFail;
  useEffect(() => {
    if (!active) return;
    let stop = false;
    let errs = 0;
    let t = 0;
    const tick = async () => {
      try {
        await ref.current();
        errs = 0;
      } catch (e) {
        errs += 1;
        if (errs >= 3) {
          fail.current?.(e as Error);
          return;
        }
      }
      if (!stop) t = window.setTimeout(tick, ms * (errs ? errs * 2 : 1));
    };
    t = window.setTimeout(tick, ms);
    return () => {
      stop = true;
      window.clearTimeout(t);
    };
  }, [ms, active]);
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}
