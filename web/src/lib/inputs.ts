/** A quick, offline guess at what was pasted into the search box, so the box can say what it will do.
 *  The engine has the final word (inputs.parse_input); this only drives the helper line and the button. */

export type InputKind = "empty" | "doi" | "pmid" | "pmcid" | "arxiv" | "url" | "title" | "many";

export interface Guess {
  kind: InputKind;
  value: string;
  label: string;
  lines?: number;
}

const DOI = /\b(10\.\d{4,9}\/[^\s"<>]+)/i;
const ARXIV_NEW = /^(?:arxiv\s*:\s*)?(\d{2}(?:0[1-9]|1[0-2])\.\d{4,5})(v\d+)?$/i;
const ARXIV_OLD = /^(?:arxiv\s*:\s*)?([a-z][a-z-]+(?:\.[a-z]{2})?\/\d{7})(v\d+)?$/i;
const PMCID = /^(?:pmcid\s*[:#]?\s*)?PMC\s?(\d{1,9})$/i;
const PMID = /^(?:(?:pmid|pubmed(?:\s*id)?)\s*[:#]?\s*)?(\d{5,9})$/i;
const URL = /^(?:https?:\/\/|www\.)\S+$/i;

export const KIND_LABEL: Record<InputKind, string> = {
  empty: "",
  doi: "DOI",
  pmid: "PubMed ID",
  pmcid: "PubMed Central ID",
  arxiv: "arXiv ID",
  url: "article link",
  title: "title or citation",
  many: "list",
};

export function cleanDoi(d: string): string {
  return d.replace(/[).,;:\]]+$/, "");
}

export function guess(text: string): Guess {
  const t = text.trim();
  if (!t) return { kind: "empty", value: "", label: "" };
  const lines = t.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (lines.length > 1) return { kind: "many", value: t, label: KIND_LABEL.many, lines: lines.length };
  if (URL.test(t)) {
    let u = t;
    try { u = decodeURIComponent(t); } catch { /* keep */ }
    const d = u.match(DOI);
    if (d && /doi\.org|biorxiv|medrxiv|\/doi\//i.test(u)) return { kind: "doi", value: cleanDoi(d[1]), label: KIND_LABEL.doi };
    const ax = u.match(/arxiv\.org\/(?:abs|pdf)\/([^\s?#]+?)(?:\.pdf)?(?:[?#].*)?$/i);
    if (ax) return { kind: "arxiv", value: ax[1], label: KIND_LABEL.arxiv };
    const pmc = u.match(/PMC(\d{1,9})/i);
    if (pmc && /ncbi|europepmc/i.test(u)) return { kind: "pmcid", value: `PMC${pmc[1]}`, label: KIND_LABEL.pmcid };
    const pm = u.match(/pubmed\.ncbi\.nlm\.nih\.gov\/(\d{1,9})/i);
    if (pm) return { kind: "pmid", value: pm[1], label: KIND_LABEL.pmid };
    return { kind: "url", value: t, label: KIND_LABEL.url };
  }
  const d = t.match(DOI);
  if (d && t.length < 300 && t.split(/\s+/).length <= 3) return { kind: "doi", value: cleanDoi(d[1]), label: KIND_LABEL.doi };
  let m = t.match(PMCID);
  if (m) return { kind: "pmcid", value: `PMC${m[1]}`, label: KIND_LABEL.pmcid };
  m = t.match(ARXIV_NEW) ?? t.match(ARXIV_OLD);
  if (m) return { kind: "arxiv", value: m[1], label: KIND_LABEL.arxiv };
  m = t.match(PMID);
  if (m) return { kind: "pmid", value: m[1], label: KIND_LABEL.pmid };
  if (d) return { kind: "doi", value: cleanDoi(d[1]), label: KIND_LABEL.doi };
  return { kind: "title", value: t, label: KIND_LABEL.title };
}
