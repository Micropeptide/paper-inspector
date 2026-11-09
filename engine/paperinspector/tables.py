"""Read tables from wherever a paper keeps them: spreadsheets, CSV/TSV, Word documents, JATS XML, PDF pages.

Every table becomes a `Table`: a grid of cell strings plus where it came from, so a finding can say
"Supplementary Data 2.xlsx, sheet 'Fig3', cells C4:C40".
"""
from __future__ import annotations

import codecs
import csv
import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# a thousands group never starts with 0: '0,125' is a decimal comma, not 125
NUM_RE = re.compile(r"^[\s(\ufeff]*([-−–]?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?|[-−–]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
                    r"[\s)%*†‡]*$")
DECIMAL_COMMA_RE = re.compile(r"^\s*[-−–]?(?:\d{1,3}(?:[.\s]\d{3})+|\d+),\d+\s*%?\s*$")
MAX_ROWS, MAX_COLS = 20000, 400
MAX_CELLS = 4_000_000                 # rows x columns kept from one sheet
MAX_XML = 300_000_000                 # uncompressed sheet XML a workbook may hold (streamed)
MAX_STRINGS = 60_000_000              # uncompressed shared-strings XML (loaded whole by openpyxl)
MAX_DOCX_IMAGES, MAX_DOCX_IMAGE = 40, 60_000_000


@dataclass
class Table:
    name: str                    # sheet or table caption
    file: str                    # file it came from ("" = the article)
    rows: list[list[str]]
    hidden: bool = False         # a hidden sheet
    formulas: dict[str, str] = field(default_factory=dict)   # "C4" -> "=RAND()*10"
    hidden_rows: list[int] = field(default_factory=list)
    hidden_cols: list[int] = field(default_factory=list)
    meta: dict = field(default_factory=dict)                  # spreadsheet author, dates
    fonts: dict[int, list[tuple[int, str]]] = field(default_factory=dict)   # column -> [(row, font)] of numbers

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.rows), max((len(r) for r in self.rows), default=0)

    def column(self, j: int) -> list[str]:
        return [r[j] if j < len(r) else "" for r in self.rows]


def number(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s)
    m = NUM_RE.match(str(s))
    if not m:
        return None
    t = m.group(1).replace(",", "").replace("−", "-").replace("–", "-")
    try:
        return float(t)
    except ValueError:
        return None


def col_name(j: int) -> str:
    s = ""
    j += 1
    while j:
        j, r = divmod(j - 1, 26)
        s = chr(65 + r) + s
    return s


def cell_ref(i: int, j: int) -> str:
    return f"{col_name(j)}{i + 1}"


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        return repr(v) if v != int(v) or abs(v) >= 1e15 else str(int(v))
    if hasattr(v, "isoformat"):
        return "DATE:" + v.isoformat()[:10]      # dates are marked: gene names turned into dates show up here
    return str(v)


def _xlsx_budget(path: Path) -> bool:
    """False for a workbook whose sheets inflate past what a scan should load (a few MB of zip can hold GBs of XML)."""
    try:
        with zipfile.ZipFile(path) as z:
            info = z.infolist()
    except (zipfile.BadZipFile, OSError):
        return False
    sheets = sum(i.file_size for i in info if i.filename.startswith("xl/worksheets/"))
    strings = sum(i.file_size for i in info if i.filename.startswith("xl/sharedStrings"))
    return sheets <= MAX_XML and strings <= MAX_STRINGS


def _hidden_dims(z: zipfile.ZipFile, member: str, max_row: int, max_col: int) -> tuple[list[int], list[str]]:
    """Hidden row numbers and column letters, streamed from the sheet XML (read-only workbooks do not keep them)."""
    import xml.etree.ElementTree as ET

    rows: list[int] = []
    cols: list[str] = []
    try:
        with z.open(member) as fh:
            for _, el in ET.iterparse(fh):
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "col" and el.get("hidden") in ("1", "true"):
                    lo, hi = int(el.get("min", "0")), int(el.get("max", "0"))
                    cols += [col_name(j - 1) for j in range(max(lo, 1), min(hi, max_col) + 1)]
                elif tag == "row":
                    r = int(el.get("r", "0") or 0)
                    if r > max_row:
                        break
                    if el.get("hidden") in ("1", "true"):
                        rows.append(r)
                    el.clear()
                elif tag == "c":
                    el.clear()
    except (KeyError, ValueError, ET.ParseError, OSError):
        pass
    return rows[:500], cols[:200]


MAX_META_XML = 2_000_000              # docProps/*.xml, workbook.xml, link relationship files
MAX_CALC_XML = 50_000_000             # xl/calcChain.xml (streamed)
MAX_CHAIN = 200_000                   # calculation-chain entries kept per workbook


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _zip_xml(z: zipfile.ZipFile, name: str):
    import xml.etree.ElementTree as ET

    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_META_XML:
        return None
    try:
        return ET.fromstring(z.read(info))
    except (ET.ParseError, OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return None


def _ooxml_meta(z: zipfile.ZipFile, kind: str) -> dict:
    """Program and dates an Office file records about itself (docProps/core.xml and app.xml).  Person names
    (creator, last saved by) are deliberately not read: the report never needs them."""
    out: dict = {"kind": kind}
    core = _zip_xml(z, "docProps/core.xml")
    if core is not None:
        for el in core:
            tag, text = _local(el.tag), (el.text or "").strip()
            if tag in ("created", "modified") and text:
                out[tag] = text[:25]
            elif tag == "revision" and text.isdigit():
                out["revision"] = int(text)
    app = _zip_xml(z, "docProps/app.xml")
    if app is not None:
        for el in app:
            tag, text = _local(el.tag), (el.text or "").strip()
            if tag == "Application" and text:
                out["app"] = text[:80]
            elif tag == "AppVersion" and text:
                out["app_version"] = text[:20]
            elif tag == "TotalTime" and text.isdigit():
                out["edit_minutes"] = int(text)
    return out


def _pdf_date(s: str) -> str:
    """'D:20210304101500+01'00'' -> '2021-03-04' (empty when it does not parse)."""
    m = re.match(r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?", (s or "").strip())
    if not m:
        return ""
    y, mo, d = m.group(1), m.group(2) or "01", m.group(3) or "01"
    return f"{y}-{mo}-{d}"


def file_meta(path: Path) -> dict:
    """What a supplementary file says about how and when it was made: program, created and last-saved dates.
    Works for Office files (.xlsx, .xlsm, .docx, .pptx) and PDFs; anything else, or a damaged file, gives {}."""
    ext = path.suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm", ".docx", ".pptx"):
            with zipfile.ZipFile(path) as z:
                return _ooxml_meta(z, ext[1:])
        if ext == ".pdf":
            import pymupdf

            with pymupdf.open(str(path)) as doc:
                m = doc.metadata or {}
            out = {"kind": "pdf", "app": (m.get("creator") or "")[:80], "producer": (m.get("producer") or "")[:80],
                   "created": _pdf_date(m.get("creationDate") or ""), "modified": _pdf_date(m.get("modDate") or "")}
            return {k: v for k, v in out.items() if v}
    except Exception:
        return {}
    return {}


def _calc_chains(z: zipfile.ZipFile) -> dict[str, list[str]]:
    """Excel's calculation chain (xl/calcChain.xml): every formula cell, per sheet, in the order Excel keeps them.
    Formulas filled down a column enter the chain in row order and keep their place when a row is later cut and
    pasted elsewhere, so the chain can show rows that were moved."""
    import xml.etree.ElementTree as ET

    try:
        info = z.getinfo("xl/calcChain.xml")
    except KeyError:
        return {}
    if info.file_size > MAX_CALC_XML:
        return {}
    names: dict[str, str] = {}
    wb = _zip_xml(z, "xl/workbook.xml")
    if wb is not None:
        for el in wb.iter():
            if _local(el.tag) == "sheet" and el.get("sheetId"):
                names[el.get("sheetId")] = el.get("name", "")
    out: dict[str, list[str]] = {}
    sheet, n = "", 0
    try:
        with z.open(info) as fh:
            for _, el in ET.iterparse(fh):
                if _local(el.tag) != "c":
                    continue
                sheet = el.get("i") or sheet
                ref = el.get("r") or ""
                if ref and sheet:
                    out.setdefault(names.get(sheet, ""), []).append(ref.replace("$", "").upper())
                    n += 1
                el.clear()
                if n >= MAX_CHAIN:
                    break
    except (ET.ParseError, OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return {}
    out.pop("", None)
    return out


def _external_links(z: zipfile.ZipFile) -> list[str]:
    """Names of other workbooks this one pulls values from (file names only, never their folders)."""
    from urllib.parse import unquote

    out = []
    for name in z.namelist()[:5000]:
        if not (name.startswith("xl/externalLinks/_rels/") and name.endswith(".rels")):
            continue
        root = _zip_xml(z, name)
        if root is None:
            continue
        for el in root.iter():
            if _local(el.tag) == "Relationship" and (el.get("TargetMode") or "").lower() == "external":
                target = unquote(el.get("Target") or "")
                base = re.split(r"[\\/]", target.rstrip("/\\"))[-1][:120]
                if base and base not in out:
                    out.append(base)
    return out[:20]


def read_xlsx(path: Path, label: str) -> list[Table]:
    """Values, formulas, fonts and hidden rows of each sheet, streamed in read-only mode and bounded to
    MAX_ROWS x MAX_COLS, so a small but hostile file cannot expand to gigabytes of cell objects."""
    import openpyxl

    out = []
    if not _xlsx_budget(path):
        return out
    try:
        wb_v = openpyxl.load_workbook(path, data_only=True, read_only=True)
        wb_f = openpyxl.load_workbook(path, data_only=False, read_only=True)
    except Exception:
        return out
    try:
        props = wb_v.properties
        meta = {"creator": props.creator or "", "last_modified_by": props.lastModifiedBy or "",
                "created": str(props.created or ""), "modified": str(props.modified or "")}
        with zipfile.ZipFile(path) as z:
            try:
                meta.update({k: v for k, v in _ooxml_meta(z, path.suffix.lower()[1:]).items()
                             if k not in ("created", "modified") or not meta.get(k)})
                chains = _calc_chains(z)
                links = _external_links(z)
            except Exception:             # the internals are a bonus; the values must still be read
                chains, links = {}, []
            if links:
                meta["external_links"] = links
            for ws in wb_v.worksheets:
                if not hasattr(ws, "iter_rows"):
                    continue              # chartsheets
                width = min(ws.max_column or MAX_COLS, MAX_COLS)
                if width <= 1:
                    width = MAX_COLS      # a missing or wrong dimension tag; trailing blanks are trimmed below
                rows, cells = [], 0
                for r in ws.iter_rows(max_row=MAX_ROWS, max_col=width, values_only=True):
                    row = [_cell_str(v) for v in r]
                    while row and not row[-1]:
                        row.pop()
                    rows.append(row)
                    cells += len(row)
                    if cells > MAX_CELLS:
                        break
                max_row = len(rows)
                while rows and not any(rows[-1]):
                    rows.pop()
                if not rows:
                    continue
                ncols = max(len(r) for r in rows)
                rows = [r + [""] * (ncols - len(r)) for r in rows]
                wf = wb_f[ws.title]
                formulas = {}
                # formulas without a cached value (files saved by scripts) read as blanks above: scan the full width
                for i, r in enumerate(wf.iter_rows(max_row=max_row, max_col=width, values_only=True)):
                    for j, v in enumerate(r):
                        if isinstance(v, str) and v.startswith("="):
                            formulas[cell_ref(i, j)] = v[:200]
                fonts: dict[int, list[tuple[int, str]]] = {}
                for r in wf.iter_rows(max_row=min(len(rows), 5000), max_col=min(ncols, 60)):
                    for c in r:
                        v = getattr(c, "value", None)
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            try:
                                f = c.font
                            except (AttributeError, IndexError):
                                continue
                            if f is not None:
                                fonts.setdefault(c.column - 1, []).append((c.row - 1, f"{f.name}/{f.sz}"))
                hid_r, hid_c = _hidden_dims(z, getattr(ws, "_worksheet_path", ""), len(rows), ncols)
                sheet_meta = {**meta, "calc_chain": chains[ws.title]} if ws.title in chains else meta
                out.append(Table(ws.title, label, rows, hidden=ws.sheet_state != "visible", formulas=formulas,
                                 hidden_rows=hid_r, hidden_cols=hid_c, meta=sheet_meta, fonts=fonts))
    except Exception:
        return out
    finally:
        wb_v.close()
        wb_f.close()
    return out


def read_xls(path: Path, label: str) -> list[Table]:
    try:
        import xlrd  # optional
    except ImportError:
        return []
    out = []
    try:
        wb = xlrd.open_workbook(str(path))
    except Exception:
        return out
    for sh in wb.sheets():
        rows = [[_cell_str(sh.cell_value(i, j)) for j in range(min(sh.ncols, MAX_COLS))]
                for i in range(min(sh.nrows, MAX_ROWS))]
        out.append(Table(sh.name, label, rows))
    return out


def _decode(b: bytes) -> str:
    """Text of a delimited file: Excel's 'Unicode Text' export is UTF-16, older exports are cp1252."""
    if b.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return b.decode("utf-16", errors="replace")
    if b.startswith(codecs.BOM_UTF8):
        return b[3:].decode("utf-8", errors="replace")
    head = b[:4000]
    if head and head.count(0) > 0.3 * len(head):          # UTF-16 without a BOM
        return b.decode("utf-16-le" if head[1::2].count(0) > head[0::2].count(0) else "utf-16-be", errors="replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace")


def _decimal_comma(rows: list[list[str]]) -> bool:
    """A table from a German or French Excel: '12,5' and '0,125' mean 12.5 and 0.125.  Decided per table, and only
    on unambiguous evidence ('1,234' alone could be a thousands separator)."""
    comma = [c for r in rows[:2000] for c in r if "," in c and DECIMAL_COMMA_RE.match(c)]
    if len(comma) < 5:
        return False
    clear = sum(1 for c in comma if not re.search(r",\d{3}\s*%?\s*$", c) or re.match(r"^\s*[-−–]?0,", c))
    dotted = sum(1 for r in rows[:2000] for c in r if re.match(r"^\s*[-−–]?\d+\.\d+\s*$", c))
    return clear >= 0.2 * len(comma) and dotted < 0.1 * len(comma)


def _from_comma(c: str) -> str:
    return c.replace(".", "").replace(" ", "").replace(",", ".") if DECIMAL_COMMA_RE.match(c) else c


def read_delimited(path: Path, label: str) -> list[Table]:
    raw = _decode(path.read_bytes()[:50_000_000])
    try:
        dialect = csv.Sniffer().sniff(raw[:20000], delimiters=",\t;|")
        delim = dialect.delimiter
    except csv.Error:
        delim = "\t" if raw.count("\t") > raw.count(",") else ","
    rows = list(csv.reader(io.StringIO(raw), delimiter=delim))[:200000]
    if not rows:
        return []
    meta = {}
    if delim != "," and _decimal_comma(rows):
        rows = [[_from_comma(c) for c in r] for r in rows]
        meta["decimal"] = ","
    return [Table(path.stem, label, rows, meta=meta)]


def read_docx(path: Path, label: str) -> tuple[list[Table], str, list[bytes]]:
    """Tables, body text and embedded images of a Word file."""
    import docx

    try:
        d = docx.Document(str(path))
    except Exception:
        return [], "", []
    text = "\n".join(p.text for p in d.paragraphs)
    tables = []
    for k, t in enumerate(d.tables):
        rows = []
        for r in t.rows:
            # a merged cell is returned once per grid column it spans; keep its text in the first one only
            seen: set[int] = set()
            row = []
            for c in r.cells:
                row.append("" if id(c._tc) in seen else c.text.strip())
                seen.add(id(c._tc))
            rows.append(row)
        tables.append(Table(f"Table {k + 1}", label, rows))
    images = []
    with zipfile.ZipFile(path) as z:
        try:
            fm = _ooxml_meta(z, "docx")
        except Exception:
            fm = {}
        for t in tables:
            t.meta.update(fm)
        for i in z.infolist():
            # the caller keeps 40; a crafted file must not make us inflate every entry first
            if len(images) >= MAX_DOCX_IMAGES:
                break
            if i.filename.startswith("word/media/") and i.file_size <= MAX_DOCX_IMAGE and \
                    Path(i.filename).suffix.lower() in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
                images.append(z.read(i))
    return tables, text, images


def read_jats_tables(xml: str, label: str = "") -> list[Table]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml.encode())
    except ET.ParseError:
        return []
    out = []
    for tw in root.iter("table-wrap"):
        lab = "".join(tw.find("label").itertext()).strip() if tw.find("label") is not None else "Table"
        for t in tw.iter("table"):
            rows = []
            pending: dict[int, int] = {}          # column -> rows still covered by a rowspan from above
            for tr in t.iter("tr"):
                row: list[str] = []
                for c in tr:
                    if c.tag in ("td", "th"):
                        _skip_spanned(row, pending)
                        col, rspan = len(row), _span(c.get("rowspan"))
                        row.append(" ".join("".join(c.itertext()).split()))
                        row += [""] * (_span(c.get("colspan")) - 1)
                        if rspan > 1:
                            pending.update({j: rspan - 1 for j in range(col, len(row))})
                _skip_spanned(row, pending)
                rows.append(row[:MAX_COLS])
                if len(rows) >= MAX_ROWS:
                    break
            if rows:
                out.append(Table(lab, label, rows))
    return out


def _skip_spanned(row: list[str], pending: dict[int, int]) -> None:
    """Hold the places of cells merged down from an earlier row, so later columns stay aligned."""
    while pending.get(len(row), 0) > 0:
        pending[len(row)] -= 1
        row.append("")


def _span(v) -> int:
    """A colspan/rowspan attribute, defensively: '2.0', 'inf' and '20000000' must not crash or allocate."""
    try:
        return max(1, min(int(float(v or 1)), 200))
    except (ValueError, OverflowError):
        return 1


def read_pdf_tables(pdf_path: Path, label: str = "", max_pages: int = 60) -> list[Table]:
    import pymupdf

    out = []
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        return out
    for pno, page in enumerate(doc):
        if pno >= max_pages:
            break
        try:
            found = page.find_tables()
        except Exception:
            continue
        for k, t in enumerate(found.tables):
            rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in t.extract()]
            if len(rows) >= 3 and max(len(r) for r in rows) >= 2:
                out.append(Table(f"page {pno + 1} table {k + 1}", label, rows, meta={"page": pno + 1}))
    if out:
        try:
            m = doc.metadata or {}
            fm = {"kind": "pdf", "app": (m.get("creator") or "")[:80], "producer": (m.get("producer") or "")[:80],
                  "created": _pdf_date(m.get("creationDate") or ""), "modified": _pdf_date(m.get("modDate") or "")}
            for t in out:
                t.meta.update({k: v for k, v in fm.items() if v})
        except Exception:
            pass
    doc.close()
    return out


def read_any(path: Path, label: str) -> list[Table]:
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(path, label)
    if ext == ".xls":
        return read_xls(path, label)
    if ext in (".csv", ".tsv", ".txt"):
        return read_delimited(path, label)
    if ext == ".docx":
        return read_docx(path, label)[0]
    return []
