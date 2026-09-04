"""
config.py — infra constants + LEX's config loader.

Forked from the project-llm-wiki template, which centralizes its catalog
in a shared MEDSOCMS database and normally ingests from SharePoint via
Microsoft Graph API. LEX follows neither part of the template: it is
fully self-contained in its own MEDSCOMA database — catalog
(APP_CATALOG.PROJECTS), data (DATA_LEX), Streamlit app/stage, and its own
dedicated network-drive credential secret all live there — and its
contracts library was confirmed to be a genuine on-prem network drive
(SMB), not SharePoint, so there is no Graph API anywhere in this
codebase. Nothing LEX reads or writes lives in MEDSOCMS, and it holds no
reference to any other project's resources.
"""

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Infra constants. Warehouse and compute pool are NOT here — they're
# per-project columns on PROJECTS (QUERY_WAREHOUSE, COMPUTE_POOL). MTMWH02
# below is only the *fallback default* if none is specified at creation
# time. DATABASE/CATALOG_SCHEMA are where LEX's own catalog lives —
# MEDSCOMA, the same dedicated database LEX's actual data lives in (see
# ProjectConfig.data_database) — not a database shared with anything else.
# ---------------------------------------------------------------------------
WAREHOUSE_NAME = "MTMWH02"  # default only — see ProjectConfig.query_warehouse
DATABASE = "MEDSCOMA"       # home of LEX's own catalog (APP_CATALOG), always
ROLE = "ADVANCEDANALYTICS"
CATALOG_SCHEMA = "APP_CATALOG"

# Model fallback if a project row doesn't specify one
FREE_MODEL = "llama3.1-70b"

MIN_PARSED_TEXT_CHARS = 100


@dataclass
class ProjectConfig:
    """One row of MEDSCOMA.APP_CATALOG.PROJECTS, typed."""
    project_id: int
    project_code: str
    project_name: str
    description: Optional[str]
    data_database: str
    data_schema: str
    stage_name: str
    streamlit_app_name: str
    streamlit_stage_name: str
    query_warehouse: str
    compute_pool: Optional[str]
    network_drive_host: str
    network_drive_share: str
    network_drive_default_path: Optional[str]
    network_drive_domain: Optional[str]
    network_drive_secret_name: Optional[str]
    active_model: str
    max_document_chars: int
    max_section_chars: int
    query_cache_ttl_hours: int
    max_citations_display: int
    segmentation_profile: str
    status: str
    segmentation_granularity: str
    enable_reranking: bool
    enable_vector_search: bool
    max_candidate_docs: int

    @property
    def qualified_schema(self) -> str:
        return f"{self.data_database}.{self.data_schema}"

    @property
    def qualified_stage(self) -> str:
        return f"{self.qualified_schema}.{self.stage_name}"

    @property
    def qualified_streamlit_app(self) -> str:
        return f"{DATABASE}.{CATALOG_SCHEMA}.{self.streamlit_app_name}"

    @property
    def qualified_streamlit_stage(self) -> str:
        return f"{DATABASE}.{CATALOG_SCHEMA}.{self.streamlit_stage_name}"

    @property
    def is_container_runtime(self) -> bool:
        return bool(self.compute_pool)

    @property
    def clamped_max_candidate_docs(self) -> int:
        """MAX_CANDIDATE_DOCS is a config number, not DB-enforced (Snowflake
        accepts CHECK constraint syntax but never enforces it) — clamp here."""
        return max(5, min(10, self.max_candidate_docs))

    @property
    def resolved_network_drive_secret_name(self) -> str:
        """Fully-qualified secret object name for the network drive's
        username/password — always LEX's own, e.g.
        MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_SECRET (see
        sql/test_network_drive_connectivity.sql). Note this only affects
        which SECRET object the deploy step binds under the Streamlit
        app's fixed local alias 'network_drive_credential' — application
        code always reads st.secrets['network_drive_credential']
        regardless (see utils/network_drive_client.py), so no code path
        needs to know which underlying secret that alias actually points
        to. Raises clearly rather than silently proceeding with no
        credentials if the provisioning notebook's network-drive-secret
        cell hasn't been run yet."""
        if not self.network_drive_secret_name:
            raise ValueError(
                "NETWORK_DRIVE_SECRET_NAME is not set on the PROJECTS row. Run "
                "the network drive credentials cell in "
                "pipeline/00_provision_project.ipynb first."
            )
        return self.network_drive_secret_name


def load_project(session, project_code: str) -> ProjectConfig:
    """Fetch a project's config row. Raises ValueError if not found/archived."""
    rows = session.sql(
        f"""SELECT PROJECT_ID, PROJECT_CODE, PROJECT_NAME, DESCRIPTION,
                   DATA_DATABASE, DATA_SCHEMA, STAGE_NAME,
                   STREAMLIT_APP_NAME, STREAMLIT_STAGE_NAME,
                   QUERY_WAREHOUSE, COMPUTE_POOL,
                   NETWORK_DRIVE_HOST, NETWORK_DRIVE_SHARE,
                   NETWORK_DRIVE_DEFAULT_PATH, NETWORK_DRIVE_DOMAIN,
                   NETWORK_DRIVE_SECRET_NAME,
                   ACTIVE_MODEL, MAX_DOCUMENT_CHARS, MAX_SECTION_CHARS,
                   QUERY_CACHE_TTL_HOURS, MAX_CITATIONS_DISPLAY,
                   SEGMENTATION_PROFILE, STATUS,
                   SEGMENTATION_GRANULARITY, ENABLE_RERANKING,
                   ENABLE_VECTOR_SEARCH, MAX_CANDIDATE_DOCS
            FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
            WHERE PROJECT_CODE = ?""",
        params=[project_code.strip().upper()],
    ).collect()

    if not rows:
        raise ValueError(f"No project found with code '{project_code}'")

    r = rows[0]
    return ProjectConfig(
        project_id=r["PROJECT_ID"],
        project_code=r["PROJECT_CODE"],
        project_name=r["PROJECT_NAME"],
        description=r["DESCRIPTION"],
        data_database=r["DATA_DATABASE"],
        data_schema=r["DATA_SCHEMA"],
        stage_name=r["STAGE_NAME"],
        streamlit_app_name=r["STREAMLIT_APP_NAME"],
        streamlit_stage_name=r["STREAMLIT_STAGE_NAME"],
        query_warehouse=r["QUERY_WAREHOUSE"],
        compute_pool=r["COMPUTE_POOL"],
        network_drive_host=r["NETWORK_DRIVE_HOST"],
        network_drive_share=r["NETWORK_DRIVE_SHARE"],
        network_drive_default_path=r["NETWORK_DRIVE_DEFAULT_PATH"],
        network_drive_domain=r["NETWORK_DRIVE_DOMAIN"],
        network_drive_secret_name=r["NETWORK_DRIVE_SECRET_NAME"],
        active_model=r["ACTIVE_MODEL"],
        max_document_chars=r["MAX_DOCUMENT_CHARS"],
        max_section_chars=r["MAX_SECTION_CHARS"],
        query_cache_ttl_hours=r["QUERY_CACHE_TTL_HOURS"],
        max_citations_display=r["MAX_CITATIONS_DISPLAY"],
        segmentation_profile=r["SEGMENTATION_PROFILE"],
        status=r["STATUS"],
        segmentation_granularity=r["SEGMENTATION_GRANULARITY"],
        enable_reranking=bool(r["ENABLE_RERANKING"]),
        enable_vector_search=bool(r["ENABLE_VECTOR_SEARCH"]),
        max_candidate_docs=r["MAX_CANDIDATE_DOCS"],
    )


def list_active_projects(session):
    """Used by the Streamlit project selector."""
    return session.sql(
        f"""SELECT PROJECT_CODE, PROJECT_NAME
            FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
            WHERE STATUS = 'ACTIVE'
            ORDER BY PROJECT_NAME"""
    ).collect()
