import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, Catalog, CATEGORY_NAMES, checkLabel, Compare, FigureInfo, Finding, Job, loadCatalog, pairOf, panelLabel, Report,
  Review, RULING_LABEL, RulingId, scoreTone, scoreWord, SEV_GLYPH, SEV_LABEL, SEVERITY_ORDER, url, VERDICT, whereOf,
} from "./api";
import { CompareStage } from "./Blink";
import { ExportMenu, LettersDialog } from "./Exports";
import { Inspector, InspectorHandle, ManualSummary, RulingMark } from "./Inspector";
import { Route, useDebounced, useKeydown, usePoll, useStored } from "./lib/hooks";
import { Command, useCommands } from "./Palette";
import PaperTab from "./PaperTab";
import { FigureStage, NonFigureCard, worstByFigure } from "./Stage";
import { copyText, Icon, Menu, Popover, Segmented, Spinner, useToast, Resizer, Meter } from "./ui";

type Tab = "findings" | "figures" | "paper" | "draft";
const TABS: { id: Tab; label: string; key: string }[] = [
  { id: "findings", label: "Findings", key: "f" },
  { id: "figures", label: "Figures", key: "i" },
  { id: "paper", label: "Paper", key: "p" },
  { id: "draft", label: "Draft", key: "d" },
];

interface Panes {
  rail: number;
  insp: number;
  railOpen: boolean;
  inspOpen: boolean;
}
const PANES: Panes = { rail: 320, insp: 400, railOpen: true, inspOpen: true };

type KindFilter = "all" | "integrity" | "quality";
type ReviewFilter = "all" | RulingId;
type Sort = "severity" | "figure" | "confidence";

export default function RunView({ id, route, go, onSettings, onShortcuts }: {
  id: string;
  route: Route;
  go: (r: Route, replace?: boolean) => void;
  onSettings: () => void;
  onShortcuts: () => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [err, setErr] = useState("");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [view, setView] = useStored("pi.view", "original");
  const [panes, setPanes] = useStored<Panes>("pi.panes", PANES);
  const [first, setFirst] = useState<string | null>(null);       // first manually picked panel
  const [figOverride, setFigOverride] = useState<string | null>(null);
  const [aiJob, setAiJob] = useState<Job | null>(null);
  const [aiErr, setAiErr] = useState("");
  const [saving, setSaving] = useState(false);
  const [letters, setLetters] = useState(false);
  const [narrowPane, setNarrowPane] = useState<"findings" | "stage" | "details">("findings");
  const [cmpRes, setCmpRes] = useState<Compare | null>(null);
  const [cmpErr, setCmpErr] = useState("");
  const [cmpTry, setCmpTry] = useState(0);
  const [drawer, setDrawer] = useState(false);
  // filters
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [cat, setCat] = useState("all");
  const [rev, setRev] = useState<ReviewFilter>("all");
  const [showInfo, setShowInfo] = useStored("pi.showNotes", false);
  const [hideAi, setHideAi] = useState(false);
  const [sort, setSort] = useState<Sort>("severity");
  const search = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const inspRef = useRef<InspectorHandle>(null);
  const gPending = useRef(0);
  const toast = useToast();
  const dq = useDebounced(q, 120);

  const tab = (TABS.find((t) => t.id === route.tab)?.id ?? "findings") as Tab;

  const load = useCallback(() => {
    setErr("");
    return api.run(id).then(setReport, (e) => setErr(String((e as Error).message || e)));
  }, [id]);

  useEffect(() => {
    setReport(null);
    load();
  }, [load]);
  useEffect(() => {
    loadCatalog().then(setCatalog, () => undefined);
  }, []);
  useEffect(() => {
    document.title = report ? `${report.paper.title || report.paper.source} · Paper Inspector` : "Paper Inspector";
    return () => { document.title = "Paper Inspector"; };
  }, [report]);

  // Default selection: the first integrity finding that is not a note.
  useEffect(() => {
    if (!report || route.finding || route.figure || route.cmp) return;
    const f = report.findings.find((x) => x.kind === "integrity" && x.severity !== "info") ?? report.findings.find((x) => x.severity !== "info");
    if (f) go({ ...route, finding: f.id }, true);
    else if (report.figures[0]) go({ ...route, figure: report.figures[0].id }, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report?.id]);

  usePoll(async () => {
    if (!aiJob) return;
    const j = await api.job(aiJob.id);
    setAiJob(j);
    if (j.status === "done") {
      toast({ text: "The AI investigator has finished." });
      load();
    } else if (j.status === "error") setAiErr(j.error ?? "unknown error");
  }, 1000, aiJob?.status === "running", (e) => setAiErr(`lost contact with the engine (${e.message})`));

  const finding = report?.findings.find((f) => f.id === route.finding) ?? null;
  const pair = pairOf(finding);
  const manual = route.cmp ?? null;
  const cmpPair: [string, string] | null = manual ?? (pair && figOverride !== finding?.id ? pair : null);
  const stageMode: "figure" | "compare" = cmpPair ? "compare" : "figure";

  // Which figure is on the stage.
  const figId = useMemo(() => {
    if (!report) return null;
    if (route.figure && report.figures.some((f) => f.id === route.figure)) return route.figure;
    if (finding) {
      const fig = finding.locations.find((l) => l.figure)?.figure;
      return fig && report.figures.some((x) => x.id === fig) ? fig : null;
    }
    return report.figures[0]?.id ?? null;
  }, [report, route.figure, finding]);
  const figure = report?.figures.find((f) => f.id === figId) ?? null;

  // Manual comparison: ask the engine to align the two panels.
  useEffect(() => {
    if (!manual) return;
    setCmpRes(null);
    setCmpErr("");
    api.compare(id, manual[0], manual[1]).then(setCmpRes, (e) => setCmpErr((e as Error).message));
  }, [id, manual?.[0], manual?.[1], cmpTry]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the selected finding visible in the list (deep links, palette, j/k).
  useEffect(() => {
    if (!finding) return;
    const t = window.setTimeout(() => document.getElementById(`fi-${finding.id}`)?.scrollIntoView({ block: "nearest" }), 50);
    return () => window.clearTimeout(t);
  }, [finding?.id, tab]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ---------------------------------------------------------------- filtered list */

  const base = useMemo(() => (report?.findings ?? []).filter((f) => showInfo || f.severity !== "info"), [report, showInfo]);
  const match = useCallback((f: Finding, skip?: "cat") => {
    if (kind !== "all" && f.kind !== kind) return false;
    if (skip !== "cat" && cat !== "all" && f.category !== cat) return false;
    if (rev !== "all" && (f.ruling?.ruling ?? "unreviewed") !== rev) return false;
    if (hideAi && f.ai?.verdict === "false_positive") return false;
    if (dq.trim()) {
      const hay = `${f.title} ${checkLabel(f.check, catalog)} ${f.check} ${f.detail} ${whereOf(f, report?.figures ?? [])} ${f.ruling?.note ?? ""}`.toLowerCase();
      if (!dq.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w))) return false;
    }
    return true;
  }, [kind, cat, rev, hideAi, dq, catalog, report]);

  const shown = useMemo(() => {
    const list = base.filter((f) => match(f));
    const figOrder = (f: Finding) => {
      const fig = f.locations.find((l) => l.figure)?.figure;
      const i = report?.figures.findIndex((x) => x.id === fig) ?? -1;
      return i < 0 ? 999 : i;
    };
    const rank = (f: Finding) => (f.kind === "integrity" ? 0 : 1) * 10 + SEVERITY_ORDER.indexOf(f.severity) + (f.ai?.verdict === "false_positive" ? 0.5 : 0);
    return [...list].sort((a, b) =>
      sort === "confidence" ? b.confidence - a.confidence :
        sort === "figure" ? figOrder(a) - figOrder(b) || rank(a) - rank(b) : rank(a) - rank(b));
  }, [base, match, sort, report]);

  const catCounts = useMemo(() => base.filter((f) => match(f, "cat"))
    .reduce<Record<string, number>>((m, f) => ({ ...m, [f.category]: (m[f.category] ?? 0) + 1 }), {}), [base, match]);
  const filtering = Boolean(dq.trim()) || kind !== "all" || cat !== "all" || rev !== "all" || hideAi;
  const clearFilters = () => { setQ(""); setKind("all"); setCat("all"); setRev("all"); setHideAi(false); };

  const worst = useMemo(() => worstByFigure(report?.findings ?? []), [report]);

  /* ---------------------------------------------------------------- actions */

  const select = useCallback((f: Finding) => {
    setFirst(null);
    go({ page: "run", run: id, finding: f.id, tab: route.tab }, true);
    setNarrowPane((p) => (p === "findings" ? "findings" : p));
    setDrawer(true);
  }, [go, id, route.tab]);

  const step = (d: 1 | -1) => {
    if (!shown.length) return;
    const i = shown.findIndex((f) => f.id === finding?.id);
    const n = i < 0 ? (d > 0 ? 0 : shown.length - 1) : Math.max(0, Math.min(shown.length - 1, i + d));
    select(shown[n]);
    window.setTimeout(() => document.getElementById(`fi-${shown[n].id}`)?.scrollIntoView({ block: "nearest" }), 0);
  };

  const setTab = (t: Tab) => go({ ...route, page: "run", run: id, tab: t });

  const openFigure = (fid: string) => {
    setFirst(null);
    const keep = finding && finding.locations.some((l) => l.figure === fid);
    go({ page: "run", run: id, tab: route.tab, ...(keep ? { finding: finding!.id, figure: fid } : { figure: fid }) }, true);
    if (keep) setFigOverride(finding!.id);
  };

  const onPanel = (pid: string) => {
    if (!first || first === pid) {
      setFirst(first === pid ? null : pid);
      return;
    }
    go({ page: "run", run: id, cmp: [first, pid], tab: route.tab });
    setFirst(null);
  };

  const leaveCompare = () => {
    if (manual) {
      if (window.history.length > 1 && route.cmp) window.history.back();
      else go({ page: "run", run: id, figure: figId ?? undefined, tab: route.tab }, true);
    } else if (finding) setFigOverride(finding.id);
  };

  const toggleCompare = () => {
    if (stageMode === "compare") leaveCompare();
    else if (pair && finding) setFigOverride(null);
  };

  const applyReview = (fid: string, ruling: Finding["ruling"], review: Review) => {
    setReport((r) => r && ({ ...r, review, findings: r.findings.map((f) => (f.id === fid ? { ...f, ruling } : f)) }));
  };

  const rule = async (f: Finding, r: RulingId, note?: string) => {
    const prev = f.ruling;
    // optimistic: the list updates at once
    setReport((rep) => rep && ({ ...rep, findings: rep.findings.map((x) => (x.id === f.id ? { ...x, ruling: { ruling: r, note: note ?? prev?.note ?? "", updated: new Date().toISOString() } } : x)) }));
    setSaving(true);
    try {
      const res = r === "unreviewed" && !note && !prev?.note ? await api.clearRuling(id, f.id) : await api.setRuling(id, f.id, r, note);
      applyReview(f.id, res.ruling, res.review);
      if (note === undefined) toast({ text: `${RULING_LABEL[r]}: ${f.title.slice(0, 60)}`, action: prev && prev.ruling !== r ? { label: "Undo", run: () => rule({ ...f, ruling: res.ruling }, prev.ruling) } : undefined });
      else toast({ text: "Note saved" });
    } catch (e) {
      setReport((rep) => rep && ({ ...rep, findings: rep.findings.map((x) => (x.id === f.id ? { ...x, ruling: prev } : x)) }));
      toast({ text: `Not saved: ${(e as Error).message}`, tone: "error" });
    } finally {
      setSaving(false);
    }
  };

  const link = () => `${window.location.origin}${window.location.pathname}${window.location.hash}`;
  const copyLink = async () => {
    const ok = await copyText(link());
    toast(ok ? { text: "Link copied" } : { text: "Copying was blocked by the browser.", tone: "error" });
  };

  const copySummary = async () => {
    if (!report) return;
    const flagged = report.findings.filter((f) => f.ruling?.ruling === "confirmed" || f.ruling?.ruling === "needs_raw_data");
    const list = (flagged.length ? flagged : report.findings.filter((f) => f.kind === "integrity" && ["critical", "high", "medium"].includes(f.severity)));
    const md = [
      `**${report.paper.title || report.paper.source}**`,
      report.paper.doi ? `DOI: ${report.paper.doi}` : "",
      `Integrity score ${report.score_ai ?? report.score} of 100. ${report.verdict}`,
      "",
      ...list.map((f) => `- ${SEV_LABEL[f.severity]}, ${checkLabel(f.check, catalog)}: ${f.title}${f.ruling && f.ruling.ruling !== "unreviewed" ? ` (${RULING_LABEL[f.ruling.ruling]})` : ""}`),
    ].filter((x, i) => x || i > 2).join("\n");
    const ok = await copyText(md);
    toast(ok ? { text: "Summary copied" } : { text: "Copying was blocked by the browser.", tone: "error" });
  };

  const investigate = async () => {
    setAiErr("");
    try {
      setAiJob(await api.investigate(id));
    } catch (e) {
      setAiErr((e as Error).message);
    }
  };

  const setPane = (patch: Partial<Panes>) => setPanes((p) => ({ ...p, ...patch }));

  /* ---------------------------------------------------------------- keyboard */

  useKeydown((e) => {
    if (!report) return;
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.shiftKey && (e.key === "c" || e.key === "C")) { e.preventDefault(); copyLink(); return; }
    if (mod || e.altKey) return;
    if (Date.now() - gPending.current < 1000) {
      gPending.current = 0;
      const t = TABS.find((x) => x.key === e.key);
      if (t) { e.preventDefault(); setTab(t.id); return; }
      if (e.key === "h") { e.preventDefault(); go({ page: "home" }); return; }
      if (e.key === "q") { e.preventDefault(); go({ page: "queue" }); return; }
    }
    switch (e.key) {
      case "g": gPending.current = Date.now(); return;
      case "j": case "ArrowDown":
        if (e.key === "ArrowDown" && !listRef.current?.contains(document.activeElement)) return;
        e.preventDefault(); step(1); return;
      case "k": case "ArrowUp":
        if (e.key === "ArrowUp" && !listRef.current?.contains(document.activeElement)) return;
        e.preventDefault(); step(-1); return;
      case "/": e.preventDefault(); setTab("findings"); window.setTimeout(() => search.current?.focus(), 0); return;
      case "[": e.preventDefault(); setPane({ railOpen: !panes.railOpen }); return;
      case "]": e.preventDefault(); setPane({ inspOpen: !panes.inspOpen }); setDrawer((d) => !d); return;
      case "v": e.preventDefault(); toggleCompare(); return;
      case "Enter":
        if (pair && stageMode === "figure" && !(document.activeElement instanceof HTMLButtonElement) && !(document.activeElement as Element)?.closest?.("[role=button]")) {
          e.preventDefault(); setFigOverride(null);
        }
        return;
      case "Escape":
        if (first) { e.preventDefault(); setFirst(null); return; }
        if (manual) { e.preventDefault(); leaveCompare(); }
        return;
    }
    if (finding) {
      const map: Record<string, RulingId> = { c: "confirmed", f: "false_positive", r: "needs_raw_data", u: "unreviewed" };
      if (map[e.key]) { e.preventDefault(); rule(finding, map[e.key]); return; }
      if (e.key === "n") { e.preventDefault(); setPane({ inspOpen: true }); setDrawer(true); setNarrowPane("details"); window.setTimeout(() => inspRef.current?.focusNote(), 0); }
    }
  }, Boolean(report));

  /* ---------------------------------------------------------------- palette */

  const commands = useMemo<Command[]>(() => {
    if (!report) return [];
    const c: Command[] = [];
    report.findings.forEach((f) => c.push({
      id: `f-${f.id}`, group: "Findings", label: f.title, hint: `${SEV_GLYPH[f.severity]} ${checkLabel(f.check, catalog)}${f.ruling && f.ruling.ruling !== "unreviewed" ? `, ${RULING_LABEL[f.ruling.ruling]}` : ""}`,
      run: () => { setTab("findings"); select(f); },
    }));
    report.figures.forEach((f) => c.push({ id: `g-${f.id}`, group: "Figures", label: f.label, hint: `page ${f.page}, ${f.panels.length} panels`, run: () => openFigure(f.id) }));
    report.figures.forEach((f) => f.panels.forEach((p) => c.push({
      id: `p-${p.id}`, group: "Panels", label: `${panelLabel(p.id, report.figures)}`, hint: first ? "compare with the picked panel" : "pick for comparison",
      run: () => { openFigure(f.id); onPanel(p.id); },
    })));
    c.push(
      { id: "a-ai", group: "Actions", label: report.ai_review ? "Ask the AI investigator again" : "Ask the AI investigator", run: investigate },
      { id: "a-csv", group: "Actions", label: "Download the findings table (CSV)", run: () => { window.location.href = url.csv(id); } },
      { id: "a-pdf", group: "Actions", label: "Download the report (PDF)", run: () => { window.location.href = url.pdf(id); } },
      { id: "a-zip", group: "Actions", label: "Download the evidence pack", run: () => { window.location.href = url.evidencePack(id); } },
      { id: "a-letter", group: "Actions", label: "Draft a letter", run: () => setLetters(true) },
      { id: "a-draft", group: "Actions", label: "Open the draft comment", keys: "g d", run: () => setTab("draft") },
      { id: "a-link", group: "Actions", label: "Copy link to this view", run: copyLink },
      { id: "a-sum", group: "Actions", label: "Copy a summary", run: copySummary },
      { id: "a-unrev", group: "Actions", label: "Show only findings not reviewed yet", run: () => { setTab("findings"); setRev("unreviewed"); } },
    );
    return c;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report, catalog, first]);
  useCommands("run", commands);

  /* ---------------------------------------------------------------- render */

  if (err) {
    return (
      <div className="page-msg">
        <h1>This scan could not be opened</h1>
        <p>{err}</p>
        <div className="row"><button className="btn" onClick={load}>Retry</button><button className="btn" onClick={() => go({ page: "home" })}>All scans</button></div>
      </div>
    );
  }
  if (!report) return <div className="page-msg" aria-live="polite"><Spinner size={20} /><p>Opening the scan…</p></div>;

  const score = report.score_ai ?? report.score;
  const review = report.review;
  const pos = finding ? `${Math.max(1, shown.findIndex((f) => f.id === finding.id) + 1)} of ${shown.length}` : "";
  const meta = report.meta ?? {};
  const year = meta.issued?.slice(0, 4);
  const firstAuthor = meta.authors?.length ? `${meta.authors[0].family}${meta.authors.length > 1 ? " et al." : ""}` : "";
  const supp = report.sources && report.sources.length > 1 ? report.sources.length - 1 : 0;
  const chips: (string | JSX.Element)[] = [
    report.case ? `${report.case.length} papers compared` : "",
    meta.journal ?? "", year ?? "", firstAuthor,
    report.paper.doi ? <a key="doi" href={`https://doi.org/${report.paper.doi}`} target="_blank" rel="noreferrer">doi:{report.paper.doi}</a> : "Uploaded file",
    `${report.figures.length} figure${report.figures.length === 1 ? "" : "s"}`,
    `${report.panels} panels`,
    report.tables ? `${report.tables} tables` : "",
    supp ? `${supp} supplementary file${supp === 1 ? "" : "s"}` : "",
    Number.isFinite(report.timings?.total) ? `${Math.round(report.timings.total)} s` : "",
  ].filter(Boolean);

  const modeSwitch = (
    <Segmented label="Stage" className="dark" value={stageMode}
      onChange={(m) => (m === "compare" ? (pair ? setFigOverride(null) : undefined) : leaveCompare())}
      options={[
        { id: "figure", label: "Figure", title: "The whole figure (v)" },
        { id: "compare", label: "Compare", title: pair || manual ? "The blink comparator (v)" : "Pick two panels, or select a reused-image finding", disabled: !pair && !manual },
      ]} />
  );

  const cols = [
    panes.railOpen ? `${panes.rail}px` : "40px",
    "6px",
    "minmax(0, 1fr)",
    "6px",
    panes.inspOpen ? `${panes.insp}px` : "40px",
  ].join(" ");

  return (
    <div className={`run pane-${narrowPane}${drawer ? " drawer-open" : ""}`}>
      <a className="skip" href="#findings-list" onClick={(e) => { e.preventDefault(); setTab("findings"); listRef.current?.focus(); }}>Skip to findings</a>
      <header className="run-head">
        <button className="brand" onClick={() => go({ page: "home" })} aria-label="All scans" title="All scans (g h)">
          <img src="/icon.svg" alt="" width={30} height={30} />
        </button>
        <div className="run-title">
          <h1 title={report.paper.title || report.paper.source}>{report.paper.title || report.paper.source}</h1>
          <p className="chips-line">
            {meta.is_retracted && <span className="badge-retracted">Retracted</span>}
            {chips.map((c, i) => <span key={i} className="chip-part">{c}</span>)}
          </p>
        </div>
        {review && review.total > 0 && (
          <button className="review-meter" onClick={() => { setTab("findings"); setRev("unreviewed"); }} title="Show the findings you have not reviewed">
            <span>Reviewed {review.reviewed} of {review.total}</span>
            <Meter value={review.reviewed} max={review.total} label={`${review.reviewed} of ${review.total} findings reviewed`} />
          </button>
        )}
        <ScoreButton report={report} score={score} />
        <div className="run-actions">
          <button className="btn primary ai-btn" disabled={aiJob?.status === "running"} onClick={investigate} title={aiJob?.status === "running" ? aiJob.stage : undefined}>
            {aiJob?.status === "running" ? <><Spinner /> Investigating…</> : <><Icon name="sparkle" /> {report.ai_review ? "Ask the AI again" : "Ask the AI investigator"}</>}
          </button>
          <ExportMenu report={report} onLetters={() => setLetters(true)} onCopySummary={copySummary} onCopyLink={copyLink} />
          <Menu label="More" buttonClass="icon-btn" button={<Icon name="more" />} items={[
            { label: "Batch queue", icon: "queue", run: () => go({ page: "queue" }) },
            { label: "Settings", icon: "gear", hint: "Cmd/Ctrl ,", run: onSettings },
            { label: "Keyboard shortcuts", icon: "keyboard", hint: "?", run: onShortcuts },
          ]} />
        </div>
        {aiJob?.status === "running" && <div className="head-progress" role="progressbar" aria-label={aiJob.stage} aria-valuetext={aiJob.stage} />}
        <span className="sr" aria-live="polite">{aiJob?.status === "running" ? aiJob.stage : ""}</span>
      </header>
      {aiErr && (
        <div className="banner error" role="alert">
          <span>The AI investigator stopped: {aiErr}</span>
          <button className="btn s" onClick={onSettings}>Open settings</button>
          <button className="btn s" onClick={investigate}>Retry</button>
          <button className="icon-btn s" aria-label="Dismiss" onClick={() => setAiErr("")}><Icon name="close" /></button>
        </div>
      )}

      <div className="run-body" style={{ gridTemplateColumns: cols, ["--rail" as string]: panes.railOpen ? `${panes.rail}px` : "40px" }}>
        <nav className={`rail${panes.railOpen ? "" : " collapsed"}`} aria-label="Findings and paper">
          {panes.railOpen ? (
            <>
              <div className="tabs" role="tablist" aria-label="Rail">
                {TABS.map((t) => (
                  <button key={t.id} role="tab" id={`tab-${t.id}`} aria-selected={tab === t.id} aria-controls={`panel-${t.id}`} tabIndex={tab === t.id ? 0 : -1}
                    className={tab === t.id ? "on" : ""} onClick={() => setTab(t.id)} title={`${t.label} (g ${t.key})`}
                    onKeyDown={(e) => {
                      const i = TABS.indexOf(t);
                      const n = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i - 1 + TABS.length) % TABS.length : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
                      if (n >= 0) { e.preventDefault(); setTab(TABS[n].id); window.setTimeout(() => document.getElementById(`tab-${TABS[n].id}`)?.focus(), 0); }
                    }}>
                    {t.label}
                    {t.id === "findings" && <span className="count">{report.findings.filter((f) => f.severity !== "info").length}</span>}
                    {t.id === "figures" && <span className="count">{report.figures.length}</span>}
                  </button>
                ))}
                <button className="icon-btn s collapse" aria-label="Hide this pane" title="Hide ([)" onClick={() => setPane({ railOpen: false })}><Icon name="chevL" /></button>
              </div>
              <div className="tabpanel" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
                {tab === "findings" && (
                  <FindingsTab report={report} catalog={catalog} shown={shown} sel={finding?.id ?? null} onSelect={select}
                    q={q} setQ={setQ} kind={kind} setKind={setKind} cat={cat} setCat={setCat} rev={rev} setRev={setRev}
                    showInfo={showInfo} setShowInfo={setShowInfo} hideAi={hideAi} setHideAi={setHideAi} sort={sort} setSort={setSort}
                    catCounts={catCounts} filtering={filtering} clearFilters={clearFilters} search={search} listRef={listRef}
                    onBrowse={() => setTab("figures")} onAi={investigate} />
                )}
                {tab === "figures" && <FiguresTab run={id} figures={report.figures} worst={worst} current={figId} onOpen={(f) => { openFigure(f); setNarrowPane("stage"); }} />}
                {tab === "paper" && <PaperTab report={report} onTags={(tags) => setReport((r) => r && ({ ...r, tags }))} onFinding={(fid) => { const f = report.findings.find((x) => x.id === fid); if (f) { setTab("findings"); select(f); } }} />}
                {tab === "draft" && <DraftTab report={report} />}
              </div>
            </>
          ) : (
            <button className="icon-btn collapsed-open" aria-label="Show the findings pane" title="Show ([)" onClick={() => setPane({ railOpen: true })}><Icon name="chevR" /></button>
          )}
        </nav>
        <Resizer label="Resize the findings pane" side="left" value={panes.rail} min={260} max={480} onChange={(v) => setPane({ rail: v, railOpen: true })} onReset={() => setPane({ rail: PANES.rail })} />

        <main className="stage" aria-label="Image stage">
          {cmpPair ? (
            <CompareStage key={`${cmpPair[0]}|${cmpPair[1]}`} active={stageMode === "compare"}
              a={url.frame(id, "a", cmpPair[0], cmpPair[1])} b={url.frame(id, "b", cmpPair[0], cmpPair[1])} diff={url.frame(id, "diff", cmpPair[0], cmpPair[1])}
              labelA={panelLabel(cmpPair[0], report.figures)}
              labelB={`${panelLabel(cmpPair[1], report.figures)}${manual && cmpRes?.aligned === false ? "" : ", aligned"}`}
              aligned={manual ? (cmpRes ? cmpRes.aligned : null) : true}
              onBack={leaveCompare}
              measures={<CompareMeasures d={manual ? (cmpRes as unknown as Record<string, unknown>) : finding?.data} />}
              toolbarStart={modeSwitch} />
          ) : figure ? (
            <FigureStage run={id} fig={figure} figures={report.figures} finding={finding} pick={first ? [first] : []} onPanel={onPanel}
              onFigure={openFigure} view={view} setView={setView} worst={worst} active={stageMode === "figure"} modeSwitch={modeSwitch} />
          ) : (
            <NonFigureCard run={id} f={finding} onBrowse={() => { setTab("figures"); if (report.figures[0]) openFigure(report.figures[0].id); }} />
          )}
        </main>
        <Resizer label="Resize the details pane" side="right" value={panes.insp} min={320} max={600} onChange={(v) => setPane({ insp: v, inspOpen: true })} onReset={() => setPane({ insp: PANES.insp })} />

        <aside className={`inspector${panes.inspOpen ? "" : " collapsed"}`} aria-label="Finding details">
          {panes.inspOpen ? (
            <>
              <div className="insp-bar">
                <button className="icon-btn s" aria-label="Hide the details pane" title="Hide (])" onClick={() => { if (window.matchMedia("(max-width: 1100px)").matches) setDrawer(false); else setPane({ inspOpen: false }); }}><Icon name="chevR" /></button>
              </div>
              {manual ? (
                <ManualSummary a={manual[0]} b={manual[1]} figures={report.figures} res={cmpRes} err={cmpErr}
                  onClear={leaveCompare} onRetry={() => setCmpTry((n) => n + 1)} />
              ) : finding ? (
                <Inspector ref={inspRef} run={id} f={finding} figures={report.figures} catalog={catalog} saving={saving}
                  onRuling={(r, note) => rule(finding, r, note)} onFigure={openFigure} onCompare={() => { setFigOverride(null); setNarrowPane("stage"); }}
                  onPrev={() => step(-1)} onNext={() => step(1)} onCopyLink={copyLink} position={pos} />
              ) : (
                <div className="insp-empty">
                  <p>Pick a finding, or click two panels to compare them.</p>
                  <ul className="cheats">
                    <li><kbd className="kbd">j</kbd> <kbd className="kbd">k</kbd> next and previous finding</li>
                    <li><kbd className="kbd">c</kbd> <kbd className="kbd">f</kbd> <kbd className="kbd">r</kbd> confirm, false positive, needs raw data</li>
                    <li><kbd className="kbd">?</kbd> all shortcuts</li>
                  </ul>
                </div>
              )}
            </>
          ) : (
            <button className="icon-btn collapsed-open" aria-label="Show the details pane" title="Show (])" onClick={() => setPane({ inspOpen: true })}><Icon name="chevL" /></button>
          )}
        </aside>
      </div>

      <nav className="narrow-tabs" aria-label="Panes">
        {([["findings", "Findings"], ["stage", "Image"], ["details", "Details"]] as const).map(([p, l]) => (
          <button key={p} className={narrowPane === p ? "on" : ""} aria-pressed={narrowPane === p} onClick={() => setNarrowPane(p)}>{l}</button>
        ))}
      </nav>

      {letters && <LettersDialog run={id} onClose={() => setLetters(false)} />}
    </div>
  );
}

function CompareMeasures({ d }: { d?: Record<string, unknown> | null }) {
  if (!d) return null;
  const tex = Number(d.ncc_texture);
  const parts = [
    Number.isFinite(tex) ? <span key="t" title="Texture match: copies usually score above 0.5, unrelated images near 0.">Texture match {tex.toFixed(2)}</span> : null,
    Number.isFinite(Number(d.inliers)) ? <span key="k" title="Distinct points that agree on one geometric transform.">{Number(d.inliers)} keypoints</span> : null,
    d.mirrored ? <span key="m">mirrored</span> : null,
    Number.isFinite(Number(d.scale)) && Math.abs(Number(d.scale) - 1) > 0.05 ? <span key="s">×{Number(d.scale).toFixed(2)}</span> : null,
  ].filter(Boolean);
  return <>{parts}</>;
}

function ScoreButton({ report, score }: { report: Report; score: number }) {
  const tone = scoreTone(score);
  const r = 16;
  const c = 2 * Math.PI * r;
  const label = `Integrity score ${score} of 100. ${scoreWord(score)}.`;
  return (
    <Popover label={label} buttonClass={`score ${tone}`} align="end" button={
      <>
        <svg viewBox="0 0 40 40" aria-hidden="true">
          <circle cx="20" cy="20" r={r} className="track" />
          <circle cx="20" cy="20" r={r} className="arc" strokeDasharray={`${(c * score) / 100} ${c}`} />
          <text x="20" y="21" textAnchor="middle" dominantBaseline="middle" transform="rotate(90 20 20)">{score}</text>
        </svg>
        <span className="score-words"><b>{scoreWord(score)}</b><small>integrity score</small></span>
      </>
    }>
      {() => (
        <div className="score-pop">
          <h3>Integrity score {score} of 100</h3>
          <p>Set by integrity signals only. Quality notes are not counted.</p>
          <table>
            <tbody>
              {(["critical", "high", "medium", "low"] as const).map((s) => (
                <tr key={s}><th scope="row"><span className={`sev-text ${s}`}>{SEV_GLYPH[s]} {SEV_LABEL[s]}</span></th><td>{report.counts[s] ?? 0}</td></tr>
              ))}
            </tbody>
          </table>
          {report.score_ai !== undefined && report.score_ai !== null && <p className="muted">Before the AI review: {report.score}</p>}
          {report.review && report.review.reviewed > 0 && report.review.score_reviewed !== score && (
            <p className="muted">With your rulings applied: {report.review.score_reviewed}</p>
          )}
          <p>{report.verdict}</p>
          {report.coverage && report.coverage !== "full" && <p className="muted">Coverage: {report.coverage}. Some files could not be checked.</p>}
        </div>
      )}
    </Popover>
  );
}

/* ---------------------------------------------------------------- findings tab */

function FindingsTab(p: {
  report: Report; catalog: Catalog | null; shown: Finding[]; sel: string | null; onSelect: (f: Finding) => void;
  q: string; setQ: (s: string) => void; kind: KindFilter; setKind: (k: KindFilter) => void; cat: string; setCat: (c: string) => void;
  rev: ReviewFilter; setRev: (r: ReviewFilter) => void; showInfo: boolean; setShowInfo: (b: boolean) => void; hideAi: boolean; setHideAi: (b: boolean) => void;
  sort: Sort; setSort: (s: Sort) => void; catCounts: Record<string, number>; filtering: boolean; clearFilters: () => void;
  search: React.RefObject<HTMLInputElement>; listRef: React.RefObject<HTMLDivElement>; onBrowse: () => void; onAi: () => void;
}) {
  const { report, shown, sel, catalog } = p;
  const [aiOpen, setAiOpen] = useStored("pi.aiCard", true);
  const total = report.findings.filter((f) => p.showInfo || f.severity !== "info").length;
  const notes = report.counts.info ?? 0;
  const groups = (["integrity", "quality"] as const).map((k) => ({ k, items: shown.filter((f) => f.kind === k) }));
  return (
    <div className="rail-list">
      {report.ai_review && (
        <div className="ai-card">
          <button className="ai-card-head" aria-expanded={aiOpen} onClick={() => setAiOpen(!aiOpen)}>
            <Icon name="sparkle" /> <b>AI investigator</b>
            <small>{report.ai_review.model}, {report.ai_review.usage?.turns ?? "–"} steps, {Math.round(report.ai_review.seconds ?? 0)} s</small>
            <Icon name={aiOpen ? "chevD" : "chevR"} size={14} />
          </button>
          {aiOpen && <p>{report.ai_review.summary}</p>}
        </div>
      )}
      <div className="filters">
        <div className="search-box">
          <Icon name="search" />
          <label className="sr" htmlFor="find-q">Search findings</label>
          <input id="find-q" ref={p.search} value={p.q} placeholder="Search findings" onChange={(e) => p.setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") { p.setQ(""); (e.target as HTMLInputElement).blur(); } }} />
          <kbd className="kbd">/</kbd>
        </div>
        <div className="filter-row">
          <Segmented label="Kind" value={p.kind} onChange={p.setKind} options={[
            { id: "all", label: "All" }, { id: "integrity", label: "Integrity" }, { id: "quality", label: "Quality" },
          ]} />
          <label className="sr" htmlFor="rev-f">Review</label>
          <select id="rev-f" className="select s" value={p.rev} onChange={(e) => p.setRev(e.target.value as ReviewFilter)}>
            <option value="all">Any review</option>
            {(Object.keys(RULING_LABEL) as RulingId[]).map((r) => <option key={r} value={r}>{RULING_LABEL[r]}</option>)}
          </select>
          <Menu label="More filters" buttonClass="icon-btn s" button={<Icon name="more" />} items={[
            { label: `${p.showInfo ? "Hide" : "Show"} notes (${notes})`, run: () => p.setShowInfo(!p.showInfo), disabled: !notes },
            { label: p.hideAi ? "Show AI-dismissed findings" : "Hide AI-dismissed findings", run: () => p.setHideAi(!p.hideAi) },
            { separator: true, label: "" },
            { label: `${p.sort === "severity" ? "✓ " : ""}Sort by severity`, run: () => p.setSort("severity") },
            { label: `${p.sort === "figure" ? "✓ " : ""}Sort by figure order`, run: () => p.setSort("figure") },
            { label: `${p.sort === "confidence" ? "✓ " : ""}Sort by confidence`, run: () => p.setSort("confidence") },
          ]} />
        </div>
        <div className="chips" role="group" aria-label="Filter by category">
          <button className={`chip${p.cat === "all" ? " on" : ""}`} aria-pressed={p.cat === "all"} onClick={() => p.setCat("all")}>All</button>
          {Object.keys(CATEGORY_NAMES).filter((c) => p.catCounts[c] || p.cat === c).map((c) => (
            <button key={c} className={`chip${p.cat === c ? " on" : ""}`} aria-pressed={p.cat === c} onClick={() => p.setCat(p.cat === c ? "all" : c)}>
              {catalog?.categories[c] ?? CATEGORY_NAMES[c]} <span>{p.catCounts[c] ?? 0}</span>
            </button>
          ))}
        </div>
        {p.filtering && <p className="filter-note">Showing {shown.length} of {total}. <button className="link-btn" onClick={p.clearFilters}>Clear filters</button></p>}
      </div>

      {report.findings.length === 0 ? (
        <div className="empty-card">
          <p><b>The automatic checks found nothing to flag.</b></p>
          <p>That is not proof the paper is clean. Look through the figures yourself, or ask the AI investigator for a second opinion.</p>
          <div className="row"><button className="btn" onClick={p.onBrowse}>Browse figures</button><button className="btn" onClick={p.onAi}>Ask the AI investigator</button></div>
        </div>
      ) : shown.length === 0 ? (
        <div className="empty-card">
          <p>No findings match these filters.</p>
          {p.filtering && <button className="btn" onClick={p.clearFilters}>Clear filters</button>}
          {!p.filtering && !p.showInfo && notes > 0 && <button className="btn" onClick={() => p.setShowInfo(true)}>Show {notes} notes</button>}
        </div>
      ) : (
        <div className="find-list" role="listbox" id="findings-list" aria-label="Findings" tabIndex={0} ref={p.listRef}
          aria-activedescendant={sel ? `fi-${sel}` : undefined}>
          {groups.map(({ k, items }) => items.length ? (
            <div key={k} className="kind-block" role="group" aria-labelledby={`kh-${k}`}>
              <h2 className="kind-h" id={`kh-${k}`}>{k === "integrity" ? "Integrity signals" : "Quality notes"}
                <small>{k === "integrity" ? "Could indicate manipulation. These set the score." : "Problems and missing information. Not counted in the score."}</small>
              </h2>
              {(p.sort === "severity" ? SEVERITY_ORDER : [null]).map((s) => {
                const list = s ? items.filter((f) => f.severity === s) : items;
                if (!list.length) return null;
                return (
                  <div key={s ?? "all"} className="sev-group">
                    {s && <h3 className={`sev-h ${s}`}><span aria-hidden="true">{SEV_GLYPH[s]}</span> {SEV_LABEL[s]} <span className="n">{list.length}</span></h3>}
                    {list.map((f) => <FindingRow key={f.id} f={f} sel={f.id === sel} onSelect={p.onSelect} figures={report.figures} catalog={catalog} />)}
                  </div>
                );
              })}
            </div>
          ) : null)}
        </div>
      )}

      {report.ai_review?.observations?.length ? (
        <div className="ai-only">
          <h2 className="kind-h">Seen by the AI only<small>Not checked by a detector. Verify each one yourself.</small></h2>
          {report.ai_review.observations.map((o, i) => (
            <div key={i} className="item ai-obs">
              <span className="item-kind"><span className="ai-glyph">AI</span> {o.where}</span>
              <span className="item-title">{o.title}</span>
              <span className="item-detail">{o.detail}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function FindingRow({ f, sel, onSelect, figures, catalog }: { f: Finding; sel: boolean; onSelect: (f: Finding) => void; figures: FigureInfo[]; catalog: Catalog | null }) {
  const r = f.ruling?.ruling ?? "unreviewed";
  const where = whereOf(f, figures);
  return (
    <div id={`fi-${f.id}`} role="option" aria-selected={sel} className={`item ${f.severity}${sel ? " sel" : ""} r-${r}`} onClick={() => onSelect(f)}>
      <span className="item-kind">
        <span>{checkLabel(f.check, catalog)}</span>
        {where && <span className="item-where">{where}</span>}
        <span className="grow" />
        <RulingMark r={r} />
      </span>
      <span className="item-title">{f.title}</span>
      {(f.ai || f.ruling?.note) && (
        <span className="item-foot">
          {f.ai && <span className={`verdict ${f.ai.verdict}`}>{VERDICT[f.ai.verdict]}</span>}
          {f.ruling?.note && <span className="has-note" title={f.ruling.note}><Icon name="note" size={12} /> note</span>}
        </span>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- figures tab */

function FiguresTab({ run, figures, worst, current, onOpen }: {
  run: string; figures: FigureInfo[]; worst: Record<string, { s: string; n: number }>; current: string | null; onOpen: (id: string) => void;
}) {
  const [only, setOnly] = useState(false);
  const list = only ? figures.filter((f) => worst[f.id]) : figures;
  if (!figures.length) return <div className="rail-list"><p className="empty-card">No figures were found in this paper.</p></div>;
  return (
    <div className="rail-list">
      <label className="check fig-only"><input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} /> Only figures with findings</label>
      <div className="fig-grid">
        {list.map((f) => (
          <button key={f.id} className={`fig-item${f.id === current ? " sel" : ""}`} onClick={() => onOpen(f.id)} aria-current={f.id === current || undefined}>
            <span className="fig-thumb"><img src={url.figure(run, f.id)} alt="" loading="lazy" />
              {worst[f.id] && <i className={`thumb-badge ${worst[f.id].s}`}>{worst[f.id].n}</i>}</span>
            <b>{f.label}</b>
            <small>page {f.page}, {f.panels.length} panels{worst[f.id] ? `, ${worst[f.id].n} finding${worst[f.id].n > 1 ? "s" : ""}` : ""}</small>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- draft tab */

function DraftTab({ report }: { report: Report }) {
  const key = `pi.draft.${report.id}`;
  const [gen, setGen] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const [text, setText] = useStored<{ text: string; base: string } | null>(key, null);
  const [savedMsg, setSavedMsg] = useState("");
  const toast = useToast();
  const area = useRef<HTMLTextAreaElement>(null);

  const load = useCallback(() => {
    setErr("");
    api.pubpeer(report.id).then(setGen, (e) => setErr((e as Error).message));
  }, [report.id]);
  useEffect(load, [load, report.ai_review]);

  const value = text?.text ?? gen ?? "";
  const stale = text && gen !== null && text.base !== gen;
  const words = value.trim() ? value.trim().split(/\s+/).length : 0;

  const copy = async () => {
    if (await copyText(value)) toast({ text: "Draft copied" });
    else {
      area.current?.select();
      toast({ text: "Press Cmd or Ctrl and C to copy the selected text." });
    }
  };

  return (
    <div className="draft-tab">
      <h2 className="pane-title">Draft comment</h2>
      <p className="muted">A starting point. Check every item yourself, and phrase each point as a question. It lists the findings you confirmed or marked as needing raw data.</p>
      {stale && (
        <div className="banner info" role="status">
          <span>A new draft is available.</span>
          <button className="btn s" onClick={() => setText(null)}>Use the new draft</button>
          <button className="btn s" onClick={() => setText({ text: text!.text, base: gen! })}>Keep mine</button>
        </div>
      )}
      {err ? (
        <div className="inline-error"><p>The draft could not be loaded: {err}</p><button className="btn" onClick={load}>Retry</button></div>
      ) : gen === null ? (
        <div className="skeleton" style={{ height: 320 }} aria-label="Loading the draft" />
      ) : (
        <>
          <label className="sr" htmlFor="draft-text">Draft comment</label>
          <textarea id="draft-text" ref={area} value={value} rows={18} spellCheck
            onChange={(e) => { setText({ text: e.target.value, base: gen }); setSavedMsg("Saved in this browser"); }} />
          <div className="row">
            <span className="muted grow" aria-live="polite">{words} words{savedMsg && text ? `. ${savedMsg}` : ""}</span>
            {text && <button className="btn ghost" onClick={() => { setText(null); setSavedMsg(""); }}>Reset to generated</button>}
            <button className="btn primary" onClick={copy}>Copy draft</button>
          </div>
          {report.paper.doi && (
            <p><a href={`https://pubpeer.com/search?q=${encodeURIComponent(report.paper.doi)}`} target="_blank" rel="noreferrer">Open this paper on PubPeer <Icon name="ext" size={12} /></a></p>
          )}
        </>
      )}
    </div>
  );
}
