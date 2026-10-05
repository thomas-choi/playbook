---
name: lcr-playbook-pdf
description: Load an LCR/912 "Playbook" PDF (ETF/bond trade suggestions) into Trading.ETF_Options_v1 — run playbook_etf.py for one file or one date, read its parse report and LLM repair log, verify the CSV before upload, and fix a date that is already loaded. Use when asked to process, re-process, debug or re-fix a Playbook PDF, a Playbook CSV or an ETF_Options row.
---

# Playbook PDF → CSV → MySQL

`playbook_etf.py` turns a `*Playbook YYYY-MM-DD.pdf` into one row per option leg, writes a CSV next
to the PDF, appends the rows to `Trading.ETF_Options_v1`, and posts page 2's market commentary to
llmwiki. The parsing contract (page map, block grammar, every field, the known malformed layouts) is
in [reference/playbook-extraction-spec.md](reference/playbook-extraction-spec.md) — read it before
changing a regex or judging whether a row is right. The design and its history are in
`docs/PDFreader.md`.

## Run it

Always CSV-first, then upload:

```bash
# 1. verify: CSV + text + reports only, nothing touches MySQL or llmwiki
.venv/bin/python playbook_etf.py "/home/thomas/lcr/prod/Playbooks/912 Playbook 2026-09-07.pdf" --csv-only

# 2. upload a new date (skips dates already in the table)
.venv/bin/python playbook_etf.py /home/thomas/lcr/prod/Playbooks

# 3. re-fix a date that is already loaded (DELETE + INSERT in one transaction)
.venv/bin/python playbook_etf.py /home/thomas/lcr/prod/Playbooks --date 2022-06-06 --replace-date
```

Other flags: `--since YYYY-MM-DD`, `--out-dir`, `--skip-db`, `--skip-llmwiki`, `--log-level DEBUG`,
`--llm off|repair|force` (below), `--no-confirm` (don't ask about a Symbol/Trend the parser could
not read) and `--no-clean` (keep a loaded date's `<stem>.txt` and
`<stem>.parse-report.json`, which a completed load deletes). A non-zero exit means a file failed, a row could not be
loaded, or an LLM repair was rejected — the summary line names the counts.

## What it writes next to the PDF (or in `--out-dir`)

| File | Use |
|---|---|
| `<stem>.csv` | the rows, in DB column order |
| `<stem>.txt` | the extracted PDF text the parser actually saw — start here when a row looks wrong |
| `<stem>.parse-report.json` | every `[SYMBOL]`/Trend block, and the flags raised inside it |
| `<stem>.repairs.json` | each LLM repair call: flags, rows before/after, accepted or why rejected |
| `.pdfreader-decisions.json` (in the PDF folder) | the recorded human answers, replayed on every run |
| `<stem>.rejected.csv` | rows the table cannot take (empty `Low-High`, duplicate primary key) with a `_reason` column |
| `<stem>_commentary.txt` | the `Market Expectations ...` block sent to llmwiki |

`<stem>.txt` and `<stem>.parse-report.json` are rebuilt from the PDF on every run, so a date that
loads completely deletes them (`--no-clean` keeps them; a date with a rejected row, an unsettled
block or an undelivered commentary keeps them anyway). The decisions store is the one file nothing
can recompute — keep it.

## Reading a parse report

`counts` and `summary` first: `"5987 blocks, 1 flagged"` is a healthy full-archive run. Each flag is
one of

- `exception` — the line raised; always worth a look.
- `unconsumed` — a line that looks like trade content but matched no branch.
- `incomplete` — a row is missing a field (no strike, no `(M/D/YY exp)`, no `[status]`).
- `unknown_header` — a `... & Maintenance Suggestions` variant with no asset class before it.

Find the flagged line in `<stem>.txt`, decide from the spec what the right reading is, then either
fix the regex in `playbook_etf.py` (preferred — it is free and repeatable) or let the LLM repair
handle it. Known-good references to compare against: `test/912 Playbook 2025-01-20.pdf` (a
`Featured Investor & Maintenance Suggestions` section plus a net-credit combo) and
`test/LCR Playbook 2022-11-21.pdf`.

## LLM repair

With `--llm repair` (the default) only **flagged** blocks are re-read by the model; unflagged blocks
never change. Needs `LLM_BASE_URL`, `LLM_MODEL` and (for hosted endpoints) `LLM_API_KEY` in `.env` —
any OpenAI-standard endpoint, including a local ollama/vLLM server. Without them the run logs
"LLM_BASE_URL not set" and every flagged block is recorded as `unavailable`.

A repair is taken only if it validates: schema-shaped, `PnC` in `P`/`C`, symbol present in the block
text, expiration `M/D/YY`, and **every number present verbatim in the block text**. Otherwise the
regex rows stand and `.repairs.json` says why. `--llm force` re-reads every block, for diagnosing a
layout the regexes have never seen; `--llm off` is the pure-regex path.

## When nothing can settle a block: `--review`

A flagged block that the LLM did not fix (validation rejected it, no endpoint, or the model was
wrong) is a question for you. `--review` asks it in the terminal and **remembers the answer**:

```bash
.venv/bin/python playbook_etf.py "/home/thomas/lcr/prod/Playbooks/912 Playbook 2026-09-07.pdf" --csv-only --review
```

Per block it prints the flags, the block's own PDF text (flagged lines marked `>>`), the rows the
regexes produced and the LLM's proposal with the reason it was rejected, then offers: accept the LLM
proposal / keep the regex rows / set a field / confirm the value is not in the PDF / drop a row / add
a row / unresolvable / skip / quit. A value you type is checked the same way the model's is — a
number that is not printed in the block is refused unless you insist.

Answers go to `.pdfreader-decisions.json` next to the PDFs (`--decisions PATH` to move it), keyed by
a fingerprint of the block's text. Every later run replays them before the LLM is consulted, so the
same question is never asked twice and a `--replace-date` reload reproduces the same rows with nobody
present. Change in the PDF text → new fingerprint → asked again, on purpose. Back the file up; it is
human judgement, not derived data.

At EOF on stdin (cron, `</dev/null`) the review leaves everything pending and says so — an
unattended run cannot block. Without `--review`, unsettled blocks are logged
(`N block(s) still need a human decision`) and the run exits non-zero.

## A missing Symbol or Trend is always asked about

`Trend` and `Symbol` are the key columns you can supply (`Date` comes from the filename,
`Low-High` from the strikes), and a row without either goes straight to `.rejected.csv`. So that
question is asked on **every** interactive run, not just under `--review`, and **before** the LLM —
reading the answer off the block beats waiting for a repair round-trip, and the model then gets your
answer as given.

Each question carries the parser's candidate and where it found it; Enter accepts it, `s` skips the
block, `q` stops. `912_Playbook_2026-10-05.pdf` is two keystrokes:

```
[1/1] Walmart/?   rows 1-2
     Walmart Inc [WMT]
     Walmart Inc [Bullish/Countertrend]
     BTO March 105/115 Bull Call Spread (3/19/27 exp) &
     STO 2 March 90 Naked Puts (3/19/27 exp)
     for conditional Entry of a net .50 Credit, conditional Target @ WMT 114.45
  Symbol — the heading 'Walmart Inc' states the status, not a [TICKER]
    candidate: WMT   (from a '@ WMT price' in this group)
    [Enter] accept WMT      [s] skip for now      [q] stop asking
  Trend — the group states no Investor/Trader Trend
    candidate: Investor   (from the section heading 'Featured Investor & Maintenance Suggestions')
    [1] Investor   <- the page suggests this
    [2] Trader
    [Enter] accept Investor
```

A ticker you type is checked like the model's: one that appears nowhere in the block text is refused
unless you insist. Answers are recorded at the end of the run with the block's final rows, so a
re-run (or a `--replace-date` reload) reproduces them with nobody present, and an accepted LLM
repair can never overwrite what you confirmed.

With no terminal (cron, `</dev/null`) nothing is asked; the run logs
`N row(s) still have no Symbol/Trend and cannot load (rows …)` and those rows wait in
`.rejected.csv`. `--no-confirm` turns the question off.

### Where those blocks come from

Usually a `Featured Investor & Maintenance Suggestions` section whose heading puts the status where
the ticker belongs and prints no Trend line at all — `Walmart Inc [Bullish/Countertrend]`
(2026-10-05), `Lennar Corp [Bullish/Countertrend]` under Boeing's section (2026-09-21). The parser
opens a group there, takes `Status` from the bracket, and labels `Symbol` and `Trend` as missing
rather than inheriting them: **the company on such a heading is not always the one above it**, and
inheriting it is what filed Lennar's legs as `BA`. Before this, those legs silently joined the
Trader group above them, inherited its Trend and Status, and overwrote its Entry/Target/Stop.

A group header the parser cannot read an `Investor`/`Trader` off (e.g. `Intermediate Trend
[Bullish/Hold]`) likewise opens a block with an empty Trend, so the legs under it are parsed and
labelled — the whole symbol section no longer disappears. Legs printed before any heading get a
block of their own, labelled for both columns.

If the heading names a company and the group never prints `@ TICKER price`, there is no candidate:
the block also gets an `unknown_header` flag and stays in the `--review` queue after you answer, so
check `Symbol` there.

**A decision that recurs is a bug, not an answer.** If the same shape comes up a third time, put it
in the regexes and in the extraction spec instead.

Playbook-specific: `flag_duplicate_legs` compares every leg against the table's primary key
(`Date, Trend, Symbol, Low-High`) across the whole issue, so two legs that cannot both load become a
review item instead of a dropped row. That is how the stale-boilerplate issues surface —
`LCR Playbook 2020-06-01` and `2020-06-22` repeat SPY/QQQ/IWM at 2016 strikes (`12/16/2016`
expirations) beside the real 2020 sections; the answer is `drop_row` on the stale block and
`keep_regex` on the current one. Repeated sections get `#2` suffixes in the block id
(`IWM/Trader`, `IWM/Trader#2`).

## When the PDF changes shape

1. Reproduce with `--csv-only --llm off --log-level DEBUG` and read `<stem>.txt`.
2. Add the new line shape to the table at the end of the extraction spec.
3. Prefer a regex fix (`SECTION_RE`, `EXP_RE`, `STATUS_RE`, `SKIP_RE`, the strike/price helpers) and
   re-run the whole archive with `--csv-only --out-dir /tmp/check` — row counts must not fall:
   `python - <<'EOF'` comparing `len(csv)` per file against the previous output.
4. Only then rely on the LLM path for the genuinely irregular issues.
