# Design Plan: Page 1 features (Playbook PDF)

## Background

Both notebooks currently skip page 1 entirely:

```python
for page in doc:
    c += 1
    if c < 3: continue   # ETF_1_Code_v3_UBugfix2.ipynb, cell 5
    text = page.get_text()
```

Page 1 of the Playbook PDF (physical page 2 — the newsletter's own numbering starts at "P 1" per
its Table of Contents, which sits on the same physical page) has two halves that need to be
captured separately:

- **A) Trade/Investor trend table.** Same shape as the tables already parsed from later pages
  (`Bond ETF Trade & Maintenance Suggestions`, etc.) and belongs in the existing
  `Trading.ETF_Options` table.
- **B) Market commentary.** Free-text narrative on the current market recommendation. Not
  tabular, not meant for a SQL table — it should be ingested into the (upcoming) llmwiki
  knowledge base as a document, so the query agent can answer questions against it later.

**⚠️ Correction (confirmed against `Playbook-2026-09-07.pdf`):** the order assumed above when this
doc was first written — commentary on top, trade table below, split at
`"Featured Trade & Maintenance Suggestions"` — was wrong. The real layout on physical page 2 is:

1. Table of Contents (boilerplate, discarded either way).
2. `"Featured Trade & Maintenance Suggestions"` → the trade table (Feature B — single-leg, e.g.
   Boeing `[BA]`).
3. `"Featured Investor & Maintenance Suggestions"` → more of the same trade table (this is also
   where the real-world multi-leg combo example lives — Paypal `[PYPL]`, see "Multi-leg combo
   trades" below).
4. `"Copyright © <year> 912 Financial Group, all rights reserved"`.
5. The market-commentary prose (Feature A, "Market Expectations for `<Month>`: ...") — **after**
   the trade table, not before it.

**Split-point marker (confirmed, corrected twice):** the order above is what PyMuPDF emits
for the *newer* issues (2025+). Older issues (e.g. `LCR Playbook 2022-11-21.pdf`) come out as
TOC → commentary → trade table → `"Copyright"` as the very last line, so a plain "everything
after `Copyright`" split yields empty commentary and leaks the prose into the trade parser.
`_split_commentary()` therefore cuts the commentary out as the block from the line starting with
`"Market Expectations"` up to the next `"Trade & Maintenance Suggestions"` / `"Copyright"` line
(or end of page); the remaining lines are fed to the trade-table parser (Feature B). Only if no
`"Market Expectations"` line exists does it fall back to the after-`"Copyright"` split. The
existing `"Trade & Maintenance Suggestions"` marker is still useful and unchanged in role: it's
what the trade-table parser's own pre-scan (cell 8 of `ETF_1_Code_v3_UBugfix2.ipynb`) uses to skip
past the TOC boilerplate before parsing starts — generalized from the page-3-only
`"Bond ETF Trade & Maintenance Suggestions"` string to the bare `"Trade & Maintenance
Suggestions"` so its first match is now `"Featured Trade & Maintenance Suggestions"` on physical
page 2 — but it's no longer the Feature A/B split point. `"Featured"` plays the same role as the
`Type_name` prefix (`"Bond ETF"`, etc.) does on later pages, so no new header-detection code is
needed there. Implemented in `ETF_2_Code.ipynb` (see `docs/HISTORY.md`): the page-loop skip
changed from `if c < 3: continue` to `if c < 2: continue` (page 1's Disclosure Statement is still
skipped; physical page 2 is now split in-place, before its trade-table half is appended to the
same `text_full` the parser already builds from later pages).

## Feature A — market commentary → llmwiki

### What llmwiki expects

Per `docs/llm-wiki-technical-document.md` (§6.1, §6.2, §6.3), llmwiki's public surface is one
function set (`llmwiki.tools`) exposed three ways:

| Transport | Call | Accepts raw text? |
|---|---|---|
| MCP tool `ingest_source` | `(url: str, title: str = "")` | **No** — URL only, by design (§5.7: MCP surface deliberately kept to 6 tools with minimal args) |
| REST `POST /upload` | multipart `file`, `title?` | Yes — any `mime`, including `text/plain` |
| Python `tools.ingest_source()` | `(url=None, file=None, filename=None, mime="", title="", cfg=None)` | Yes |

Since the market-commentary text has no URL — it's extracted text from a local PDF — **the MCP
`ingest_source` tool is not usable for this**. The commentary must go in as a `file=` upload with
`mime="text/plain"`, which routes through `extractors/text.py` (`TextExtractor`). That means this
repo needs to call llmwiki's **REST API** (`POST /upload`), not its MCP tools, for this feature.

### Proposed flow

1. During PDF text extraction, split page 1's text into the two halves (see "Open questions"
   below for where the split point is detected).
2. Build a title, e.g. `f"LCR Playbook Market Commentary {InputDate}"`.
3. `POST {LLMWIKI_BASE_URL}/upload` with the commentary text as a `text/plain` file body, the
   title above, and `Authorization: Bearer {LLMWIKI_API_TOKEN}` (llmwiki's `INGEST_API_TOKEN`
   auth, §6.2).
4. Treat this as fire-and-forget from the notebook's perspective — `/upload` returns a
   `SourceRef` with `status="queued"`; the notebook doesn't need to poll `/sources/{id}` unless
   we want to confirm compilation succeeded before moving on.

### New configuration needed

Two new env vars, following this repo's existing `DB_Config.env` pattern:

```
LLMWIKI_BASE_URL=http://localhost:8000
LLMWIKI_API_TOKEN=changeme
MAX_LLMWIKI_RETRY=5
```

These should also be recorded in a new `docs/../.env.example` (see `docs/TODOS.md`) once added,
per the global Claude Code convention that every env var the code reads must be mirrored there
with a placeholder value.

### Retry/error handling (confirmed)

Controlled by a new env var, following the existing `DB_Config.env` naming style:

```
MAX_LLMWIKI_RETRY=5
```

Behavior: on a failed `POST /upload` (network error, non-2xx response, timeout), retry up to
`MAX_LLMWIKI_RETRY` times (e.g. with a short fixed or backoff delay between attempts). If every
attempt fails, **log the failure and skip** — move on to the next item rather than aborting the
notebook run, consistent with `dataUtil.py`'s existing try/except-and-log style
(`logging.error(..., exc_info=True)` then return/continue). A missing/unset
`MAX_LLMWIKI_RETRY` should default to a sane value (e.g. `5`) rather than failing to start.

### Open questions / blockers

- **llmwiki isn't deployed yet** ("the coming llm-wiki mcp") — this feature can be designed now
  but not exercised end-to-end until a `LLMWIKI_BASE_URL` exists to point at.

## Feature B — page 1 Trade/Investor rows → `Trading.ETF_Options`

### Target schema (as specified)

```sql
CREATE TABLE ETF_Options (
    Date        DATE         NOT NULL,
    Type        VARCHAR(45),
    Trend       VARCHAR(15)  NOT NULL,
    Symbol      VARCHAR(20)  NOT NULL,
    Status      VARCHAR(45),
    Expiration  DATE,
    PnC         VARCHAR(1),
    `Low-High`  VARCHAR(45)  NOT NULL,
    L_Strike    FLOAT,
    H_Strike    FLOAT,
    Entry_Sign  VARCHAR(5),
    Entry       FLOAT,
    Target_Sign VARCHAR(5),
    Target      FLOAT,
    Stop_Sign   VARCHAR(5),
    Stop        FLOAT,
    quantity    INT          NOT NULL DEFAULT 1,
    PRIMARY KEY (Date, Trend, Symbol, `Low-High`)
);
```

`quantity` is the new column for the signed contract count described in "Trade action & leg
semantics" below (`BTO`/`STO` direction × contract count); it defaults to `1` for rows that don't
carry an explicit count. It is not part of the primary key.

(Table name assumed to be `Trading.ETF_Options` — matching the `trd_DB = "Trading"` /
`opt_tbl = "ETF_Options"` already used in `ETF_1_Code_v3_UBugfix2.ipynb` cell 30. Flag if
"Trade.ETF_Options" in the request meant a different, new database name.)

### This mostly already matches the current pipeline

The column list/order above is **identical** to what `ETF_1_Code_v3_UBugfix2.ipynb` (cell 20)
already builds for later pages:

```python
df = df[["Date","Type", "Trend", "Symbol", "Status", "Expiration", "PnC", "Low-High",
         "L_Strike", "H_Strike", "Entry_Sign", "Entry", "Target_Sign", "Target",
         "Stop_Sign", "Stop"]]
```

So Feature B is really: **run the same parsing/shaping logic against page 1's bottom half that
already runs against the Bond ETF/other sections**, then feed the resulting rows into the same
`final_df` the notebook already sends to `DU.StoreEOD(final_df, "Trading", "ETF_Options")`
(cell 30). No new column mapping is needed — the new part is (a) locating page 1's table text and
(b) the composite primary key below (and (c) multi-leg combo trades — see below).

### Reusing the existing "page 2" parsing logic

`ETF_1_Code_v3_UBugfix2.ipynb` cell 9 is the reference implementation for turning an
"`<Type> Trade & Maintenance Suggestions`" section into Investor/Trader trend rows, and page 1's
bottom half (`"Featured Trade & Maintenance Suggestions"`) is the same shape, so the plan is to
extend that loop to also run over page 1 rather than write a parallel parser:

- Section header `"<Type> Trade & Maintenance Suggestions"` → `Type_name` (already generic; for
  page 1 this yields `Type_name = "Featured"`).
- `[SYMBOL]` bracket line → `Stock_name_tmp`; the following line's `Investor|Trader` +
  `[Status]` → `Trend`/`Status`.
- A `"STO"`/`"BTO"` line starts a new row: `Put|Call` → `PnC`, `(MM/DD/YY exp)` → `Expiration`,
  the number(s) before `(` → `L_Strike`/`H_Strike` (one number = `H_Strike` only, two = both).
- `"Entry @ "` line → `Entry`/`Target` (split on `,`).
- `"Stop"` line → `Stop`.
- Cell 11 builds `Low-High` from `L_Strike`/`H_Strike`/`PnC`; cells 13–14 back-fill missing
  `Stop` from the next row and derive `Entry_Sign`/`Target_Sign`/`Stop_Sign` from `PnC`.

Enhancing the notebook means widening this existing loop's page range (dropping the page-1 skip
for the bottom half) rather than adding a second, separate parser — the multi-leg handling below
is the one genuine extension needed on top of it.

**Test input:** `Playbook-2026-09-07.pdf` can be used as the example/reference PDF for building
and validating this against page 1's real layout.

### Trade action & leg semantics (confirmed)

- **Naked Call / Naked Put** is a single-leg trade — just one `Put|Call` leg, no combo. This is
  today's already-supported single-leg case (see "Reusing the existing 'page 2' parsing logic"
  above); a "Naked Call"/"Naked Put" label does not by itself trigger combo parsing.
- **`BTO`/`STO` → Long/Short direction, plus contract count → `quantity` column (resolved).**
  `BTO` = Long/Buy, `STO` = Short/Sell. The number immediately following the term is the
  option-contract count; store it, signed by direction, in the new `quantity` column (see target
  schema above):
  - `STO 2` (short 2 contracts) → `quantity = -2`
  - `STO` with no explicit count → `quantity = -1`
  - `BTO 2` → `quantity = 2`; `BTO` with no explicit count → `quantity = 1` (the column default).
- **Combo strike → leg mapping is by strike size, not text position.** For a combo header like
  `BTO September 707/737 Bull Call Spread (9/18/26 exp)`:
  - `Bull Call Spread` names the combo type — a 2-leg combo (one long call + one short call).
  - `BTO` sets the overall direction (Long/Buy) for the spread.
  - `(9/18/26 exp)` → `Expiration = 2026-09-18`.
  - The **smaller** strike (`707`) is always the **Long Call** leg; the **larger** strike (`737`)
    is always the **Short Call** leg — this holds regardless of which order the two numbers appear
    in the source text, so it's strike-value comparison, not text position, that decides each
    leg's `PnC`/direction.
- **Combo composition is open-ended within the 5-leg cap.** A combo can mix any of Bull Call
  Spread, Naked Put, and Naked Call legs under one `[SYMBOL]`/`Trend` block — the "is this a
  combo" detection below isn't limited to a single spread type; all legs found before the shared
  entry line belong to the same combo and share the same broadcast Entry/Target/Stop (see below).

### Multi-leg combo trades (new logic — up to 5 legs)

Suggested trades can be a single leg (today's only supported case) or a combo/spread of up to 5
legs. New rules for the combo case:

- Each leg of the combo is still emitted as **its own row** (one `STO`/`BTO` line ⇒ one row, same
  as today) — so a 3-leg combo produces 3 rows for that trade, not one.
- A combo's entry line reads as a net price phrase, e.g. `"Entry of a net 1.25 Credit"` rather
  than today's `"Entry @ <price>"`. `Entry` stays numeric (see resolution below): parse out the
  value (`1.25`) and negate it when the wording is `"Credit"` (→ `-1.25`); keep the original
  (positive) value when the wording is `"Debit"`. **Every row belonging to that combo gets this
  same signed numeric value** in `Entry`.
- `Target`, `Target_Sign`, `Stop`, `Stop_Sign` are likewise **broadcast identically to every row**
  of the combo (parsed once from the trade's shared entry/target/stop lines, same as the
  single-leg case today, just copied across all N leg-rows instead of one).
- Detecting "this trade is a combo, not a single leg" needs a marker — the presence of an
  `"Entry of a net ... Credit"` (or `"... Debit"`) phrase, versus today's `"Entry @ <price>, ..."`
  format, is the natural signal; multiple consecutive `STO`/`BTO` lines under the same
  `[SYMBOL]`/`Trend` block before that shared entry line is what delimits the leg group.
- No new PK column should be needed for storage: each leg typically has a different strike, so
  `L_Strike`/`H_Strike` differ and `Low-High` (already part of the composite PK) should already
  disambiguate the legs of one combo. **Flag if two legs of the same combo can ever share an
  identical `Low-High`** (e.g. a calendar spread on the same strike, different expiration) — the
  PK also includes `Date`/`Trend`/`Symbol` but not `Expiration`, so that specific case would
  collide and needs a decision before implementation.

### `Entry` schema conflict — resolved

A combo leg's `Entry` line reads as a net price phrase (e.g. `"Entry of a net 1.25 Credit"`),
which doesn't parse as a bare number the way the single-leg `"Entry @ <price>"` case does. Of the
two options previously proposed (widen `Entry` to `VARCHAR`, or add a separate `Entry_Note`/sign
column), the second is what's confirmed, in this simplified form:

**Decision: keep `Entry` as `FLOAT` — no DDL change.** Extract just the numeric value and encode
the Credit/Debit qualifier as its sign: wording `"Credit"` → store the value negated (e.g.
`-1.25`); wording `"Debit"` → store the original (positive) float value. No new
column is needed — the sign alone carries the Credit/Debit distinction. See "A combo's entry line
reads as..." above for how this applies per-row.

### The composite primary key changes the storage semantics

`DU.StoreEOD` (`dataUtil.py`) currently does:

```python
eoddata.to_sql(name=TBLn, con=dbcon, schema=DBn, if_exists='append', index=False)
```

This blindly appends — there is no dedupe today, so re-running a notebook for a date that was
already loaded produces duplicate rows. The PK given here
(`Date, Trend, Symbol, Low-High`) implies that's no longer acceptable once the table enforces it:
a second run for the same date would hit a duplicate-key error instead of silently duplicating.

**Recommendation:** add an upsert path — either a `dataUtil.py` function that issues
`INSERT ... ON DUPLICATE KEY UPDATE` per row/batch, or `pandas.DataFrame.to_sql(..., method=<custom
upsert callable>)` — before enabling Feature B against a table that actually has this PK. Tracked
in `docs/TODOS.md`.

## Sequencing

1. ✅ Extract page 1's text from `Playbook-2026-09-07.pdf` (sample/reference input) and confirm
   the marker split — done, corrected to split on `"Copyright"` rather than
   `"Featured Trade & Maintenance Suggestions"` (see the correction above).
2. ✅ Feature B, single-leg case — widened the existing page-loop/parsing logic (cell 9 and
   friends) to include physical page 2's trade-table half; reuses the existing code path almost
   unchanged. Implemented in `ETF_2_Code.ipynb`.
3. ✅ Resolved the `Entry` column schema conflict (kept `FLOAT`, signed by Credit/Debit) and added
   multi-leg combo parsing on top of step 2, including a real fix found while implementing it:
   the trailing contract count on a `BTO`/`STO` line (e.g. the `2` in `"STO 2 March 47.5 Naked
   Puts"`) was previously indistinguishable from a strike price by the existing number-extraction
   regex — it now reads `BTO`/`STO` + optional count first (→ `quantity`) and parses strikes only
   from the remainder of the line. Implemented and validated against `Playbook-2026-09-07.pdf`'s
   real combo example (Paypal `[PYPL]`, Bull Call Spread + Naked Puts) in `ETF_2_Code.ipynb`.
4. ⬜ Add the `Trading.ETF_Options` composite PK + `quantity` column + upsert support in
   `dataUtil.py`/the live schema (needed once Feature B can re-run against the same date more than
   once). **Not done** — `ETF_2_Code.ipynb` defaults to CSV-only output (`SKIP_DB_UPLOAD = True`)
   specifically because the live table doesn't have this migration yet; flipping that flag before
   this step lands risks a failed `to_sql` (unknown `quantity` column) or silent duplicate rows.
5. 🚧 Feature A (market commentary → llmwiki) — the text-splitting half is done in
   `ETF_2_Code.ipynb` (commentary is saved to a local `.txt` next to the CSV), but the actual
   `POST /upload` call is still a no-op until `LLMWIKI_BASE_URL`/`LLMWIKI_API_TOKEN` exist and
   llmwiki's endpoint is reachable — the retry/skip (`MAX_LLMWIKI_RETRY`) function is written and
   ready, just gated on `LLMWIKI_BASE_URL` being set. **Update:** `playbook_etf.py` (step 6) now
   makes the call for real via `POST /ingest {text, title}` — the `/upload` design above is
   superseded; see "Batch script: `playbook_etf.py`" → "llmwiki".
6. ✅ Batch script `playbook_etf.py` — folder scan of `*Playbook YYYY-MM-DD.pdf`, same parser as
   `ETF_2_Code.ipynb`, DB append with skip-already-loaded-dates, llmwiki `/ingest` with the date
   in title and body. Design in "Batch script: `playbook_etf.py`" at the end of this document.

## Batch script: `playbook_etf.py`

### Purpose

`ETF_2_Code.ipynb` handles one PDF per hand-edited run (`InputDate` + filename in cell 3).
`playbook_etf.py` is the script version of that notebook: point it at a folder and it processes
every `*Playbook YYYY-MM-DD.pdf` it finds — same parser, same CSV/commentary side files, then the
DB append and the llmwiki ingest — skipping dates that are already in the database. The notebook
stays in the repo as the interactive/debug reference; the script is what a scheduled or catch-up
run should call.

### CLI

```
python playbook_etf.py FOLDER [--since YYYY-MM-DD] [--force] [--skip-db] [--skip-llmwiki]
                              [--dry-run] [--env-file DB_Config.env] [--out-dir DIR]
                              [--log-level INFO]
```

- `FOLDER` is scanned non-recursively for filenames matching
  `Playbook[ -](\d{4}-\d{2}-\d{2})\.pdf$` (case-insensitive) — i.e. both the Google-Drive name
  `912 Playbook 2026-09-07.pdf` and the repo sample `Playbook-2026-09-07.pdf`. The date is taken
  from the filename; this replaces the notebook's hand-edited `InputDate`.
- PDFs are processed oldest-first. `--since` drops anything dated before the given day.
- `--dry-run` = `--skip-db --skip-llmwiki` (parse + write the CSV and `_commentary.txt` only).
- `--out-dir` defaults to the PDF's own folder, matching the notebook (CSV and
  `<stem>_commentary.txt` next to the PDF).
- `--env-file` (default `DB_Config.env`) is passed to `load_dotenv()` before anything touches
  `dataUtil`; a missing file is a warning, not an error, so the process environment can be used
  instead (e.g. in a container).

### Stage → function map (all ported from `ETF_2_Code.ipynb`)

| Function | Notebook cell(s) | Notes |
|---|---|---|
| `find_playbook_pdfs(folder)` | 3 | new — folder scan + date from filename |
| `extract_text(pdf)` → `(table_text, commentary_text)` | 4 | skip physical page 1; cut the `"Market Expectations"` block out of page 2 (fallback: after `"Copyright"`); warn if neither marker is found |
| `parse_trades(table_text)` → `DataFrame` | 5, 7, 9 | **only structural change:** rows are accumulated as dicts and turned into a DataFrame once, instead of the notebook's pre-seeded DataFrame + chained assignment (`df['Col'][row] = v`), which pandas ≥ 2 no longer supports; the seed-row/`df[:-1]` dance goes away with it. Regexes, branch order, combo broadcast and the log-and-continue per-line `try/except` are unchanged. The section-header test is `find(...) >= 0` rather than the notebook's `> 0`. Raises if no `"Trade & Maintenance Suggestions"` header is found (the notebook would silently parse nothing). |
| `derive_columns(df)` | 11, 14–17 | `Low`/`High`/`Low-High`, the three `*_Sign` columns, Stop back-fill from the next row, sign forward-fill within a Type/Symbol/Trend group — same sequential semantics via `df.at`. Cell 13 (computes `nstop` and discards it) is dropped as dead code. |
| `finalize(df, date)` → `(csv_df, db_df)` | 18–21, 23–25 | `csv_df` is exactly what the notebook writes to CSV (strings, Investor rows then Trader rows); `db_df` is the typed frame the notebook got back from its CSV round-trip (`Date`/`Expiration` as datetimes, strikes/prices as floats, `quantity` as int). `Expiration` is parsed with an explicit `%m/%d/%y` / `%m/%d/%Y` format instead of inference. |
| `upload_commentary(text, date)` | 27 | see "llmwiki" below |
| `loaded_dates(db, table)` / `store(db_df, db, table)` | 29 | `DU.load_df_SQL` / `DU.StoreEOD` |
| `process_pdf(...)`, `main()` | — | per-file driver + CLI |

Validated by running the notebook's cells 5–21 verbatim and the script's functions on the same
extracted text of `Playbook-2026-09-07.pdf`: both produce the identical 58-row frame (Boeing
`[BA]` `quantity=-1`; Paypal `[PYPL]` two rows with `Entry=-0.75`, `Target=64.37`, `Stop=43.73`,
`quantity=1/-2`; same pre-existing `Trader Trend [Bullish/Hold` warning on page 6).

### Dedupe rule

`DU.StoreEOD` is still a plain `to_sql(if_exists='append')` (the composite-PK/upsert migration in
`docs/TODOS.md` hasn't landed), so the script guards against duplicates itself: at startup it runs
`SELECT DISTINCT Date FROM {DBTRADING}.{TBLETFOPTIONS}` once and skips every PDF whose date is
already present. `--force` disables the check (rows are then **appended**, not replaced — use it
only after deleting the date's rows or once upsert exists). If the query fails the script logs a
warning and skips nothing; with `--skip-db` the query isn't run at all.

### llmwiki

Feature A's earlier `/upload` (multipart `.txt`) design is superseded: the script calls
`POST {LLMWIKI_BASE_URL}/ingest` with JSON `{"text": "Date: YYYY-MM-DD\n\n<commentary>",
"title": "LCR Playbook Market Commentary YYYY-MM-DD"}` and `Authorization: Bearer
{LLMWIKI_API_TOKEN}` (`docs/llm-wiki-technical-document.md` §6.2). The date is therefore carried
in both the title and the first line of the body. llmwiki content-addresses `text` sources
(`pipeline/ingest.py::_source_id`), so re-sending the same PDF's commentary is idempotent.
Retry/skip policy is unchanged: up to `MAX_LLMWIKI_RETRY` attempts (short backoff between them),
then log and move on — a llmwiki outage never fails the run. Unset `LLMWIKI_BASE_URL` → skipped
with an info log. The commentary is always saved to `<stem>_commentary.txt` first, so a skipped
upload can be replayed later.

### Error policy

- One PDF failing (unreadable file, no section header, etc.) is logged with a traceback and the
  loop continues; the exit code is `1` if any file failed, `0` otherwise, `2` for a bad folder.
- Per-line parse exceptions inside `parse_trades` are warnings, as in the notebook.
- `StoreEOD` logs and swallows DB errors (existing `dataUtil.py` behaviour) — check the log for
  `Exception occurred` if a date is unexpectedly missing from the table.

### Configuration

Read from the environment after `--env-file` is loaded (mirrored in `.env.example`):

| Var | Default | Used for |
|---|---|---|
| `DBTRADING` | `Trading` | target database |
| `TBLETFOPTIONS` | `ETF_Options_v1` | target table (the notebook's current table; has `quantity`) |
| `LLMWIKI_BASE_URL` | unset → skip | llmwiki base URL |
| `LLMWIKI_API_TOKEN` | `""` | bearer token |
| `MAX_LLMWIKI_RETRY` | `5` | attempts per PDF |
| `LLMWIKI_TIMEOUT` | `30` | seconds per request |
| `DBHOST`/`DBPORT`/`DBUSER`/`DBPWD`/`DBMKTDATA` | — | consumed by `dataUtil.get_DBengine` |
