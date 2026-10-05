# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

"playbook" processes the "LCR Playbook" and "LCR Top20" options-trading PDFs into structured
trade-suggestion tables (symbol, strike, entry/target/stop prices, etc.) and loads them into a
MySQL database. The whole pipeline lives in two Jupyter notebooks; `dataUtil.py` is the only
shared Python module (DB access + symbol-list loading).

There is no package/app code, no test suite, and no build step — this is an analyst's notebook
workflow, not a library.

## Repository layout

- `ETF_1_Code_v3_UBugfix2.ipynb` — parses the "Playbook" PDF (ETF/bond trade suggestions,
  Investor vs. Trader trends) and writes rows to the `Trading.ETF_Options` table.
- `Top20-Ubuntu.ipynb` — parses the "Top20" PDF (per-stock entry/target/stop levels) and writes
  rows to the `Trading.Stock_Options` table.
- `Top20_2_Code.ipynb` — v2 of the above: same parsing (written with `df.loc` so it works on
  pandas >= 2), plus the printed-page-1 market commentary captured and sent to llmwiki.
- `top20_stock.py` — CLI version of `Top20_2_Code.ipynb`, same shape as `playbook_etf.py`:
  scans a folder of `*Top20 YYYY-MM-DD.pdf`, loads rows to `Trading.Stock_Options_v1` (skipping
  already-loaded dates) and posts the commentary to llmwiki.
- `playbook_etf.py` — CLI version of `ETF_2_Code.ipynb`: scans a folder of Playbook PDFs, loads
  rows to `Trading.ETF_Options_v1` (skipping already-loaded dates) and sends the page-2 market
  commentary to llmwiki.
- `llm_repair.py` — shared by both CLI scripts: the `ParseReport` the parsers record their flagged
  blocks in, an OpenAI-standard LLM client (`LLM_BASE_URL`/`LLM_MODEL`, any compatible endpoint
  including a local ollama/vLLM), `repair_block`, the `validate_rows` gate that rejects invented
  numbers, and the `.repairs.json` audit log. See `docs/PDFreader.md`.
- `hil_review.py` — the human-in-the-loop half: a decisions store keyed by a fingerprint of the
  block's text (`.pdfreader-decisions.json`, kept next to the PDFs), `apply_decisions` to replay
  recorded answers on every later run, and the `--review` terminal prompt for the blocks neither the
  regexes nor the LLM could settle.
- `.claude/skills/lcr-playbook-pdf/`, `.claude/skills/lcr-top20-pdf/` — the per-publication
  `SKILL.md` (how to run, how to read the reports) plus `reference/*-extraction-spec.md`, the
  document grammar and target schema. The spec file is also the system prompt the LLM repair sends,
  so it is the single source of truth for what a row means — update it with the parser.
- `dataUtil.py` — `DU` module imported by both notebooks: builds the SQLAlchemy/PyMySQL engine
  from env vars, and provides `load_df`, `load_eod_price`, `load_symbols`, `StoreEOD`, etc. for
  reading/writing the market-data and options tables.
- `DB_Config.env` — `.env`-style file with DB host/user/password and table names, loaded via
  `load_dotenv("DB_Config.env")` inside each notebook (not via `.env`, and not currently
  git-ignored — see Security note below).
- `temp/` — git-ignored scratch area holding older/experimental notebook versions and one-off
  CSV exports; not part of the current pipeline, safe to ignore.
- `.ipynb_checkpoints/` — Jupyter's own autosave copies; ignore.

## Notebook pipeline (the actual "architecture")

Both notebooks follow the same shape and must be read top-to-bottom — cell order is meaningful
and later cells depend on module-level variables set earlier (`df`, `final_df`, `file`,
`InputDate`, etc.):

1. **Locate & read the source PDF** — a cell hardcodes several candidate Windows/WSL paths to the
   same Google-Drive-synced PDF (commented out except the last one, `/mnt/i/My Drive/...`), then
   extracts text per-page with PyMuPDF (`fitz`). `InputDate` and the filename must be updated by
   hand before each run.
2. **Line-by-line text parsing** — the extracted text is iterated line-by-line with `re` to pull
   out ticker, strikes, entry/target/stop prices into a `pandas.DataFrame`. This parsing is
   brittle and tied exactly to the PDF's current layout/wording (e.g. looks for literal strings
   like `"Bond ETF Trade & Maintenance Suggestions"`, `"Entry @ "`, bracket-wrapped tickers
   `[XXX]`). If the PDF template changes, this is the code to inspect first.
3. **Forward-fill / sign derivation** — `for index, row in df.iterrows()` loops backfill Entry/
   Target/Stop values and signs (`<`/`>`) from the previous row when a stock spans multiple lines
   (`ETF_1_Code_v3_UBugfix2.ipynb`), or derive them from the `PnC` (put/call) column
   (`Top20-Ubuntu.ipynb`).
4. **Reshape & export** — columns are reordered into the final schema and written to a CSV next
   to the source PDF (`file.replace('.pdf', '.csv')`), then re-read back from that CSV before the
   DB step.
5. **Load to MySQL** — `load_dotenv("DB_Config.env")` populates env vars, then
   `DU.StoreEOD(df, "Trading", "<table>")` appends the DataFrame to the target table via
   `to_sql(..., if_exists='append')`. There is no dedupe/upsert — re-running a notebook against
   the same date will append duplicate rows.

When editing the parsing logic, change the corresponding notebook's cells only — there is no
shared parsing code between the two notebooks even though the logic is very similar; a fix to one
does not propagate to the other.

## Environment / running

- The checked-in `.venv` is **Python 3.11.17** with pandas 3.0 (rebuilt since the 3.8 one the
  notebooks were written against) — run everything with `.venv/bin/python`.
- Install deps: `pip install -r requirements.txt` (pandas, numpy, pdfplumber, PyMuPDF, notebook,
  python-dotenv, PyMySQL, SQLAlchemy).
- Run via Jupyter: `jupyter notebook` (or open in VS Code) and execute cells top-to-bottom.
- The CLI scripts take **a single PDF or a folder**, and the safe sequence is always
  `--csv-only` (CSV + extracted text + parse/repair reports, no DB, no llmwiki) → inspect →
  re-run to load. `--date YYYY-MM-DD` picks one date; `--replace-date` re-loads a date that is
  already in the table (DELETE + INSERT in one transaction, via `DU.ReplaceDate`); `--llm
  off|repair|force` controls the LLM repair of flagged blocks (default `repair`); `--review` asks
  about whatever is still unsettled and records the answer so it is never asked again. Full design
  and the defect catalogue: `docs/PDFreader.md`.
- Batch/CLI alternative for the Playbook PDF: `python playbook_etf.py <folder> [--dry-run]`
  processes every `*Playbook YYYY-MM-DD.pdf` in the folder with the same logic as
  `ETF_2_Code.ipynb` (one function per notebook stage), skips dates already in
  `Trading.ETF_Options_v1`, and posts the page-2 market commentary to llmwiki `/ingest`. Design
  and options are in `docs/Design-Plan.md` → "Batch script". `python top20_stock.py <folder>
  [--dry-run]` does the same for `*Top20 YYYY-MM-DD.pdf` → `Trading.Stock_Options_v1` (table name
  from `TBLSTOCKOPTIONS`).
- Source PDFs are read from a Google-Drive path mounted in WSL (`/mnt/i/My Drive/...`) — that
  mount and the specific dated PDF filename must exist locally before the first cell will run.
- DB config is loaded from `DB_Config.env` (via `load_dotenv`), not `.env`; `dataUtil.py` reads
  `DBHOST`, `DBPORT`, `DBUSER`, `DBPWD`, `DBMKTDATA`, `TBLDLYPRICE`, `PROD_LIST_DIR`, etc. from
  the environment.

## Security note

`DB_Config.env` contains a live MySQL host, username, and **plaintext password**, and is
currently **untracked but not git-ignored** (`.gitignore` excludes `.env`, not `DB_Config.env`).
Do not `git add`/commit this file, and treat its contents as a secret when reading or quoting it.
If asked to touch `.gitignore` or DB config handling, flag this mismatch and prefer renaming the
file to `.env` (or adding `DB_Config.env` to `.gitignore`) over leaving it exposed.
