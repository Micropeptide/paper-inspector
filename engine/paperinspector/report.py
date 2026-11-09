"""Turn a scan into words a person can send: a cautious PubPeer-style comment and a self-contained HTML report.

The phrasing follows how careful sleuths write: describe what is seen, never allege intent, ask for the raw data,
leave room for an innocent explanation.
"""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from . import catalog
from .models import RULING_LABELS, SEVERITIES

ASK = {
    "image.duplicate": "Could the authors share the original, uncropped image files for these panels?",
    "image.clone": "Could the authors share the original microscope/camera file for this panel?",
    "image.splice": "Could the authors share the uncropped blot, and indicate where lanes were joined?",
    "image.flat": "Could the authors share the unprocessed image for this panel?",
    "image.duplicate_chart": "Could the authors confirm which data these plots show?",
    "stats.recompute": "Could the authors check these test results against their raw data?",
    "stats.grim": "Could the authors share the underlying data for these means?",
    "stats.percent": "Could the authors check these percentages?",
    "stats.repeated_se": "Could the authors share the raw values behind this table?",
}


def _active(report: dict) -> list[dict]:
    out = []
    for f in report["findings"]:
        if f["severity"] == "info":
            continue
        ai = f.get("ai") or {}
        rl = (f.get("ruling") or {}).get("ruling") if isinstance(f.get("ruling"), dict) else f.get("ruling")
        if rl == "false_positive":
            continue                    # a person looked and ruled it out: that outranks every machine
        if ai.get("verdict") == "false_positive" and rl != "confirmed":
            continue
        out.append(f)
    return out


def pubpeer(report: dict, max_items: int = 8) -> str:
    """A draft comment.  It is a draft: a person must look at every item before posting anything."""
    p = report["paper"]
    items = [f for f in _active(report) if f["severity"] in ("critical", "high", "medium")
             and (f.get("kind", "integrity") == "integrity" or f.get("category") in ("data", "stats"))
             and not f["check"].startswith("record.")][:max_items]
    ai = report.get("ai_review") or {}
    if ai.get("pubpeer"):
        return ai["pubpeer"]
    if not items:
        return "No issues worth raising were found by the automatic screen."
    lines = [f"Some aspects of {('*' + p['title'] + '*') if p.get('title') else 'this article'} may need "
             "clarification. I noticed the following and would be grateful if the authors could comment.", ""]
    asks = []
    for i, f in enumerate(items, 1):
        where = ", ".join(sorted({l.get("panel") or l.get("figure") or
                                  (f"{l['file']}{(' ' + l['cells']) if l.get('cells') else ''}" if l.get("file") else "") or
                                  (f"page {l['page']}" if l.get("page") else "")
                                  for l in f.get("locations", []) if l} - {""})) or "text"
        d = f.get("data") or {}
        if f["check"] == "image.duplicate":
            note = (f" After alignment their fine texture correlates at {d.get('ncc_texture', 0):.2f}"
                    f"{', and the second appears mirrored' if d.get('mirrored') else ''}.")
        elif f["check"].startswith("image."):
            note = ""
        else:
            note = " " + f["detail"]
        lines.append(f"{i}. **{where}**: {f['title'].split(': ', 1)[-1]}.{note}")
        q = ASK.get(f["check"]) or catalog.ask(f["check"])
        if q and q not in asks:
            asks.append(q)
    lines.append("")
    lines += asks
    lines.append("")
    lines.append("There may be an innocent explanation for each of these, such as a mix-up when assembling the "
                 "figures. Annotated images are attached.")
    return "\n".join(lines)


SEV_COLOR = {"critical": "#dc2626", "high": "#ea580c", "medium": "#ca8a04", "low": "#2563eb", "info": "#6b7280"}


def _img(path: Path) -> str:
    if not path.exists():
        return ""
    mime = "image/gif" if path.suffix == ".gif" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def html_report(run: Path) -> str:
    """One file with every image inlined, so it can be emailed or attached to a journal report."""
    r = json.loads((run / "report.json").read_text())
    try:
        from .rulings import annotate
        annotate(r, run)
    except Exception:                   # noqa: BLE001 - the report must still render without saved rulings
        pass
    p = r["paper"]
    esc = html.escape
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Paper Inspector report</title><style>
:root{{--ink:#0f172a;--mute:#64748b;--line:#e2e8f0;--bg:#fff;--card:#f8fafc}}
@media (prefers-color-scheme:dark){{:root{{--ink:#e2e8f0;--mute:#94a3b8;--line:#1e293b;--bg:#0b1120;--card:#111827}}}}
body{{font:15px/1.55 -apple-system,system-ui,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:24px 16px}}
main{{max-width:980px;margin:auto}} h1{{font-size:22px;margin:0 0 4px}} .mute{{color:var(--mute)}}
.score{{font-size:44px;font-weight:700}} .f{{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0;background:var(--card)}}
.sev{{display:inline-block;padding:1px 8px;border-radius:99px;color:#fff;font-size:12px;font-weight:600;text-transform:uppercase}}
.f img{{max-width:100%;border-radius:6px;margin:8px 8px 0 0;vertical-align:top}} pre{{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);padding:12px;border-radius:8px}}
</style></head><body><main>
<p class="mute">Paper Inspector {esc(r['version'])} · automated screen · {esc(r['created'])}</p>
<h1>{esc(p.get('title') or p['source'])}</h1><p class="mute">{esc(p['source'])}{(' · doi ' + esc(p['doi'])) if p.get('doi') else ''}
 · {p['pages']} pages · {len(r['figures'])} figures · {r['panels']} panels</p>
<p><span class="score">{r['score']}</span><span class="mute">/100</span> &nbsp; {esc(r['verdict'])}</p>
<p class="mute">An automated screen finds candidates, not verdicts. Every item below needs a person to look at it.</p>"""]
    ai = r.get("ai_review")
    if ai:
        parts.append(f"<h2>AI investigator ({esc(ai.get('model', ''))})</h2><p>{esc(ai.get('summary', ''))}</p>")
    shown_kind = None
    for f in sorted(r["findings"], key=lambda f: f.get("kind", "integrity") != "integrity"):
        if f["severity"] == "info":
            continue
        kind = f.get("kind", "integrity")
        if kind != shown_kind:
            shown_kind = kind
            parts.append("<h2>Integrity signals</h2><p class='mute'>Could indicate manipulation. These set the score.</p>"
                         if kind == "integrity" else
                         "<h2>Quality notes</h2><p class='mute'>Honest problems and missing information.</p>")
        verdict = (f.get("ai") or {}).get("verdict")
        rul = f.get("ruling") if isinstance(f.get("ruling"), dict) else {}
        mine = RULING_LABELS.get(rul.get("ruling", "unreviewed")) if rul.get("ruling", "unreviewed") != "unreviewed" else ""
        parts.append(f"""<div class="f"><span class="sev" style="background:{SEV_COLOR[f['severity']]}">{f['severity']}</span>
 <b>{esc(f['title'])}</b>{f' <span class="mute">· AI: {esc(verdict.replace("_", " "))}</span>' if verdict else ''}
{f' <span class="mute">· Your review: <b>{esc(mine)}</b></span>' if mine else ''}
<p>{esc(f['detail'])}</p>""")
        for loc in f.get("locations", [])[:4]:
            if loc.get("file"):
                parts.append(f"<p class='mute'>{esc(loc['file'])}{(', sheet “' + esc(loc['sheet']) + '”') if loc.get('sheet') else ''}"
                             f"{(', cells ' + esc(loc['cells'])) if loc.get('cells') else ''}</p>")
            if loc.get("quote"):
                parts.append(f"<p class='mute'>p.{loc.get('page', '?')}: “{esc(loc['quote'][:200])}”</p>")
        for e in f.get("evidence", [])[:3]:
            src = _img(run / "evidence" / e)
            if src:
                parts.append(f"<img src='{src}' alt='{esc(e)}'>")
        parts.append("</div>")
    parts.append(f"<h2>Draft comment</h2><pre>{esc(pubpeer(r))}</pre></main></body></html>")
    return "".join(parts)
