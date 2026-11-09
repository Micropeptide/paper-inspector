import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Kbd, Modal } from "./ui";

export interface Command {
  id: string;
  group: string;
  label: string;
  hint?: string;
  keys?: string;
  run: () => void;
}

type Register = (owner: string, items: Command[]) => void;
const Ctx = createContext<Register>(() => undefined);

/** Pages register the commands they offer (findings, figures, actions); the palette shows them all. */
export function useCommands(owner: string, items: Command[]) {
  const reg = useContext(Ctx);
  useEffect(() => {
    reg(owner, items);
  }, [reg, owner, items]);
  useEffect(() => () => reg(owner, []), [reg, owner]);
}

export function CommandProvider({ children, render }: { children: ReactNode; render: (items: Command[]) => ReactNode }) {
  const [owners, setOwners] = useState<Record<string, Command[]>>({});
  const reg = useCallback<Register>((owner, items) => setOwners((o) => (o[owner] === items ? o : { ...o, [owner]: items })), []);
  const all = useMemo(() => Object.values(owners).flat(), [owners]);
  return <Ctx.Provider value={reg}>{children}{render(all)}</Ctx.Provider>;
}

/** Subsequence match with a score that prefers word starts and contiguous runs. */
function score(q: string, text: string): number {
  if (!q) return 1;
  const t = text.toLowerCase();
  const idx = t.indexOf(q);
  if (idx >= 0) return 100 - idx + (idx === 0 || t[idx - 1] === " " ? 50 : 0);
  let ti = 0, s = 0, run = 0;
  for (const ch of q) {
    const n = t.indexOf(ch, ti);
    if (n < 0) return 0;
    run = n === ti ? run + 1 : 0;
    s += 1 + run + (n === 0 || t[n - 1] === " " ? 2 : 0);
    ti = n + 1;
  }
  return s;
}

export function Palette({ items, onClose }: { items: Command[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return items
      .map((c) => ({ c, s: Math.max(score(needle, c.label), score(needle, `${c.group} ${c.hint ?? ""}`) * 0.5) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => (needle ? b.s - a.s : 0))
      .slice(0, 60)
      .map((x) => x.c);
  }, [q, items]);
  useEffect(() => setSel(0), [q]);
  useEffect(() => {
    input.current?.focus();
  }, []);
  useEffect(() => {
    list.current?.querySelector(`#cmd-${sel}`)?.scrollIntoView({ block: "nearest" });
  }, [sel]);
  const runIt = (c?: Command) => {
    if (!c) return;
    onClose();
    window.setTimeout(c.run, 0);
  };
  let lastGroup = "";
  return (
    <Modal title="Go to or do" onClose={onClose} className="palette">
      <input ref={input} className="pal-input" role="combobox" aria-expanded="true" aria-controls="pal-list" aria-activedescendant={shown[sel] ? `cmd-${sel}` : undefined}
        aria-label="Type a command, a finding or a figure" placeholder="Type a finding, a figure, a scan or an action" value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(shown.length - 1, s + 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
          else if (e.key === "Enter") { e.preventDefault(); runIt(shown[sel]); }
        }} />
      <div className="pal-list" id="pal-list" role="listbox" ref={list} aria-label="Results">
        {shown.length === 0 && <p className="muted pal-empty">Nothing matches “{q}”.</p>}
        {shown.map((c, i) => {
          const head = c.group !== lastGroup && !q ? c.group : null;
          lastGroup = c.group;
          return (
            <div key={c.id}>
              {head && <div className="pal-group" role="presentation">{head}</div>}
              <div id={`cmd-${i}`} role="option" aria-selected={i === sel} className={`pal-item${i === sel ? " on" : ""}`}
                onMouseMove={() => setSel(i)} onClick={() => runIt(c)}>
                <span className="pal-label">{c.label}</span>
                {c.hint && <span className="pal-hint">{c.hint}</span>}
                {q && <span className="pal-hint">{c.group}</span>}
                {c.keys && <Kbd>{c.keys}</Kbd>}
              </div>
            </div>
          );
        })}
      </div>
    </Modal>
  );
}

export const SHORTCUTS: [string, [string, string][]][] = [
  ["Anywhere", [
    ["Cmd/Ctrl K", "Command palette"], ["?", "This list"], ["g h", "All scans"], ["g q", "Batch queue"], ["Cmd/Ctrl ,", "Settings"],
  ]],
  ["Findings", [
    ["j / ↓", "Next finding"], ["k / ↑", "Previous finding"], ["c", "Confirm"], ["f", "False positive"], ["r", "Needs raw data"],
    ["u", "Back to not reviewed"], ["n", "Write a note"], ["/", "Search findings"], ["Cmd/Ctrl Shift C", "Copy link to this finding"],
  ]],
  ["Run view", [
    ["g f / g i / g p / g d", "Findings, figures, paper, draft tab"], ["[", "Hide or show the findings pane"], ["]", "Hide or show the details pane"],
    ["Enter", "Compare the pair on the stage"], ["v", "Switch between figure and comparison"], ["Esc", "Clear picked panels, leave comparison"],
  ]],
  ["Figure stage", [
    [", / .", "Previous or next figure"], ["1 to 6", "Original, levels, reveal, noise, edges, false colour"], ["+ / −", "Zoom in or out"],
    ["0", "Fit"], ["Shift 1", "100%"], ["l", "Loupe"], ["o", "Panel outlines"], ["m", "Finding marks"], ["Shift F", "Figure strip"],
    ["Enter on a panel", "Pick it for comparison"],
  ]],
  ["Comparison", [
    ["1 to 5", "Blink, swipe, onion, difference, side by side"], ["Space", "Flip once"], ["p", "Play or pause"], ["hold a / hold b", "Show only A or B"],
  ]],
];

export function ShortcutSheet({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Keyboard shortcuts" onClose={onClose} className="shortcuts">
      <div className="sc-grid">
        {SHORTCUTS.map(([group, rows]) => (
          <section key={group}>
            <h3>{group}</h3>
            <dl>
              {rows.map(([k, what]) => (
                <div key={k}><dt>{k.split(" / ").map((x, i) => <span key={i}>{i ? " or " : ""}<Kbd>{x}</Kbd></span>)}</dt><dd>{what}</dd></div>
              ))}
            </dl>
          </section>
        ))}
      </div>
      <p className="muted">Single-key shortcuts are off while you type in a box.</p>
    </Modal>
  );
}
