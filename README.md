# playbook

Processes the **912/LCR Playbook** and **LCR Top20** options-trading PDFs into structured
trade-suggestion rows (symbol, strikes, entry/target/stop, quantity, …), loads them into MySQL,
and posts each issue's market commentary to llmwiki.

The PDFs do not always follow their own layout, so a run has three stages: the regexes parse what
they can and **flag** the blocks that confused them, an **LLM re-reads only those blocks** (over any
OpenAI-standard endpoint), and `--review` **asks you** about whatever is still unsettled — recording
the answer so it is never asked twice. Design and the defect catalogue:
[docs/PDFreader.md](docs/PDFreader.md). The per-publication parsing contracts (page map, block
grammar, every field) live with the skills in [.claude/skills/](.claude/skills/).

| Source PDF | Runner | Target table |
|---|---|---|
| `*Playbook YYYY-MM-DD.pdf` | **`playbook_etf.py`** (CLI) or `ETF_2_Code.ipynb` | `Trading.ETF_Options_v1` |
| `*Top20 YYYY-MM-DD.pdf` | **`top20_stock.py`** (CLI) or `Top20_2_Code.ipynb` | `Trading.Stock_Options_v1` |
| Playbook (older format) | `ETF_1_Code_v3_UBugfix2.ipynb` | `Trading.ETF_Options` |
| Top20 (older format) | `Top20-Ubuntu.ipynb` | `Trading.Stock_Options` |

Shared modules: `dataUtil.py` (SQLAlchemy/PyMySQL engine, `load_df_SQL`, `StoreEOD_strict`,
`ReplaceDate`), `llm_repair.py` (parse reports + the LLM repair pass), `hil_review.py` (the
decisions store and the `--review` prompt).

## Setup

```bash
git clone <repo-url> playbook && cd playbook
python3 -m venv .venv            # the checked-in .venv is Python 3.11
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

Config is read from **`.env`** via `python-dotenv` (`--env-file` points elsewhere). It is
git-ignored; create it from the template and fill in real values:

```bash
cp .env.example .env
```

Keys that matter for the CLI scripts:

| Key | Purpose |
|---|---|
| `DBHOST`, `DBPORT`, `DBUSER`, `DBPWD`, `DBMKTDATA` | MySQL connection (`dataUtil.py` builds the engine from these) |
| `DBTRADING` / `TBLETFOPTIONS` | Target DB / table for `playbook_etf.py` rows (default `Trading` / `ETF_Options_v1`) |
| `TBLSTOCKOPTIONS` | Target table for `top20_stock.py` rows (default `Stock_Options_v1`) |
| `LLMWIKI_BASE_URL`, `LLMWIKI_API_TOKEN` | llmwiki `POST /ingest` endpoint for the commentary; leave `LLMWIKI_BASE_URL` unset to skip |
| `MAX_LLMWIKI_RETRY`, `LLMWIKI_TIMEOUT` | Retry count / seconds per llmwiki call (default 5 / 30) |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | OpenAI-standard endpoint used to re-read flagged blocks — `https://api.openai.com/v1`, a gateway, or a local server (`http://localhost:11434/v1` for ollama). Leave `LLM_BASE_URL` unset to skip the repair pass |
| `LLM_TEMPERATURE`, `LLM_TIMEOUT`, `LLM_MAX_RETRY` | Defaults `0` / `60` s / `3` attempts |

Nothing in the repair path is provider-specific: any endpoint that serves
`POST /v1/chat/completions` works, and the model is whichever id `LLM_MODEL` names.

The remaining keys in `.env.example` are used by other `dataUtil.py` helpers and can stay as
placeholders.

## Quick start

Once `.env` is filled in and the PDFs are on disk (see **Getting the PDFs**):

```bash
source .venv/bin/activate

# 1. parse and inspect — writes CSV + text + reports, touches neither MySQL nor llmwiki
python playbook_etf.py ~/lcr/prod/Playbooks --csv-only
python top20_stock.py  ~/lcr/prod/top20     --csv-only

# 2. answer anything the regexes and the LLM could not settle (remembered for every later run)
python playbook_etf.py ~/lcr/prod/Playbooks --csv-only --review

# 3. load the new dates (dates already in the table are skipped)
python playbook_etf.py ~/lcr/prod/Playbooks
python top20_stock.py  ~/lcr/prod/top20

# fix one date that is already loaded (DELETE + INSERT for that date only)
python playbook_etf.py ~/lcr/prod/Playbooks --date 2022-06-06 --replace-date
```

Both scripts take **a single PDF or a folder**, so a one-off is
`python top20_stock.py ~/lcr/prod/top20/"912 Top20 2025-07-21.pdf" --csv-only`.

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

Takes a single `*Playbook YYYY-MM-DD.pdf` or a folder of them (e.g. `912 Playbook 2026-09-07.pdf`,
`LCR Playbook 2022-11-21.pdf`), oldest first, and for each PDF:

1. extracts text (PyMuPDF), splitting page 2 into the trade table and the
   "Market Expectations …" commentary, and saves it as `<pdf-name>.txt`,
2. parses every `STO`/`BTO` leg into a row, recording any line it could not read in a
   **parse report**, and derives `Low-High`, the sign columns, and the group's Entry/Target/Stop,
3. applies any **recorded decisions** for this issue's blocks, then has the **LLM re-read** the
   blocks still flagged (`--llm repair`, the default), then **asks you** about the rest (`--review`),
4. writes `<pdf-name>.csv` plus the reports and `<pdf-name>_commentary.txt` next to the PDF
   (or `--out-dir`),
5. appends the rows to `Trading.ETF_Options_v1` — dates already in the table are skipped, and rows
   the table cannot take are held back in `<pdf-name>.rejected.csv` instead of failing the date,
6. posts the commentary to llmwiki.

```bash
source .venv/bin/activate

# Parse only: CSV + text + reports, no DB, no llmwiki (safe to run anytime)
python playbook_etf.py test --csv-only

# Full run against the Google-Drive folder (see "Getting the PDFs" for your host).
# Pure Ubuntu: run ~/lcr/bin/sync_lcr.sh first
python playbook_etf.py ~/lcr/prod/Playbooks
# WSL on Windows: mount I: first
python playbook_etf.py "/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/Playbooks"

# One PDF, or one date out of a folder
python playbook_etf.py ~/lcr/prod/Playbooks/"912 Playbook 2026-09-07.pdf" --csv-only
python playbook_etf.py ~/lcr/prod/Playbooks --date 2026-09-07 --csv-only

# Only PDFs dated on/after a date
python playbook_etf.py ~/lcr/prod/Playbooks --since 2026-09-01

# Re-load a date that is already in the DB: replace its rows (idempotent, re-runnable)
python playbook_etf.py ~/lcr/prod/Playbooks --date 2022-06-06 --replace-date

# Answer whatever the regexes and the LLM could not settle
python playbook_etf.py ~/lcr/prod/Playbooks --csv-only --review

# Pure regex, no LLM call at all; or send every block (diagnosing a brand-new layout)
python playbook_etf.py ~/lcr/prod/Playbooks --csv-only --llm off
python playbook_etf.py ~/lcr/prod/Playbooks/"912 Playbook 2026-09-07.pdf" --csv-only --llm force

# Load to DB but don't post commentary; write outputs somewhere else; verbose logging
python playbook_etf.py ~/lcr/prod/Playbooks --skip-llmwiki --out-dir /tmp/pb --log-level DEBUG
```

| Option | Description |
|---|---|
| `target` | A single `*Playbook YYYY-MM-DD.pdf`, or a directory to scan for them |
| `--date YYYY-MM-DD` | Only process this date |
| `--since YYYY-MM-DD` | Only process PDFs dated on/after this |
| `--csv-only` | Write CSV, text and reports only — no DB, no llmwiki (`--dry-run` is the same thing) |
| `--replace-date` | Re-load a date: `DELETE` its rows then insert, in one transaction (implies `--force`) |
| `--force` | Process dates already in the DB by appending (hits the primary key — prefer `--replace-date`) |
| `--llm off\|repair\|force` | LLM re-read of flagged blocks: `repair` (default), none, or every block |
| `--review` | Ask about every block still unsettled and record the answer |
| `--decisions PATH` | Where those answers live (default `.pdfreader-decisions.json` next to the PDFs) |
| `--skip-db` | Parse and write files only |
| `--skip-llmwiki` | Don't send commentary to llmwiki |
| `--env-file FILE` | Dotenv file (default `.env`) |
| `--out-dir DIR` | Where to write the CSV and reports (default: next to the PDF) |
| `--log-level LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, … |

### What a run writes

| File | Use |
|---|---|
| `<stem>.csv` | the rows, in DB column order |
| `<stem>.txt` | the extracted PDF text the parser actually saw — start here when a row looks wrong |
| `<stem>.parse-report.json` | every block, the flags raised in it, and whether it is settled |
| `<stem>.repairs.json` | each LLM call: flags, rows before/after, accepted or why rejected |
| `<stem>.rejected.csv` | rows the table cannot take (empty `Low-High`, duplicate key) with a `_reason` |
| `<stem>_commentary.txt` | the commentary sent to llmwiki |
| `.pdfreader-decisions.json` | your `--review` answers, in the PDF folder, replayed on every later run |

Exit code is `0` when everything is settled and loaded, `1` if a PDF failed **or** anything is left
unresolved (a flagged block with no answer, a row that could not load), `2` if `target` does not
exist or is a file that is not named like a Playbook PDF. A failed PDF is logged with a traceback and the run continues with the next one; the closing
`Done: …` line carries the counts.

Sample PDFs live in `test/` (the whole folder is git-ignored, outputs included). To see what a
parser change does, run the fixtures — or the whole archive — into two directories and diff them:

```bash
git stash && python playbook_etf.py test --csv-only --llm off --out-dir /tmp/before
git stash pop && python playbook_etf.py test --csv-only --llm off --out-dir /tmp/after
diff -r /tmp/before /tmp/after      # row counts must not fall
```

## Running the Top20 batch script (`top20_stock.py`)

The Top20 counterpart of `playbook_etf.py` (script version of `Top20_2_Code.ipynb`), with the
**same options, outputs, dedupe rule and exit codes**. Takes a single `*Top20 YYYY-MM-DD.pdf` or a
folder (e.g. `912 Top20 2025-07-21.pdf`, `LCR Top20 2022-11-21.pdf`) and for each PDF:

1. extracts text (PyMuPDF): physical page 1 (disclosure) is skipped, the printed "Page 1"
   (physical page 2) yields the "Market Expectations …" commentary, pages 3+ the per-stock text,
2. parses one row per stock (`Entry @ lo-hi`, `Target @`, `STO/BTO <strike> Put|Call (exp)`,
   `… @ SYM <price> Stop`) and derives the sign columns (entry range swapped for Calls),
3. same decisions → LLM repair → `--review` sequence as the Playbook script,
4. writes the CSV, text and reports next to the PDF (or `--out-dir`),
5. appends the rows to `Trading.Stock_Options_v1` — dates already in the table are skipped,
6. posts the commentary to llmwiki (`LCR Top20 Market Commentary YYYY-MM-DD`).

```bash
python top20_stock.py test --csv-only                      # parse only, safe anytime
python top20_stock.py ~/lcr/prod/top20                     # pure Ubuntu (after sync_lcr.sh)
python top20_stock.py "/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/Top20s"   # WSL
python top20_stock.py ~/lcr/prod/top20 --csv-only --review  # answer the unsettled blocks
python top20_stock.py ~/lcr/prod/top20 --date 2022-12-19 --replace-date
```

Known source-PDF quirks the review queue is there for: `912 Top20 2022-12-19` prints a stock heading
("Uber Technologies Inc") with no `[TICKER]`, so that row has no `Symbol` until you supply one;
`LCR Top20 2020-05-18` prints Playbook-style two-leg combos (PENN, UBER) that a one-row-per-symbol
table cannot hold; a few 2023 issues omit the word `Stop` on an exit line. A 0-byte PDF in the folder
is logged as failed and skipped.

## Reviewing what nothing could settle

A block the regexes flagged and the LLM did not fix — validation rejected it, no endpoint was
configured, or the model got it wrong — is a question only a person can answer. `--review` asks it in
the terminal, one block at a time:

```
[1/2] IWM/Trader  (IWM/Trader)
  flag: duplicate — IWM/Trader 146P is in both IWM/Trader (12/16/2016) and IWM/Trader#2 (6/19/20)
  --- block text -----------------------------------------------------------
     iShares RusSTO 2000 [IWM]
     Trader Trend [Bullish/Hold]
     STO March  146 put (12/16/2016 exp) Limit .25
     BTO March  146/154 Bull Call spread (12/16/2016 exp) using
  --------------------------------------------------------------------------
  rows the regexes produced:
    [0] Symbol=IWM Trend=Trader Expiration=12/16/2016 PnC=P H_Strike=146 quantity=-1
    [1] Symbol=IWM Trend=Trader Expiration=12/16/2016 PnC=C L_Strike=146 H_Strike=154 quantity=1
  what should these rows be?
    [1] accept the LLM proposal   (none offered)
    [2] keep the regex rows as they are (they are right)
    [3] set a field on a row
    [4] confirm the value is not in the PDF (keep it empty, stop flagging)
    [5] drop a row          [6] add a row
    [u] unresolvable — record that and move on
    [s] skip for now        [q] stop reviewing
```

A value you type is held to the same bar as the model's: a number that is not printed in the block is
refused (`! 999.99 is not a number printed in this block`) unless you insist, and the resulting rows
must validate before they are recorded.

**Answers are remembered.** Each one is stored under a fingerprint of the block's own text in
`.pdfreader-decisions.json` in the PDF folder (`--decisions PATH` moves it) and replayed on every
later run *before* the LLM is consulted — which is what lets a `--replace-date` reload months later
reproduce the same rows with nobody present. If the publisher changes that block the fingerprint
changes and it is asked again, on purpose. Back the file up: it is human judgement, not derived data.

`--review` reads stdin and at EOF (cron, `</dev/null`) leaves the items pending and says so, so an
unattended run can never block. Without `--review`, a run with unsettled blocks names them in a
warning and exits non-zero:

```
WARNING playbook_etf: LCR Playbook 2020-06-01.pdf: 2 block(s) still need a human decision
                      (--review): IWM/Trader, IWM/Trader#2
```

If the same question comes up a third time it is a parser bug, not an answer: fix the regex and the
extraction spec in [.claude/skills/](.claude/skills/) instead.

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
  `parse_trades` → `run_repairs` → `derive_columns` → `finalize` → `split_loadable` → `store` /
  `upload_commentary`).
- `top20_stock.py` — same shape for the Top20 PDFs (`extract_text` → `parse_stocks` → …) →
  `Trading.Stock_Options_v1`.
- `llm_repair.py` — `ParseReport`/`Block`/`Flag` (what confused the parser, per block), the
  OpenAI-standard client, `repair_block`, the `validate_rows` gate that rejects invented numbers,
  and the `.repairs.json` audit log.
- `hil_review.py` — the decisions store (fingerprinted on the block text), `apply_decisions` to
  replay recorded answers, and the `--review` prompt.
- `dataUtil.py` — DB engine + read/write helpers shared by the scripts and notebooks
  (`StoreEOD_strict` and `ReplaceDate` are what the scripts use).
- `.claude/skills/lcr-playbook-pdf/`, `.claude/skills/lcr-top20-pdf/` — `SKILL.md` (how to run and
  debug each pipeline) + `reference/*-extraction-spec.md`, the document grammar and target schema.
  The spec file is also the system prompt the LLM repair sends — update it with the parser.
- `ETF_2_Code.ipynb`, `ETF_1_Code_v3_UBugfix2.ipynb`, `Top20_2_Code.ipynb`, `Top20-Ubuntu.ipynb` —
  notebook pipelines (`Top20_2_Code` = `Top20-Ubuntu` + commentary → llmwiki).
- `docs/PDFreader.md` — how the PDFs are read: the defect catalogue, the flag/repair/review design,
  the DB rules. `docs/Design-Plan.md` — original design notes for the batch scripts;
  `docs/HISTORY.md`, `docs/TODOS.md`.
- `test/` — sample Playbook and Top20 PDFs with their parsed CSV / commentary output.
- `.env.example` — template for `.env`.

## Troubleshooting

- **`N row(s) are not loadable`** — those rows are in `<stem>.rejected.csv` with a `_reason`
  (an empty `Low-High`, or a duplicate primary key). The rest of the date still loads. An empty
  `Low-High` means no strike was parsed for that leg: find the leg in `<stem>.txt` and in
  `<stem>.parse-report.json`, then `--review` it. (This used to be the
  `Column 'Low-High' cannot be null` error that silently dropped the whole date.)
- **`N block(s) still need a human decision`** — run again with `--review` and answer them, or read
  `<stem>.parse-report.json` to see what was flagged and why.
- **A row looks wrong but nothing was flagged** — compare the CSV against `<stem>.txt` (the exact
  text the parser saw), then against the grammar in the extraction spec for that publication. A
  wrong-but-unflagged row is a parser bug: fix the regex and add the line shape to the spec.
- **`LLM_BASE_URL not set; skipping LLM repair`** — expected when no endpoint is configured; flagged
  blocks are recorded as `unavailable` in `.repairs.json` and wait for `--review`.
- **LLM repair rejected** — `.repairs.json` gives the reason (`Stop='999.99' is not a number printed
  in the block text`, `symbol 'X' is not in the block text`, …). The regex rows are kept; nothing
  unvalidated ever reaches the CSV.
- **`no market commentary captured from page 2`** — the "Market Expectations" heading (or the
  fallback "Copyright" line) wasn't found on page 2. Read `<stem>.txt` and compare against the
  markers in the script. (`LCR Top20 2020-05-18` genuinely has none.)
- **PDF not picked up** — the filename must match `*Playbook YYYY-MM-DD.pdf` /
  `*Top20 YYYY-MM-DD.pdf` (case-insensitive, space or `-` before the date; Playbook also accepts `_`).
- **Date skipped** — it's already in the target table; use `--replace-date` to re-load it.
