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
   │   rows[] + blocks, each with flags and labels    │   (same)
   │ label_missing_keys()  backstop for blank keys    │ label_missing_keys()
   ├── <stem>.parse-report.json ──────────────────────┤
   │ apply_decisions()  recorded human answers         │ apply_decisions()
   │ confirm_missing()  Symbol/Trend nothing could    │ confirm_missing()  Symbol
   │                    read — asked before the LLM   │
   │ run_repairs()   flagged blocks only → LLM        │ run_repairs()
   │ review()        --review: ask about the rest     │ review()
   │ record_confirmations()  the answers, final rows  │ record_confirmations()
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
content, matched no branch), `incomplete` (a row is missing a field), `unknown_header`,
`unknown_header`. Any flag makes the block *flagged*.

Alongside the flags each block carries **labels**: `Block.missing[column] = Missing(reason, suggest,
source, answer)`, set by the parser at the line where it gave up on a column the table cannot take a
NULL in. A flag says "I could not use this line"; a label says "I never read this column". Labels
are what `confirm_missing` asks about, they appear in `<stem>.parse-report.json` under `missing`, and
an unanswered one keeps the block unsettled no matter what else happens to it.

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
the block's own text, so an answer stops applying the moment the publisher changes that block: a new
issue with different numbers is a new question, not a stale answer. On every later run
`hil_review.apply_decisions` replays it before the LLM is consulted, which is what makes a
`--replace-date` reload months later produce the same rows with nobody present. Where the file lives,
what is in it, and how to revoke or correct an answer: *The decisions store*, below.

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

### A missing key column is always asked about

`Trend` and `Symbol` are the key columns a person can supply — `Date` comes from the filename and
`Low-High` from the strikes — and `ETF_Options_v1` takes no NULL in any of them
(`Stock_Options_v1`: `Symbol`). A row without one is not a cosmetic gap: `split_loadable` rejects it
and the leg never reaches the table. That makes it the one question that is *not* behind `--review`,
and it is asked **before** the LLM, because reading the answer off the block is quicker than waiting
for a repair round-trip — and because the model then gets the answer as given:

```
912_Playbook_2026-10-05.pdf: 1 block(s) are missing a required column (Symbol, Trend)
──────────────────────────────────────────────────────────────────────────────
[1/1] Walmart/?   rows 1-2
  --- block text -----------------------------------------------------------
     Walmart Inc [WMT]
     Walmart Inc [Bullish/Countertrend]
     BTO March 105/115 Bull Call Spread (3/19/27 exp) &
     STO 2 March 90 Naked Puts (3/19/27 exp)
     for conditional Entry of a net .50 Credit, conditional Target @ WMT 114.45
     Place: Conditional Exit to BTC Naked Puts & STC Bull Call Spread @ WMT 90.61 Stop
  --------------------------------------------------------------------------
    [1] Type=Featured  Status=Bullish/Countertrend  Expiration=3/19/27  PnC=C  L_Strike=105  …
    [2] Type=Featured  Status=Bullish/Countertrend  Expiration=3/19/27  PnC=P  H_Strike=90  …
  Symbol — the heading 'Walmart Inc' states the status, not a [TICKER]
    candidate: WMT   (from a '@ WMT price' in this group)
    [type a symbol]
    [Enter] accept WMT      [s] skip for now      [q] stop asking
  Symbol for these rows? > ⏎
  recorded: Symbol = WMT
  Trend — the group states no Investor/Trader Trend
    candidate: Investor   (from the section heading 'Featured Investor & Maintenance Suggestions')
    [1] Investor   <- the page suggests this
    [2] Trader
    [Enter] accept Investor      [s] skip for now      [q] stop asking
  Trend for these rows? > ⏎
  recorded: Trend = Investor
```

It asks rather than guesses, but it does not ask blind: each label carries the parser's candidate
and where it came from, Enter accepts it, and the common case is one keystroke per column. A typed
value is held to the same bar as the model's — `validate_rows` runs on the result, so a ticker that
is printed nowhere in the block is refused unless you insist (typing `BA` into the Lennar block of
`2026-09-21` gets `! row 0: symbol 'BA' is not in the block text`).

Needs a terminal: with no TTY (cron, `</dev/null`) nothing is asked, the run logs
`N row(s) still have no Symbol/Trend and cannot load`, and those rows wait in `.rejected.csv` for
the next interactive run. `--no-confirm` turns the question off.

**Ask early, record late.** The answers go onto the rows immediately, but the decision is written to
the store at the *end* of the run (`record_confirmations`), holding the block's rows as they finally
stand. Recording the pre-repair rows instead would make the next run differ from this one: a
replayed decision marks the block decided, which excludes it from `flagged_blocks()`, so the LLM
would be skipped the second time round. As it is, a replay — or a `--replace-date` reload months
later — reproduces exactly the rows that were loaded, with no LLM call and nobody present. A block
`--review` already recorded is left alone; its record is the final one and carries the confirmed
columns too. If a repair is accepted for a confirmed block (`--llm force`, or a block with other
flags), `reapply_answers` puts the confirmed columns back — the model does not get to overrule a
person.

### Where a block with no Symbol or Trend comes from

A group header the regex cannot read an `Investor`/`Trader` off opens a block with an empty Trend
instead of dropping back into the Trend branch for every line below it — which used to lose the whole
symbol section silently. Legs printed before any heading get a block of their own, labelled for both
columns. In Top20, a stock heading whose `[TICKER]` the PDF dropped is labelled for `Symbol`
(`912 Top20 2022-12-19`: `Uber Technologies Inc`, candidate `UBER` from
`@ UBER 27.87 Stop` — it used to need `--review`).

The main one is the **Featured Investor heading that states the status where the ticker belongs** and
prints no Trend line at all:

```
                   Featured Trade & Maintenance Suggestions
Walmart Inc [WMT]                       <- symbol heading, Trend line below it
            Trader Trend [Bullish]
                      STO October 103 Naked Put (10/16/26 exp)
                     Place: Conditional Exit to BTC Naked Put @ WMT 102.27 Stop

                Featured Investor & Maintenance Suggestions
        Walmart Inc [Bullish/Countertrend]      <- no [TICKER], and no Trend line anywhere
BTO March 105/115 Bull Call Spread (3/19/27 exp) &
STO 2 March 90 Naked Puts (3/19/27 exp)
for conditional Entry of a net .50 Credit, conditional Target @ WMT 114.45
```

`_status_head` recognises it (a bracket that is a status word and not an all-caps ticker, on a line
whose left half is not itself a `... Trend` label) and opens a group there. Before the fix nothing
matched that line, so the parser carried on inside the *Trader* group and three things went wrong at
once, none of them visible: the two combo legs inherited `Trend=Trader` and `Status=Bullish`, they
sat outside every block (so no flag, no LLM, no review could reach them), and the combo's
`Entry of a net .50 Credit` / Target / Stop were broadcast back over the Trader naked put, whose own
`@ WMT 102.27 Stop` was overwritten. `912 Playbook 2026-09-21` has the same shape and had already
loaded that way.

**The company on such a heading is not always the one above it.** `2026-09-21` prints
`Lennar Corp [Bullish/Countertrend]` under Boeing's section, so inheriting the open symbol would file
Lennar's legs as `BA` — which is how that date loaded. The parser therefore inherits nothing here: it
labels `Symbol` missing and *offers* a candidate, taken from the group's own conditional prices
(`conditional Target @ LEN 98.62` → `LEN`), falling back to the section's ticker only when the
heading repeats that section's company name. Nothing is filled in until a person accepts it. If the
heading matches neither — a different company with no `@ TICKER price` — the block also gets an
`unknown_header` flag, so the candidate is marked as the guess it is and the block stays in the
`--review` queue after the columns are answered.

## The decisions store (`.pdfreader-decisions.json`)

Every other artefact of a run is derived: delete `<stem>.csv`, `<stem>.txt`,
`<stem>.parse-report.json` or `<stem>.repairs.json` and the next run rebuilds them from the PDF. This
one is not. It holds the answers a person gave — which of two duplicate legs is the stale one, the
Trend of a group that prints none, the ticker a status heading left out — and nothing can recompute
them. It is also what makes a re-run silent and a `--replace-date` reload months later reproduce the
rows that were really loaded, with no LLM call and nobody present. Back it up with the PDFs.

### Where it lives

`.pdfreader-decisions.json` in the **PDF's own folder**, so `~/lcr/prod/Playbooks/` and
`~/lcr/prod/top20/` each keep one next to the issues it is about. `--decisions PATH` overrides that,
and so does `$PDFREADER_DECISIONS` (`hil_review.store_path`). One file serves a whole folder run —
`get_store` keeps a single `DecisionStore` per path, so 165 Playbook PDFs share one, written after
each change and again by `save_all()` when the run ends.

Moving the PDFs, or pointing `--decisions` somewhere new, starts from an empty store: the answers are
not found and every question is asked again.

### What is in it

`{"version": 1, "decisions": {<fingerprint>: {…}}}`, written with `indent=2, sort_keys=True` so it
diffs and reads cleanly. One real entry, trimmed to one of its two rows:

```json
{
  "decisions": {
    "b02974c6806c2878": {
      "action": "set_fields",
      "block": "WMT/Investor",
      "date": "2026-10-05",
      "decided_at": "2026-10-05T00:59:57",
      "decided_by": "thomas",
      "flags": [],
      "note": "Symbol=WMT, Trend=Investor confirmed by a person",
      "rows": [
        { "Type": "Featured", "Symbol": "WMT", "Trend": "Investor",
          "Status": "Bullish/Countertrend", "Expiration": "3/19/27", "PnC": "C",
          "L_Strike": "105", "H_Strike": "115", "quantity": 1,
          "Entry": -0.5, "Target": "114.45", "Stop": "90.61" }
      ],
      "source": "912_Playbook_2026-10-05.pdf",
      "symbol": "WMT",
      "trend": "Investor"
    }
  },
  "version": 1
}
```

| key | what it is |
|---|---|
| `rows` | the block's rows **as they finally stood** — the replay payload, spliced back in verbatim |
| `action` | which kind of answer it was (vocabulary below) |
| `block`, `symbol`, `trend` | the block's identity *after* the answer, so the file can be read without the PDF |
| `date`, `source` | the issue date and PDF filename the answer was first given on |
| `note` | why, in words: `Symbol=WMT, Trend=Investor confirmed by a person`, `the value is not printed in the PDF`, or `forced past validation: <reason>` |
| `flags` | what was wrong with the block at the time (empty when the only problem was a column the parser never read) |
| `decided_at`, `decided_by` | ISO timestamp to the second, and the OS user who answered |

| `action` | written by | means |
|---|---|---|
| `set_fields` | `record_confirmations`, or `--review` `[3]` | a column was filled in by hand |
| `keep_regex` | `--review` `[2]` | the regex rows were right as they were |
| `accept_llm` | `--review` `[1]` | the model's rows were right |
| `no_value` | `--review` `[4]` | the value really is not in the PDF — keep it empty, stop flagging |
| `drop_row` / `add_row` | `--review` `[5]` / `[6]` | a leg the regexes invented / missed |
| `unresolvable` | `--review` `[u]` | nobody could read it; `note` says why |

### The key is the block's text

`fingerprint()` is `sha1("<publication>\0<block text>")[:16]`, where the block text has each line's
runs of whitespace collapsed to one space and its blank lines dropped. Two consequences, both
deliberate:

- **An edit to the block retires the answer.** A decision is about *that text*; re-priced or
  re-worded, it is a new question rather than a silently stale answer. This is the property that lets
  the store be replayed unattended.
- **Re-typesetting alone does not retire it.** The normalisation absorbs the indentation and column
  padding PyMuPDF's extraction shifts around between issues.

The publication prefix (`playbook` / `top20`) keeps the two namespaces apart, so one file can hold
both if `--decisions` points them at it.

### Who writes it, and when

Two writers, and never the same fingerprint:

1. **`review()`** — `--review` only. Writes each answer the moment it is given, then `store.save()`.
2. **`record_confirmations()`** — the `Symbol`/`Trend` confirmations, written at the **end** of the
   parse stage with the rows as they finally stand, skipping any block `review` already recorded. Why
   the end and not when the question was asked: *Ask early, record late*, above.

`save()` is a no-op when nothing changed, and the write is atomic — a `.pdfreader-decisions.json.tmp`
sibling, then a rename — so an interrupted run cannot truncate the store.

**Answers are recorded in `--csv-only` / `--dry-run` runs too.** Both writers run before the DB stage
and are not gated on it, so the verify pass of the usual `--csv-only` → inspect → load sequence
already remembers what you answered, and the load run replays it instead of asking again.

### How it is read back

`apply_decisions()` runs early — after `label_missing_keys`, before `confirm_missing` and before the
LLM — and only over `flagged_blocks()`. So a decision is looked up only while its block still has
something to ask about: once the regexes parse a block cleanly, the entry about it is never consulted
again (it stays in the file, inert). For a block whose fingerprint matches it:

- splices the recorded `rows` in with `replace_rows`, which shifts the later blocks' row indices so
  the report and any prompt still point at the right rows;
- sets `block.decision`, which takes the block out of `flagged_blocks()` — no LLM call, and nothing
  for `--review` to ask;
- answers the block's `Missing` labels from the recorded rows, credited to whoever gave them
  (`decided_by`), so a confirmed column is not asked about a second time;
- re-titles the block from those answers, so a replayed run's log and report read `WMT/Investor`, like
  the run that asked.

### Changing your mind

The file is small, sorted and meant to be edited by hand:

- **revoke an answer** — delete its entry. The block flags again and the question comes back on the
  next interactive run.
- **correct a value** — edit the entry's `rows`. They are replayed **verbatim and unvalidated**:
  `validate_rows` gates what a person or the model proposes at the prompt, not what the store already
  holds. A typo here reaches MySQL.
- **audit** — `decided_by`, `decided_at`, `source` and `note` are there to be grepped;
  `jq '.decisions | length'` is the count, `jq -r '.decisions[] | "\(.source) \(.action) \(.note)"'`
  the log.
- **a corrupt or non-conforming file** is warned about (`could not read … starting a new store`) and
  replaced by an empty one, which the next `save()` writes over the old — copy it aside before
  experimenting.
- **nothing prunes it.** There is no GC for an entry whose block no longer appears in any issue.
  Growth is slow (one entry per human answer — 8 blocks across the 238-PDF archive, plus the
  `Symbol`/`Trend` confirmations) and stale entries are inert, but they are never removed
  automatically.

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
# (a missing Symbol/Trend is asked about on any interactive run, unless --no-confirm)
.venv/bin/python playbook_etf.py /home/thomas/lcr/prod/Playbooks --csv-only --review
.venv/bin/python playbook_etf.py /home/thomas/lcr/prod/Playbooks --replace-date
```

The positional argument is a single PDF or a folder. Exit status is non-zero if a file failed, a row
could not be loaded, or a flagged block went unrepaired — the `Done:` line carries the counts.

### What a run leaves behind, and what is cleaned up

| file | kept because |
|---|---|
| `<stem>.pdf` | the only source; everything else is derived from it |
| `.pdfreader-decisions.json` | the one irreplaceable artefact — see the section above |
| `<stem>.csv` | the record of what loaded for that date, and what you diff after a parser change |
| `<stem>_commentary.txt` | the llmwiki payload; lets you re-post without re-parsing |
| `<stem>.repairs.json` | written only when the LLM was asked. An LLM call is not reproducible, so this is the only record of what it proposed and why that was accepted or rejected |
| `<stem>.txt`, `<stem>.parse-report.json` | **rebuildable**: the extracted text and the block/flag audit of the *last* run, rewritten on every run. These are the two a finished date cleans up |
| `<stem>.rejected.csv` | a to-do list rather than an archive — written when a row cannot load, and deleted by the next run of that date that has none |

Nothing in either script reads any of them back (the only `exists()` check is the one that clears a
stale `.rejected.csv`), so dropping the rebuildable two costs nothing but the ability to inspect that
run afterwards.

Those two go automatically, **per date, and only when that date is actually finished**: every parsed
row loaded, none rejected, no block unsettled, no `Symbol`/`Trend` left blank, and the commentary
delivered to llmwiki (when one is configured and was not skipped). Fail any of those and they stay,
with the run naming the condition that held them — a run that did not finish its job is exactly the
run whose text and parse report you want to read next:

```
912_Playbook_2026-10-05.pdf: cleaned 912_Playbook_2026-10-05.txt, 912_Playbook_2026-10-05.parse-report.json
912 Top20 2022-12-19.pdf: keeping 912 Top20 2022-12-19.txt and the parse report — 1 row(s) were rejected; 1 block(s) are unsettled; 1 row(s) have no Symbol
```

A verify pass (`--csv-only` / `--skip-db`) loads nothing, so it never cleans: its reports are the
entire point of it. `--no-clean` keeps them after a successful load too — worth it while a parser
change is being checked date by date, since `.parse-report.json` is the only per-date record of which
blocks were flagged. `--out-dir DIR` is the other way to keep the archive tidy: every derived file
goes elsewhere and the PDF folder stays as PDFs plus CSVs. The decisions store is never touched by
any of this, by design.

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
- The Trend-less Featured heading, over both archives (165 Playbook + 80 Top20, `--llm off
  --csv-only`): exactly two Playbook issues change and every other CSV is byte-identical to the
  previous parser. `2026-10-05` → WMT's Trader put keeps its own `102.27` Stop and the two combo
  legs become a block answered `Symbol=WMT, Trend=Investor` in two keystrokes; `2026-09-21` → BA's
  Trader call recovers its `219.40` Stop and the combo legs are offered `LEN`, not `BA` (typing
  `BA` is refused as not printed in the block). The only Top20 change is `2022-12-19`'s Uber
  section, whose `incomplete` flag became a `Symbol` label with candidate `UBER`; answering it
  loads all 20 rows with nothing in `.rejected.csv`.
- Ask-early/record-late: the interactive run and the non-interactive replay of the same PDF produce
  byte-identical CSVs, and the replay reports `0 flagged, 0 unsettled` without calling the LLM.
  Against a stub endpoint under `--llm force`, a repair that returns `Trend=Trader` for a block a
  person confirmed as `Investor` is accepted and `reapply_answers` restores `Investor`.
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
- A `Featured Investor` heading that names a company with no `@ TICKER price` anywhere in its group
  has no candidate to offer: the `Symbol` label is asked with the section's ticker as a guess (or
  nothing), flagged `unknown_header`, and must be typed. With no TTY those rows stay in
  `.rejected.csv` — which is the intended failure, not a silent mis-file.
- A decision is bound to the exact block text it was made about. Re-flowed text in a corrected
  re-issue of the same date is a new fingerprint and will be asked again — deliberately.
