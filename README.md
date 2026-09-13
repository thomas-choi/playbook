# playbook

Processes the **912/LCR Playbook** and **LCR Top20** options-trading PDFs into structured
trade-suggestion rows (symbol, strikes, entry/target/stop, quantity, …), loads them into MySQL,
and posts the Playbook's page-2 market commentary to llmwiki.

| Source PDF | Runner | Target table |
|---|---|---|
| `*Playbook YYYY-MM-DD.pdf` | `playbook_etf.py` (CLI, batch) or `ETF_2_Code.ipynb` | `Trading.ETF_Options_v1` |
| Playbook (older format) | `ETF_1_Code_v3_UBugfix2.ipynb` | `Trading.ETF_Options` |
| Top20 | `Top20-Ubuntu.ipynb` | `Trading.Stock_Options` |

`dataUtil.py` is the only shared module (SQLAlchemy/PyMySQL engine + `load_df_SQL` / `StoreEOD`).

## Setup

```bash
git clone <repo-url> playbook && cd playbook
python3 -m venv .venv            # the checked-in .venv is Python 3.11
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

All config is read from **`DB_Config.env`** (not `.env`) via `python-dotenv`. It is git-ignored;
create it from the template and fill in real values:

```bash
cp .env.example DB_Config.env
```

Keys that matter for the batch scripts:

| Key | Purpose |
|---|---|
| `DBHOST`, `DBPORT`, `DBUSER`, `DBPWD`, `DBMKTDATA` | MySQL connection (`dataUtil.py` builds the engine from these) |
| `DBTRADING` / `TBLETFOPTIONS` | Target DB / table for `playbook_etf.py` rows (default `Trading` / `ETF_Options_v1`) |
| `TBLSTOCKOPTIONS` | Target table for `top20_stock.py` rows (default `Stock_Options`) |
| `LLMWIKI_BASE_URL`, `LLMWIKI_API_TOKEN` | llmwiki `POST /ingest` endpoint for the commentary; leave `LLMWIKI_BASE_URL` unset to skip |
| `MAX_LLMWIKI_RETRY`, `LLMWIKI_TIMEOUT` | Retry count / seconds per llmwiki call (default 5 / 30) |

The remaining keys in `.env.example` are used by other `dataUtil.py` helpers and can stay as
placeholders.

## Getting the PDFs

The source PDFs live in Google Drive under `ReadProjects/Neural Matrix Investment/LCR/`
(`Playbooks/` and `Top20s/`). How you reach them depends on the host:

| Host | Approach | PDF folder to pass to the script / notebooks |
|---|---|---|
| WSL on Windows (Google Drive for desktop installed, drive `I:`) | mount the Windows drive | `/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/{Playbooks,Top20s}` |
| Pure Ubuntu host (no Google Drive client) | `rclone` pull into `~/lcr/prod` | `~/lcr/prod/Playbooks`, `~/lcr/prod/top20` |

### WSL on Windows — mount the Google Drive letter

Google Drive for desktop keeps the folder in sync on the Windows side; WSL just needs the drive
mounted. Do this once per WSL session (the mount does not survive a WSL restart):

```bash
sudo mkdir -p /mnt/i
sudo mount -t drvfs I: /mnt/i
ls "/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/Playbooks" | tail
```

Then point the script / notebooks at the mounted path, e.g.
`python playbook_etf.py "/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/Playbooks"`.
No sync step is needed — the files are always current. To mount automatically at WSL start, add
`I: /mnt/i drvfs defaults 0 0` to `/etc/fstab`.

### Pure Ubuntu host — pull with rclone

`rclone` (remote `gdrive:`, set up with `rclone config`) mirrors the two Drive folders locally:

| Google Drive folder | Local folder |
|---|---|
| `ReadProjects/Neural Matrix Investment/LCR/Playbooks` | `~/lcr/prod/Playbooks` |
| `ReadProjects/Neural Matrix Investment/LCR/Top20s` | `~/lcr/prod/top20` |

**Before doing any work, pull the latest PDFs:**

```bash
~/lcr/bin/sync_lcr.sh
tail ~/lcr/sync.log            # see what was fetched
```

The script uses `rclone copy --include "*.pdf"`: it only downloads new/changed PDFs and never
deletes anything locally, so the `.csv` / `_commentary.txt` files that `playbook_etf.py` writes
next to each PDF are preserved. (Do **not** use `rclone sync` on these folders — it would delete
those generated files.) Re-running it when nothing changed is cheap and safe.

To run the sync automatically every hour instead, install the systemd user timer that sits next to
the script:

```bash
mkdir -p ~/.config/systemd/user
cp ~/lcr/bin/lcr-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lcr-sync.timer
loginctl enable-linger $USER   # keep it running while logged out
systemctl --user list-timers lcr-sync.timer
```

The last line of `sync_lcr.sh` (commented out) runs `playbook_etf.py` on the Playbooks folder
after each sync; enable it for a one-command "fetch + load".

## Running the batch script (`playbook_etf.py`)

Scans a folder for `*Playbook YYYY-MM-DD.pdf` (e.g. `912 Playbook 2026-09-07.pdf`,
`LCR Playbook 2022-11-21.pdf`), oldest first, and for each PDF:

1. extracts text (PyMuPDF), splitting page 2 into the trade table and the
   "Market Expectations …" commentary,
2. parses every `STO`/`BTO` leg into a row and derives `Low-High`, sign columns, stops,
3. writes `<pdf-name>.csv` and `<pdf-name>_commentary.txt` next to the PDF (or `--out-dir`),
4. appends the rows to `Trading.ETF_Options_v1` — dates already in the table are skipped,
5. posts the commentary to llmwiki.

```bash
source .venv/bin/activate

# Parse only: write CSV + commentary files, no DB, no llmwiki (safe to run anytime)
python playbook_etf.py test --dry-run

# Full run against the Google-Drive folder (see "Getting the PDFs" for your host).
# Pure Ubuntu: run ~/lcr/bin/sync_lcr.sh first
python playbook_etf.py ~/lcr/prod/Playbooks
# WSL on Windows: mount I: first
python playbook_etf.py "/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/Playbooks"

# Only PDFs dated on/after a date
python playbook_etf.py ~/lcr/prod/Playbooks --since 2026-09-01

# Re-load a date that is already in the DB (rows are appended, not replaced — dedupe by hand)
python playbook_etf.py ~/lcr/prod/Playbooks --since 2026-09-07 --force

# Load to DB but don't post commentary; write outputs somewhere else; verbose logging
python playbook_etf.py ~/lcr/prod/Playbooks --skip-llmwiki --out-dir /tmp/pb --log-level DEBUG
```

| Option | Description |
|---|---|
| `folder` | Directory to scan for `*Playbook YYYY-MM-DD.pdf` |
| `--since YYYY-MM-DD` | Only process PDFs dated on/after this |
| `--force` | Process dates already present in the DB (appends duplicates) |
| `--skip-db` | Parse and write CSV/commentary only |
| `--skip-llmwiki` | Don't send commentary to llmwiki |
| `--dry-run` | Shorthand for `--skip-db --skip-llmwiki` |
| `--env-file FILE` | Dotenv file (default `DB_Config.env`) |
| `--out-dir DIR` | Where to write CSV/commentary (default: next to the PDF) |
| `--log-level LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, … |

Exit code is `0` on success (or nothing to do), `1` if any PDF failed, `2` if `folder` is not a
directory. A failed PDF is logged with a traceback and the run continues with the next one.

Sample PDFs live in [test/](test/); `python playbook_etf.py test --dry-run` regenerates their
`.csv` / `_commentary.txt` files, so `git diff test/` shows the effect of a parser change.

## Running the Top20 batch script (`top20_stock.py`)

The Top20 counterpart of `playbook_etf.py` (script version of `Top20_2_Code.ipynb`), with the
same options, dedupe rule, exit codes and llmwiki policy. Scans a folder for
`*Top20 YYYY-MM-DD.pdf` (e.g. `912 Top20 2025-07-21.pdf`, `LCR Top20 2022-11-21.pdf`), oldest
first, and for each PDF:

1. extracts text (PyMuPDF): physical page 1 (disclosure) is skipped, the printed "Page 1"
   (physical page 2) yields the "Market Expectations …" commentary, pages 3+ the per-stock text,
2. parses one row per stock (`Entry @ lo-hi`, `Target @`, `STO/BTO <strike> Put|Call (exp)`,
   `… @ SYM <price> Stop`) and derives the sign columns (entry range swapped for Calls),
3. writes `<pdf-name>.csv` and `<pdf-name>_commentary.txt` next to the PDF (or `--out-dir`),
4. appends the rows to `Trading.Stock_Options` — dates already in the table are skipped,
5. posts the commentary to llmwiki (`LCR Top20 Market Commentary YYYY-MM-DD`).

```bash
python top20_stock.py test --dry-run                       # parse only, safe anytime
python top20_stock.py ~/lcr/prod/top20                     # pure Ubuntu (after sync_lcr.sh)
python top20_stock.py "/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/Top20s"   # WSL
python top20_stock.py ~/lcr/prod/top20 --since 2025-01-01 --skip-llmwiki --log-level DEBUG
```

Known source-PDF quirks: a few 2023 issues omit the word `Stop` on one stock's exit line
(`… BTC Naked Call @ DASH 89.53`), so that row's `Stop` is blank/NULL — the notebook produces
the same. A 0-byte PDF in the folder is logged as failed and skipped.

## Running the notebooks

```bash
source .venv/bin/activate
jupyter notebook          # or open the .ipynb in VS Code and pick the .venv kernel
```

Make the PDFs available first (mount `I:` on WSL, or run `~/lcr/bin/sync_lcr.sh` on Ubuntu —
see "Getting the PDFs"), then execute cells top-to-bottom — later cells depend on variables set
by earlier ones. Before each run, edit the first cell to point at the correct PDF path
(`/mnt/i/My Drive/.../LCR/...` on WSL, `~/lcr/prod/Playbooks/...` or `~/lcr/prod/top20/...` on
Ubuntu) and `InputDate`. The notebooks have no "already loaded" check, so re-running one against
the same date appends duplicate rows.

## Repository layout

- `playbook_etf.py` — batch CLI (one function per notebook stage: `extract_text` →
  `parse_trades` → `derive_columns` → `finalize` → `store` / `upload_commentary`).
- `top20_stock.py` — same shape for the Top20 PDFs (`extract_text` → `parse_stocks` →
  `derive_columns` → `finalize` → `store` / `upload_commentary`) → `Trading.Stock_Options`.
- `dataUtil.py` — DB engine + read/write helpers shared by the script and notebooks.
- `ETF_2_Code.ipynb`, `ETF_1_Code_v3_UBugfix2.ipynb`, `Top20_2_Code.ipynb`, `Top20-Ubuntu.ipynb` —
  notebook pipelines (`Top20_2_Code` = `Top20-Ubuntu` + commentary → llmwiki).
- `docs/Design-Plan.md` — design notes for the batch script (PDF layout, split markers,
  combo-trade handling); `docs/HISTORY.md`, `docs/TODOS.md`.
- `test/` — sample Playbook and Top20 PDFs with their parsed CSV / commentary output.
- `.env.example` — template for `DB_Config.env`.

## Troubleshooting

- **`Column 'Low-High' cannot be null`** — every parsed row must have a `Low-High` value
  (`-100P` for a single leg, `+180C-205C` for a spread). Run with `--dry-run --log-level DEBUG`
  and inspect the CSV for blank `Low-High` cells; the culprit is usually a PDF line the parser
  didn't recognise (logged as `Exception on line: ...`).
- **`no market commentary captured from page 2`** — the "Market Expectations" heading (or the
  fallback "Copyright" line) wasn't found on page 2. Dump the page with `--log-level DEBUG`
  (`==> page 2`) and compare against the markers in `playbook_etf.py`.
- **PDF not picked up** — the filename must match `*Playbook YYYY-MM-DD.pdf` (case-insensitive,
  space or `-` before the date).
- **Date skipped** — it's already in `Trading.ETF_Options_v1`; use `--force` to append again.
