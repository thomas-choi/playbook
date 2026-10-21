# HISTORY.md

Chronological log of every code change, config change, and architectural decision in this
repository, per the project's mandatory change-logging rule. Newest entries at the top.

---

## 2026-10-20 — Stop tracking `.claude/`

- **Goal:** Have git ignore everything under `.claude/`.
- **Root cause:** `.claude/` was already in `.gitignore`, but ignore rules do not apply to files
  git already tracks. `.claude/skills/lcr-playbook-pdf/SKILL.md` and
  `.claude/skills/lcr-top20-pdf/SKILL.md` were committed before the rule was added.
- **Implementation detail:** Ran `git rm -r --cached .claude/`. The files stay on disk, and the next
  commit removes them from the repository. `llm_repair.spec_path` still reads the extraction spec
  from `.claude/skills/lcr-<name>-pdf/reference/`, so a fresh clone has no spec unless
  `LLM_SPEC_<NAME>` points to one. It then falls back to a minimal instruction.
- **Related files:** `.gitignore` (unchanged; `.claude/` was already listed), `docs/HISTORY.md`.
- **Test coverage:** No code changed, so no unit tests changed. Checked that `git status` shows both
  files as deleted from the index and that they are still on disk.

## 2026-10-20 — `.gitignore`: make the dated-CSV pattern match real dates

- **Goal:** Stop dated CSVs such as `912 Playbook 2026-10-19.csv` from being picked up as untracked.
- **Root cause:** `.gitignore` uses glob patterns, not regexes, so `*YYYY-MM-DD*.csv` only matched
  the literal letters `YYYY-MM-DD`.
- **Implementation detail:** The pattern is now `*[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*.csv`.
  This also ignores patch CSVs such as `ETF_2026-10-19.csv`.
- **Related files:** `.gitignore`, `docs/HISTORY.md`.
- **Test coverage:** No code changed, so no unit tests changed. Checked with `git check-ignore -v`:
  both dated CSVs are ignored, and `requirements.txt` and `ETF_2026-10-19.applied.json` are not.

## 2026-10-20 — `db_patch.py`: delete + insert of one key replaces the row; strip Excel's `'`

- **Goal:** Allow `ETF_2026-10-19.csv`, which deletes two SPCX rows and inserts them again with
  `Expiration` 2027-04-16. It failed with `duplicate primary key` on lines 4 and 5.
- **Root cause:**
  - `parse_patch` ran the loaders' duplicate-key check over all rows whatever their `Action`, so a
    delete and an insert of the same key counted as a duplicate.
  - Past that check, `plan()` would also have rejected the inserts ("a different row is already
    there"), because it did not know the same patch deletes those rows first.
  - The CSV's `Low-High` cells were saved from Excel as `'-110P`. That apostrophe became part of
    the key, so the deletes matched no row.
- **Implementation detail:**
  - The duplicate check now runs on the delete rows and the other rows separately. An `update` of
    a key the patch also deletes is still a `duplicate primary key`.
  - `plan()` treats a delete + insert of one key as a replace: both are applied, or both are
    no-ops once the row already equals the insert. The dry run shows `(replaces row)` with the
    columns that change. The insert row is built by the new helper `insert_row`.
  - `typed()` strips a leading `'` from every cell. The fix uses `startswith`, not
    `str.removeprefix`, because `.venv` is Python 3.8.
- **Related files:** `db_patch.py`, `tests/unit/test_db_patch.py`, `docs/PDFreader.md`,
  `docs/HISTORY.md`.
- **Test coverage:**
  - Added `test_delete_then_insert_same_key_replaces_row`: an ETF Expiration fix with `'`-prefixed
    `Low-High`. It applies, and re-applying it is a no-op.
  - Added `test_parse_rejects_two_inserts_of_one_key`.
  - All 33 unit tests pass. No tests removed.

## 2026-10-20 — Mask the DB password in `dataUtil.get_DBengine`'s log line

- **Goal:** Stop the MySQL password from being written to the logs in full.
- **Root cause:** `get_DBengine()` logged the whole connection URL, password included, at INFO
  level. Every CLI script turns INFO on, so each run printed the password.
- **Implementation detail:**
  - New `DU.mask_secret()` returns `xxxx` plus the last 4 characters. A secret of 4 characters or
    fewer becomes plain `xxxx`.
  - The log line now uses a URL built with the masked password. The engine still gets the real one.
  - The second log line (`dbconn=>Engine(...)`) was already masked by SQLAlchemy (`***`).
- **Related files:** `dataUtil.py`, `tests/unit/test_dataUtil.py`, `CLAUDE.md`, `docs/HISTORY.md`.
- **Test coverage:**
  - Added `tests/unit/test_dataUtil.py`, 6 cases:
    - `mask_secret` with a long secret, a 5-character secret, a 4-character secret, an empty
      secret and `None`.
    - `get_DBengine` logs the masked URL and never the password, with `create_engine` stubbed.
  - All 31 unit tests pass. No tests removed.

## 2026-10-20 — `db_patch.py`: hand corrections and missing rows for the `_v1` tables

- **Goal:** Correct specific cells of rows already in `Trading.ETF_Options_v1` /
  `Trading.Stock_Options_v1`, and add rows a loaded date is missing, without rebuilding the whole
  date from its PDF (`--replace-date` was the only way to change loaded data).
- **Implementation detail:**
  - New `db_patch.py <patch.csv|folder> --table etf|stock [--date D] [--apply]`. A patch CSV holds
    `update`/`insert`/`delete` rows keyed by the table's primary key; a row with a blank `Action`
    is skipped. It is a dry-run diff by default.
  - Before anything is written, it validates columns, types, `Trend`/sign/`PnC` values, NULL or
    duplicate keys (via the loaders' `split_loadable`), updates of missing rows, and inserts over a
    different existing row.
  - An ETF insert with a blank `Low-High` gets it built as `derive_columns` does. Blank signs come
    from `PnC`, and a blank `quantity` becomes 1. Updating the key parts `L_Strike`/`H_Strike`/`PnC`
    is refused.
  - It is idempotent, so it can be re-applied after a reload. Each apply appends the before and
    after rows to `<patch>.applied.json`.
  - New `DU.ApplyPatch`: deletes, updates and inserts in one transaction, rolled back on any error.
  - The two loaders' inline `astype` dicts moved to module constants `DB_TYPES`, so `db_patch.py`
    types cells the same way, and their `store()` replace path now warns that `db_patch.py`
    corrections for the date must be re-applied.
  - `pytest` was added to `requirements.txt`.
- **Related files:** `db_patch.py`, `dataUtil.py`, `playbook_etf.py`, `top20_stock.py`,
  `requirements.txt`, `tests/conftest.py`, `tests/unit/test_db_patch.py`, `CLAUDE.md`,
  `docs/PDFreader.md`, `docs/TODOS.md`, `.claude/skills/lcr-playbook-pdf/SKILL.md`,
  `.claude/skills/lcr-top20-pdf/SKILL.md`, `docs/HISTORY.md`.
- **Test coverage:**
  - Added the repo's first unit tests, `tests/unit/test_db_patch.py`: 25 cases on an in-memory
    SQLite copy of the live DDL, all passing.
    - Parsing both tables, typing, and the `--date` filter.
    - 10 rejection cases: unknown column, missing key column, bad number, bad Trend, bad sign,
      key-part update, NULL key, duplicate key, bad action, empty update.
    - Derived `Low-High`/signs/`quantity` on insert.
    - Rows with a blank `Action` are skipped.
    - Missing-row and conflicting-insert errors.
    - Dry run writes nothing.
    - Only the targeted cells change, and `NULL` clears a cell.
    - Idempotent re-apply.
    - A conflict applies nothing.
    - `ApplyPatch` rollback on a DB error.
    - Audit before/after.
    - Folder plus `--date`.
    - The `--replace-date` warning in both loaders.
  - No tests removed.
  - Regression: `--csv-only --llm off` CSVs for `LCR Playbook 2022-11-21` and `LCR Top20 2022-06-21`
    are byte-identical before and after the change.
  - Live: a dry run against MySQL 8.4 for 2026-10-19 matched the rows; `--apply` was run on
    `CREATE TABLE … LIKE` copies, changed exactly the targeted cells, and a second apply reported 0
    changes. The copies were dropped, and the live rows were not written.

## 2026-10-19 — Fix `Top20_2_Code.ipynb` producing an empty Stock_Options frame

- **Goal:** `Top20_2_Code.ipynb` stopped producing stock-option rows (the CSV was empty and
  `StoreEOD size: 0`), while the commentary → llmwiki step still worked.
- **Root cause:** In an uncommitted edit, the per-stock parsing cell (`for line in iterates:`, the
  logic from `Top20-Ubuntu.ipynb`) had been switched from a code cell to a **Markdown** cell, so it
  never ran. `df` kept only its blank seed row, `df[:-1]` dropped that row, and every later cell
  worked on an empty frame. The parsing logic itself was intact (the same as `Top20-Ubuntu.ipynb`,
  apart from the `df.loc` writes needed on pandas 3).
- **Implementation detail:** Changed the cell back to a code cell, without moving it. Also made two
  matches tolerant of PDF typos, which affected CPRT in the 2026-10-19 issue (the original notebook
  has the same flaws): the Status regex `\S*` → `\S+`, so a status line that starts with a space no
  longer gives `''`, and the marker `"Target @ "` → `"Target @"`, so `Target @22.00` is parsed.
  `top20_stock.py` already handled the second case.
- **Related files:** `Top20_2_Code.ipynb`, `docs/HISTORY.md`.
- **Test coverage:** The repo has no test suite. Ran the notebook's DataFrame-setup, parse and sign
  cells against `912 Top20 2026-10-19.pdf`: 20 rows, all with Symbol/Status/Expiration/PnC/
  Strike/Entry/Target/Stop filled in (CPRT before the fix: blank Status and Target). No tests
  added or removed. Not exercised: the DB load.

## 2026-10-04 — `playbook_etf.py` accepts `*Playbook_YYYY-MM-DD.pdf`

- **Goal:** Let the batch script pick up Playbook PDFs named with an underscore before the date
  (`Playbook_2026-09-07.pdf`), alongside the existing space and `-` forms.
- **Implementation detail:** `PDF_NAME_RE` changed from `Playbook[ -](...)` to
  `Playbook[ _-](...)`; the single-file error message names the accepted separators. The date is
  still taken from the filename, so nothing downstream changes.
- **Related files:** `playbook_etf.py`, `README.md`, `docs/Design-Plan.md`, `docs/HISTORY.md`.
- **Test coverage:** No test suite in the repo. Checked `PDF_NAME_RE` by hand against
  `912 Playbook 2026-09-07.pdf`, `Playbook-2026-09-07.pdf`, `Playbook_2026-09-07.pdf` (all match,
  date `2026-09-07`) and `Playbook2026-09-07.pdf` (no match).

## 2026-09-12 — Add `Top20_2_Code.ipynb` and `top20_stock.py` (Top20 commentary → llmwiki, batch CLI)

- **Goal:** Give the Top20 pipeline the same two upgrades the Playbook pipeline got: capture the
  market commentary from the printed page 1 and send it to llmwiki, and provide a folder-scanning
  CLI that mirrors `playbook_etf.py`.
- **Implementation detail:**
  - `Top20_2_Code.ipynb` — copy of `Top20-Ubuntu.ipynb` with: a commentary split on physical
    page 2 (from the `"Market Expectations"` line to the `Vol N Issue N` footer), saved as
    `<pdf>_commentary.txt` and posted to `POST {LLMWIKI_BASE_URL}/ingest` as
    `{text: "Date: …\n\n…", title: "LCR Top20 Market Commentary YYYY-MM-DD"}` with the
    retry-then-skip policy; `load_dotenv("DB_Config.env")` with a `.env` fallback; and the nine
    `df[col][row] = …` chained assignments in the parsing cell rewritten as `df.loc[row, col] = …`
    because the current `.venv` (Python 3.11 / pandas 3.0.5, Copy-on-Write) silently ignores
    chained assignment — the original notebook writes 20 all-NaN rows on that kernel.
  - `top20_stock.py` — one function per notebook stage (`find_top20_pdfs`, `extract_text`,
    `split_commentary`, `parse_stocks`, `derive_columns`, `finalize`, `upload_commentary`,
    `loaded_dates`/`store`, `process_pdf`, CLI) with the same flags as `playbook_etf.py`.
    Rows are collected as dicts (no seed row / `df[:-1]`); a line that fails to parse is logged
    and skipped instead of aborting; the `Expiration` slice is `.strip()`ped (the PDF text has a
    trailing space, which the explicit `%m/%d/%y` parse rejects); the `"Entry @"`/`"Target @"`
    markers no longer require a trailing space (eight 2023 issues print `Target @250.00`).
    Table name from `TBLSTOCKOPTIONS` (default `Stock_Options`).
  - `.env.example` — added `TBLSTOCKOPTIONS`. `README.md`, `CLAUDE.md` — documented both.
- **Verification:** `--dry-run` over the 29 PDFs in `~/lcr/prod/top20`: 27 × 20 rows with
  commentary (3.5–4.7 KB each); the 2 failures are 0-byte files in the mirror. Remaining blanks:
  four 2023 issues have one stock whose exit line omits the word `Stop`. Stubbed
  `DU.load_df_SQL`/`DU.StoreEOD` + a mock `/ingest` server: already-loaded dates skipped, typed
  DB frame matches the notebook's, bearer header / title / `Date:` body line correct; unreachable
  host with `MAX_LLMWIKI_RETRY=2` logs twice and skips. Not exercised: real MySQL, real llmwiki.
- **Related files:** `Top20_2_Code.ipynb`, `top20_stock.py`, `.env.example`, `README.md`,
  `CLAUDE.md`, `docs/HISTORY.md`.

## 2026-09-11 — Add `playbook_etf.py` batch script (folder scan → DB → llmwiki)

- **Goal:** Convert `ETF_2_Code.ipynb` into a re-runnable CLI that takes a folder, processes
  every `*Playbook YYYY-MM-DD.pdf` in it, appends the rows to `Trading.ETF_Options_v1`, and
  sends each PDF's page-2 market commentary to llmwiki with the date.
- **Implementation detail:**
  - `playbook_etf.py` (new) — one function per notebook stage (`extract_text`, `parse_trades`,
    `derive_columns`, `finalize`, `upload_commentary`, `loaded_dates`/`store`, `process_pdf`,
    `main`); the mapping to notebook cells is tabulated in `docs/Design-Plan.md` → "Batch script".
    Parsing logic is unchanged except that rows are built as a list of dicts instead of pandas
    chained assignment (pandas ≥ 2 compatibility; also removes the seed-row/`df[:-1]` trick), the
    section-header test is `>= 0` rather than `> 0`, a missing header now raises instead of
    silently parsing nothing, and `Expiration` is parsed with an explicit format. Dead cell 13
    (`nstop` computed and discarded) was not ported. Dedupe: `SELECT DISTINCT Date` from the
    target table at startup, skip loaded dates unless `--force`. llmwiki: `POST /ingest`
    `{text: "Date: YYYY-MM-DD\n\n…", title: "LCR Playbook Market Commentary YYYY-MM-DD"}` with the
    existing retry-then-skip policy (supersedes the notebook's multipart `/upload` stub). CLI
    flags: `--since`, `--force`, `--skip-db`, `--skip-llmwiki`, `--dry-run`, `--env-file`,
    `--out-dir`, `--log-level`. `import pymupdf as fitz` with a fallback to `import fitz` for
    older PyMuPDF.
  - `.env.example` — added `DBTRADING`, `TBLETFOPTIONS`, `LLMWIKI_TIMEOUT`.
  - `docs/Design-Plan.md` — appended the "Batch script: `playbook_etf.py`" section (CLI, stage →
    function map, dedupe rule, llmwiki payload, error policy, config); added Sequencing step 6 and
    noted Feature A's `/upload` plan is superseded by `/ingest`.
  - `docs/TODOS.md` — checked off Feature A; added a follow-up to run the script against the real
    Google-Drive folder and schedule it.
  - `CLAUDE.md` — listed the script in the layout and "Environment / running" sections.
  - `ETF_2_Code.ipynb` — untouched (kept as the interactive reference).
- **Related files:** `playbook_etf.py`, `.env.example`, `docs/Design-Plan.md`, `docs/TODOS.md`,
  `docs/HISTORY.md`, `CLAUDE.md`.
- **Test coverage:** no test suite exists (see `CLAUDE.md`). Verified in a scratch venv
  (Python 3.10, pandas 2.3, PyMuPDF 1.28) by: (1) running the notebook's cells 5–21 verbatim and
  the script's functions on the same cached page text of `Playbook-2026-09-07.pdf` — identical
  58-row output, same single pre-existing `[Bullish/Hold` warning; (2) building three synthetic
  Playbook PDFs (+ one non-matching file) from that text and running `--dry-run`/`--since` —
  correct file selection and ordering, CSV + `_commentary.txt` written, missing-`Copyright`
  page handled with a warning; (3) a mock `/ingest` HTTP server — correct path, bearer header,
  title and `Date:` body line; an unreachable host with `MAX_LLMWIKI_RETRY=2` — two logged
  failures then skip, run still completes; (4) stubbing `DU.load_df_SQL`/`DU.StoreEOD` — dates
  already "in the DB" are skipped and only the new date reaches `StoreEOD`. Not exercised: a
  real MySQL connection and a real llmwiki service (neither is reachable from this machine).

## 2026-09-09 — Implement page 1 parsing in new `ETF_2_Code.ipynb`

- **Goal:** Implement `docs/Design-Plan.md`'s Feature B (page 1 Trade/Investor rows, including
  multi-leg combo trades and the new `quantity` column) in a new notebook, with a flag to skip the
  database upload and only generate the CSV.
- **Discovery (corrects prior design):** extracting `Playbook-2026-09-07.pdf` page 2 showed the
  design doc's assumed layout was wrong — market commentary sits **after** the trade table (split
  by a `"Copyright © ..."` line), not before it split by `"Featured Trade & Maintenance
  Suggestions"`. Fed unsplit, the commentary's prose also false-triggers the existing
  `Trend`-header-detection branch (it contains the literal substring `"Trend"`). Corrected
  `docs/Design-Plan.md`'s "Background" section to match and to record the real page-2 content
  order.
- **Root cause (bug fixed while implementing combo parsing):** the existing number-extraction
  regex for a `BTO`/`STO` line couldn't distinguish a trailing contract count from a strike price
  — e.g. `"STO 2 March 47.5 Naked Puts"` would have read `2` and `47.5` as two strikes. Fixed by
  matching the `BTO`/`STO` keyword plus optional trailing count first (→ signed `quantity`) and
  parsing strikes only from the remainder of the line.
- **Implementation detail:**
  - `ETF_2_Code.ipynb` (new) — based on `ETF_1_Code_v3_UBugfix2.ipynb`'s cells almost unchanged;
    page-loop skip changed from `if c < 3` to `if c < 2` (only page 1's Disclosure Statement is
    now skipped); physical page 2 is split at `"Copyright"` into a trade-table half (fed into the
    same parser, after generalizing the pre-scan marker from `"Bond ETF Trade & Maintenance
    Suggestions"` to `"Trade & Maintenance Suggestions"`) and a commentary half (saved to a local
    `.txt`, with a ready-but-gated `upload_commentary_to_llmwiki()` retry/skip function per
    `MAX_LLMWIKI_RETRY` — a no-op until `LLMWIKI_BASE_URL` is set). Main parsing loop adds signed
    `quantity` extraction, a new `"Entry of a net <value> Credit|Debit"` combo branch (`Entry`
    kept numeric, negated for Credit), and broadcasts `Entry`/`Target`/`Stop` across every leg of
    a detected combo — the existing single-leg `"Entry @ "` path and the `Entry_Sign`/`Target_
    Sign`/`Stop_Sign` forward-fill cells (which already broadcast a shared sign across rows with
    matching Type/Symbol/Trend) are unchanged. Output columns add `quantity`; final cell adds
    `SKIP_DB_UPLOAD` (default `True`) to write CSV-only instead of calling `DU.StoreEOD`.
  - `.env.example` (new) — documents every env var `dataUtil.py` reads plus the new
    `LLMWIKI_BASE_URL`/`LLMWIKI_API_TOKEN`/`MAX_LLMWIKI_RETRY`, with a note that this repo's actual
    convention is `DB_Config.env`, not `.env`.
  - `requirements.txt` — added `requests` (used by the llmwiki upload stub).
  - `docs/Design-Plan.md` — corrected the page-2 layout/split-point section; marked Sequencing
    steps 1-3 done and noted the `quantity`-vs-strike fix.
  - `docs/TODOS.md` — checked off Feature B (notebook) and `.env.example`; reframed the remaining
    open item as "migrate the live schema" (composite PK + `quantity` column + upsert in
    `dataUtil.py`), since that's what's actually blocking `SKIP_DB_UPLOAD = False`.
- **Related files:** `ETF_2_Code.ipynb`, `.env.example`, `requirements.txt`,
  `docs/Design-Plan.md`, `docs/TODOS.md`, `docs/HISTORY.md`.
- **Test coverage:** no test suite exists in this repo (notebook workflow, no build step — see
  `CLAUDE.md`). Validated by executing `ETF_2_Code.ipynb` top-to-bottom against
  `Playbook-2026-09-07.pdf` via `jupyter nbconvert --execute` and inspecting output: Boeing `[BA]`
  (single-leg naked call) parses with `quantity=-1`; Paypal `[PYPL]` (2-leg combo) parses as two
  rows sharing `Entry=-0.75` (`.75 Credit`), `Target=64.37`, `Stop=43.73`, with
  `quantity=1`/`-2` for its `BTO`/`STO 2` legs respectively and the smaller strike (55) on the Long
  Call leg / larger (65) on the Short Call leg; the only exception logged during the run
  (`Trader Trend [Bullish/Hold` missing its closing bracket, page 6/XOP) was confirmed
  pre-existing by diffing against `ETF_1_Code_v3_UBugfix2.ipynb`'s own cached cell-9 output, not a
  regression from this change. No existing tests to break; none proposed (none exist to extend).

## 2026-09-09 — Flesh out Design-Plan.md for page 1 features

- **Goal:** Incorporate five clarifications from the user into `docs/Design-Plan.md`: the
  confirmed page-1 split marker, the retry/skip design for llmwiki ingestion, an explicit note
  that the existing "page 2" parsing logic (cell 9, `ETF_1_Code_v3_UBugfix2.ipynb`) is the
  reference implementation to extend rather than duplicate, a named sample PDF
  (`Playbook-2026-09-07.pdf`) for testing, and a new multi-leg (up to 5) combo-trade design.
- **Implementation detail:** Updated `docs/Design-Plan.md` — added the
  `"Featured Trade & Maintenance Suggestions"` split marker (resolving that open question), a
  `MAX_LLMWIKI_RETRY`-driven retry/skip design for Feature A, a walkthrough mapping cell 9's
  existing parsing branches onto page 1's bottom half, and a new "Multi-leg combo trades" section
  for Feature B — including a flagged schema conflict (`Entry` is specified as `FLOAT` but a
  combo leg's entry is the literal text `"Entry of a net <value> Credit"`), with two resolution
  options and a recommendation (widen `Entry` to `VARCHAR`). Updated the Sequencing section to
  match. No notebook or `dataUtil.py` code was changed — still design-only.
- **Related files:** `docs/Design-Plan.md`, `docs/HISTORY.md`.
- **Test coverage:** N/A (documentation only, no code paths changed).

## 2026-09-08 — Add docs/ scaffolding (HISTORY.md, TODOS.md, Design-Plan.md)

- **Goal:** Give the repo a persistent place to log changes (`HISTORY.md`), track open work
  (`TODOS.md`), and capture the design for the two new page-1 ingestion features
  (`Design-Plan.md`), all under `docs/` as requested.
- **Implementation detail:** Created `docs/HISTORY.md` (this file), `docs/TODOS.md` seeded with
  the "upgrade to Python 3.11" item, and `docs/Design-Plan.md` capturing the plan for (A) routing
  page 1's market-commentary text to the upcoming llmwiki MCP service and (B) storing page 1's
  Trade/Investor trend rows into `Trading.ETF_Options` under the PK'd schema supplied by the
  user. No pipeline code was changed in this step — see `docs/TODOS.md` for the follow-up
  implementation work.
- **Related files:** `docs/HISTORY.md`, `docs/TODOS.md`, `docs/Design-Plan.md`.
- **Test coverage:** N/A (documentation only, no code paths changed).

## 2026-09-08 — Stop tracking `DB_Config.env`

- **Goal:** Prevent the live MySQL host/user/plaintext-password in `DB_Config.env` from ever
  being committed.
- **Root cause:** `.gitignore` only excluded the literal filename `.env`; `DB_Config.env` (the
  name actually used by `load_dotenv("DB_Config.env")` in both notebooks) didn't match that
  pattern and showed up as untracked in `git status`.
- **Implementation detail:** Added `DB_Config.env` as its own line in `.gitignore`.
- **Related files:** `.gitignore`.
- **Test coverage:** N/A (git metadata change only). Verified with
  `git check-ignore -v DB_Config.env`.

## 2026-09-08 — Add repository CLAUDE.md

- **Goal:** Document the repo's architecture (two PDF-parsing notebooks + `dataUtil.py`), the
  environment (Python 3.8 venv, `DB_Config.env` loading), and the `DB_Config.env` exposure risk
  for future Claude Code sessions.
- **Implementation detail:** Added `CLAUDE.md` at the repo root describing the notebook pipeline
  stages, the `temp/` and `.ipynb_checkpoints/` scratch directories, and the security note about
  `DB_Config.env`.
- **Related files:** `CLAUDE.md`.
- **Test coverage:** N/A (documentation only).
