# TODOS.md

Open work for this repository. Check items off (`[x]`) and move a short note to `HISTORY.md`
when a task lands.

## Environment

- [x] **Upgrade to Python 3.11.** `.venv` is Python 3.11.17 with pandas 3.0.5, PyMuPDF 1.28 and
      SQLAlchemy 2.0. The CLI scripts are what is exercised under it; the notebooks have not been
      re-run top-to-bottom on 3.11.

## PDF reading (see `docs/PDFreader.md`)

- [x] **LLM repair of flagged parse blocks.** `llm_repair.py` + `--llm off|repair|force` in both CLI
      scripts, over any OpenAI-standard endpoint (`LLM_BASE_URL`/`LLM_MODEL`). Flagged blocks only;
      a repair is accepted only if every number in it is printed in the block text. Parsing
      contracts live in `.claude/skills/lcr-{playbook,top20}-pdf/reference/`.
- [x] **Stop silently dropping rows and dates.** The regex fixes (case-insensitive put/call,
      `Featured Investor &` headers, bracketed statuses with spaces, `Stop`/`Exit` without `@`,
      `(Quarterly)` before a strike, single-digit strikes, a skip list for prose/footers) plus
      `split_loadable` + `DU.StoreEOD_strict` + `DU.ReplaceDate`. Archive-wide: 398 parse exceptions
      → 1 flagged block (Playbook) and 3 (Top20), no file loses a row.
- [x] **Human-in-the-loop for the blocks nothing can settle.** `hil_review.py` + `--review` /
      `--decisions`: a terminal prompt per unsettled block, answers fingerprinted on the block text
      and replayed on every later run. Primary-key collisions are now flagged for review
      (`flag_duplicate_legs` / `flag_duplicate_symbols`) instead of being dropped at load time.
- [ ] **Answer the 8 open review items.** `--csv-only --review` over each archive:
      4 Playbook blocks (the 2016-boilerplate IWM/SPY/QQQ collisions in 2020-06-01 and 2020-06-22),
      `912 Playbook 2026-08-24` EFA (`@ EFA Stop`, no price in the PDF — answer `[4] not in the PDF`),
      `912 Top20 2022-12-19` (ticker-less Uber heading), and `LCR Top20 2020-05-18` PENN/UBER
      (two-leg combos in a one-row-per-symbol table). The answers in the scratch runs were not kept.
- [ ] **Re-process the historical archive with the new parser.** Only the three dates that had never
      loaded were re-loaded (Playbook 2022-06-06, Top20 2020-05-18 and 2022-12-19). The other 235
      dates still hold rows from the old parser — mostly NULL `Entry`/`Target` on legs that share a
      group value, and the ~65 rows the old parser dropped. Re-loading them is
      `--replace-date` per date (idempotent); decide whether the changed Entry/Target semantics are
      wanted in the history before doing it.
- [ ] **`DU.ExecSQL` is dead under SQLAlchemy 2** (`engine.execute` was removed). Unused by the CLI
      scripts; fix or delete it when a notebook needs it.

## Page 1 ingestion features (see `docs/Design-Plan.md`)

- [x] **Feature B — page 1 Trade/Investor rows → `Trading.ETF_Options`.** Implemented in
      `ETF_2_Code.ipynb` (page-loop now starts at physical page 2 instead of page 3), including
      multi-leg combo parsing (net Credit/Debit `Entry`, broadcast `Entry`/`Target`/`Stop` across
      combo legs) and the new `quantity` column (signed `BTO`/`STO` contract count). Validated
      against `Playbook-2026-09-07.pdf`'s real combo example (Paypal `[PYPL]`). The notebook
      writes CSV only by default (`SKIP_DB_UPLOAD = True`) — actually loading to
      `Trading.ETF_Options` is still blocked on the next item.
- [ ] **Migrate the live `Trading.ETF_Options` schema.** Add the `quantity INT DEFAULT 1` column
      and the composite primary key (`Date`, `Trend`, `Symbol`, `Low-High`) to the live table, and
      add upsert support in `dataUtil.py` (`DU.StoreEOD` currently does a plain
      `to_sql(if_exists='append')` with no dedupe — re-running a notebook for an already-loaded
      date would either hit a duplicate-key error or silently duplicate rows). Once this lands,
      `playbook_etf.py --force` can become a true re-load instead of an append.
- [x] **Feature A — market commentary → llmwiki.** `playbook_etf.py` posts each PDF's page-2
      commentary to `POST {LLMWIKI_BASE_URL}/ingest` as `{text, title}` with the date in both
      (retry `MAX_LLMWIKI_RETRY` times, then log and skip). Still needs a reachable llmwiki to
      exercise end-to-end; until then it logs "LLMWIKI_BASE_URL not set" and skips. The
      notebook's `/upload` stub is superseded — see `docs/Design-Plan.md` → "Batch script".
- [x] **Run `playbook_etf.py` against the real archive.** 160 Playbook PDFs in
      `/home/thomas/lcr/prod/Playbooks` and 78 Top20 PDFs in `/home/thomas/lcr/prod/top20` are
      processed and loaded; see `docs/PDFreader.md` for the measured outcome.
- [ ] **Run `playbook_etf.py` against the real Google-Drive folder** once it's mounted on the
      target machine (`/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/`) and compare
      the first CSV with one produced by `ETF_2_Code.ipynb` for the same date. Then consider
      scheduling it (cron) — it is safe to re-run because already-loaded dates are skipped.
- [x] **Add `.env.example`.** Added at the repo root, covering both the existing `DB_Config.env`
      keys and the new `LLMWIKI_*` vars.
