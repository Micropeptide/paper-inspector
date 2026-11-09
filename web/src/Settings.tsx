import { useEffect, useState } from "react";
import { api, Settings } from "./api";
import { SHORTCUTS } from "./Palette";
import { Kbd, Modal, Spinner } from "./ui";

type Section = "ai" | "data" | "look" | "keys";
const SECTIONS: [Section, string][] = [["ai", "AI investigator"], ["data", "Scholarly data"], ["look", "Appearance"], ["keys", "Keyboard"]];

export default function SettingsDialog({ onClose, theme, setTheme }: { onClose: () => void; theme: string; setTheme: (t: string) => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [sec, setSec] = useState<Section>("ai");
  const [key, setKey] = useState("");
  const [oaKey, setOaKey] = useState("");
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState("");
  const [fieldErr, setFieldErr] = useState("");
  const [note, setNote] = useState("");

  const load = () => {
    setLoadErr("");
    api.settings().then((x) => { setS(x); setEmail(x.email ?? ""); }, (e) => setLoadErr((e as Error).message));
  };
  useEffect(load, []);

  const save = async (patch: Partial<Settings> & { keys?: Record<string, string> }) => {
    setFieldErr("");
    try {
      const n = await api.saveSettings(patch);
      setS(n);
      setStatus("Saved");
      window.setTimeout(() => setStatus(""), 1500);
      return n;
    } catch (e) {
      setFieldErr((e as Error).message);
      return null;
    }
  };

  const pre = s ? s.presets[s.provider] : null;

  return (
    <Modal title="Settings" onClose={onClose} className="settings"
      footer={<><span className="muted grow" aria-live="polite">{status}</span><button className="btn primary" onClick={onClose}>Done</button></>}>
      {loadErr ? (
        <div className="inline-error"><p>Settings could not be loaded: {loadErr}</p><div className="row"><button className="btn" onClick={load}>Retry</button><button className="btn" onClick={onClose}>Close</button></div></div>
      ) : !s || !pre ? (
        <p className="muted"><Spinner /> Loading…</p>
      ) : (
        <div className="settings-grid">
          <div className="settings-nav" role="tablist" aria-orientation="vertical" aria-label="Settings sections">
            {SECTIONS.map(([id, label]) => (
              <button key={id} role="tab" id={`st-${id}`} aria-selected={sec === id} aria-controls={`sp-${id}`} tabIndex={sec === id ? 0 : -1}
                className={sec === id ? "on" : ""} onClick={() => setSec(id)}
                onKeyDown={(e) => {
                  const i = SECTIONS.findIndex(([x]) => x === id);
                  const n = e.key === "ArrowDown" ? (i + 1) % SECTIONS.length : e.key === "ArrowUp" ? (i - 1 + SECTIONS.length) % SECTIONS.length : -1;
                  if (n >= 0) { e.preventDefault(); setSec(SECTIONS[n][0]); window.setTimeout(() => document.getElementById(`st-${SECTIONS[n][0]}`)?.focus(), 0); }
                }}>{label}</button>
            ))}
          </div>
          <div className="settings-pane" role="tabpanel" id={`sp-${sec}`} aria-labelledby={`st-${sec}`}>
            {fieldErr && <p className="error-text" role="alert">Not saved: {fieldErr}</p>}
            {sec === "ai" && (
              <>
                <p className="muted">The investigator sends the flagged figures and the detectors' measurements to the model you choose. Keys are stored only on this computer.</p>
                <label className="field" htmlFor="set-provider">Provider</label>
                <select id="set-provider" className="select" value={s.provider} onChange={async (e) => {
                  const n = await save({ provider: e.target.value, model: "", base_url: "" });
                  if (n) setNote(`Model reset to ${n.model || n.presets[n.provider]?.model || "the default"}.`);
                }}>
                  {Object.entries(s.presets).map(([id, p]) => (
                    <option key={id} value={id}>{p.label}{s.key_set[id] || p.no_key ? "" : " (needs a key)"}</option>
                  ))}
                </select>
                {note && <small className="muted">{note}</small>}
                <label className="field" htmlFor="set-model">Model</label>
                <input id="set-model" list="models" value={s.model} onChange={(e) => setS({ ...s, model: e.target.value })} onBlur={() => save({ model: s.model })}
                  onKeyDown={(e) => { if (e.key === "Enter") save({ model: s.model }); }} />
                <datalist id="models">{(pre.models ?? [pre.model]).map((m) => <option key={m} value={m} />)}</datalist>
                <small className="muted">Default for this provider: {pre.model}</small>
                {pre.kind === "openai" && (
                  <>
                    <label className="field" htmlFor="set-url">Endpoint</label>
                    <input id="set-url" value={s.base_url} placeholder="https://…/v1" onChange={(e) => setS({ ...s, base_url: e.target.value })} onBlur={() => save({ base_url: s.base_url })} />
                  </>
                )}
                {!pre.no_key && (
                  <>
                    <label className="field" htmlFor="set-key">API key</label>
                    <div className="row">
                      <input id="set-key" type="password" autoComplete="off" value={key} className="grow"
                        placeholder={s.key_set[s.provider] ? "Replace the saved key" : "Paste your key"} onChange={(e) => setKey(e.target.value)} />
                      <button className="btn" disabled={!key} onClick={async () => { await save({ keys: { [s.provider]: key } }); setKey(""); }}>Save key</button>
                      {s.key_set[s.provider] && <button className="btn ghost" onClick={() => save({ keys: { [s.provider]: "" } })}>Remove</button>}
                    </div>
                    <small className="muted">{s.key_set[s.provider] ? "A key is saved on this computer." : pre.env ? `No key saved. ${pre.env} is used if it is set.` : "No key saved."}</small>
                  </>
                )}
                <label className="check"><input type="checkbox" checked={s.vision} onChange={(e) => save({ vision: e.target.checked })} /> This model can see images</label>
                {pre.kind === "anthropic" && (
                  <>
                    <label className="field" htmlFor="set-effort">Effort</label>
                    <select id="set-effort" className="select" value={s.effort} onChange={(e) => save({ effort: e.target.value })}>
                      {["low", "medium", "high", "xhigh", "max"].map((x) => <option key={x}>{x}</option>)}
                    </select>
                  </>
                )}
              </>
            )}
            {sec === "data" && (
              <>
                <p className="muted">Reference checks use free public services. An optional free <a href="https://openalex.org/settings/api" target="_blank" rel="noreferrer">OpenAlex key</a> lifts the small daily limit shared by everyone on your network.</p>
                <label className="field" htmlFor="set-oa">OpenAlex key</label>
                <div className="row">
                  <input id="set-oa" type="password" autoComplete="off" value={oaKey} className="grow"
                    placeholder={s.data_keys?.openalex ? "Replace the saved key" : "Optional"} onChange={(e) => setOaKey(e.target.value)} />
                  <button className="btn" disabled={!oaKey} onClick={async () => { await save({ keys: { openalex: oaKey } }); setOaKey(""); }}>Save key</button>
                </div>
                <small className="muted">{s.data_keys?.openalex ? "A key is saved on this computer." : "No key saved."}</small>
                <label className="field" htmlFor="set-email">Contact email (optional)</label>
                <input id="set-email" type="email" value={email} autoComplete="email" placeholder="Leave empty to stay anonymous"
                  onChange={(e) => setEmail(e.target.value)}
                  onBlur={async () => {
                    if (email === (s.email ?? "")) return;
                    const n = await save({ email: email.trim() });
                    if (n && email.trim() && n.email !== email.trim()) { setFieldErr("That does not look like an email address, so it was not saved."); setEmail(n.email ?? ""); }
                  }} />
                <small className="muted">Crossref and OpenAlex answer faster for requests that name a contact. It is sent only to those services, and only if you fill it in.</small>
              </>
            )}
            {sec === "look" && (
              <fieldset className="radios">
                <legend>Theme</legend>
                {[["system", "Follow the system"], ["light", "Light"], ["dark", "Dark"]].map(([id, label]) => (
                  <label key={id} className="check"><input type="radio" name="theme" checked={theme === id} onChange={() => setTheme(id)} /> {label}</label>
                ))}
              </fieldset>
            )}
            {sec === "keys" && (
              <div className="sc-mini">
                {SHORTCUTS.map(([g, rows]) => (
                  <section key={g}><h3>{g}</h3><dl>{rows.map(([k, w]) => <div key={k}><dt><Kbd>{k}</Kbd></dt><dd>{w}</dd></div>)}</dl></section>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
