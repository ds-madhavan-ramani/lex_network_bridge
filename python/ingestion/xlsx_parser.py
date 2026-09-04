"""
xlsx_parser.py — native XLSX parsing for spreadsheet-sourced documents (e.g.
a securities/BG-and-cash register), bypassing AI_PARSE_DOCUMENT.

Unchanged from the project-llm-wiki template.
AI_PARSE_DOCUMENT is OCR/layout-oriented and not built for tabular data — a
register's rows are structured data, not a scanned page, so read them
directly.

Deliberately stdlib-only (zipfile + xml.etree.ElementTree) rather than
pandas/openpyxl: adding `openpyxl` to environment.yml broke the deployed
Streamlit app outright on this account — it failed at load, before any page
code ran, with Snowflake's generic sandbox-bootstrap error ("Python
Interpreter Error: TypeError: bad argument type for built-in operation"),
because openpyxl isn't resolvable in this account's Anaconda channel
snapshot for warehouse-runtime Streamlit apps. An .xlsx file is just a zip
of XML parts (worksheets + a shared-string table), so no third-party
package is actually needed to read one.

The serialized text still lands in RAW_DOCUMENTS.RAW_TEXT and stays
addressable by the character offsets DOCUMENT_INDEX.NODE_TEXT_REF already
uses, so no schema change is needed downstream.
"""

import io
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

XLSX_EXTENSIONS = (".xlsx", ".xlsm")

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def is_xlsx(file_name: str) -> bool:
    return file_name.lower().endswith(XLSX_EXTENSIONS)


def normalize_token(s: str) -> str:
    """Lowercases and strips everything but letters/digits, so 'File Name',
    'File_Name', 'FILE-NAME' and 'filename' all compare equal — used for
    matching column headers and register/actual file names, which are
    typed inconsistently across a workbook maintained by hand."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _col_index(cell_ref: str) -> int:
    """'C7' -> 2 (0-based column index)."""
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
    return idx - 1


def _load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings = []
    for si in root.findall(f"{{{_MAIN_NS}}}si"):
        # Concatenate every <t> run so rich-text cells (multiple <r><t>) still
        # come through as one string.
        strings.append("".join(t.text or "" for t in si.iter(f"{{{_MAIN_NS}}}t")))
    return strings


def _load_sheets(zf: zipfile.ZipFile) -> Dict[str, str]:
    """Returns {sheet_name: 'xl/worksheets/sheetN.xml'} in workbook order."""
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {
        rel.get("Id"): rel.get("Target")
        for rel in rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    sheets = {}
    for sheet in wb_root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet"):
        name = sheet.get("name") or ""
        rid = sheet.get(f"{{{_DOC_REL_NS}}}id")
        target = rid_to_target.get(rid, "")
        # Targets come as either an absolute in-archive path ("/xl/worksheets/
        # sheet1.xml") or a path relative to xl/ ("worksheets/sheet1.xml") —
        # normalize both to the zip member name.
        if target.startswith("/"):
            target = target.lstrip("/")
        elif target and not target.startswith("xl/"):
            target = f"xl/{target}"
        if target in zf.namelist():
            sheets[name] = target
    return sheets


def _cell_value(cell: ET.Element, shared: List[str]) -> Optional[str]:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        is_el = cell.find(f"{{{_MAIN_NS}}}is")
        if is_el is None:
            return None
        text = "".join(t.text or "" for t in is_el.iter(f"{{{_MAIN_NS}}}t"))
        return text or None
    v_el = cell.find(f"{{{_MAIN_NS}}}v")
    if v_el is None or v_el.text is None:
        return None
    if cell_type == "s":
        idx = int(v_el.text)
        return shared[idx] if 0 <= idx < len(shared) else None
    return v_el.text


def _parse_rows(zf: zipfile.ZipFile, sheet_path: str, shared: List[str]) -> List[List[Optional[str]]]:
    root = ET.fromstring(zf.read(sheet_path))
    rows: List[List[Optional[str]]] = []
    for row_el in root.findall(f"{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row"):
        by_col: Dict[int, str] = {}
        max_col = -1
        next_col = 0
        for cell in row_el.findall(f"{{{_MAIN_NS}}}c"):
            ref = cell.get("r", "")
            col = _col_index(ref) if ref else next_col
            next_col = col + 1
            value = _cell_value(cell, shared)
            if value not in (None, ""):
                by_col[col] = value
                max_col = max(max_col, col)
        if max_col >= 0:
            rows.append([by_col.get(i) for i in range(max_col + 1)])
    return rows


def parse_xlsx_to_text(raw_bytes: bytes) -> str:
    """
    Reads every sheet and serializes it to offset-addressable plain text: one
    '=== Sheet: <name> ===' header per sheet, then one line per data row as
    'Header: value | Header: value | ...' using row 1 as the header row.
    Fully blank rows/cells are dropped so the segmentation prompt sees dense
    text.
    """
    parts: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        shared = _load_shared_strings(zf)
        for sheet_name, sheet_path in _load_sheets(zf).items():
            rows = _parse_rows(zf, sheet_path, shared)
            if not rows:
                continue
            header = rows[0]
            parts.append(f"=== Sheet: {sheet_name} ===")
            for row in rows[1:]:
                cells = []
                for i, value in enumerate(row):
                    if value in (None, ""):
                        continue
                    col_name = header[i] if i < len(header) and header[i] else f"Column {i + 1}"
                    cells.append(f"{col_name}: {value}")
                if cells:
                    parts.append(" | ".join(cells))
            parts.append("")
    return "\n".join(parts)


_HEADER_SCAN_ROWS = 10  # a register sheet may have a title/merged group-header
# row (or several) above the real header row, so don't assume row 1 is the
# header — search the first few rows for one that actually looks like it.


def _find_header_row(rows: List[List[Optional[str]]], wanted_tokens: set):
    """Returns (row_index, [matching column indices]) for the first of the
    first _HEADER_SCAN_ROWS rows containing a cell matching wanted_tokens,
    or (None, []) if none do."""
    for r_idx, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        col_idxs = [i for i, h in enumerate(row) if h and normalize_token(h) in wanted_tokens]
        if col_idxs:
            return r_idx, col_idxs
    return None, []


def list_headers(raw_bytes: bytes, max_rows: int = _HEADER_SCAN_ROWS) -> Dict[str, List[List[str]]]:
    """Returns {sheet_name: [[row1 cells], [row2 cells], ...]} for the first
    max_rows of every sheet. A diagnostic for figuring out why
    extract_column_values() found no matching column in a workbook whose
    exact header text/row isn't known in advance."""
    preview: Dict[str, List[List[str]]] = {}
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        shared = _load_shared_strings(zf)
        for sheet_name, sheet_path in _load_sheets(zf).items():
            rows = _parse_rows(zf, sheet_path, shared)
            preview[sheet_name] = [[c for c in row if c] for row in rows[:max_rows]]
    return preview


def extract_column_values(raw_bytes: bytes, header_names) -> List[str]:
    """
    Scans each sheet's first few rows (see _HEADER_SCAN_ROWS) for one acting
    as a header — containing a cell matching one of header_names, compared
    via normalize_token so spelling/spacing variance doesn't matter — then
    returns every non-blank value found under that column, from the rows
    below it. Unlike parse_xlsx_to_text (full-document serialization for
    indexing), this reads one specific column out of a register workbook.
    """
    wanted = {normalize_token(h) for h in header_names}
    values: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        shared = _load_shared_strings(zf)
        for _, sheet_path in _load_sheets(zf).items():
            rows = _parse_rows(zf, sheet_path, shared)
            if not rows:
                continue
            header_row_idx, col_idxs = _find_header_row(rows, wanted)
            if header_row_idx is None:
                continue
            for row in rows[header_row_idx + 1:]:
                for i in col_idxs:
                    if i < len(row) and row[i]:
                        values.append(row[i].strip())
    return values


def extract_row_records(raw_bytes: bytes, column_aliases: Dict[str, tuple]) -> List[Dict[str, Optional[str]]]:
    """
    Like extract_column_values, but reads several columns per row, keeping
    them aligned — e.g. a register with both a CW number column and a
    contract name column, where each output record should pair the two
    values from the SAME row rather than two independent flat lists.

    column_aliases: {canonical_key: (header_variant1, header_variant2, ...)}
    — e.g. {"cw_number": ("CW Number", "Contract Workspace ID"),
             "title": ("Contract Workspace Name",)}.

    Returns one dict per non-blank data row, keyed by canonical_key ->
    stripped cell value (a key is simply absent from a record if that
    sheet's header didn't include it at all, so callers should use
    .get(key) rather than assume every key is present). A row blank across
    every matched column is skipped.
    """
    normalized_aliases = {key: {normalize_token(a) for a in aliases}
                          for key, aliases in column_aliases.items()}
    records: List[Dict[str, Optional[str]]] = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        shared = _load_shared_strings(zf)
        for _, sheet_path in _load_sheets(zf).items():
            rows = _parse_rows(zf, sheet_path, shared)
            if not rows:
                continue

            header_row_idx, col_map = None, None
            for r_idx, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
                found = {}
                for i, h in enumerate(row):
                    if not h:
                        continue
                    token = normalize_token(h)
                    for key, aliases in normalized_aliases.items():
                        if key not in found and token in aliases:
                            found[key] = i
                if found:
                    header_row_idx, col_map = r_idx, found
                    break
            if header_row_idx is None:
                continue

            for row in rows[header_row_idx + 1:]:
                record = {
                    key: (row[idx].strip() if idx < len(row) and row[idx] else None)
                    for key, idx in col_map.items()
                }
                if any(record.values()):
                    records.append(record)
    return records
