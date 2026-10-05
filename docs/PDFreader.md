# PDFreader — Playbook / Top20 PDFs → CSV → MySQL, with LLM repair

How the two batch scripts read the PDF archive, what went wrong with the pure-regex parsers, and
what was changed. The per-publication parsing contracts live with the skills:

- [`.claude/skills/lcr-playbook-pdf/`](../.claude/skills/lcr-playbook-pdf/SKILL.md) —
  `SKILL.md` (how to run it) + `reference/playbook-extraction-spec.md` (the document grammar and
  target schema, which is also the LLM system prompt).
- [`.claude/skills/lcr-top20-pdf/`](../.claude/skills/lcr-top20-pdf/SKILL.md) — the same pair for
  Top20.

## Why this exists

Both scripts worked: PyMuPDF text extraction → line-by-line regex parse → CSV → MySQL → llmwiki
commentary. The problem was that the PDFs do not always follow their own layout, and the scripts
swallowed the damage. From the last full run before this change (`play-log.txt`, 160 Playbook PDFs,
2026-09-13):

- **398 lines raised an exception and were skipped** — each one a dropped trade leg, stop, status or
  section header, logged as `WARNING ... Exception on line` and otherwise invisible.
- **18 of 160 DB loads failed outright** with
  `IntegrityError (1048, "Column 'Low-High' cannot be null")`. `DU.StoreEOD` caught and logged the
  exception, so the run still reported success; a single unparseable strike cost the whole date.
- `Trading.ETF_Options_v1` had **5,844 of 9,621 rows with a NULL `Entry`**, because the PDF states
  one conditional Entry/Target per Trend group but only the leg whose line carried the text got it.
- One Playbook date (`2022-06-06`) and two Top20 dates (`2020-05-18`, `2022-12-19`) were absent from
  the tables entirely.

The fix is not to replace the regexes. It is: fix what the regexes can simply handle, make the parser
notice when it is confused, hand *that block* to an LLM with an explicit spec, validate what comes
back, and never let a bad row take a date with it.

## Pipeline

```
*Playbook YYYY-MM-DD.pdf                          *Top20 YYYY-MM-DD.pdf
   │ extract_text()  (PyMuPDF, page 1 skipped)        │ extract_text()  (page 2 = commentary only)
   ├── <stem>.txt  — exactly what the parser saw ─────┤
   │ parse_trades()  + ParseReport                    │ parse_stocks()  + ParseReport
   ├── <stem>.parse-report.json ──────────────────────┤
   │ apply_decisions()  recorded human answers         │ apply_decisions()
   │ run_repairs()   flagged blocks only → LLM        │ run_repairs()
   │ review()        --review: ask about the rest     │ review()
   ├── <stem>.repairs.json, .pdfreader-decisions.json ┤
   │ derive_columns() → finalize() → split_loadable() │ (same)
   ├── <stem>.csv / <stem>.rejected.csv ──────────────┤
   │ DU.StoreEOD_strict() | DU.ReplaceDate()          │ (same)
   └── <stem>_commentary.txt → llmwiki POST /ingest ──┘
```

`hil_review.py` is the decisions store and the review prompt; `llm_repair.py` is the parsing support: `ParseReport`/`Block`/`Flag`, the OpenAI-standard
client, `repair_block`, `validate_rows`, `RepairLog`. The two scripts stay separate, as before — a
fix to one does not propagate to the other.

## What the regexes now handle

| Was dropped | Now |
|---|---|
| `STO March 146 put (12/16/2016 exp)` | `PNC_RE` is case-insensitive |
| `Featured Investor & Maintenance Suggestions` | `SECTION_RE` matches `(Trade\|Investor) & Maintenance Suggestions`; `Type` is the text before it |
| `Trader Trend [Bullish/Counter Trend]`, `Trader Trend [Bullish/Hold` | `STATUS_RE` = `\[([^\]\n]*)\]?` — spaces allowed, closing bracket optional |
| `conditional position Exit 145.52 Stop`, `... @ BIIB 116.00` (no `Stop` word) | the Stop branch triggers on `Stop` **or** `Exit` and reads the price after the last `@`, else the first one |
| `{12/16/16 exp)`, `8/15/25 exp)`, `(6/30 /23 exp)` | `EXP_RE` allows `(`/`{`/nothing and strips spaces |
| `STO June (Quarterly) 35/40 Bull Put Spread` | `(Quarterly)`/`(Monthly)`/`(Weekly)` removed before the strike is read |
| `STO 2 January 9 Naked Puts` | single-digit strikes via the price matcher, after the ≥2-digit matcher finds nothing |
| `Limit .25` / `net .15 Credit` | a price matcher that accepts a leading `.` |
| `Conservative Traders:`, `Best practice: …` / `practice: Close positions …` (wrapped), `P 3 Bond ETFs`, `Copyright ©`, `Vol 1 Issue 2 Page 2` | one `SKIP_RE`, consulted before any content branch |
| A Top20 stock heading with no `[TICKER]` (`Uber Technologies Inc`, 912 Top20 2022-12-19) | detected from the rating line that follows it; opens its own section, so the next stock's levels no longer overwrite the previous stock's row |
| A Top20 section with no `Entry @` line (~57 in the archive) | still produces its row, instead of writing its Target/Stop onto the previous stock |

Measured over the archive, LLM off: Playbook 160 files, **5,987 blocks, 1 flagged** (an `@ EFA Stop`
line where the PDF really has no price), 9,686 rows (+68) and empty `Entry` down from 5,845 to 2,386;
Top20 78 files, **3 flagged blocks**, 1,540 rows (+2) with no file losing a row.

## Entry/Target broadcast

`_broadcast_group_values` fills empty `Entry`/`Target`/`Stop` from the leg of the same
`(Type, Symbol, Trend)` group that states them — the same rule the `Entry of a net … Credit` combo
branch already applied. Empty cells only: a leg that states its own value keeps it. This is a change
in meaning for rows already in `ETF_Options_v1`, applied from now on per file/date processed.

## Flags, and what the LLM is asked

A *block* is one `[SYMBOL]` + Trend group (Playbook) or one `[SYMBOL]` section (Top20).
`ParseReport` records, per block: `exception` (the line raised), `unconsumed` (looks like trade
content, matched no branch), `incomplete` (a row is missing a field), `unknown_header`. Any flag
makes the block *flagged*.

With `--llm repair` (the default) only flagged blocks are sent: the block text, the rows the regexes
produced, the flags, and the extraction spec as the system prompt. The model returns
`{"rows": [...]}`, and the rows replace that block's rows **only if** they validate:

- JSON-schema shaped, every column present, `PnC` ∈ {`P`,`C`}, `Expiration` is `M/D/YY`;
- the `Symbol` appears in the block text;
- **every number appears verbatim in the block text** — the guard against invented prices.

Otherwise the regex rows stand and `.repairs.json` records the reason. `--llm force` sends every
block (for a layout never seen before); `--llm off` is the pure-regex path. No endpoint configured →
each flagged block is recorded `unavailable` and the run continues.

Configuration (`.env`, see `.env.example`): `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`,
`LLM_TEMPERATURE`, `LLM_TIMEOUT`, `LLM_MAX_RETRY`. Any OpenAI-standard endpoint — OpenAI, a gateway,
or a local ollama / vLLM / LM Studio server. Nothing in the repair path is provider-specific.

## Human-in-the-loop review

An LLM repair that fails validation, a block with no endpoint to ask, and a layout the model gets
wrong all end in the same place: a question only a person can answer — *what does this text actually
say?* `--review` asks it in the terminal, one block at a time:

```
[1/2] IWM/Trader  (IWM/Trader)
  flag: duplicate — IWM/Trader 146P is in both IWM/Trader (12/16/2016) and IWM/Trader#2 (6/19/20)
        [row 13] IWM 146P (12/16/2016)
  --- block text -----------------------------------------------------------
     iShares RusSTO 2000 [IWM]
     Trader Trend [Bullish/Hold]
     STO March  146 put (12/16/2016 exp) Limit .25
     BTO March  146/154 Bull Call spread (12/16/2016 exp) using
     condition Entry SPY 147.73, conditional Target SPY 154.35
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

A typed value is held to the same bar as the model's: a number that is not printed in the block is
refused (`! 999.99 is not a number printed in this block` — overridable with an explicit `y`), and
the resulting rows must validate before they are recorded.

**Answers are remembered, so nothing is asked twice.** Each decision is stored under a fingerprint of
the block's own text (whitespace-normalised, per publication) in `.pdfreader-decisions.json` next to
the PDFs — `--decisions PATH` or `PDFREADER_DECISIONS` moves it. On every later run
`hil_review.apply_decisions` replays the answer before the LLM is consulted, which is what makes a
`--replace-date` reload months later produce the same rows with nobody present. Because the
fingerprint covers the text verbatim, an answer stops applying the moment the publisher changes that
block: a new issue with different numbers is a new question, not a stale answer. The store is worth
backing up — it is human judgement, not derived data.

`--review` reads stdin and at EOF (cron, `</dev/null`) leaves the items pending and says so, so an
unattended run can never block. Without `--review`, a run that still has unsettled blocks logs
`N block(s) still need a human decision (--review): <block ids>` and exits non-zero.

### What reaches the queue

Anything flagged and not settled — plus one class the parser used to swallow: a **primary-key
collision**. `flag_duplicate_legs` / `flag_duplicate_symbols` compare every row against the table's
key across the whole issue, so two legs (or two sections) that cannot both load become a decision
instead of a silently dropped row. That is how the stale-boilerplate issues surface: `LCR Playbook
2020-06-01` and `2020-06-22` repeat SPY/QQQ/IWM at 2016 strikes with `12/16/2016` expirations
alongside the real 2020 sections, and the right answer — drop the stale leg, keep the current one —
is a judgement call, not a regex.

The whole backlog over the 238-PDF archive is **8 blocks**: the two Playbook duplicate pairs (4), the
`@ EFA Stop` with no price, the Top20 ticker-less Uber heading, and the two Top20 combo sections
(PENN, UBER in `LCR Top20 2020-05-18`). At that volume the terminal is the right surface; a decision
that recurs belongs in the regexes and the extraction spec instead.

## Database

`Trading.ETF_Options_v1` — PK `(Date, Trend, Symbol, Low-High)`, those four columns NOT NULL.
`Trading.Stock_Options_v1` — PK `(Date, Symbol)`.

- `split_loadable()` keeps a row with a NULL key column, or a duplicate key, out of the DB frame and
  writes it to `<stem>.rejected.csv` with a `_reason`. The row stays in the CSV; the date still loads.
- `DU.StoreEOD_strict()` reports what did not load instead of swallowing the error, and on a failed
  bulk insert retries row by row under savepoints — one bad row costs one row.
- `DU.ReplaceDate()` is `DELETE WHERE Date=:d` + insert in one transaction: this is what makes
  re-processing an already-loaded date idempotent. `DU.StoreEOD` and `DU.ExecSQL` are untouched (the
  notebooks still use them; note `ExecSQL`'s `engine.execute` is dead under SQLAlchemy 2).

## Running it

```bash
# verify first — CSV, text and reports only; nothing reaches MySQL or llmwiki
.venv/bin/python playbook_etf.py "/home/thomas/lcr/prod/Playbooks/912 Playbook 2026-09-07.pdf" --csv-only
.venv/bin/python top20_stock.py  "/home/thomas/lcr/prod/top20/912 Top20 2025-07-21.pdf"        --csv-only

# load new dates (dates already in the table are skipped)
.venv/bin/python playbook_etf.py /home/thomas/lcr/prod/Playbooks
.venv/bin/python top20_stock.py  /home/thomas/lcr/prod/top20

# re-fix one date that is already loaded
.venv/bin/python playbook_etf.py /home/thomas/lcr/prod/Playbooks --date 2022-06-06 --replace-date

# answer the blocks nothing could settle, then load with the answers applied
.venv/bin/python playbook_etf.py /home/thomas/lcr/prod/Playbooks --csv-only --review
.venv/bin/python playbook_etf.py /home/thomas/lcr/prod/Playbooks --replace-date
```

The positional argument is a single PDF or a folder. Exit status is non-zero if a file failed, a row
could not be loaded, or a flagged block went unrepaired — the `Done:` line carries the counts.

## Verified

- Full archive, `--llm off --csv-only`: no file loses a row versus the previous parser; all
  differences are recovered rows or group-filled Entry/Target/Stop. Commentary files are
  byte-identical for 159/160 Playbook and 77/77 Top20 issues; the one difference
  (`LCR Playbook 2020-08-03`) is a fix — the `Featured Investor & Maintenance Suggestions` section
  used to be swallowed into the commentary, and its two DIS legs are now parsed.
- Repair path, against a stub OpenAI-standard server: a valid repair is accepted (912 Top20
  2022-12-19 → `UBER` recovered, Comcast's own levels intact), a row with an invented price is
  rejected, HTTP 500 is retried then falls back to the regex rows, and a missing `LLM_BASE_URL` is
  recorded `unavailable`.
- Review path: the IWM duplicate pair answered `drop_row` / `keep_regex` and the ticker-less Uber
  heading answered `Symbol=UBER`; re-running without `--review` replays both (`applying recorded
  decision …`), reports `0 unsettled`, exits 0, and leaves no rejected rows. A typed `Stop=999.99`
  is refused as not printed in the block.
- DB layer, on a `CREATE TABLE … LIKE` copy: `ReplaceDate` run twice leaves the same row count; a
  batch of 3 rows with one NULL-key row inserts 2 and reports 1 with the driver's own message.
- The three dates that had never loaded are in: `ETF_Options_v1` 2022-06-06 = 52 rows,
  `Stock_Options_v1` 2020-05-18 = 20 rows and 2022-12-19 = 19 rows (the 20th is the ticker-less Uber
  row, held back in `.rejected.csv` until a repair supplies the symbol).

## Known limits

- A Top20 issue that prints a two-leg combo (`LCR Top20 2020-05-18`: PENN, UBER) cannot be fully
  represented — `Stock_Options_v1` holds one row per symbol. The first leg wins and the rest is
  flagged.
- `912 Playbook 2026-08-24` has `@ EFA Stop` with no price in the PDF. No parser and no model can
  recover it; the Stop stays empty.
- Two dates (`2020-06-01`, `2020-06-22`) carry the publisher's stale `146`-strike boilerplate
  alongside the real sections (also the source of the `12/16/2016` expirations). Both colliding
  blocks are flagged for review; until someone answers, the second row lands in `.rejected.csv`.
- A decision is bound to the exact block text it was made about. Re-flowed text in a corrected
  re-issue of the same date is a new fingerprint and will be asked again — deliberately.
