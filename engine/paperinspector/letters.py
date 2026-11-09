"""Letter templates filled from the findings a person has confirmed: one to the journal's editor, one to the authors.

The tone is neutral on purpose: describe what was seen and where, ask for the original data, leave room for an
innocent explanation, never allege intent.  The letters are drafts; the sender reads, edits and signs them.
Placeholders in [square brackets] are for the sender to fill in.
"""
from __future__ import annotations

from typing import Optional

from . import catalog, rulings
from .models import where

TEMPLATES = {
    "editor": {"label": "To the journal's editor",
               "description": "Asks the editor to look into the points you confirmed and to request the original "
                              "data from the authors."},
    "authors": {"label": "To the authors",
                "description": "Asks the corresponding author, politely, to explain the points you confirmed and "
                               "to share the original data."},
}

# Words a letter written by this module must never contain: it describes observations, it does not accuse.
NEVER = ("fraud", "fabricat", "falsif", "misconduct", "manipulat", "fake", "cheat", "dishonest", "deliberate",
         "intentional", "plagiar")


def _item(n: int, f: dict) -> list[str]:
    loc = where(f)
    title = (f.get("title") or catalog.label(f.get("check", ""))).rstrip(".")
    lines = [f"{n}. {loc + ': ' if loc else ''}{title}."]
    d = f.get("data") or {}
    if f.get("check") == "image.duplicate" and isinstance(d.get("ncc_texture"), (int, float)):
        lines.append(f"   After the two panels are aligned, their fine texture correlates at {d['ncc_texture']:.2f} "
                     "(unrelated images score near 0).")
    q = catalog.ask(f.get("check", ""))
    if q:
        lines.append(f"   {q}")
    return lines


def _points(confirmed: list[dict], unsure: list[dict]) -> list[str]:
    out = []
    n = 0
    for f in confirmed:
        n += 1
        out += _item(n, f)
    if unsure:
        out += ["", "I could not decide the following from the published version alone; the original data would "
                    "settle them:"]
        for f in unsure:
            n += 1
            out += _item(n, f)
    return out


def draft(report: dict, kind: str, include_needs_data: bool = True, sender: Optional[str] = None) -> dict:
    """A letter from an annotated report (rulings.annotate).  Returns subject, body, the finding ids used, and
    'empty' when no finding is confirmed yet (the body then says so instead of listing points)."""
    if kind not in TEMPLATES:
        raise KeyError(kind)
    p = report.get("paper") or {}
    meta = report.get("meta") or {}
    title = p.get("title") or meta.get("title") or "[article title]"
    doi = p.get("doi") or meta.get("doi") or ""
    journal = meta.get("journal") or ""
    ref = f"“{title}”" + (f" (https://doi.org/{doi})" if doi else "") + (f", published in {journal}" if journal else "")
    confirmed = rulings.confirmed(report, ("confirmed",))
    unsure = rulings.confirmed(report, ("needs_raw_data",)) if include_needs_data else []
    used = [f.get("id") for f in confirmed + unsure]
    empty = not confirmed and not unsure
    sign = [f"{sender or '[Your name]'}", "[Your affiliation, if you wish]"]
    if kind == "editor":
        subject = f"Questions about the figures and data in {('“' + title + '”') if title else 'an article'}"
        body = [f"Dear Editor{(' of ' + journal) if journal else ''},", "",
                f"I am writing about the article {ref}. While reading it, I noticed some points that I could not "
                "reconcile, and I would be grateful if they could be looked at.", "",
                "I first found these points with an automated screening tool and then checked each one by eye. They "
                "are observations, not conclusions. Each may have an innocent explanation, such as an error when the "
                "figures or tables were put together.", ""]
        body += (["[No findings are marked as confirmed yet. Mark the findings you have checked as confirmed, and "
                  "they will be listed here.]"] if empty else _points(confirmed, unsure))
        body += ["", "Annotated images for each point are attached [or: are available on request].", "",
                 "I would be grateful if the authors could be asked to comment and, where possible, to share the "
                 "original image files and raw data. I am happy to provide any further detail.", "",
                 "Kind regards,"] + sign
    else:
        subject = f"A question about your article {('“' + title + '”') if title else ''}".strip()
        body = ["Dear [corresponding author's name],", "",
                f"I read your article {ref} with interest. While going through the figures and data, I came across "
                "some points I could not reconcile, and I hope you can help me understand them.", "",
                "There may well be a simple explanation for each, such as a mix-up when the figures were assembled.", ""]
        body += (["[No findings are marked as confirmed yet. Mark the findings you have checked as confirmed, and "
                  "they will be listed here.]"] if empty else _points(confirmed, unsure))
        body += ["", "Could you share the original, unprocessed image files and the raw data behind these points? "
                 "I have attached annotated images to show exactly what I mean.", "",
                 "Thank you for your time.", "", "Best regards,"] + sign
    return {"kind": kind, "label": TEMPLATES[kind]["label"], "subject": subject, "body": "\n".join(body),
            "included": used, "empty": empty}


def templates() -> list[dict]:
    return [{"id": k, **v} for k, v in TEMPLATES.items()]
