# pipeline/

`00_provision_project.ipynb` runs inside the Snowflake Workspace (one-time
setup). Everything else here is the **network drive bridge** — a stopgap
for while Snowflake can't reach the MTM network drive directly (DNS
resolution issue, see `sql/test_network_drive_connectivity.sql`). It runs
on a Linux host *inside* the MTM network and pushes files out to Snowflake
instead.

## Files

| File | Purpose |
|---|---|
| `environment.yml` | Conda env definition for the bridge host |
| `network_drive_to_stage.py` | CLI: syncs files from the network drive to a Snowflake stage |
| `network_drive_browser_app.py` | Streamlit UI over the same logic — browse a folder, pick files, stage them |

Both scripts import from `../python/` (`config.py`, `contract_linking.py`,
`required_contracts.py`, `ingestion/xlsx_parser.py`, `utils/logging_utils.py`,
`utils/network_drive_client.py`). If copying this folder out on its own,
bring those files too, keeping the same relative layout:

```
<bridge-folder>/
├── pipeline/   (this folder's contents)
└── python/     (the 6 files listed above, in their existing subfolders)
```

## Setup

```bash
conda env create -f environment.yml
conda activate lex_network_bridge
```

Set these environment variables (see `network_drive_to_stage.py`'s module
docstring for full detail, including the recommended key-pair service user):

```
SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY_PATH (or SNOWFLAKE_PASSWORD)
NETWORK_DRIVE_USERNAME, NETWORK_DRIVE_PASSWORD
```

## Usage

**CLI** — sync specific files or everything eligible:

```bash
python network_drive_to_stage.py --files "Ariba/CW14465_Executed.pdf" "Ariba/CW20841_Executed.pdf"
python network_drive_to_stage.py --all
```

**Streamlit UI** — browse and stage interactively:

```bash
streamlit run network_drive_browser_app.py
```

The UI searches only the CW folders listed in `Contracts.xlsx`, which
**you maintain directly in this folder** (next to the script) — a single
column headed `Contract_Workspace_ID`, one CW number per row. Not
committed to the repo (it's data, not code — see `.gitignore`). Edit the
file, then click **Reload Contracts.xlsx** in the app to pick up changes
without restarting it.

**Verify what landed:**

```sql
LIST @MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STAGE;
```

Staging only copies raw bytes into `NETWORK_DRIVE_INBOX_STAGE` — picking
those files up into `RAW_DOCUMENTS` (parsing, hashing, contract linking)
is a separate, not-yet-built step.
