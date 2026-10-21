"""Shared fixtures: the repo root on sys.path, and an in-memory stand-in for the Trading DB."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dataUtil as DU  # noqa: E402

# The live MySQL 8.4 tables (SHOW CREATE TABLE), in SQLite spelling.
DDL = [
    """CREATE TABLE Trading.ETF_Options_v1 (
        Date DATE NOT NULL, Type VARCHAR(45), Trend VARCHAR(15) NOT NULL,
        Symbol VARCHAR(20) NOT NULL, Status VARCHAR(45), Expiration DATE, PnC VARCHAR(1),
        "Low-High" VARCHAR(45) NOT NULL, L_Strike FLOAT, H_Strike FLOAT, Entry_Sign VARCHAR(5),
        Entry FLOAT, Target_Sign VARCHAR(5), Target FLOAT, Stop_Sign VARCHAR(5), Stop FLOAT,
        quantity INT NOT NULL DEFAULT 1,
        PRIMARY KEY (Date, Trend, Symbol, "Low-High"))""",
    """CREATE TABLE Trading.Stock_Options_v1 (
        Date DATE NOT NULL, Symbol VARCHAR(20) NOT NULL, Status VARCHAR(45), Expiration DATE,
        PnC VARCHAR(1), Strike FLOAT, Entry_Sign VARCHAR(1), Entry1 FLOAT, Entry2 FLOAT,
        Target_Sign VARCHAR(1), Target FLOAT, Stop_Sign VARCHAR(1), Stop FLOAT,
        Trade_Status VARCHAR(10), Description VARCHAR(45), quantity INT NOT NULL DEFAULT 1,
        PRIMARY KEY (Date, Symbol))""",
]


@pytest.fixture
def db(monkeypatch):
    """An in-memory SQLite engine with both tables under schema ``Trading``; DU uses it."""
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _attach(dbapi_conn, _):
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS Trading")

    with engine.begin() as conn:
        for ddl in DDL:
            conn.execute(text(ddl))
    monkeypatch.setattr(DU, "get_DBengine", lambda: engine)
    monkeypatch.setenv("DBTRADING", "Trading")
    monkeypatch.setenv("TBLETFOPTIONS", "ETF_Options_v1")
    monkeypatch.setenv("TBLSTOCKOPTIONS", "Stock_Options_v1")
    yield engine
    engine.dispose()
