"""db_patch.py: hand corrections and added rows, against an in-memory SQLite stand-in (conftest)."""
import json
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import text

import dataUtil as DU
import db_patch as P

ETF = P.TABLES["etf"]
STOCK = P.TABLES["stock"]
D = "2024-03-04"


def _csv(tmp_path, body, name="fix.csv"):
    path = tmp_path / name
    path.write_text(body.strip() + "\n")
    return path


def _rows(engine, table, where=""):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM Trading.{table} {where}"))
                .mappings()]


def _seed(engine):
    """Two ETF legs and two stocks for date D."""
    with engine.begin() as conn:
        conn.execute(text(
            """INSERT INTO Trading.ETF_Options_v1 (Date, Type, Trend, Symbol, Status, Expiration,
               PnC, "Low-High", L_Strike, H_Strike, Entry_Sign, Entry, Target_Sign, Target,
               Stop_Sign, Stop, quantity) VALUES
               ('2024-03-04','Bond','Investor','HYG','Bullish','2024-04-19','P','+78P-75P',
                78, 75, '<', 78.5, '>', 82.06, '<', NULL, 1),
               ('2024-03-04','Bond','Trader','XOP','Bearish','2024-04-19','C','+130C-140C',
                130, 140, '>', 135, '<', 120, '>', 140, 1)"""))
        conn.execute(text(
            """INSERT INTO Trading.Stock_Options_v1 (Date, Symbol, Status, Expiration, PnC, Strike,
               Entry_Sign, Entry1, Entry2, Target_Sign, Target, Stop_Sign, Stop, quantity) VALUES
               ('2024-03-04','AAPL','Outperform','2024-04-19','P',170,'<',172,171,'>',180,'<',165,1),
               ('2024-03-04','MSFT','Outperform','2024-04-19','P',400,'<',405,402,'>',420,'<',390,1)
            """))


def _run(tmp_path, *args):
    return P.main([*map(str, args), "--env-file", str(tmp_path / "none.env"),
                   "--log-level", "WARNING"])


# --------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------
def test_parse_mixed_etf_patch(tmp_path):
    path = _csv(tmp_path, """
Action,Date,Trend,Symbol,Low-High,PnC,L_Strike,H_Strike,Entry,Expiration
update,2024-03-04,Investor,HYG,+78P-75P,,,,78.95,
insert,2024-03-04,Trader,TLT,,P,95,90,96.5,8/15/25
delete,2024-03-04,Trader,XOP,+130C-140C,,,,,
""")
    up, ins, dele = P.parse_patch(path, ETF)
    assert (up.action, up.key, up.values) == (
        "update", {"Date": date(2024, 3, 4), "Trend": "Investor", "Symbol": "HYG",
                   "Low-High": "+78P-75P"}, {"Entry": 78.95})
    assert ins.key["Low-High"] == "+95P-90P"   # built from the strikes like derive_columns
    assert ins.values["Expiration"] == date(2025, 8, 15)
    assert (dele.action, dele.values) == ("delete", {})


def test_parse_stock_patch_types_cells(tmp_path):
    path = _csv(tmp_path, """
Action,Date,Symbol,Entry1,Stop,Description,Expiration
update,2024-03-04,AAPL,172.5,NULL,manual fix,2024-05-17
""")
    (e,) = P.parse_patch(path, STOCK)
    assert e.values == {"Entry1": 172.5, "Stop": None, "Description": "manual fix",
                        "Expiration": date(2024, 5, 17)}


@pytest.mark.parametrize("body, message", [
    ("Action,Date,Trend,Symbol,Low-High,Bogus\nupdate,2024-03-04,Investor,HYG,+78P-75P,1",
     "not a column of the table: Bogus"),
    ("Action,Date,Trend,Symbol\nupdate,2024-03-04,Investor,HYG", "missing column(s): Low-High"),
    ("Action,Date,Trend,Symbol,Low-High,Entry\nupdate,2024-03-04,Investor,HYG,+78P-75P,abc",
     "Entry 'abc'"),
    ("Action,Date,Trend,Symbol,Low-High,Entry\nupdate,2024-03-04,Swing,HYG,+78P-75P,1",
     "Trend 'Swing': must be one of Investor, Trader"),
    ("Action,Date,Trend,Symbol,Low-High,Entry_Sign\nupdate,2024-03-04,Investor,HYG,+78P-75P,=",
     "Entry_Sign '=': must be one of <, >"),
    ("Action,Date,Trend,Symbol,Low-High,L_Strike\nupdate,2024-03-04,Investor,HYG,+78P-75P,79",
     "L_Strike is part of the key Low-High"),
    ("Action,Date,Trend,Symbol,Low-High,Entry\nupdate,2024-03-04,Investor,HYG,,1",
     "NULL in Date, Trend, Symbol, Low-High"),
    ("Action,Date,Trend,Symbol,Low-High,Entry\nupdate,2024-03-04,Investor,HYG,+78P-75P,1\n"
     "delete,2024-03-04,Investor,HYG,+78P-75P,", "duplicate primary key"),
    ("Action,Date,Trend,Symbol,Low-High,Entry\nupsert,2024-03-04,Investor,HYG,+78P-75P,1",
     "Action 'upsert' is not one of"),
    ("Action,Date,Trend,Symbol,Low-High,Entry\nupdate,2024-03-04,Investor,HYG,+78P-75P,",
     "update with nothing to change"),
])
def test_parse_rejects(tmp_path, body, message):
    with pytest.raises(P.PatchError) as exc:
        P.parse_patch(_csv(tmp_path, body), ETF)
    assert any(message in err for err in exc.value.errors), exc.value.errors


def test_parse_skips_rows_without_action(tmp_path):
    path = _csv(tmp_path, """
Action,Date,Symbol,Stop
,2024-03-04,AAPL,160
update,2024-03-04,MSFT,390
  ,2024-03-04,MSFT,391
""")
    (e,) = P.parse_patch(path, STOCK)   # the blank-Action rows are not even a duplicate key
    assert (e.key["Symbol"], e.values["Stop"]) == ("MSFT", 390.0)


def test_parse_date_filter(tmp_path):
    path = _csv(tmp_path, """
Action,Date,Symbol,Stop
update,2024-03-04,AAPL,160
update,2024-03-11,AAPL,161
""")
    assert [e.values["Stop"] for e in P.parse_patch(path, STOCK, "2024-03-11")] == [161.0]


# --------------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------------
def test_insert_derives_blank_signs_and_keeps_typed_ones(tmp_path):
    path = _csv(tmp_path, """
Action,Date,Trend,Symbol,Low-High,PnC,L_Strike,H_Strike,Entry,Stop_Sign
insert,2024-03-04,Trader,TLT,,P,95,90,96.5,>
""")
    (change,) = P.plan(P.parse_patch(path, ETF), {}, ETF)
    row = change.after
    assert (row["Low-High"], row["Entry_Sign"], row["Target_Sign"], row["Stop_Sign"]) == (
        "+95P-90P", "<", ">", ">")   # Stop_Sign was typed, so it is kept
    assert row["quantity"] == 1


def test_update_or_insert_conflict_against_table(db, tmp_path):
    _seed(db)
    path = _csv(tmp_path, """
Action,Date,Symbol,Stop
update,2024-03-04,NVDA,100
insert,2024-03-04,AAPL,1
""")
    entries = P.parse_patch(path, STOCK)
    with pytest.raises(P.PatchError) as exc:
        P.plan(entries, P.load_existing("Trading", "Stock_Options_v1", STOCK,
                                        [date(2024, 3, 4)]), STOCK, "fix.csv")
    assert any("update of 2024-03-04 NVDA: no such row" in e for e in exc.value.errors)
    assert any("insert of 2024-03-04 AAPL: a different row is already there" in e
               for e in exc.value.errors)


# --------------------------------------------------------------------------------------------
# End to end through main()
# --------------------------------------------------------------------------------------------
def test_dry_run_writes_nothing(db, tmp_path):
    _seed(db)
    path = _csv(tmp_path, "Action,Date,Symbol,Stop\nupdate,2024-03-04,AAPL,160")
    before = _rows(db, "Stock_Options_v1")
    assert _run(tmp_path, path, "--table", "stock") == 0
    assert _rows(db, "Stock_Options_v1") == before
    assert not P.audit_path(path).exists()


def test_apply_changes_only_targeted_cells(db, tmp_path):
    _seed(db)
    path = _csv(tmp_path, """
Action,Date,Trend,Symbol,Low-High,Entry,Target,PnC,L_Strike,H_Strike,Expiration,Type
update,2024-03-04,Investor,HYG,+78P-75P,78.95,NULL,,,,,
insert,2024-03-04,Trader,TLT,,96.5,99,P,95,90,8/15/25,Bond
delete,2024-03-04,Trader,XOP,+130C-140C,,,,,,,
""")
    before = {r["Symbol"]: r for r in _rows(db, "ETF_Options_v1")}
    assert _run(tmp_path, path, "--table", "etf", "--apply") == 0
    after = {r["Symbol"]: r for r in _rows(db, "ETF_Options_v1")}

    assert set(after) == {"HYG", "TLT"}
    hyg_changed = {c for c in after["HYG"] if after["HYG"][c] != before["HYG"][c]}
    assert hyg_changed == {"Entry", "Target"}
    assert (after["HYG"]["Entry"], after["HYG"]["Target"]) == (78.95, None)
    tlt = after["TLT"]
    assert (tlt["Low-High"], tlt["Entry_Sign"], tlt["Target_Sign"], tlt["quantity"]) == (
        "+95P-90P", "<", ">", 1)
    assert str(tlt["Expiration"]) == "2025-08-15"


def test_apply_is_idempotent(db, tmp_path, capsys):
    _seed(db)
    path = _csv(tmp_path, """
Action,Date,Symbol,Stop,PnC,Strike,Entry1
update,2024-03-04,AAPL,160,,,
insert,2024-03-04,NVDA,800,P,850,860
delete,2024-03-04,MSFT,,,,
""")
    assert _run(tmp_path, path, "--table", "stock", "--apply") == 0
    first = _rows(db, "Stock_Options_v1")
    capsys.readouterr()
    assert _run(tmp_path, path, "--table", "stock", "--apply") == 0
    assert _rows(db, "Stock_Options_v1") == first
    assert "0 change(s), 3 already in place" in capsys.readouterr().out
    assert len(json.loads(P.audit_path(path).read_text())["runs"]) == 1


def test_delete_then_insert_same_key_replaces_row(db, tmp_path, capsys):
    """A non-key fix (Expiration) as delete + insert of the same key, like ETF_2026-10-19.csv."""
    _seed(db)
    path = _csv(tmp_path, """
Action,Date,Type,Trend,Symbol,Status,Expiration,PnC,Low-High,L_Strike,H_Strike,Entry_Sign,Entry,Target_Sign,Target,Stop_Sign,Stop,quantity
delete,2024-03-04,Bond,Investor,HYG,Bullish,2024-04-19,P,'+78P-75P,78,75,<,78.5,>,82.06,<,,1
insert,2024-03-04,Bond,Investor,HYG,Bullish,2025-04-19,P,'+78P-75P,78,75,<,78.5,>,82.06,<,,1
""")
    assert _run(tmp_path, path, "--table", "etf", "--apply") == 0
    assert "Expiration: 2024-04-19 -> 2025-04-19" in capsys.readouterr().out
    (hyg,) = _rows(db, "ETF_Options_v1", "WHERE Symbol = 'HYG'")
    assert str(hyg["Expiration"]) == "2025-04-19"
    first = _rows(db, "ETF_Options_v1")
    assert _run(tmp_path, path, "--table", "etf", "--apply") == 0   # re-apply: a no-op
    assert _rows(db, "ETF_Options_v1") == first
    assert "0 change(s), 2 already in place" in capsys.readouterr().out


def test_parse_rejects_two_inserts_of_one_key(tmp_path):
    path = _csv(tmp_path, "Action,Date,Symbol,Stop\ndelete,2024-03-04,AAPL,\n"
                          "insert,2024-03-04,AAPL,1\ninsert,2024-03-04,AAPL,2")
    with pytest.raises(P.PatchError) as exc:
        P.parse_patch(path, STOCK)
    assert exc.value.errors == ["fix.csv:4: duplicate primary key"]


def test_conflict_applies_nothing(db, tmp_path):
    _seed(db)
    path = _csv(tmp_path, """
Action,Date,Symbol,Stop
update,2024-03-04,AAPL,160
insert,2024-03-04,MSFT,1
""")
    before = _rows(db, "Stock_Options_v1")
    assert _run(tmp_path, path, "--table", "stock", "--apply") == 1
    assert _rows(db, "Stock_Options_v1") == before


def test_apply_patch_rolls_back_on_db_error(db):
    """A DB error part-way (duplicate key on insert) undoes the update sent before it."""
    _seed(db)
    before = _rows(db, "Stock_Options_v1")
    key = {"Date": date(2024, 3, 4), "Symbol": "AAPL"}
    with pytest.raises(Exception):
        DU.ApplyPatch([(key, {"Stop": 1.0})], [{"Date": date(2024, 3, 4), "Symbol": "MSFT"}], [],
                      "Trading", "Stock_Options_v1")
    assert _rows(db, "Stock_Options_v1") == before


def test_audit_has_before_and_after(db, tmp_path):
    _seed(db)
    path = _csv(tmp_path, "Action,Date,Symbol,Stop\nupdate,2024-03-04,AAPL,160")
    assert _run(tmp_path, path, "--table", "stock", "--apply") == 0
    (run,) = json.loads(P.audit_path(path).read_text())["runs"]
    (change,) = run["changes"]
    assert (run["updated"], change["before"]["Stop"], change["after"]["Stop"]) == (1, 165.0, 160.0)
    assert change["key"] == {"Date": D, "Symbol": "AAPL"}


def test_folder_and_date_filter(db, tmp_path):
    _seed(db)
    folder = tmp_path / "corrections"
    folder.mkdir()
    _csv(folder, "Action,Date,Symbol,Stop\nupdate,2024-03-04,AAPL,160", "a.csv")
    _csv(folder, "Action,Date,Symbol,Stop\nupdate,2024-03-11,AAPL,161", "b.csv")
    assert _run(tmp_path, folder, "--table", "stock", "--date", D, "--apply") == 0
    (aapl,) = _rows(db, "Stock_Options_v1", "WHERE Symbol = 'AAPL'")
    assert aapl["Stop"] == 160.0


@pytest.mark.parametrize("loader, table", [("playbook_etf", "etf"), ("top20_stock", "stock")])
def test_replace_date_warns_to_reapply_patches(monkeypatch, caplog, loader, table):
    """A --replace-date reload drops hand corrections, so the loaders say how to re-apply them."""
    mod = __import__(loader)
    monkeypatch.setattr(DU, "ReplaceDate", lambda *a, **k: (2, 2, []))
    with caplog.at_level("WARNING"):
        assert mod.store(pd.DataFrame(), "Trading", "T", D, replace=True) == 0
    assert f"db_patch.py <corrections> --table {table} --date {D} --apply" in caplog.text
