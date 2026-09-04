"""
required_contracts.py — the authoritative "which contracts is LEX in scope
for right now" list, and the PDF/DOCX + signed/executed eligibility check
for candidate source files.

LEX-specific: not part of the generic project-llm-wiki template. The team
maintains a Required Contracts Register — an .xlsx listing every CW number
LEX should have data for (2 today, growing to 8 for Build/validation, more
later) — separate from whatever happens to sit in the network drive
folder. This module seeds CONTRACT_REGISTER from that register so the
Contract Lookup page has a fixed, known set of contract numbers to offer,
independent of ingestion order.
"""

from dataclasses import dataclass
from typing import List, Optional

from config import ProjectConfig
import contract_linking
from ingestion.xlsx_parser import extract_row_records, list_headers
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

# Column header variants accepted in the Required Contracts Register
# workbook — matched case/spacing-insensitively the same way
# xlsx_parser.normalize_token() already handles for other registers in
# this template family. Confirmed directly against the team's actual
# MTM_CONTRACT_WORKSPACES.xlsx, which uses "CONTRACT_WORKSPACE_ID" /
# "CONTRACT_WORKSPACE_NAME" — the other variants are kept as fallbacks in
# case a future version of the register is headed differently.
_REGISTER_COLUMN_ALIASES = {
    "cw_number": ("CONTRACT_WORKSPACE_ID", "CW Number", "CWNumber", "Contract Number",
                  "CW", "Contract No", "ContractNo"),
    "title": ("CONTRACT_WORKSPACE_NAME", "Contract Workspace Name", "Contract Name",
              "ContractName", "Title"),
}

# Only these extensions are ever offered/ingested as contract source
# documents — the team has been explicit that contracts are PDF, rarely
# DOCX, and nothing else (the Required Contracts Register itself is the
# one legitimate .xlsx in this workflow, read separately via
# sync_required_contracts_from_xlsx below, not through this list).
ELIGIBLE_EXTENSIONS = (".pdf", ".docx")

# Best-effort marker that a document is the fully executed copy (both
# parties signed), not a draft/unsigned template — checked against the
# file name first (cheap, works before download) and, after ingest,
# against the parsed text too (belt-and-braces: a poorly-named file with
# the right content still gets flagged rather than silently missed). This
# is a soft signal surfaced to a human, never a hard filter that silently
# drops a file — wording varies enough across contracts that a false
# negative here should never mean "the file disappears without a trace".
SIGNED_MARKERS = ("signed", "executed", "execution copy", "duly executed",
                  "fully executed", "execution version")


def is_eligible_extension(file_name: str) -> bool:
    return file_name.lower().endswith(ELIGIBLE_EXTENSIONS)


def looks_signed(file_name: str, text_sample: str = "") -> bool:
    """True if the file name OR the first part of its parsed text contains
    one of SIGNED_MARKERS. A False here is a prompt for a human to double
    check, not a rejection — see the module docstring."""
    haystack = f"{file_name}\n{text_sample[:3000]}".lower()
    return any(marker in haystack for marker in SIGNED_MARKERS)


@dataclass
class RequiredContractStatus:
    contract_id: int
    cw_number: str
    contract_title: Optional[str]
    linked_document_count: int
    extracted_field_count: int
    is_extraction_current: bool


def sync_required_contracts_from_xlsx(session, project: ProjectConfig, raw_bytes: bytes) -> dict:
    """
    Parses the Required Contracts Register workbook (CW number + contract
    name, paired per row) and upserts one CONTRACT_REGISTER row per CW
    number found — with its title, when the register provides one, backfilled
    onto an existing title-less row too (see contract_linking.get_or_create_
    contract). Idempotent: re-uploading the same or an updated register only
    adds newly-appearing CW numbers, never removes or duplicates existing
    ones — a contract dropping off a later version of the register doesn't
    retroactively delete its already-extracted history.
    """
    records = extract_row_records(raw_bytes, _REGISTER_COLUMN_ALIASES)
    # One entry per CW number (upper-cased/stripped, matching how
    # contract_linking's own auto-suggestions are normalized, so a
    # register entry and a document-derived suggestion always compare
    # equal) — first non-blank title wins if a number somehow repeats.
    by_cw_number: dict = {}
    for rec in records:
        cw_number = (rec.get("cw_number") or "").strip().upper()
        if not cw_number:
            continue
        title = rec.get("title")
        if cw_number not in by_cw_number or (title and not by_cw_number[cw_number]):
            by_cw_number[cw_number] = title

    if not by_cw_number:
        return {
            "added": [], "already_present": [],
            "headers_preview": list_headers(raw_bytes),
        }

    added, already_present = [], []
    for cw_number, title in sorted(by_cw_number.items()):
        existing = session.sql(
            f"SELECT CONTRACT_ID FROM {project.qualified_schema}.CONTRACT_REGISTER WHERE CW_NUMBER = ?",
            params=[cw_number],
        ).collect()
        if existing:
            already_present.append(cw_number)
        else:
            added.append(cw_number)
        # Runs either way — this is also what backfills a title onto an
        # already-existing, title-less row (see get_or_create_contract).
        contract_linking.get_or_create_contract(session, project, cw_number, contract_title=title)

    log_event(logger, "REQUIRED_CONTRACTS_SYNCED", project.project_code,
              added=len(added), already_present=len(already_present))
    return {"added": added, "already_present": already_present, "headers_preview": {}}


def list_required_contracts_status(session, project: ProjectConfig) -> List[RequiredContractStatus]:
    """Every contract in the register (i.e. every CW number the team has
    told LEX to care about), with enough status to drive the Contract
    Lookup dropdown and the Sync Status coverage view: how many documents
    are linked, how many stock fields have been extracted, and whether
    that extraction is still current against the linked documents' latest
    parse (see contract_extraction.is_extraction_current)."""
    from contract_extraction import is_extraction_current  # local import: contract_extraction imports
                                                            # contract_linking, not this module, so this
                                                            # direction is safe — kept local anyway to
                                                            # keep the module load order obvious
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT CR.CONTRACT_ID, CR.CW_NUMBER, CR.CONTRACT_TITLE,
                   COUNT(DISTINCT CDL.DOC_ID) AS DOC_COUNT,
                   COUNT(DISTINCT CFE.FIELD_KEY) AS FIELD_COUNT
            FROM {schema}.CONTRACT_REGISTER CR
            LEFT JOIN {schema}.CONTRACT_DOCUMENT_LINK CDL ON CR.CONTRACT_ID = CDL.CONTRACT_ID
            LEFT JOIN {schema}.CONTRACT_FIELD_EXTRACTS CFE ON CR.CONTRACT_ID = CFE.CONTRACT_ID
            GROUP BY CR.CONTRACT_ID, CR.CW_NUMBER, CR.CONTRACT_TITLE
            ORDER BY CR.CW_NUMBER"""
    ).collect()
    return [
        RequiredContractStatus(
            contract_id=r["CONTRACT_ID"], cw_number=r["CW_NUMBER"],
            contract_title=r["CONTRACT_TITLE"], linked_document_count=r["DOC_COUNT"],
            extracted_field_count=r["FIELD_COUNT"],
            is_extraction_current=(
                is_extraction_current(session, project, r["CONTRACT_ID"])
                if r["FIELD_COUNT"] else False
            ),
        )
        for r in rows
    ]
