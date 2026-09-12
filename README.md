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

Keys that matter for the batch script:

| Key | Purpose |
|---|---|
| `DBHOST`, `DBPORT`, `DBUSER`, `DBPWD`, `DBMKTDATA` | MySQL connection (`dataUtil.py` builds the engine from these) |
| `DBTRADING` / `TBLETFOPTIONS` | Target DB / table for parsed rows (default `Trading` / `ETF_Options_v1`) |
| `LLMWIKI_BASE_URL`, `LLMWIKI_API_TOKEN` | llmwiki `POST /ingest` endpoint for the commentary; leave `LLMWIKI_BASE_URL` unset to skip |
| `MAX_LLMWIKI_RETRY`, `LLMWIKI_TIMEOUT` | Retry count / seconds per llmwiki call (default 5 / 30) |

The remaining keys in `.env.example` are used by other `dataUtil.py` helpers and can stay as
placeholders.

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

# Full run against the Google-Drive folder
python playbook_etf.py "/mnt/i/My Drive/Playbook"

# Only PDFs dated on/after a date
python playbook_etf.py "/mnt/i/My Drive/Playbook" --since 2026-09-01

# Re-load a date that is already in the DB (rows are appended, not replaced — dedupe by hand)
python playbook_etf.py "/mnt/i/My Drive/Playbook" --since 2026-09-07 --force

# Load to DB but don't post commentary; write outputs somewhere else; verbose logging
python playbook_etf.py "/mnt/i/My Drive/Playbook" --skip-llmwiki --out-dir /tmp/pb --log-level DEBUG
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

## Running the notebooks

```bash
source .venv/bin/activate
jupyter notebook          # or open the .ipynb in VS Code and pick the .venv kernel
```

Execute cells top-to-bottom — later cells depend on variables set by earlier ones. Before each
run, edit the first cell to point at the correct PDF path and `InputDate`. The notebooks have no
"already loaded" check, so re-running one against the same date appends duplicate rows.

## Repository layout

- `playbook_etf.py` — batch CLI (one function per notebook stage: `extract_text` →
  `parse_trades` → `derive_columns` → `finalize` → `store` / `upload_commentary`).
- `dataUtil.py` — DB engine + read/write helpers shared by the script and notebooks.
- `ETF_2_Code.ipynb`, `ETF_1_Code_v3_UBugfix2.ipynb`, `Top20-Ubuntu.ipynb` — notebook pipelines.
- `docs/Design-Plan.md` — design notes for the batch script (PDF layout, split markers,
  combo-trade handling); `docs/HISTORY.md`, `docs/TODOS.md`.
- `test/` — sample Playbook PDFs with their parsed CSV / commentary output.
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
