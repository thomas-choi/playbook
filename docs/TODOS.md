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
      flip `SKIP_DB_UPLOAD = False` in `ETF_2_Code.ipynb`.
- [ ] **Feature A — market commentary → llmwiki.** `ETF_2_Code.ipynb` now splits physical page
      2's trailing commentary out (saved to a local `.txt`) and has a ready `MAX_LLMWIKI_RETRY`
      retry/skip upload function, but the actual `POST /upload` call is still gated on
      `LLMWIKI_BASE_URL` being set — blocked on llmwiki being deployed/reachable.
- [x] **Add `.env.example`.** Added at the repo root, covering both the existing `DB_Config.env`
      keys and the new `LLMWIKI_*` vars.
