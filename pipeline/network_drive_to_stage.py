#!/usr/bin/env python3
"""
pipeline/network_drive_to_stage.py — bridge script for a Linux host inside
the MTM network: reads selected contract files from the network drive
(SMB) and pushes them to a Snowflake internal stage via PUT.

Why this exists: Snowflake's own outbound connectivity to
\\\\metrotrains.local\\... is blocked on DNS resolution of what looks like
a DFS namespace root, not a single server address (see
sql/test_network_drive_connectivity.sql) — a networking question still
being worked with MTM's Networks team. This script sidesteps that
entirely by running *inside* the MTM network, where DNS/SMB access to the
share is already normal, and reaching *out* to Snowflake instead (the
direction PUT is built for) rather than the other way around.

Reuses utils/network_drive_client.py's SMB logic (the same module the
Streamlit app itself would use once direct connectivity works) rather
than a second SMB implementation — the only difference is how SMB
credentials are resolved: that module's get_network_drive_credentials()
falls through to NETWORK_DRIVE_USERNAME/NETWORK_DRIVE_PASSWORD
environment variables when neither st.secrets nor _snowflake is
available, which is always the case for a script like this one running
outside Snowflake/Streamlit entirely.

IMPORTANT — this is a staging step only. It does not parse documents,
compute hashes, or write RAW_DOCUMENTS rows — it only copies raw file
bytes into a holding stage (NETWORK_DRIVE_INBOX_STAGE). Picking those
files up from there into the app's normal ingest pipeline
(AI_PARSE_DOCUMENT, hashing, CONTRACT_REGISTER linking) is a separate
piece of work, not yet built.

UNVERIFIED: written without a live SMB server, Snowflake account, or
network drive to test against — treat this as a best-effort starting
point.

--------------------------------------------------------------------------
Setup
--------------------------------------------------------------------------
Prerequisites on the Linux host:
    pip install snowflake-connector-python smbprotocol cryptography

Never hardcode credentials into this file or commit them. Set these as
environment variables before running (e.g. in a shell profile, a systemd
unit's Environment= lines, or a local .env sourced by hand — NOT a
tracked file):

  SNOWFLAKE_ACCOUNT             e.g. 'xy12345.ap-southeast-2'
  SNOWFLAKE_USER                a dedicated service account username —
                                 see the CREATE USER snippet at the bottom
                                 of this docstring for a least-privilege
                                 setup recommendation (key-pair auth, no
                                 password/MFA to automate around)
  SNOWFLAKE_PRIVATE_KEY_PATH    path to an unencrypted PEM private key
                                 file for that user (recommended)
  SNOWFLAKE_PRIVATE_KEY_PASSPHRASE   optional, if the key is encrypted
  SNOWFLAKE_PASSWORD            fallback only, if key-pair isn't set up yet
  SNOWFLAKE_ROLE                defaults to ADVANCEDANALYTICS
  SNOWFLAKE_WAREHOUSE           defaults to MTMWH02
  NETWORK_DRIVE_USERNAME        SMB service account username
  NETWORK_DRIVE_PASSWORD        SMB service account password
  NETWORK_DRIVE_DOMAIN          optional NTLM domain, e.g. 'METROTRAINS' —
                                 overrides PROJECTS.NETWORK_DRIVE_DOMAIN if
                                 that column isn't populated yet

--------------------------------------------------------------------------
Usage
--------------------------------------------------------------------------
    # Sync every eligible (PDF/DOCX) file under the configured default path
    python network_drive_to_stage.py --all

    # Sync only specific files, paths relative to the share root
    python network_drive_to_stage.py --files \\
        "AppData/prd/MR5Documents/Ariba/CW14465_Executed.pdf" \\
        "AppData/prd/MR5Documents/Ariba/CW20841_Executed.pdf"

--------------------------------------------------------------------------
Recommended: a dedicated, least-privilege Snowflake service user
--------------------------------------------------------------------------
Run once by whoever holds sufficient privilege (this only needs USAGE on
the warehouse and role membership — it does not need to own or manage
anything else):

    CREATE USER IF NOT EXISTS LEX_NETWORK_DRIVE_BRIDGE
      RSA_PUBLIC_KEY = '<paste the public key generated alongside
                          SNOWFLAKE_PRIVATE_KEY_PATH above>'
      DEFAULT_ROLE = ADVANCEDANALYTICS
      DEFAULT_WAREHOUSE = MTMWH02
      COMMENT = 'Service account for the network-drive-to-stage bridge
                 script (pipeline/network_drive_to_stage.py) — password
                 auth disabled, key-pair only';
    GRANT ROLE ADVANCEDANALYTICS TO USER LEX_NETWORK_DRIVE_BRIDGE;

Generate the key pair on the Linux host itself (private key never leaves
it):
    openssl genrsa -out network_drive_bridge_key.pem 2048
    openssl rsa -in network_drive_bridge_key.pem -pubout \\
        -out network_drive_bridge_key.pub
"""

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from required_contracts import is_eligible_extension  # noqa: E402
from utils.network_drive_client import list_files, download_file, NetworkDriveError  # noqa: E402

import snowflake.connector

INBOX_STAGE = "MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STAGE"


@dataclass
class DriveConfig:
    """Minimal stand-in for config.ProjectConfig's network-drive fields.
    This script runs outside Snowflake/Streamlit, so it can't load the
    real ProjectConfig (that requires a live Snowpark session tied to the
    app itself) — it only needs these four values, fetched via a plain
    SELECT in load_drive_config() below. utils/network_drive_client.py's
    functions only ever read these four attributes, so this duck-types
    fine in place of a real ProjectConfig."""
    network_drive_host: str
    network_drive_share: str
    network_drive_default_path: Optional[str]
    network_drive_domain: Optional[str]


def _require_env(name: str, also_mention: Optional[str] = None) -> str:
    value = os.environ.get(name)
    if not value:
        hint = f" (or set {also_mention})" if also_mention else ""
        raise SystemExit(f"Missing required environment variable: {name}{hint}")
    return value


def get_snowflake_connection():
    account = _require_env("SNOWFLAKE_ACCOUNT")
    user = _require_env("SNOWFLAKE_USER")
    role = os.environ.get("SNOWFLAKE_ROLE", "ADVANCEDANALYTICS")
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", "MTMWH02")

    private_key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    if private_key_path:
        from cryptography.hazmat.primitives import serialization

        passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        with open(private_key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(
                f.read(),
                password=passphrase.encode() if passphrase else None,
            )
        pkb = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return snowflake.connector.connect(
            account=account, user=user, role=role, warehouse=warehouse,
            private_key=pkb,
        )

    password = _require_env("SNOWFLAKE_PASSWORD", also_mention="SNOWFLAKE_PRIVATE_KEY_PATH")
    return snowflake.connector.connect(
        account=account, user=user, role=role, warehouse=warehouse,
        password=password,
    )


def load_drive_config(conn) -> DriveConfig:
    cur = conn.cursor()
    cur.execute(
        "SELECT NETWORK_DRIVE_HOST, NETWORK_DRIVE_SHARE, "
        "NETWORK_DRIVE_DEFAULT_PATH, NETWORK_DRIVE_DOMAIN "
        "FROM MEDSCOMA.APP_CATALOG.PROJECTS WHERE PROJECT_CODE = 'LEX'"
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit("No PROJECT_CODE='LEX' row found in MEDSCOMA.APP_CATALOG.PROJECTS")
    host, share, default_path, domain = row
    # NETWORK_DRIVE_DOMAIN env var overrides the PROJECTS row's value —
    # convenient for testing before that column is populated for real.
    domain = os.environ.get("NETWORK_DRIVE_DOMAIN", domain)
    if not host or not share:
        raise SystemExit(
            "PROJECTS.NETWORK_DRIVE_HOST/NETWORK_DRIVE_SHARE are not set on "
            "LEX's row yet — set them via the provisioning notebook's "
            "project-creation cell first."
        )
    return DriveConfig(host, share, default_path, domain)


def ensure_inbox_stage(conn):
    conn.cursor().execute(
        f"CREATE STAGE IF NOT EXISTS {INBOX_STAGE} ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')"
    )


def sync_all(conn, drive: DriveConfig):
    items = [i for i in list_files(drive, relative_path="", recursive=True)
             if is_eligible_extension(i.name)]
    print(f"Found {len(items)} eligible file(s) under the configured path.")
    for item in items:
        _stage_one(conn, drive, item.item_id, item.name)


def sync_selected(conn, drive: DriveConfig, relative_paths: List[str]):
    for rel in relative_paths:
        rel_clean = rel.strip("/\\").replace("/", "\\")
        item_path = f"\\\\{drive.network_drive_host}\\{drive.network_drive_share}\\{rel_clean}"
        file_name = rel_clean.rsplit("\\", 1)[-1]
        if not is_eligible_extension(file_name):
            print(f"SKIPPED (not PDF/DOCX): {rel}")
            continue
        _stage_one(conn, drive, item_path, file_name)


def _stage_one(conn, drive: DriveConfig, item_path: str, file_name: str):
    try:
        raw_bytes = download_file(drive, item_path)
    except NetworkDriveError as e:
        print(f"FAILED to read {file_name}: {e}")
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = os.path.join(tmp_dir, file_name)
        with open(local_path, "wb") as f:
            f.write(raw_bytes)
        cur = conn.cursor()
        cur.execute(
            f"PUT 'file://{local_path}' @{INBOX_STAGE} AUTO_COMPRESS=FALSE OVERWRITE=TRUE"
        )
        print(f"OK  staged {file_name}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync selected contract files from LEX's network drive to a "
                     "Snowflake staging area. See this file's module docstring "
                     "for full setup instructions (env vars, prerequisites, "
                     "recommended service-user setup).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true",
        help="Sync every eligible (PDF/DOCX) file under the configured default path",
    )
    group.add_argument(
        "--files", nargs="+", metavar="RELATIVE_PATH",
        help="Sync only these specific files, paths relative to the share root",
    )
    args = parser.parse_args()

    conn = get_snowflake_connection()
    try:
        drive = load_drive_config(conn)
        ensure_inbox_stage(conn)
        if args.all:
            sync_all(conn, drive)
        else:
            sync_selected(conn, drive, args.files)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
