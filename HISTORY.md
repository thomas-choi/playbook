# HISTORY

Change log for the playbook repo, per the mandatory logging rule in the global
`CLAUDE.md`. One entry per change, newest first.

---

## 2026-09-13 — Fix `IntegrityError: Column 'Low-High' cannot be null` on ETF loads

**Goal:** Stop `playbook_etf.py` / `ETF_2_Code.ipynb` / `ETF_1_Code_v3_UBugfix2.ipynb`
from failing to load legitimate rows into `Trading.ETF_Options_v1` / `Trading.ETF_Options`
whenever a parsed row has no strike prices.

**Root cause:** `Low-High` is a derived display column (`"+40P-50P"`-style strings built
from `L_Strike`/`H_Strike`/`PnC`) that is deliberately initialized to `""` and left blank
for rows that carry no option leg — e.g. the "Major US Market"/"Major US Bond" index-level
summary rows for SPY/QQQ/IWM that only state a directional bias, not a specific strike.
Immediately before the DB upload, all three pipelines run a blanket
`df.replace(r'^\s*$', np.nan, regex=True)` to turn blank placeholders into real `NULL`s for
the columns that are nullable in the DB schema (`Expiration`, `PnC`, `L_Strike`, `H_Strike`,
`Entry`, `Target`, `Stop`). That same blanket replace also nulled out `Low-High`, but
`Low-High` is defined `NOT NULL` on both target tables, so every PDF containing a
strike-less summary row failed the whole batch insert with
`pymysql.err.IntegrityError: (1048, "Column 'Low-High' cannot be null")`
(see `playbook-log.txt:23-110`, run of 2026-09-13, `Trading.ETF_Options_v1`).

**Implementation detail:** In each of the three places that do the blanket NaN conversion,
capture `Low-High` beforehand and restore it immediately after, so it round-trips as `""`
instead of `NaN`/`NULL`, while every other column keeps its existing NULL-for-blank
behavior:
- `playbook_etf.py::finalize()` — `db_df["Low-High"] = csv_df["Low-High"]` right after the
  `replace()` call.
- `ETF_2_Code.ipynb` cell `ba81d5c7` — same pattern (`low_high = final_df["Low-High"]` /
  `final_df.replace(...)` / `final_df["Low-High"] = low_high`).
- `ETF_1_Code_v3_UBugfix2.ipynb` cell `cell-27` — same pattern, for the older
  `Trading.ETF_Options` table.

No DB schema change was made — this fix works within the existing `Low-High NOT NULL`
constraint rather than altering the live production table, since that would need
coordinated access to the MySQL instance described in `DB_Config.env`.

**Related files:**
- `/home/thomas/projects/playbook/playbook_etf.py` (`finalize()`)
- `/home/thomas/projects/playbook/ETF_2_Code.ipynb` (cell `ba81d5c7`)
- `/home/thomas/projects/playbook/ETF_1_Code_v3_UBugfix2.ipynb` (cell `cell-27`)

**Test coverage:** This repo has no pytest suite (see `CLAUDE.md`: "no test suite, and no
build step"), so no existing automated tests could regress and none were added. Verified
manually with an ad hoc script that ran `finalize()` on a synthetic two-row frame shaped
like the failing batch (one strike-less "Major US Market"/SPY row, one normal strike-bearing
row): confirmed `Low-High` has 0 nulls after `finalize()` (previously 1) while `Expiration`
still correctly nulls out for the strike-less row (no regression on the columns that are
meant to go `NULL`). No new test file was added — flagging this as a gap: if a pytest suite
is ever introduced for this repo, a regression test for `finalize()`/`derive_columns()`
covering a strike-less row belongs in it.
