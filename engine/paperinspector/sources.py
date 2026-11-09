"""Everything that belongs to one paper, read into one place: the article, its figures, its tables, and each
supplementary file (PDF, spreadsheet, Word, image, archive) - with a status line per file so the report can say
exactly what was and was not examined.
"""
from __future__ import annotations

import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

import numpy as np

from . import pdfdoc, tables as tbl
from .imgutil import read_image
from .models import Box, Figure, Paper

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
TABLE_EXT = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt"}
ANALYSED_EXT = IMAGE_EXT | TABLE_EXT | {".pdf", ".docx"}
# one archive: only the first MAX_MEMBERS files are analysed, so only those are written; the byte budget and the
# compression-ratio limit stop a zip bomb from filling the disk
MAX_MEMBERS = 30
MAX_SCANNED = 2000
MAX_MEMBER_BYTES = 150 * 2 ** 20
MAX_ARCHIVE_BYTES = 2 ** 30
MAX_RATIO = 200


@dataclass
class SourceFile:
    name: str
    label: str
    kind: str                 # article | pdf | spreadsheet | document | image | archive | other
    status: str               # analysed | not analysed: ...
    origin: str = ""          # upload | PMC | Europe PMC | publisher page
    figures: int = 0
    tables: int = 0
    path: Optional[Path] = None
    exif: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"name": self.name, "label": self.label, "kind": self.kind, "status": self.status,
                "origin": self.origin, "figures": self.figures, "tables": self.tables,
                **({"exif": self.exif} if self.exif else {})}


@dataclass
class Sources:
    paper: Paper
    tables: list[tbl.Table] = field(default_factory=list)
    supp_papers: list[Paper] = field(default_factory=list)     # text of supplementary PDFs/Word files
    files: list[SourceFile] = field(default_factory=list)


def _image_figure(img: np.ndarray, fid: str, label: str, caption: str = "") -> Figure:
    h, w = img.shape[:2]
    return Figure(id=fid, label=label, page=0, page_box=Box(0, 0, w, h), caption=caption, image=img, dpi=0)


def _read_image(path: Path) -> Optional[np.ndarray]:
    return read_image(path)


def _exif(path: Path) -> dict:
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        with Image.open(path) as im:
            ex = im.getexif()
            out = {TAGS.get(k, str(k)): str(v)[:120] for k, v in ex.items()
                   if TAGS.get(k) in ("Software", "DateTime", "Make", "Model", "Artist")}
            if b"photoshop" in (im.info.get("icc_profile") or b"").lower():
                out["Profile"] = "Photoshop"
            return out
    except Exception:
        return {}


def load(main: Optional[Path], supplements: list[tuple[Path, str, str]], meta: Optional[dict] = None,
         xml: Optional[Path] = None, jats_figures: Optional[list[tuple[Path, str, str]]] = None) -> Sources:
    """main: article PDF (or None); supplements: (path, label, origin); jats_figures: (path, label, caption)."""
    meta = meta or {}
    if main is not None:
        paper = pdfdoc.load(main)
    else:
        paper = Paper(source=meta.get("doi", "article"), pages=[""], page_count=0)
    if xml is not None and (main is None or not paper.text.strip()):
        paper.pages = [_jats_text(xml)]
    if meta.get("title"):
        paper.title = meta["title"]
    if meta.get("doi"):
        paper.doi = meta["doi"]
    src = Sources(paper)
    src.files.append(SourceFile(main.name if main else "(no PDF)", "Article", "article",
                                "analysed" if main else "not analysed: no open PDF found - upload it to check figures",
                                "upload" if not meta else "open access", len(paper.figures)))

    # original figure files from PMC are sharper than what is printed in the PDF; prefer them
    if jats_figures:
        figs = []
        for k, (p, label, caption) in enumerate(jats_figures):
            img = _read_image(p)
            if img is None:
                continue
            fid = _fig_id(label, k, stem=p.stem)
            figs.append(_image_figure(img, fid, label or p.stem, caption))
        _unique_ids(figs)
        if figs:
            paper.figures = figs
            src.files[0].figures = len(figs)
            src.files[0].status = "analysed (figures from the original image files)"

    if xml is not None:
        xml_text = xml.read_text(errors="replace")
        src.tables += tbl.read_jats_tables(xml_text, "")
        n_refs = len(re.findall(r"<ref[\s>]", xml_text))
        if n_refs:
            meta["printed_refs"] = n_refs
    elif main is not None:
        src.tables += tbl.read_pdf_tables(main, "")

    for k, (p, label, origin) in enumerate(supplements):
        _supplement(src, p, label or p.name, origin, f"S{k + 1}", depth=0)
    _unique_ids(src.paper.figures)
    return src


_DASH = r"\s*[-‐‑–—]+\s*"
_FIG = r"fig(?:ure)?s?\.?\s*"
_FIG_IDS = [      # the whole label names the figure: "Extended Data Fig. 1" is not Figure 1
    (re.compile(r"extended\s+data\s+" + _FIG + r"(\d+)", re.I), "ED{}"),
    (re.compile(r"appendix\s*(\d+)" + _DASH + _FIG + r"(\d+)", re.I), "A{}-F{}"),
    (re.compile(_FIG + r"(S?\d+)" + _DASH + r"(?:" + _FIG + r")?supplement(?:ary)?\s*(?:" + _FIG + r")?(\d+)", re.I),
     "F{}-S{}"),
    (re.compile(r"supplementa(?:l|ry)\s+" + _FIG + r"S?(\d+)", re.I), "FS{}"),
    (re.compile(_FIG + r"(S?\d+)", re.I), "F{}"),
    (re.compile(r"^\s*(S?\d+)\s*[.:]?\s*$", re.I), "F{}"),
]


def _fig_id(label: str, k: int, stem: str = "") -> str:
    """Figure id from a JATS label.  A label that is only the file name (PMC passes the stem when the XML has no
    <label>) says nothing except PLOS's 'g001' numbering; its other digits are an article number shared by every
    figure."""
    if label and label != stem:
        for rx, fmt in _FIG_IDS:
            m = rx.search(label)
            if m:
                return fmt.format(*(g.upper().lstrip("0") or "0" for g in m.groups()))
    m = re.search(r"\.g0*(\d+)$", stem or label or "")
    return f"F{m.group(1)}" if m else f"F{k + 1}"


def _unique_ids(figs: list[Figure]) -> None:
    """Figure ids name files (figures/<id>.png), panels and evidence, so two figures must never share one."""
    used: set[str] = set()
    for f in figs:
        base, n = f.id, 1
        while f.id in used:
            n += 1
            f.id = f"{base}-{n}"
        used.add(f.id)


def _supplement(src: Sources, p: Path, label: str, origin: str, prefix: str, depth: int) -> None:
    ext = p.suffix.lower()
    sf = SourceFile(p.name, label, "other", "", origin, path=p)
    src.files.append(sf)
    try:
        if ext == ".pdf":
            sp = pdfdoc.load(p)
            sf.kind = "pdf"
            for f in sp.figures:
                f.id = f"{prefix}-{f.id}"
                f.label = f"{label}: {f.label}"
                src.paper.figures.append(f)
            sp.source = p.name
            src.supp_papers.append(sp)
            t = tbl.read_pdf_tables(p, p.name, max_pages=40)
            src.tables += t
            sf.figures, sf.tables, sf.status = len(sp.figures), len(t), "analysed"
        elif ext in TABLE_EXT:
            t = tbl.read_any(p, p.name)
            sf.kind, sf.tables = "spreadsheet", len(t)
            src.tables += t
            sf.status = "analysed" if t else "not analysed: could not read it as a table"
        elif ext == ".docx":
            t, text, imgs = tbl.read_docx(p, p.name)
            sf.kind = "document"
            src.tables += t
            src.supp_papers.append(Paper(source=p.name, pages=[text], page_count=1))
            for k, data in enumerate(imgs[:40]):
                img = read_image(data)
                if img is not None and min(img.shape[:2]) >= 80:
                    src.paper.figures.append(_image_figure(img, f"{prefix}-img{k + 1}", f"{label}: image {k + 1}"))
                    sf.figures += 1
            sf.tables, sf.status = len(t), "analysed"
        elif ext in IMAGE_EXT:
            img = _read_image(p)
            sf.kind = "image"
            if img is None:
                sf.status = "not analysed: unreadable image"
            else:
                src.paper.figures.append(_image_figure(img, prefix, label))
                pages = _page_count(p) if ext in (".tif", ".tiff") else 1
                sf.figures, sf.status = 1, "analysed" + (f" (page 1 of {pages})" if pages > 1 else "")
                sf.exif = _exif(p)
        elif depth == 0 and (zipfile.is_zipfile(p) or ext in (".gz", ".tgz") and tarfile.is_tarfile(p)):
            sf.kind = "archive"
            out = p.parent / (p.stem + "_unpacked")
            out.mkdir(exist_ok=True)
            members, skipped = _unpack(p, out)
            sf.status = f"unpacked: {len(members)} files" + (f" ({skipped} more not examined)" if skipped else "")
            for k, (m, inner) in enumerate(members):
                _supplement(src, m, f"{label} / {inner}", origin, f"{prefix}{_letters(k)}", depth + 1)
        elif ext == ".doc":
            sf.kind, sf.status = "document", "not analysed: old .doc format (save as .docx to include it)"
        else:
            sf.status = f"not analysed: {ext or 'unknown'} files are not supported"
    except Exception as e:                      # one bad file must not sink the whole report
        sf.status = f"not analysed: {type(e).__name__}"


def _letters(k: int) -> str:
    """a..z, then aa, ab, ...: archive members never share a prefix."""
    out = ""
    k += 1
    while k:
        k, r = divmod(k - 1, 26)
        out = chr(97 + r) + out
    return out


def _page_count(p: Path) -> int:
    """Pages of a multi-page TIFF.  Only the first is screened: the others are usually channels or z-slices of the
    same field, which would match each other as 'duplicates'."""
    try:
        from PIL import Image
        with Image.open(p) as im:
            return int(getattr(im, "n_frames", 1) or 1)
    except Exception:
        return 1


def _unpack(p: Path, out: Path) -> tuple[list[tuple[Path, str]], int]:
    """Write an archive's files into `out` and return [(file, path inside the archive)] plus how many files were
    left out.  Folder names are dropped but every file keeps its own name (Fig1/blot.tif and Fig2/blot.tif are two
    files); nothing is written outside `out`.  Members are copied in chunks against a per-file and a per-archive
    byte budget, and a member that claims an absurd compression ratio is skipped unread."""
    kept: list[tuple[Path, str]] = []
    used: set[str] = set()
    budget = [MAX_ARCHIVE_BYTES]
    skipped = 0

    def wanted(name: str) -> bool:
        parts = PurePosixPath(name.replace("\\", "/")).parts
        return bool(parts) and "__MACOSX" not in parts and not parts[-1].startswith(".")

    def dest_for(name: str) -> Path:
        parts = [q for q in PurePosixPath(name.replace("\\", "/")).parts if q not in ("", ".", "..", "/")]
        base = "".join(c if c.isalnum() or c in "._- " else "_" for c in (parts[-1] if parts else "file"))[:120]
        base = base.lstrip(". ") or "file"
        cand, n = base, 1
        while cand.lower() in used:
            n += 1
            stem, dot, ext = base.rpartition(".")
            cand = f"{stem}_{n}.{ext}" if dot and stem else f"{base}_{n}"
        used.add(cand.lower())
        return out / cand

    def copy(fh, dest: Path, declared: int) -> bool:
        limit = min(MAX_MEMBER_BYTES, budget[0], declared if declared >= 0 else MAX_MEMBER_BYTES)
        n = 0
        try:
            with open(dest, "wb") as fo:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > limit:
                        raise ValueError("member larger than declared or over budget")
                    fo.write(chunk)
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError, EOFError):
            dest.unlink(missing_ok=True)
            return False
        budget[0] -= n
        return True

    def order(items, name):
        # files we can analyse first, so a README or a movie does not use up the 30 places
        return sorted(items, key=lambda it: Path(name(it)).suffix.lower() not in ANALYSED_EXT)

    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            infos = [i for i in z.infolist()[:MAX_SCANNED] if not i.is_dir() and wanted(i.filename)]
            for info in order(infos, lambda i: i.filename):
                ratio = info.file_size / max(info.compress_size, 1)
                if len(kept) >= MAX_MEMBERS or info.file_size > min(MAX_MEMBER_BYTES, budget[0]) \
                        or info.file_size > 2 ** 20 and ratio > MAX_RATIO:
                    skipped += 1
                    continue
                dest = dest_for(info.filename)
                try:
                    with z.open(info) as fh:
                        ok = copy(fh, dest, info.file_size)
                except (RuntimeError, NotImplementedError, OSError, zipfile.BadZipFile):    # encrypted, odd codec
                    ok = False
                if ok:
                    kept.append((dest, info.filename))
                else:
                    skipped += 1
    else:
        with tarfile.open(p) as t:
            members, seen = [], 0
            for k, m in enumerate(t):            # iterate: getmembers() would index a million-entry bomb first
                seen += m.size
                if k >= MAX_SCANNED or seen > 8 * MAX_ARCHIVE_BYTES:
                    break                        # reaching the next header means inflating everything before it
                if m.isfile() and wanted(m.name):
                    members.append(m)
            for m in order(members, lambda m: m.name):
                if len(kept) >= MAX_MEMBERS or m.size > min(MAX_MEMBER_BYTES, budget[0]):
                    skipped += 1
                    continue
                fh = t.extractfile(m)
                if fh is None:
                    continue
                dest = dest_for(m.name)
                with fh:
                    if copy(fh, dest, m.size):
                        kept.append((dest, m.name))
                    else:
                        skipped += 1
    return kept, skipped


def _jats_text(xml: Path) -> str:
    s = xml.read_text(errors="replace")
    s = re.sub(r"<(xref|sup|sub)[^>]*>", "", s)
    s = re.sub(r"</(p|title|sec|caption|td|th|tr|ref)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s


def copy_into(run: Path, files: list[SourceFile]) -> None:
    d = run / "supplements"
    d.mkdir(exist_ok=True)
    for f in files:
        if f.path is not None and f.path.exists() and f.kind != "archive":
            try:
                shutil.copy(f.path, d / f.path.name)
            except OSError:
                pass
