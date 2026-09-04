"""
network_drive_client.py — SMB/CIFS client for LEX's on-prem network drive.

Replaces this template's usual Microsoft Graph API / SharePoint ingestion
path entirely: LEX's contracts library was confirmed to be a genuine
network file share (a UNC path over SMB), not SharePoint, so there is no
Graph API, no OAuth token, and no Azure AD app registration anywhere in
this codebase.

Built on `smbprotocol` (PyPI) via its `smbclient` submodule, which exposes
an os/os.path-like API (listdir, stat, open_file) over SMB2/3 — needed
since modern Windows Server file shares generally reject the older SMB1
that simpler libraries like pysmb assume.

Requires LEX's network drive host to be reachable from the Streamlit
container runtime's outbound network — a Snowflake External Access
Integration network rule for <host>:445 (see
sql/test_network_drive_connectivity.sql), which in turn requires the
existing Private Link / VPN connectivity between Snowflake and the MTM
network (per the architecture doc) to actually route raw SMB traffic, not
just HTTPS.

CONFIRMED against a live network share via the bridge host
(pipeline/network_drive_browser_app.py): the initial version passed
domain= to smbclient.register_session(), which doesn't accept it in the
installed smbprotocol version (TypeError: register_session() got an
unexpected keyword argument 'domain') — fixed by folding the NTLM domain
into the username as "DOMAIN\\username" instead, which is what
register_session actually expects.

Also CONFIRMED: LEX's network drive path (\\metrotrains.local\apps$) is a
domain-based DFS namespace, not a plain file server — smbclient follows
the DFS referral transparently and opens a second connection to whatever
server the referral names (observed as MTADFS201V.metrotrains.local),
which register_session() can't pre-register since that hostname isn't
known until the referral response arrives mid-request. Without a fallback
credential, that second connection attempted anonymous auth and hung
until the server reset it. Fixed by also calling
smbclient.ClientConfig(username=..., password=...) once — its
username/password serve as the default credential for any connection the
library opens on its own, referral targets included.

Still UNVERIFIED for the Streamlit-in-Snowflake path specifically (only
the standalone bridge host has been tested so far): if that path doesn't
connect, the two most likely culprits are (1) the network path only
permitting HTTPS egress, not raw SMB/445, and (2) smbprotocol's compiled
dependency (cryptography) failing to resolve via this account's PyPI
access integration, the same class of failure other packages have hit
elsewhere in this codebase's history.
"""

import os
from dataclasses import dataclass
from typing import List, Optional

import smbclient
import smbclient.path

from utils.logging_utils import get_logger

logger = get_logger(__name__)

_registered_hosts = set()


class NetworkDriveError(Exception):
    pass


@dataclass
class NetworkDriveItem:
    item_id: str              # the file's UNC path — a stable per-file identity
    name: str
    path: str
    size_bytes: int = 0
    last_modified: Optional[float] = None


def get_network_drive_credentials(alias: str = "network_drive_credential"):
    """
    Reads the username/password pair bound to this app under the given
    local alias by the deploy cell's SECRETS = ('network_drive_credential'
    = <PASSWORD-type secret>) clause — see
    pipeline/00_provision_project.ipynb. Container runtime (what this app
    runs on) exposes a PASSWORD-type secret as st.secrets[alias] with
    username/password fields; warehouse runtime's equivalent,
    _snowflake.get_username_password_secret(alias), is tried as a fallback
    in case warehouse runtime is ever used.

    UNVERIFIED for a PASSWORD-type secret specifically (GENERIC_STRING
    secrets, used elsewhere in this codebase for the old Graph API path,
    are confirmed working) — if this fails, check Snowflake's current docs
    for exactly how a PASSWORD-type secret surfaces under st.secrets, and
    adjust the attribute/key access below.

    Falls through to NETWORK_DRIVE_USERNAME/NETWORK_DRIVE_PASSWORD
    environment variables as a third option, for standalone scripts that
    run entirely outside Snowflake/Streamlit — e.g.
    pipeline/network_drive_to_stage.py, a bridge agent run from inside the
    MTM network while direct Snowflake-to-network-drive connectivity is
    still being sorted out with the Networks team.
    """
    try:
        import streamlit as st
        cred = st.secrets[alias]
        username = cred["username"] if isinstance(cred, dict) else cred.username
        password = cred["password"] if isinstance(cred, dict) else cred.password
        return username, password
    except Exception:
        pass

    try:
        import _snowflake
        cred = _snowflake.get_username_password_secret(alias)
        return cred.username, cred.password
    except Exception:
        pass

    username = os.environ.get("NETWORK_DRIVE_USERNAME")
    password = os.environ.get("NETWORK_DRIVE_PASSWORD")
    if username and password:
        return username, password

    raise NetworkDriveError(
        "Could not resolve network drive credentials from st.secrets, "
        "_snowflake, or the NETWORK_DRIVE_USERNAME/NETWORK_DRIVE_PASSWORD "
        "environment variables."
    )


_client_config_set = False


def _ensure_registered(project) -> None:
    global _client_config_set
    host = project.network_drive_host
    username, password = get_network_drive_credentials()
    domain = project.network_drive_domain
    # smbclient.register_session() has no domain= parameter (confirmed
    # against the installed smbprotocol version) — NTLM domain has to be
    # embedded in the username as "DOMAIN\username" instead. Only prefix
    # if the caller hasn't already qualified it themselves (a bare
    # backslash or a UPN-style user@domain).
    if domain and "\\" not in username and "@" not in username:
        username = f"{domain}\\{username}"

    if not _client_config_set:
        # CONFIRMED against a live share: LEX's network drive path is a
        # domain-based DFS namespace (\\metrotrains.local\apps$) — the
        # library follows the DFS referral transparently and opens a
        # second connection to whatever server the referral names (seen
        # in the logs as e.g. MTADFS201V.metrotrains.local), which
        # register_session() below can't pre-register since that hostname
        # isn't known until the referral response arrives mid-request.
        # ClientConfig's username/password act as the fallback credential
        # for any such connection the library opens on its own, which is
        # what was missing (the referral connection was attempting anonymous
        # auth — "Initialising session with username: None" — and hanging
        # until the server reset it).
        smbclient.ClientConfig(username=username, password=password)
        _client_config_set = True

    if host in _registered_hosts:
        return
    smbclient.register_session(host, username=username, password=password)
    _registered_hosts.add(host)


def _unc_path(project, relative_path: str = "") -> str:
    base = f"\\\\{project.network_drive_host}\\{project.network_drive_share}"
    sub = (relative_path or project.network_drive_default_path or "").strip("\\/")
    return f"{base}\\{sub}" if sub else base


def list_files(project, relative_path: str = "", recursive: bool = True) -> List[NetworkDriveItem]:
    """Lists files under the share (optionally a subfolder), recursively by
    default — a contracts library is commonly organized into
    year/client/type subfolders. Materializes iter_files() into a list —
    see iter_files() for a streaming version."""
    return list(iter_files(project, relative_path, recursive))


def iter_files(project, relative_path: str = "", recursive: bool = True):
    """Streaming version of list_files(): yields each NetworkDriveItem as
    it's found instead of waiting for the whole (possibly large, deeply
    nested) recursive walk to finish first. A contracts library with
    hundreds of CW folders, each many subfolders deep, can take a while to
    walk in full — a caller like Streamlit can use this to update its UI
    incrementally rather than showing a single spinner for the entire
    walk."""
    _ensure_registered(project)
    root = _unc_path(project, relative_path)
    yield from _iter_dir(root, recursive)


def _iter_dir(root: str, recursive: bool):
    try:
        entries = smbclient.listdir(root)
    except Exception as e:
        raise NetworkDriveError(f"Could not list '{root}': {e}") from e

    for name in entries:
        full = f"{root}\\{name}"
        try:
            if smbclient.path.isdir(full):
                if recursive:
                    yield from _iter_dir(full, recursive=True)
                continue
            info = smbclient.stat(full)
            yield NetworkDriveItem(
                item_id=full, name=name, path=full,
                size_bytes=getattr(info, "st_size", 0),
                last_modified=getattr(info, "st_mtime", None),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("EVENT=NETWORK_DRIVE_LIST_ITEM_FAILED path=%s error=%s", full, e)


def download_file(project, item_path: str) -> bytes:
    _ensure_registered(project)
    try:
        with smbclient.open_file(item_path, mode="rb") as f:
            return f.read()
    except Exception as e:
        raise NetworkDriveError(f"Could not download '{item_path}': {e}") from e
