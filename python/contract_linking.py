"""
contract_linking.py — group RAW_DOCUMENTS rows into contract families.

LEX-specific: not part of the generic project-llm-wiki template this was
forked from. A signed contract is rarely one static document — over its
life it accumulates variations, extensions, and novation deeds, each
ingested as its own RAW_DOCUMENTS row. CONTRACT_REGISTER (one row per
real-world contract, keyed on CW number) and CONTRACT_DOCUMENT_LINK (which
documents belong to which contract, and in what role) let both free-form
chat and the stock-field extraction job (contract_extraction.py) route
across a contract's full history rather than just its base document.

Linking is suggestion-only: suggest_cw_number() is a best-effort regex
guess from a file name, never auto-applied without a human confirming it
in the Contract Register page — a wrong auto-link would silently merge two
unrelated contracts' answers together, a correctness risk this template
treats as unacceptable for a legal tool (see the README).
"""

import re
from dataclasses import dataclass
from typing import List, Optional

from config import ProjectConfig
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

DOC_ROLES = ("BASE", "VARIATION", "EXTENSION", "NOVATION", "DEED_OF_AMENDMENT")

# Best-effort pattern for a CW-style contract/work-order number, e.g.
# "CW12345", "CW-4567", "CW 890123". Deliberately loose (this is a
# suggestion, never applied automatically) — tighten once the team
# confirms the actual numbering convention (see the plan's open questions).
_CW_NUMBER_RE = re.compile(r"\bCW[\s\-]?(\d{3,8})\b", re.IGNORECASE)


@dataclass
class ContractSummary:
    contract_id: int
    cw_number: str
    contract_title: Optional[str]
    status: str
    document_count: int


def suggest_cw_number(file_name: str, raw_text: Optional[str] = None) -> Optional[str]:
    """Best-effort CW-number guess from a file name, falling back to the
    first ~2000 characters of the document text (a contract's cover page
    or first clause commonly restates its own reference number) if the
    file name doesn't have one. Returns a normalized 'CW<digits>' string,
    or None if nothing matched — never guessed at, never applied without
    a human confirming it in the UI."""
    for source in (file_name, (raw_text or "")[:2000]):
        match = _CW_NUMBER_RE.search(source or "")
        if match:
            return f"CW{match.group(1)}"
    return None


def get_or_create_contract(session, project: ProjectConfig, cw_number: str,
                           contract_title: Optional[str] = None) -> int:
    """Idempotent: returns the existing CONTRACT_ID for cw_number if one
    exists, otherwise creates it. If the row already exists with no title
    set yet and one is given now, it's backfilled once (e.g. a contract
    auto-created by document-linking before the Required Contracts
    Register was ever uploaded, which has no title of its own to offer) —
    but a title the row already has is never overwritten here; editing an
    existing title is the Contract Register UI's job, not this function's."""
    schema = project.qualified_schema
    cw_number = cw_number.strip().upper()

    existing = session.sql(
        f"SELECT CONTRACT_ID, CONTRACT_TITLE FROM {schema}.CONTRACT_REGISTER WHERE CW_NUMBER = ?",
        params=[cw_number],
    ).collect()
    if existing:
        if contract_title and not existing[0]["CONTRACT_TITLE"]:
            session.sql(
                f"UPDATE {schema}.CONTRACT_REGISTER SET CONTRACT_TITLE = ? WHERE CONTRACT_ID = ?",
                params=[contract_title, existing[0]["CONTRACT_ID"]],
            ).collect()
        return existing[0]["CONTRACT_ID"]

    session.sql(
        f"""INSERT INTO {schema}.CONTRACT_REGISTER (CW_NUMBER, CONTRACT_TITLE)
            SELECT ?, ?""",
        params=[cw_number, contract_title],
    ).collect()
    contract_id = session.sql(
        f"SELECT CONTRACT_ID FROM {schema}.CONTRACT_REGISTER WHERE CW_NUMBER = ?",
        params=[cw_number],
    ).collect()[0]["CONTRACT_ID"]

    log_event(logger, "CONTRACT_CREATED", project.project_code,
              contract_id=contract_id, cw_number=cw_number)
    return contract_id


def link_document(session, project: ProjectConfig, contract_id: int, doc_id: int,
                  doc_role: str, effective_date=None, linked_by: str = "AUTO") -> None:
    """Idempotent on (CONTRACT_ID, DOC_ID) — re-linking the same document to
    the same contract just updates its role/date rather than erroring or
    duplicating, since CONTRACT_DOCUMENT_LINK has a UNIQUE constraint on
    that pair."""
    if doc_role not in DOC_ROLES:
        raise ValueError(f"doc_role must be one of {DOC_ROLES}, got {doc_role!r}")

    schema = project.qualified_schema
    session.sql(
        f"""MERGE INTO {schema}.CONTRACT_DOCUMENT_LINK AS tgt
            USING (SELECT ? AS CONTRACT_ID, ? AS DOC_ID, ? AS DOC_ROLE,
                          ? AS EFFECTIVE_DATE, ? AS LINKED_BY) AS src
            ON tgt.CONTRACT_ID = src.CONTRACT_ID AND tgt.DOC_ID = src.DOC_ID
            WHEN MATCHED THEN UPDATE SET
                DOC_ROLE = src.DOC_ROLE, EFFECTIVE_DATE = src.EFFECTIVE_DATE,
                LINKED_BY = src.LINKED_BY
            WHEN NOT MATCHED THEN INSERT
                (CONTRACT_ID, DOC_ID, DOC_ROLE, EFFECTIVE_DATE, LINKED_BY)
                VALUES (src.CONTRACT_ID, src.DOC_ID, src.DOC_ROLE, src.EFFECTIVE_DATE, src.LINKED_BY)""",
        params=[contract_id, doc_id, doc_role, effective_date, linked_by],
    ).collect()
    log_event(logger, "DOCUMENT_LINKED", project.project_code,
              contract_id=contract_id, doc_id=doc_id, doc_role=doc_role)


def unlink_document(session, project: ProjectConfig, contract_id: int, doc_id: int) -> None:
    schema = project.qualified_schema
    session.sql(
        f"DELETE FROM {schema}.CONTRACT_DOCUMENT_LINK WHERE CONTRACT_ID = ? AND DOC_ID = ?",
        params=[contract_id, doc_id],
    ).collect()


def get_contract(session, project: ProjectConfig, contract_id: int) -> Optional[dict]:
    """One contract's own register row: CW number, title, lifecycle
    status, the Executive Assessment narrative (OVERVIEW_SUMMARY),
    Recommended Actions (a list, parsed from VARIANT), and the
    classification scorecard (a dict, parsed from VARIANT) —
    contract_extraction.py's three synthesis outputs. The Contract Lookup
    page and docx_report.py both read this directly."""
    import json
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT CONTRACT_ID, CW_NUMBER, CONTRACT_TITLE, STATUS,
                   OVERVIEW_SUMMARY, OVERVIEW_GENERATED_AT,
                   RECOMMENDED_ACTIONS, CLASSIFICATION_SCORECARD
            FROM {schema}.CONTRACT_REGISTER WHERE CONTRACT_ID = ?""",
        params=[contract_id],
    ).collect()
    if not rows:
        return None
    contract = dict(rows[0].as_dict())
    contract["RECOMMENDED_ACTIONS"] = (
        json.loads(contract["RECOMMENDED_ACTIONS"]) if contract.get("RECOMMENDED_ACTIONS") else []
    )
    contract["CLASSIFICATION_SCORECARD"] = (
        json.loads(contract["CLASSIFICATION_SCORECARD"]) if contract.get("CLASSIFICATION_SCORECARD") else {}
    )
    return contract


def list_contract_families(session, project: ProjectConfig) -> List[ContractSummary]:
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT CR.CONTRACT_ID, CR.CW_NUMBER, CR.CONTRACT_TITLE, CR.STATUS,
                   COUNT(CDL.DOC_ID) AS DOC_COUNT
            FROM {schema}.CONTRACT_REGISTER CR
            LEFT JOIN {schema}.CONTRACT_DOCUMENT_LINK CDL ON CR.CONTRACT_ID = CDL.CONTRACT_ID
            GROUP BY CR.CONTRACT_ID, CR.CW_NUMBER, CR.CONTRACT_TITLE, CR.STATUS
            ORDER BY CR.CW_NUMBER"""
    ).collect()
    return [
        ContractSummary(r["CONTRACT_ID"], r["CW_NUMBER"], r["CONTRACT_TITLE"],
                        r["STATUS"], r["DOC_COUNT"])
        for r in rows
    ]


def get_family_doc_ids(session, project: ProjectConfig, contract_id: int) -> List[int]:
    schema = project.qualified_schema
    rows = session.sql(
        f"SELECT DOC_ID FROM {schema}.CONTRACT_DOCUMENT_LINK WHERE CONTRACT_ID = ?",
        params=[contract_id],
    ).collect()
    return [r["DOC_ID"] for r in rows]


def list_family_documents(session, project: ProjectConfig, contract_id: int) -> List[dict]:
    """Linked documents for one contract, with enough detail for the
    Contract Register page's expander (role, effective date, file name)."""
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT CDL.DOC_ID, CDL.DOC_ROLE, CDL.EFFECTIVE_DATE, CDL.SEQUENCE_NO,
                   RD.FILE_NAME, RD.SOURCE_URL
            FROM {schema}.CONTRACT_DOCUMENT_LINK CDL
            JOIN {schema}.RAW_DOCUMENTS RD ON CDL.DOC_ID = RD.DOC_ID
            WHERE CDL.CONTRACT_ID = ?
            ORDER BY CDL.EFFECTIVE_DATE NULLS LAST, CDL.SEQUENCE_NO NULLS LAST, RD.FILE_NAME""",
        params=[contract_id],
    ).collect()
    return [dict(r.as_dict()) for r in rows]


def get_significant_variations(session, project: ProjectConfig, contract_id: int) -> List[dict]:
    """Non-BASE linked documents (variations/extensions/novations/deeds of
    amendment) for one contract, each with its own document-level summary
    — the template's "Significant Variations" bullet list. Reuses each
    document's DOCUMENT_INDEX root-node NODE_SUMMARY (already generated at
    indexing time) rather than a separate Cortex call — indexing already
    produces exactly the "what is this document" synopsis this list needs."""
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT CDL.DOC_ID, CDL.DOC_ROLE, CDL.EFFECTIVE_DATE, RD.FILE_NAME, RD.SOURCE_URL,
                   DI.NODE_SUMMARY
            FROM {schema}.CONTRACT_DOCUMENT_LINK CDL
            JOIN {schema}.RAW_DOCUMENTS RD ON CDL.DOC_ID = RD.DOC_ID
            LEFT JOIN {schema}.DOCUMENT_INDEX DI ON DI.DOC_ID = CDL.DOC_ID AND DI.PARENT_NODE_ID IS NULL
            WHERE CDL.CONTRACT_ID = ? AND CDL.DOC_ROLE != 'BASE'
            ORDER BY CDL.EFFECTIVE_DATE NULLS LAST, CDL.SEQUENCE_NO NULLS LAST, RD.FILE_NAME""",
        params=[contract_id],
    ).collect()
    return [dict(r.as_dict()) for r in rows]


def list_unlinked_documents(session, project: ProjectConfig) -> List[dict]:
    """RAW_DOCUMENTS not yet linked to any contract — the Contract Register
    page's "documents to link" queue."""
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT RD.DOC_ID, RD.FILE_NAME, RD.RAW_TEXT
            FROM {schema}.RAW_DOCUMENTS RD
            WHERE RD.DOC_ID NOT IN (
                SELECT DOC_ID FROM {schema}.CONTRACT_DOCUMENT_LINK
            )
            ORDER BY RD.CREATED_AT DESC"""
    ).collect()
    return [
        {"doc_id": r["DOC_ID"], "file_name": r["FILE_NAME"],
         "suggested_cw_number": suggest_cw_number(r["FILE_NAME"], r["RAW_TEXT"])}
        for r in rows
    ]
