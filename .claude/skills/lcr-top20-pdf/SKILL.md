---
name: lcr-top20-pdf
description: Load an LCR/912 "Top20" PDF (per-stock entry/target/stop levels) into Trading.Stock_Options_v1 — run top20_stock.py for one file or one date, read its parse report and LLM repair log, verify the CSV before upload, and fix a date that is already loaded. Use when asked to process, re-process, debug or re-fix a Top20 PDF, a Top20 CSV or a Stock_Options row.
---

# Top20 PDF → CSV → MySQL

`top20_stock.py` turns a `*Top20 YYYY-MM-DD.pdf` into one row per stock (~20 per issue), writes a
CSV next to the PDF, appends the rows to `Trading.Stock_Options_v1`, and posts page 2's market
commentary to llmwiki. The parsing contract (page map, block grammar, every field, the known
malformed layouts) is in [reference/top20-extraction-spec.md](reference/top20-extraction-spec.md) —
read it before changing a regex or judging whether a row is right. The design and its history are in
`docs/PDFreader.md`.

## Run it

Always CSV-first, then upload:

```bash
# 1. verify: CSV + text + reports only, nothing touches MySQL or llmwiki
.venv/bin/python top20_stock.py "/home/thomas/lcr/prod/top20/912 Top20 2025-07-21.pdf" --csv-only

# 2. upload a new date (skips dates already in the table)
.venv/bin/python top20_stock.py /home/thomas/lcr/prod/top20

# 3. re-fix a date that is already loaded (DELETE + INSERT in one transaction)
.venv/bin/python top20_stock.py /home/thomas/lcr/prod/top20 --date 2022-12-19 --replace-date
```

Other flags: `--since YYYY-MM-DD`, `--out-dir`, `--skip-db`, `--skip-llmwiki`, `--log-level DEBUG`,
and `--llm off|repair|force` (below). A non-zero exit means a file failed, a row could not be
loaded, or an LLM repair was rejected — the summary line names the counts.

## What it writes next to the PDF (or in `--out-dir`)

| File | Use |
|---|---|
| `<stem>.csv` | the rows, in DB column order |
| `<stem>.txt` | the extracted PDF text the parser actually saw — start here when a row looks wrong |
| `<stem>.parse-report.json` | every `[SYMBOL]` block and the flags raised inside it |
| `<stem>.repairs.json` | each LLM repair call: flags, rows before/after, accepted or why rejected |
| `.pdfreader-decisions.json` (in the PDF folder) | the recorded human answers, replayed on every run |
| `<stem>.rejected.csv` | rows the table cannot take (no `Symbol`, duplicate `(Date, Symbol)`) with a `_reason` column |
| `<stem>_commentary.txt` | the `Market Expectations ...` block sent to llmwiki |

## Reading a parse report

A healthy issue is `"20 blocks, 0 flagged"`. The flags are `exception`, `unconsumed`, `incomplete`
(see the Playbook skill for the same taxonomy). Two Top20-specific ones matter most:

- `stock heading without a [TICKER]` — the PDF printed a company name with no ticker
  (`912 Top20 2022-12-19`, "Uber Technologies Inc"). The section is still opened, so the next
  stock's levels no longer overwrite the previous stock's row; the row has no `Symbol`, lands in
  `.rejected.csv`, and is what the LLM repair is for.
- `<field> is already '<value>' — a second stock may be in this block` — two trade suggestions in
  one section. `LCR Top20 2020-05-18` has Playbook-style two-leg combos (PENN, UBER); the table
  holds one leg per symbol, so the first leg wins and the rest is flagged.

Known-good references: `test/912 Top20 2025-07-21.pdf`, `test/LCR Top20 2022-11-21.pdf`.

## LLM repair

Identical policy to the Playbook skill: `--llm repair` (default) sends only flagged blocks to the
OpenAI-standard endpoint in `LLM_BASE_URL` / `LLM_MODEL`, a repair is accepted only if it validates
(schema, `PnC`, symbol present in the block text, `M/D/YY` expiration, every number printed in the
block), and anything else leaves the regex rows in place with the reason in `.repairs.json`.
`--llm force` re-reads every block; `--llm off` is the pure-regex path.

## When nothing can settle a block: `--review`

A flagged block that the LLM did not fix (validation rejected it, no endpoint, or the model was
wrong) is a question for you. `--review` asks it in the terminal and **remembers the answer**:

```bash
.venv/bin/python top20_stock.py "/home/thomas/lcr/prod/top20/912 Top20 2025-07-21.pdf" --csv-only --review
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

**A decision that recurs is a bug, not an answer.** If the same shape comes up a third time, put it
in the regexes and in the extraction spec instead.

Top20-specific: `flag_duplicate_symbols` flags both blocks when a ticker appears twice in one issue
(`Date, Symbol` is the key). The ticker-less heading in `912 Top20 2022-12-19` is answered with
`[3] set a field` → `Symbol` → `UBER`; the two combo sections in `LCR Top20 2020-05-18` (PENN, UBER)
are answered `keep_regex` once you have decided which leg the one row should hold.

## When the PDF changes shape

1. Reproduce with `--csv-only --llm off --log-level DEBUG` and read `<stem>.txt`.
2. Add the new line shape to the table at the end of the extraction spec.
3. Prefer a regex fix (`ENTRY_RE`, `TARGET_RE`, `EXP_RE`, `STATUS_LINE_RE`, `SKIP_RE`) and re-run the
   whole archive with `--csv-only --out-dir /tmp/check`; row counts must not fall (78 issues ≈ 1540
   rows).
4. Only then rely on the LLM path for the genuinely irregular issues.
