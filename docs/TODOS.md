# TODOS.md

Open work for this repository. Check items off (`[x]`) and move a short note to `HISTORY.md`
when a task lands.

## Environment

- [ ] **Upgrade to Python 3.11.** The checked-in `.venv` is still Python 3.8
      (`.venv/pyvenv.cfg`), well below the 3.11+ baseline. Rebuild the venv
      (`python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`)
      and confirm both notebooks still run top-to-bottom under the new interpreter before
      retiring the 3.8 one.

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
- [ ] **Run `playbook_etf.py` against the real Google-Drive folder** once it's mounted on the
      target machine (`/mnt/i/My Drive/ReadProjects/Neural Matrix Investment/LCR/`) and compare
      the first CSV with one produced by `ETF_2_Code.ipynb` for the same date. Then consider
      scheduling it (cron) — it is safe to re-run because already-loaded dates are skipped.
- [x] **Add `.env.example`.** Added at the repo root, covering both the existing `DB_Config.env`
      keys and the new `LLMWIKI_*` vars.
