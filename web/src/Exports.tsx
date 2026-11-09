import { useEffect, useState } from "react";
import { api, Letter, LetterTemplate, Report, url } from "./api";
import { copyText, Icon, Menu, Modal, Spinner, useToast } from "./ui";

/** The Export menu in the run header: files to download and the letter drafts. */
export function ExportMenu({ report, onLetters, onCopySummary, onCopyLink }: {
  report: Report; onLetters: () => void; onCopySummary: () => void; onCopyLink: () => void;
}) {
  const id = report.id;
  return (
    <Menu label="Export" buttonClass="btn" button={<><Icon name="download" /> Export <Icon name="chevD" size={12} /></>}
      items={[
        { label: "Findings table (CSV)", hint: "with your rulings and notes", icon: "table", href: url.csv(id), download: true },
        { label: "Findings workbook (Excel)", icon: "table", href: url.xlsx(id), download: true },
        { label: "Report (PDF)", icon: "doc", href: url.pdf(id), download: true },
        { label: "Report (HTML)", icon: "doc", href: url.html(id, true), download: true },
        { label: "Evidence pack (zip)", hint: "files, figures, checksums", icon: "download", href: url.evidencePack(id), download: true },
        { separator: true, label: "" },
        { label: "Draft a letter…", hint: "to the editor or the authors", icon: "note", run: onLetters },
        { label: "Copy a summary", hint: "Markdown", icon: "text", run: onCopySummary },
        { label: "Copy link to this view", icon: "link", run: onCopyLink },
        { separator: true, label: "" },
        { label: "Open the report in a new tab", icon: "ext", href: url.html(id), newTab: true },
        { label: "Open the paper", icon: "ext", href: url.source(id), newTab: true },
      ]} />
  );
}

/** Letter drafts built by the engine from the findings you confirmed or marked as needing raw data. */
export function LettersDialog({ run, onClose }: { run: string; onClose: () => void }) {
  const [templates, setTemplates] = useState<LetterTemplate[] | null>(null);
  const [kind, setKind] = useState("editor");
  const [needsData, setNeedsData] = useState(true);
  const [letter, setLetter] = useState<Letter | null>(null);
  const [body, setBody] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const toast = useToast();

  useEffect(() => {
    api.letters().then((r) => setTemplates(r.templates), (e) => setErr((e as Error).message));
  }, []);

  const load = () => {
    setLoading(true);
    setErr("");
    api.letter(run, kind, needsData).then((l) => { setLetter(l); setBody(l.body); }, (e) => setErr((e as Error).message)).finally(() => setLoading(false));
  };
  useEffect(load, [run, kind, needsData]); // eslint-disable-line react-hooks/exhaustive-deps

  const copy = async () => {
    const ok = await copyText(`Subject: ${letter?.subject ?? ""}\n\n${body}`);
    toast(ok ? { text: "Letter copied" } : { text: "Copying was blocked. Select the text and copy it by hand.", tone: "error" });
  };

  return (
    <Modal title="Draft a letter" onClose={onClose} className="letters"
      footer={<>
        <span className="muted grow">A starting point. Fill in the [bracketed] parts and check every point yourself.</span>
        <button className="btn" onClick={onClose}>Close</button>
        <button className="btn primary" disabled={!letter || loading} onClick={copy}>Copy letter</button>
      </>}>
      <div className="letter-opts">
        <div className="seg" role="radiogroup" aria-label="Who the letter is to">
          {(templates ?? [{ id: "editor", label: "To the journal's editor", description: "" }, { id: "authors", label: "To the authors", description: "" }]).map((t) => (
            <button key={t.id} role="radio" aria-checked={kind === t.id} className={kind === t.id ? "on" : ""} title={t.description} onClick={() => setKind(t.id)}>{t.label}</button>
          ))}
        </div>
        <label className="check"><input type="checkbox" checked={needsData} onChange={(e) => setNeedsData(e.target.checked)} /> Include findings marked “Needs raw data”</label>
      </div>
      {templates?.find((t) => t.id === kind)?.description && <p className="muted">{templates.find((t) => t.id === kind)!.description}</p>}
      {err && <div className="inline-error"><p>The letter could not be drafted: {err}</p><button className="btn" onClick={load}>Retry</button></div>}
      {loading && !letter && <p className="muted"><Spinner /> Drafting…</p>}
      {letter && (
        <>
          {letter.empty && (
            <div className="banner info" role="status">
              No findings are confirmed yet. Rule on the findings first (press c to confirm, r for needs raw data), then the letter lists them.
            </div>
          )}
          <label className="field">Subject<input value={letter.subject} onChange={(e) => setLetter({ ...letter, subject: e.target.value })} /></label>
          <label className="field">Letter<textarea value={body} onChange={(e) => setBody(e.target.value)} rows={16} spellCheck /></label>
          <p className="muted">{letter.included.length} finding{letter.included.length === 1 ? "" : "s"} included. Nothing is sent from here.</p>
        </>
      )}
    </Modal>
  );
}
