import { useState } from "react";
import { api } from "./api";
import { Icon, useToast } from "./ui";

/** Tags on one scan: shown as chips, with an inline box to add one.  The library search can filter by them. */
export function TagEditor({ run, tags, onChange, compact = false }: { run: string; tags: string[]; onChange: (t: string[]) => void; compact?: boolean }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const edit = async (patch: { add?: string[]; remove?: string[] }) => {
    setBusy(true);
    try {
      const r = await api.editTags(run, patch);
      onChange(r.tags);
    } catch (e) {
      toast({ text: `Tags not saved: ${(e as Error).message}`, tone: "error" });
    } finally {
      setBusy(false);
    }
  };
  const add = () => {
    const t = text.trim().replace(/\s+/g, " ").slice(0, 40);
    if (!t) return;
    if (tags.length >= 20) {
      toast({ text: "A scan can carry at most 20 tags.", tone: "error" });
      return;
    }
    setText("");
    edit({ add: [t] });
  };
  return (
    <div className={`tags${compact ? " compact" : ""}`} aria-busy={busy}>
      {tags.map((t) => (
        <span key={t} className="tag">
          <Icon name="tag" size={12} /> {t}
          <button className="tag-x" aria-label={`Remove tag ${t}`} onClick={() => edit({ remove: [t] })}><Icon name="close" size={12} /></button>
        </span>
      ))}
      <form className="tag-add" onSubmit={(e) => { e.preventDefault(); add(); }}>
        <label className="sr" htmlFor={`tag-${run}`}>Add a tag</label>
        <input id={`tag-${run}`} value={text} onChange={(e) => setText(e.target.value)} placeholder={tags.length ? "Add tag" : "Add a tag, e.g. to-check"} maxLength={40} />
      </form>
    </div>
  );
}
