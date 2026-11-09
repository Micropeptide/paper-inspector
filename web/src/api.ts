export type Severity = "critical" | "high" | "medium" | "low" | "info";
export type RulingId = "unreviewed" | "confirmed" | "false_positive" | "needs_raw_data";

export interface Loc {
  figure?: string;
  panel?: string;
  page?: number;
  box?: number[];
  quote?: string;
  file?: string;
  sheet?: string;
  cells?: string;
}

export interface Ruling {
  ruling: RulingId;
  note: string;
  updated: string | null;
}

export interface Finding {
  id: string;
  check: string;
  severity: Severity;
  category: string;
  kind: "integrity" | "quality";
  title: string;
  detail: string;
  confidence: number;
  locations: Loc[];
  evidence: string[];
  data: Record<string, unknown>;
  ai?: { verdict: "confirmed" | "likely" | "unclear" | "false_positive"; reason: string };
  fingerprint?: string;
  ruling?: Ruling;
}

export interface PanelInfo {
  id: string;
  figure: string;
  box: number[];
  kind: string;
}

export interface FigureInfo {
  id: string;
  label: string;
  page: number;
  caption: string;
  width: number;
  height: number;
  panels: PanelInfo[];
}

export interface Review {
  paper_key: string;
  counts: Record<RulingId, number>;
  total: number;
  reviewed: number;
  score_reviewed: number;
  unmatched: number;
  labels: Record<string, string>;
}

export interface Report {
  id: string;
  version: string;
  created: string;
  paper: { source: string; title: string; doi: string; pages: number };
  figures: FigureInfo[];
  findings: Finding[];
  score: number;
  score_ai?: number;
  verdict: string;
  counts: Record<Severity, number>;
  panels: number;
  timings: Record<string, number>;
  case?: { index: number; source: string; title: string; doi: string }[];
  kinds?: { integrity: number; quality: number };
  categories?: Record<string, number>;
  tables?: number;
  coverage?: string;
  meta?: PaperMeta;
  references?: RefStats;
  timeline?: Record<string, string | number>;
  history?: {
    author_retractions?: [string, number][];
    accessions?: { cited: string[]; not_found: string[] };
    same_title_elsewhere?: { title: string; doi: string; year: number }[];
    references_deposited?: number;
    references_printed?: number;
  };
  sources?: SourceFile[];
  log?: string[];
  review?: Review;
  tags?: string[];
  ai_review?: {
    model: string;
    provider: string;
    summary: string;
    observations: { title: string; where: string; severity: string; detail: string }[];
    pubpeer: string;
    usage: { input: number; output: number; turns: number };
    seconds: number;
  };
}

export interface Author {
  given: string;
  family: string;
  orcid?: string;
  affiliations?: string[];
}

export interface PaperMeta {
  doi?: string;
  title?: string;
  journal?: string;
  publisher?: string;
  issued?: string;
  authors?: Author[];
  notices?: { type: string; doi: string; date: string }[];
  dates?: Record<string, string | null>;
  topics?: string[];
  cited_by_count?: number;
  is_retracted?: boolean;
  pmcid?: string;
  license?: string[];
  funders?: string[];
  abstract?: string;
}

export interface RefStats {
  count?: number;
  resolved?: number;
  dois?: number;
  bad_dois?: string[];
  unmatched?: number;
  retracted?: number;
  self_citations?: number;
  self_citation_rate?: number;
  checked?: number;
  journal_self_citations?: number;
  off_topic?: number;
  top_cited_authors?: { name: string; count: number }[];
}

export interface SourceFile {
  name: string;
  label: string;
  kind: string;
  status: string;
  origin: string;
  figures: number;
  tables: number;
  exif?: Record<string, string>;
}

export interface RunSummary {
  id: string;
  title: string;
  source: string;
  score: number;
  counts: Record<Severity, number>;
  created: string;
  case: boolean;
  tags?: string[];
  doi?: string;
  journal?: string;
  authors?: string[];
  score_ai?: number | null;
  verdict?: string;
  kinds?: { integrity: number; quality: number };
  coverage?: string;
  version?: string;
}

export interface TagCount {
  tag: string;
  count: number;
}

export interface SearchResult {
  runs: RunSummary[];
  total: number;
  tags: TagCount[];
}

export interface Job {
  id: string;
  kind: string;
  stage: string;
  progress: number;
  status: "running" | "done" | "error" | "cancelled";
  run: string | null;
  error: string | null;
  started?: number;
  finished?: number | null;
  began?: number;
}

export type QueueStatus = "queued" | "running" | "done" | "error" | "cancelled";

export interface QueueItem {
  id: string;
  batch: string;
  input: string;
  doi: string | null;
  online: boolean;
  status: QueueStatus;
  stage: string;
  progress: number;
  run: string | null;
  error: string | null;
  added: string;
  started: string | null;
  finished: string | null;
  title: string | null;
  score: number | null;
}

export interface Batch {
  id: string;
  name: string;
  created: string;
  count: number;
}

export interface QueueState {
  items: QueueItem[];
  counts: Record<QueueStatus, number>;
  running: string | null;
  batches: Batch[];
}

export interface QueueAdd {
  batch: Batch | null;
  added: QueueItem[];
  skipped: { input: string; reason: string }[];
}

export interface RulingResult {
  finding: string;
  fingerprint: string;
  ruling: Ruling;
  review: Review;
}

export interface CheckInfo {
  id: string;
  label: string;
  category: string;
  category_label: string;
  kind: "integrity" | "quality";
  scored: boolean;
  what: string;
  false_alarms: string;
  look_at: string;
  ask: string;
  known: boolean;
}

export interface Catalog {
  checks: CheckInfo[];
  categories: Record<string, string>;
  rulings: { id: RulingId; label: string }[];
}

export interface LetterTemplate {
  id: string;
  label: string;
  description: string;
}

export interface Letter {
  kind: string;
  label: string;
  subject: string;
  body: string;
  included: string[];
  empty: boolean;
}

/** What /api/resolve returns (see engine inputs.resolve). */
export interface Resolved {
  kind: string;
  input: string;
  value: string;
  doi: string | null;
  label?: string;
  message: string;
  error?: string;
  candidates: { doi: string; title: string; journal: string; year: string | number; first_author: string; score?: number }[];
}

export interface Compare {
  aligned: boolean;
  inliers?: number;
  ncc_texture?: number;
  mirrored?: boolean;
  rotation?: number;
  scale?: number;
  overlap_a?: number;
  overlap_b?: number;
}

export interface Preset {
  kind: string;
  label: string;
  model: string;
  base_url?: string;
  vision: boolean;
  env: string;
  no_key?: boolean;
  models?: string[];
}

export interface Settings {
  provider: string;
  model: string;
  base_url: string;
  vision: boolean;
  effort: string;
  online: boolean;
  email?: string;
  key_set: Record<string, boolean>;
  data_keys?: { openalex: boolean };
  presets: Record<string, Preset>;
}

/** The engine refuses POST and DELETE without this header: a custom header needs a CORS preflight, which other sites fail. */
const GUARD = { "X-Paper-Inspector": "1" };
const JSON_GUARD = { ...GUARD, "Content-Type": "application/json" };

export class ApiError extends Error {
  status: number;
  constructor(msg: string, status: number) {
    super(msg);
    this.status = status;
  }
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const b = await r.json();
      if (b.detail) msg = typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail);
    } catch {
      /* not json */
    }
    throw new ApiError(msg, r.status);
  }
  return r.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  fetch(path, { method: "POST", headers: body === undefined ? GUARD : JSON_GUARD, body: body === undefined ? undefined : JSON.stringify(body) }).then(j<T>);
const del = <T>(path: string) => fetch(path, { method: "DELETE", headers: GUARD }).then(j<T>);
const get = <T>(path: string) => fetch(path).then(j<T>);
const enc = encodeURIComponent;

export interface SearchQuery {
  q?: string;
  tags?: string[];
  min_score?: number;
  max_score?: number;
  sort?: "created" | "score" | "title";
  limit?: number;
}

export const api = {
  health: () => get<{ ok: boolean; version: string }>("/api/health"),
  runs: () => get<RunSummary[]>("/api/runs"),
  search: (s: SearchQuery) => {
    const p = new URLSearchParams();
    if (s.q) p.set("q", s.q);
    (s.tags ?? []).forEach((t) => p.append("tag", t));
    if (s.min_score !== undefined) p.set("min_score", String(s.min_score));
    if (s.max_score !== undefined) p.set("max_score", String(s.max_score));
    if (s.sort) p.set("sort", s.sort);
    p.set("limit", String(s.limit ?? 200));
    return get<SearchResult>(`/api/runs/search?${p}`);
  },
  tags: () => get<{ tags: TagCount[] }>("/api/tags"),
  editTags: (id: string, patch: { add?: string[]; remove?: string[]; set?: string[] }) => post<{ tags: string[] }>(`/api/runs/${id}/tags`, patch),
  run: (id: string) => get<Report>(`/api/runs/${id}`),
  remove: (id: string) => del<{ ok: boolean }>(`/api/runs/${id}`),
  jobs: () => get<Job[]>("/api/jobs"),
  job: (id: string) => get<Job>(`/api/jobs/${id}`),
  cancelJob: (id: string) => post<Job>(`/api/jobs/${id}/cancel`),
  scan: (file: File, supplements: File[] = [], online = false) => {
    const fd = new FormData();
    fd.append("file", file);
    supplements.forEach((s) => fd.append("supplements", s));
    return fetch(`/api/scan?online=${online}`, { method: "POST", headers: GUARD, body: fd }).then(j<Job>);
  },
  doi: (doi: string) => post<Job>("/api/doi", { doi }),
  /** Turn any pasted identifier into a DOI or a list of candidates.  Throws ApiError 404/405 if the engine has no such route. */
  resolve: (text: string) => post<Resolved>("/api/resolve", { text }),
  scanCase: (files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return fetch("/api/case", { method: "POST", headers: GUARD, body: fd }).then(j<Job>);
  },
  demo: () => post<Job>("/api/demo"),
  investigate: (id: string) => post<Job>(`/api/runs/${id}/investigate`),
  compare: (id: string, a: string, b: string) => get<Compare>(`/api/runs/${id}/compare?a=${enc(a)}&b=${enc(b)}`),
  pubpeer: (id: string) =>
    fetch(`/api/runs/${id}/pubpeer`).then((r) => {
      if (!r.ok) throw new ApiError(`${r.status} ${r.statusText}`, r.status);
      return r.text();
    }),
  settings: () => get<Settings>("/api/settings"),
  saveSettings: (patch: Partial<Settings> & { keys?: Record<string, string> }) => post<Settings>("/api/settings", patch),

  // rulings
  rulings: (id: string) => get<{ paper_key: string; rulings: Record<string, unknown>; by_finding: Record<string, string> }>(`/api/runs/${id}/rulings`),
  setRuling: (id: string, fid: string, ruling: RulingId, note?: string) =>
    post<RulingResult>(`/api/runs/${id}/findings/${enc(fid)}/ruling`, note === undefined ? { ruling } : { ruling, note }),
  clearRuling: (id: string, fid: string) => del<RulingResult>(`/api/runs/${id}/findings/${enc(fid)}/ruling`),

  // queue
  queue: (batch?: string) => get<QueueState>(`/api/queue${batch ? `?batch=${enc(batch)}` : ""}`),
  enqueue: (body: { items?: string[]; text?: string; name?: string; online?: boolean }) => post<QueueAdd>("/api/queue", body),
  queueItem: (qid: string) => get<QueueItem>(`/api/queue/${qid}`),
  cancelItem: (qid: string) => post<QueueItem>(`/api/queue/${qid}/cancel`),
  retryItem: (qid: string) => post<QueueItem>(`/api/queue/${qid}/retry`),
  removeItem: (qid: string) => del<{ ok: boolean }>(`/api/queue/${qid}`),
  clearQueue: () => post<{ removed: number }>("/api/queue/clear"),

  // catalogue and letters
  catalog: () => get<Catalog>("/api/catalog"),
  letters: () => get<{ templates: LetterTemplate[] }>("/api/letters"),
  letter: (id: string, kind: string, needsData = true) => get<Letter>(`/api/runs/${id}/letters/${enc(kind)}?needs_data=${needsData}`),
};

export const url = {
  figure: (run: string, fig: string, mode = "original") =>
    mode === "original" ? `/api/runs/${run}/figures/${fig}.png` : `/api/runs/${run}/figures/${fig}/view.png?mode=${mode}`,
  panel: (run: string, panel: string, mode = "original") =>
    `/api/runs/${run}/panels/${enc(panel)}.png?mode=${mode}`,
  evidence: (run: string, name: string) => `/api/runs/${run}/evidence/${enc(name)}`,
  frame: (run: string, which: "a" | "b" | "diff", a: string, b: string) =>
    `/api/runs/${run}/compare/${which}.png?a=${enc(a)}&b=${enc(b)}`,
  html: (run: string, download = false) => `/api/runs/${run}/report.html${download ? "?download=true" : ""}`,
  source: (run: string) => `/api/runs/${run}/source`,
  supplement: (run: string, name: string) => `/api/runs/${run}/supplements/${enc(name)}`,
  csv: (run: string, info = false, notes = true) => `/api/runs/${run}/export/findings.csv?info=${info}&notes=${notes}`,
  xlsx: (run: string, info = false) => `/api/runs/${run}/export/findings.xlsx?info=${info}`,
  pdf: (run: string, info = false, notes = false) => `/api/runs/${run}/export/report.pdf?info=${info}&notes=${notes}`,
  evidencePack: (run: string, notes = false) => `/api/runs/${run}/export/evidence.zip?notes=${notes}`,
  batchCsv: (bid: string) => `/api/queue/batches/${enc(bid)}.csv`,
};

export const SEVERITY_ORDER: Severity[] = ["critical", "high", "medium", "low", "info"];

export const SEV_LABEL: Record<Severity, string> = { critical: "Critical", high: "High", medium: "Medium", low: "Low", info: "Note" };
/** Severity is never shown by colour alone: each has a glyph too. */
export const SEV_GLYPH: Record<Severity, string> = { critical: "◆", high: "▲", medium: "●", low: "○", info: "·" };

export const RULING_LABEL: Record<RulingId, string> = {
  unreviewed: "Not reviewed",
  confirmed: "Confirmed",
  false_positive: "False positive",
  needs_raw_data: "Needs raw data",
};
export const RULING_KEY: Record<RulingId, string> = { unreviewed: "u", confirmed: "c", false_positive: "f", needs_raw_data: "r" };

export const VERDICT: Record<string, string> = {
  confirmed: "AI: confirmed",
  likely: "AI: likely",
  unclear: "AI: unclear",
  false_positive: "AI: false positive",
};

/** One place for the score's colour band (used by the run header, recent scans and the queue). */
export function scoreTone(score: number): Severity {
  return score >= 70 ? "critical" : score >= 40 ? "high" : score >= 15 ? "medium" : "low";
}
export function scoreWord(score: number): string {
  return score >= 70 ? "Serious concerns" : score >= 40 ? "High concern" : score >= 15 ? "Worth a look" : "Few signals";
}

/** Numbers that may be missing: '–' rather than NaN or 'undefined'. */
export function num(x: unknown, digits = 0): string {
  const n = typeof x === "number" ? x : Number(x);
  return x === null || x === undefined || x === "" || !Number.isFinite(n) ? "–" : n.toFixed(digits);
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso.includes("T") && !/[Z+]/.test(iso.slice(10)) ? iso : iso).getTime();
  if (!Number.isFinite(t)) return iso;
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)} d ago`;
  return new Date(t).toLocaleDateString();
}

export function absTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  return Number.isFinite(t) ? new Date(t).toLocaleString() : iso;
}

export const CATEGORY_NAMES: Record<string, string> = {
  image: "Images",
  data: "Data",
  stats: "Statistics",
  text: "Text",
  refs: "References",
  report: "Reporting",
  record: "Publication record",
};

export const CHECK_NAMES: Record<string, string> = {
  "image.metadata": "Image metadata",
  "data.duplicate_block": "Copied data",
  "data.derived_column": "Derived column",
  "data.random_formula": "Random-number formula",
  "data.terminal_digits": "Odd last digits",
  "data.digit_preference": "Rounded values",
  "data.repeated_values": "Repeated values",
  "data.impossible": "Impossible values",
  "data.sort_anomaly": "Rows out of order",
  "data.too_similar": "Identical spread",
  "data.gene_dates": "Gene names as dates",
  "data.hidden": "Hidden data",
  "data.mixed_fonts": "Mixed fonts",
  "data.generator_digits": "Machine precision",
  "refs.bad_doi": "Broken DOI",
  "refs.unverified": "Unfindable references",
  "refs.self_citation": "Self-citation",
  "refs.citation_pushing": "Citation concentration",
  "refs.journal_self": "Journal self-citation",
  "refs.off_topic": "Off-topic references",
  "refs.sneaked": "Sneaked references",
  "record.retracted": "Retracted",
  "record.concern": "Expression of concern",
  "record.correction": "Correction",
  "record.fast_acceptance": "Fast acceptance",
  "record.duplicate_title": "Duplicate title",
  "record.author_retractions": "Authors' retractions",
  "report.timeline": "Timeline",
  "report.missing_data": "No data statement",
  "report.missing_coi": "No conflict statement",
  "report.missing_funding": "No funding statement",
  "report.data_on_request": "Data on request",
  "report.no_ethics": "No ethics approval",
  "report.no_registration": "Unregistered trial",
  "report.data_not_shown": "Data not shown",
  "report.sem_small_n": "SEM with tiny n",
  "report.misidentified_cells": "Misidentified cells",
  "report.figure_count": "Figure count",
  "report.abstract_numbers": "Abstract numbers",
  "report.accession_missing": "Missing accession",
  "image.duplicate": "Reused image",
  "image.duplicate_chart": "Identical chart",
  "image.clone": "Cloned region",
  "image.splice": "Splice seam",
  "image.flat": "Painted patch",
  "text.ai_boilerplate": "Chatbot text",
  "text.ai_statement": "AI statement",
  "text.tortured": "Tortured phrases",
  "text.prompt_injection": "Hidden AI instructions",
  "text.hidden_text": "Hidden text",
  "text.hidden_tags": "Invisible tags",
  "text.invisible_chars": "Invisible characters",
  "stats.recompute": "p-value mismatch",
  "stats.grim": "Impossible mean",
  "stats.percent": "Wrong percentage",
  "stats.repeated_se": "Repeated spread",
  "stats.impossible": "Impossible p-value",
  "refs.uncited": "Padded bibliography",
  "refs.missing": "Missing reference",
  "refs.duplicate": "Duplicate reference",
  "refs.retracted": "Retracted citation",
  "image.unchecked": "Extra image checks not run",
  "data.offset_copy": "Copied and shifted data",
  "data.typed_over_formula": "Typed over formula",
  "data.calc_order": "Rows moved (Excel record)",
  "data.external_links": "Linked workbooks",
  "data.summary_mismatch": "Mean/SD mismatch",
  "data.precision_mix": "Mixed precision",
  "data.trailing_repeats": "Repeated decimals",
  "data.benford": "Benford first digits",
  "data.file_dates": "File dated after publication",
  "data.file_metadata": "Supplement file dates",
  "stats.grimmer": "Impossible SD",
  "stats.ci_p": "CI and p-value disagree",
  "stats.ci_estimate": "Estimate outside its interval",
  "stats.df_n": "Degrees of freedom too large",
  "stats.sd_sem": "SD or SEM mix-up",
  "stats.out_of_range": "Impossible value",
  "stats.carlisle": "Baseline too similar or too different",
  "stats.unchecked": "Baseline table not checked",
  "refs.doi_mismatch": "Cited DOI points elsewhere",
  "refs.cited_after_retraction": "Cites a work retracted before writing",
  "refs.concern": "Cites a work under concern",
  "record.special_issue": "Special-issue paper",
  "report.trial_id_format": "Trial number not valid",
  "report.trial_not_found": "Trial not found in registry",
  "report.trial_registered_late": "Trial registered late",
  "report.trial_retrospective": "Trial registered after it started",
  "report.trial_unchecked": "Trial registry not checked",
};

/** The catalogue is fetched once per page load and shared. */
let catalogPromise: Promise<Catalog> | null = null;
export function loadCatalog(): Promise<Catalog> {
  if (!catalogPromise) catalogPromise = api.catalog().catch((e) => { catalogPromise = null; throw e; });
  return catalogPromise;
}

export function checkLabel(check: string, cat?: Catalog | null): string {
  return cat?.checks.find((c) => c.id === check)?.label ?? CHECK_NAMES[check] ?? check;
}

/** Pair findings (two panels of one picture) open in the full-stage comparator. */
export function pairOf(f: Finding | null | undefined): [string, string] | null {
  if (!f || !(f.check === "image.duplicate" || f.check === "image.duplicate_chart")) return null;
  const p = f.locations.filter((l) => l.panel).map((l) => l.panel!);
  return p.length >= 2 && p[0] !== p[1] ? [p[0], p[1]] : null;
}

/** A short location summary for a finding row: 'Figure 3 · B and E', 'Table S2 · C4:C19', 'p. 7'. */
export function whereOf(f: Finding, figures: FigureInfo[]): string {
  const l = f.locations;
  if (!l.length) return "";
  const fig = l.find((x) => x.figure)?.figure;
  if (fig) {
    const label = figures.find((x) => x.id === fig)?.label ?? fig;
    const panels = Array.from(new Set(l.filter((x) => x.figure === fig && x.panel).map((x) => x.panel!.split(".").pop()!.toUpperCase())));
    return panels.length ? `${label}, ${panels.join(" and ")}` : label;
  }
  const file = l.find((x) => x.file || x.sheet);
  if (file) return [file.sheet, file.cells].filter(Boolean).join(", ") || file.file || "";
  const page = l.find((x) => x.page)?.page;
  return page ? `p. ${page}` : "";
}

export function panelLabel(id: string, figures: FigureInfo[]): string {
  const [fig, p] = id.split(".");
  const f = figures.find((x) => x.id === fig);
  return `${f?.label ?? fig} ${p ? p.toUpperCase() : ""}`.trim();
}
