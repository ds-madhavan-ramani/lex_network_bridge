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

## Network drive location config (NETWORK_DRIVE_HOST/SHARE/DEFAULT_PATH)

`get_snowflake_connection()`'s caller — `load_drive_config()` in
`network_drive_to_stage.py` — reads `NETWORK_DRIVE_HOST`,
`NETWORK_DRIVE_SHARE`, and `NETWORK_DRIVE_DEFAULT_PATH` with a plain
`SELECT` against `MEDSCOMA.APP_CATALOG.PROJECTS WHERE PROJECT_CODE =
'LEX'`. These are **not environment variables** — deliberately: they live
once in Snowflake so this repo and `lex_contracts_intel` (which owns that
table) always agree on the same values, instead of every bridge host
needing its own copy kept in sync. `bridge_env.sh` only sets
`NETWORK_DRIVE_DOMAIN` as an optional override; the other three have no
env var equivalent.

**This means they don't survive a teardown/rebuild of `APP_CATALOG`.**
Dropping and recreating that schema (e.g. re-running
`lex_contracts_intel`'s `sql/00_setup_catalog.sql`, or a full
teardown/reprovision of the LEX project) drops the `PROJECTS` table and
the whole LEX row with it — not just these three columns. A plain restart
of this repo's Streamlit app or CLI does **not** require redoing this;
only a schema/table rebuild does.

After such a rebuild, first redo `lex_contracts_intel`'s normal
provisioning (recreate the LEX project row via `CREATE_PROJECT`), then
set these three fields. If the `pipeline/00_provision_project.ipynb`
notebook (in `lex_contracts_intel`) is available, use its "Set LEX's
network drive location" cell — it binds values as parameters, which
avoids a real gotcha: a raw SQL `UPDATE` with a literal path treats `\`
as an escape character in single-quoted strings, silently dropping it
(`AppData\prd\...` becomes `AppDataprd...`) unless every backslash is
doubled.

Without notebook access — e.g. running directly from this bridge host —
the same bind-parameter safety is available via this repo's own
Snowflake connection helper, run once after a rebuild:

```bash
python3 << 'EOF'
import sys
sys.path.insert(0, "pipeline")
from network_drive_to_stage import get_snowflake_connection

conn = get_snowflake_connection()
cur = conn.cursor()

cur.execute(
    "SELECT NETWORK_DRIVE_HOST, NETWORK_DRIVE_SHARE, NETWORK_DRIVE_DEFAULT_PATH, "
    "NETWORK_DRIVE_DOMAIN FROM MEDSCOMA.APP_CATALOG.PROJECTS WHERE PROJECT_CODE = 'LEX'"
)
print("Before:", cur.fetchone())

# %s bind params -- immune to the backslash-literal-escaping gotcha above.
cur.execute(
    "UPDATE MEDSCOMA.APP_CATALOG.PROJECTS SET "
    "NETWORK_DRIVE_HOST = %s, NETWORK_DRIVE_SHARE = %s, NETWORK_DRIVE_DEFAULT_PATH = %s "
    "WHERE PROJECT_CODE = 'LEX'",
    ("metrotrains.local", "apps$", "AppData\\prd\\MR5Documents\\Ariba"),
)

cur.execute(
    "SELECT NETWORK_DRIVE_HOST, NETWORK_DRIVE_SHARE, NETWORK_DRIVE_DEFAULT_PATH, "
    "NETWORK_DRIVE_DOMAIN FROM MEDSCOMA.APP_CATALOG.PROJECTS WHERE PROJECT_CODE = 'LEX'"
)
print("After:", cur.fetchone())
conn.close()
EOF
```

Run with `bridge_env.sh` already sourced (reuses its Snowflake
credentials). Then restart this repo's Streamlit app / re-run the CLI —
no other config needed.

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

Files are staged under a **per-CW subfolder**, not flat —
`@NETWORK_DRIVE_INBOX_STAGE/<CW_NUMBER>/<filename>`. The CW number comes
from the folder actually searched (the browser app's `CW_Folder` column,
or a best-effort regex on the path for the CLI's `--all`/`--files`) —
never guessed at ingest time. This is what lets the pickup step (below)
auto-link a file to its contract safely.

**Verify what landed:**

```sql
LIST @MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STAGE;
```

Staging only copies raw bytes into `NETWORK_DRIVE_INBOX_STAGE` — picking
those files up into `RAW_DOCUMENTS` (parsing, contract linking, indexing,
extraction) is a scheduled Snowflake Task in the main `lex_contracts_intel`
repo (`python/ingestion/stage_pickup.py`, `sql/04_stage_pickup_task.sql`),
not part of this repo.
