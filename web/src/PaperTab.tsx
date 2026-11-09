import { ReactNode } from "react";
import { Report, url } from "./api";
import { TagEditor } from "./Tags";
import { Icon } from "./ui";

type Status = "ok" | "watch" | "flag";
const STATUS_WORD: Record<Status, string> = { ok: "fine", watch: "worth a look", flag: "flagged" };

/** What the paper is, what was examined, and the numbers behind the reference and record checks. */
export default function PaperTab({ report, onTags, onFinding }: { report: Report; onTags: (t: string[]) => void; onFinding: (id: string) => void }) {
  const m = report.meta ?? {};
  const refs = report.references ?? {};
  const h = report.history ?? {};
  const t = report.timeline ?? {};
  const authors = m.authors ?? [];
  const pct = (x?: number) => (x === undefined || x === null || !Number.isFinite(x) ? "–" : `${Math.round(x * 100)}%`);
  const findingFor = (check: string) => report.findings.find((f) => f.check === check)?.id;

  const Stat = ({ label, value, status, check }: { label: string; value: ReactNode; status?: Status; check?: string }) => {
    const fid = check ? findingFor(check) : undefined;
    const st: Status = status ?? (fid ? "flag" : "ok");
    return (
      <>
        <dt>{label}</dt>
        <dd>
          <span className={`stat-dot ${st}`} aria-hidden="true" /><span className="sr">{STATUS_WORD[st]}: </span>
          {value}
          {fid && <button className="link-btn" onClick={() => onFinding(fid)}>View finding</button>}
        </dd>
      </>
    );
  };

  const top = refs.top_cited_authors ?? [];
  const topMax = Math.max(1, ...top.map((a) => a.count));

  return (
    <div className="paper-tab">
      <section className="card">
        <h2 className="pane-title">{m.title || report.paper.title || report.paper.source}</h2>
        {m.title ? (
          <>
            <p className="muted">
              {authors.slice(0, 8).map((a) => `${a.given} ${a.family}`.trim()).join(", ")}
              {authors.length > 8 ? `, and ${authors.length - 8} more` : ""}
            </p>
            <dl className="facts">
              {m.journal && (<><dt>Journal</dt><dd>{m.journal}{m.publisher ? ` (${m.publisher})` : ""}</dd></>)}
              {m.issued && (<><dt>Published</dt><dd>{m.issued}</dd></>)}
              {m.doi && (<><dt>DOI</dt><dd><a href={`https://doi.org/${m.doi}`} target="_blank" rel="noreferrer">{m.doi} <Icon name="ext" size={12} /></a></dd></>)}
              {m.cited_by_count !== undefined && m.cited_by_count !== null && (<><dt>Cited by</dt><dd>{m.cited_by_count}</dd></>)}
              {t.received && (<><dt>Received</dt><dd>{String(t.received)}</dd></>)}
              {t.accepted && <Stat label="Accepted" check="record.fast_acceptance"
                value={<>{String(t.accepted)}{t.days_to_accept !== undefined ? ` (${t.days_to_accept} days later)` : ""}</>} />}
              {m.notices && m.notices.length > 0 && <Stat label="Notices" status="flag" value={m.notices.map((n) => `${n.type} ${n.date}`).join("; ")} />}
              {m.funders && m.funders.length > 0 && (<><dt>Funders</dt><dd>{m.funders.slice(0, 5).join("; ")}</dd></>)}
            </dl>
            {m.doi && (
              <p className="muted">
                Also look at <a href={`https://pubpeer.com/search?q=${encodeURIComponent(m.doi)}`} target="_blank" rel="noreferrer">PubPeer comments <Icon name="ext" size={12} /></a>
                {" "}and the <a href={`https://retractiondatabase.org/RetractionSearch.aspx#?ttl=${encodeURIComponent((m.title ?? "").slice(0, 60))}`} target="_blank" rel="noreferrer">Retraction Watch database <Icon name="ext" size={12} /></a>.
              </p>
            )}
          </>
        ) : (
          <p className="muted">Uploaded file. Paste the DOI instead to also check the references, the authors' record and the publication history.</p>
        )}
        <h3>Tags</h3>
        <TagEditor run={report.id} tags={report.tags ?? []} onChange={onTags} />
      </section>

      {refs.count ? (
        <section className="card">
          <h3>References</h3>
          <dl className="facts">
            <dt>Listed</dt><dd>{refs.count}{h.references_printed ? ` (${h.references_printed} printed in the PDF)` : ""}</dd>
            <dt>Found in OpenAlex</dt><dd>{refs.resolved ?? "–"}</dd>
            <Stat label="Self-citations" check="refs.self_citation" value={<>{refs.self_citations ?? "–"} ({pct(refs.self_citation_rate)})</>} />
            <Stat label="Same journal" check="refs.journal_self" value={refs.journal_self_citations ?? "–"} />
            <Stat label="Retracted" check="refs.retracted" value={refs.retracted ?? 0} />
            <Stat label="Not found" check="refs.unverified" value={refs.unmatched ?? 0} />
            <Stat label="Broken DOIs" check="refs.bad_doi" value={refs.bad_dois?.length ?? 0} />
            <Stat label="From unrelated fields" check="refs.off_topic" value={refs.off_topic ?? "–"} />
          </dl>
          {top.length > 0 && (
            <>
              <h4>Most-cited outside authors</h4>
              <ul className="bars">
                {top.slice(0, 5).map((a) => (
                  <li key={a.name}><span>{a.name}</span><i style={{ width: `${(a.count / topMax) * 100}%` }} aria-hidden="true" /><b>{a.count}</b></li>
                ))}
              </ul>
            </>
          )}
        </section>
      ) : null}

      {(h.author_retractions?.length || h.accessions?.cited?.length) ? (
        <section className="card">
          <h3>Authors and data</h3>
          <dl className="facts">
            {h.author_retractions && h.author_retractions.length > 0 && (
              <Stat label="Retractions" check="record.author_retractions" value={h.author_retractions.map(([n, k]) => `${n}: ${k}`).join("; ")} />
            )}
            {h.accessions?.cited?.length ? (
              <Stat label="Data accessions" status={h.accessions.not_found.length ? "flag" : "ok"}
                value={<>{h.accessions.cited.join(", ")}{h.accessions.not_found.length ? ` (not found: ${h.accessions.not_found.join(", ")})` : " (all found)"}</>} />
            ) : null}
          </dl>
        </section>
      ) : null}

      {report.sources && report.sources.length > 0 && (
        <section className="card">
          <h3>Files examined</h3>
          <ul className="sources">
            {report.sources.map((s, i) => (
              <li key={i} className={s.status.startsWith("not") ? "skipped" : ""}>
                <span className="src-name">
                  {i === 0 ? <a href={url.source(report.id)} target="_blank" rel="noreferrer">{s.label} <Icon name="ext" size={12} /></a>
                    : <a href={url.supplement(report.id, s.name)} title="Downloads the file">{s.label !== s.name ? `${s.label}: ` : ""}{s.name}</a>}
                </span>
                <span className="src-status">
                  {s.status}{s.figures ? `, ${s.figures} image${s.figures > 1 ? "s" : ""}` : ""}{s.tables ? `, ${s.tables} table${s.tables > 1 ? "s" : ""}` : ""}
                  {s.origin ? `. From ${s.origin}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {report.log && report.log.length > 0 && (
        <details className="fetch-log card">
          <summary>How the files were found</summary>
          <ul>{report.log.map((l, i) => <li key={i}>{l}</li>)}</ul>
        </details>
      )}
    </div>
  );
}
