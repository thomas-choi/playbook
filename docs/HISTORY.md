# HISTORY.md

Chronological log of every code change, config change, and architectural decision in this
repository, per the project's mandatory change-logging rule. Newest entries at the top.

---

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
