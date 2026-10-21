#!/usr/bin/env python
"""Correct rows of Trading.ETF_Options_v1 / Stock_Options_v1 by hand, or add rows a date is missing.

The loaders can only rebuild a whole date from its PDF (``--replace-date``). This is for what the
PDF cannot give: a value you know is wrong, a row the parser dropped. A patch is a CSV with an
``Action`` column (``update``/``insert``/``delete``), the table's primary-key columns, and any
other columns of the table:

    Action,Date,Trend,Symbol,Low-High,Entry,Target
    update,2024-03-04,Investor,HYG,+78P-75P,78.95,82.06
    insert,2024-03-04,Trader,TLT,,...            (ETF: a blank Low-High is built from the strikes)
    delete,2024-03-04,Trader,XOP,+130C-140C

``update`` changes only the cells that are filled in (``NULL`` clears one); ``insert`` adds a full
row, with blank signs taken from ``PnC`` and a blank ``quantity`` as 1; a key cannot be updated, so
a wrong key is a ``delete`` of the old row plus an ``insert`` of the new one (a ``delete`` and an
``insert`` of the same key replace that row whole). A row with a blank
``Action`` is skipped.

    python db_patch.py corrections/fix.csv --table stock            # dry run: show the diff
    python db_patch.py corrections/fix.csv --table stock --apply    # write it, all or nothing
    python db_patch.py corrections/ --table etf --date 2024-03-04 --apply

Re-applying a patch is safe — a cell that already has its value, a row that is already there and a
delete of a row that is gone are all no-ops — which is what restores the corrections after a
``--replace-date`` reload of that date. Every apply is logged with the rows' before and after
images to ``<patch>.applied.json`` next to the CSV.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import bindparam, text

import dataUtil as DU
import hil_review as hil
import playbook_etf
import top20_stock

NULL = "NULL"
ACTIONS = ("update", "insert", "delete")
DATE_COLUMNS = ("Date", "Expiration")
SIGN_COLUMNS = ("Entry_Sign", "Target_Sign", "Stop_Sign")
SIGN_VALUES = ("<", ">")
PNC_VALUES = ("P", "C")

log = logging.getLogger("db_patch")


@dataclass(frozen=True)
class Table:
    """What db_patch needs to know about one table, taken from the loader that fills it."""
    name: str                  # the --table value
    loader: object             # playbook_etf / top20_stock
    env_var: str
    default: str
    extra_columns: tuple = ()  # columns of the table the parser never writes
    key_parts: tuple = ()      # columns a key column is built from (cannot be updated)

    @property
    def columns(self) -> list:
        return self.loader.OUTPUT_COLUMNS + [c for c in self.extra_columns
                                             if c not in self.loader.OUTPUT_COLUMNS]

    @property
    def key_columns(self) -> list:
        return self.loader.KEY_COLUMNS

    @property
    def types(self) -> dict:
        return {"quantity": "int", **self.loader.DB_TYPES}


TABLES = {
    "etf": Table("etf", playbook_etf, "TBLETFOPTIONS", "ETF_Options_v1",
                 key_parts=("L_Strike", "H_Strike", "PnC")),
    "stock": Table("stock", top20_stock, "TBLSTOCKOPTIONS", "Stock_Options_v1",
                   extra_columns=("Trade_Status", "Description", "quantity")),
}


class PatchError(Exception):
    """A patch file that must not be applied; ``errors`` says why, one line each."""

    def __init__(self, errors: list[str]):
        super().__init__(f"{len(errors)} error(s)")
        self.errors = errors


@dataclass
class Entry:
    """One CSV row: its action, its typed key and the typed cells that were filled in."""
    action: str
    line: int
    key: dict
    values: dict = field(default_factory=dict)


@dataclass
class Change:
    """What one entry does to the table: ``status`` is ``apply``, ``noop`` or ``skip``."""
    entry: Entry
    status: str
    before: dict | None
    after: dict | None
    changed: list = field(default_factory=list)   # update: the columns that actually change


# --------------------------------------------------------------------------------------------
# Typing and comparing cells
# --------------------------------------------------------------------------------------------
def typed(table: Table, col: str, raw: str):
    """A hand-typed cell -> the value the table stores (``NULL`` -> None); ValueError if bad."""
    raw = raw.strip()
    if raw.startswith("'"):   # Excel's text marker, saved when "+78P-75P" is kept as text
        raw = raw[1:]
    if raw == NULL:
        return None
    if col in DATE_COLUMNS:
        if "/" in raw:   # the PDF's M/D/YY form
            return table.loader._parse_expiration(raw).date()
        return datetime.strptime(raw, "%Y-%m-%d").date()
    kind = table.types.get(col)
    if kind == "float":
        return float(raw)
    if kind == "int":
        return int(raw)
    if col in SIGN_COLUMNS and raw not in SIGN_VALUES:
        raise ValueError(f"must be one of {', '.join(SIGN_VALUES)}")
    if col == "PnC" and raw not in PNC_VALUES:
        raise ValueError(f"must be one of {', '.join(PNC_VALUES)}")
    if col == "Trend" and raw not in playbook_etf.TREND_VALUES:
        raise ValueError(f"must be one of {', '.join(playbook_etf.TREND_VALUES)}")
    return raw


def norm(table: Table, col: str, value):
    """A cell as read from the DB or typed by hand, in one form so the two compare equal."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NaT:
        return None
    if col in DATE_COLUMNS:
        return pd.Timestamp(value).date()
    kind = table.types.get(col)
    if kind == "float":
        return round(float(value), 6)   # FLOAT columns: compare what was typed, not the binary
    if kind == "int":
        return int(value)
    return str(value)


def key_tuple(table: Table, key: dict) -> tuple:
    return tuple(norm(table, c, key.get(c)) for c in table.key_columns)


def fmt_key(key: dict) -> str:
    return " ".join("" if v is None else str(v) for v in key.values())


def _low_high(raw: dict) -> str:
    """``+<L_Strike><PnC>-<H_Strike><PnC>``, exactly as ``playbook_etf.derive_columns`` builds it."""
    pnc = raw.get("PnC", "").strip()
    low = "+" + raw["L_Strike"].strip() + pnc if raw.get("L_Strike", "").strip() else ""
    high = "-" + raw["H_Strike"].strip() + pnc if raw.get("H_Strike", "").strip() else ""
    return low + high


# --------------------------------------------------------------------------------------------
# Reading a patch file
# --------------------------------------------------------------------------------------------
def parse_patch(path: Path, table: Table, only_date: str | None = None) -> list[Entry]:
    """Read and type every row of a patch CSV; raise ``PatchError`` listing everything wrong."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    errors = []
    unknown = [c for c in df.columns if c != "Action" and c not in table.columns]
    if unknown:
        errors.append(f"{path.name}: not a column of the table: {', '.join(unknown)}")
    missing = [c for c in ["Action"] + table.key_columns if c not in df.columns]
    if missing:
        errors.append(f"{path.name}: missing column(s): {', '.join(missing)}")
    if errors:
        raise PatchError(errors)
    wanted = typed(table, "Date", only_date) if only_date else None

    entries = []
    for i, raw in df.iterrows():
        line = i + 2   # 1-based, after the header
        where = f"{path.name}:{line}"
        action = raw["Action"].strip().lower()
        if not action:   # a row left without an Action is a note or a draft: not part of the patch
            log.info("%s: no Action, skipped", where)
            continue
        if action not in ACTIONS:
            errors.append(f"{where}: Action {raw['Action']!r} is not one of {', '.join(ACTIONS)}")
            continue
        raw = {c: v for c, v in raw.items() if c != "Action"}
        if table.key_parts and action == "insert" and not raw.get("Low-High", "").strip():
            raw["Low-High"] = _low_high(raw)
        entry = Entry(action, line, {})
        bad = False
        for col in table.key_columns:
            cell = raw.get(col, "").strip()
            if cell in ("", NULL):
                entry.key[col] = None   # reported below by split_loadable
                continue
            try:
                entry.key[col] = typed(table, col, cell)
            except ValueError as e:
                errors.append(f"{where}: {col} {cell!r}: {e}")
                bad = True
        for col, cell in raw.items():
            if col in table.key_columns or cell.strip() == "":
                continue
            if action == "delete":
                continue   # only the key matters
            if action == "update" and col in table.key_parts:
                errors.append(f"{where}: {col} is part of the key Low-High and cannot be updated; "
                              "delete the row and insert the corrected one")
                bad = True
                continue
            try:
                entry.values[col] = typed(table, col, cell)
            except ValueError as e:
                errors.append(f"{where}: {col} {cell!r}: {e}")
                bad = True
        if action == "update" and not entry.values and not bad:
            errors.append(f"{where}: update with nothing to change")
            bad = True
        if bad:
            continue
        if wanted is not None and entry.key.get("Date") != wanted:
            continue
        entries.append(entry)

    # The same checks the loaders run before a load: no NULL key column, no key twice. A delete
    # plus an insert of one key is a row replaced (ApplyPatch deletes first), so the delete rows
    # are checked on their own; an update of a key the patch also deletes or inserts is not.
    deletes = [e for e in entries if e.action == "delete"]
    others = [e for e in entries if e.action != "delete"]
    for group in (deletes, others):
        keys = pd.DataFrame([e.key for e in group], columns=table.key_columns,
                            index=[e.line for e in group])
        _, rejected = table.loader.split_loadable(keys)
        for line, reason in rejected["_reason"].items():
            errors.append(f"{path.name}:{line}: {reason}")
    updated = {key_tuple(table, e.key) for e in entries if e.action == "update"}
    for e in deletes:
        if key_tuple(table, e.key) in updated:
            errors.append(f"{path.name}:{e.line}: duplicate primary key")
    if errors:
        raise PatchError(errors)
    return entries


# --------------------------------------------------------------------------------------------
# Comparing with the table
# --------------------------------------------------------------------------------------------
def load_existing(db: str, tbl: str, table: Table, dates: list[date]) -> dict[tuple, dict]:
    """The rows of the patch's dates, by key tuple, with every cell normalized."""
    if not dates:
        return {}
    query = text(f"SELECT * FROM {db}.{tbl} WHERE Date IN :dates").bindparams(
        bindparam("dates", expanding=True))
    with DU.get_DBengine().connect() as conn:
        result = conn.execute(query, {"dates": sorted(set(dates))})
        rows = [dict(r) for r in result.mappings()]
    out = {}
    for r in rows:
        row = {c: norm(table, c, v) for c, v in r.items()}
        out[key_tuple(table, row)] = row
    return out


def plan(entries: list[Entry], existing: dict[tuple, dict], table: Table,
         source: str = "") -> list[Change]:
    """Turn entries into changes against the current rows; raise ``PatchError`` on a conflict."""
    errors = []
    changes = []
    inserts = {key_tuple(table, e.key): insert_row(e, table)
               for e in entries if e.action == "insert"}
    replaced = {key_tuple(table, e.key) for e in entries if e.action == "delete"} & set(inserts)
    for e in entries:
        where = f"{source}:{e.line}"
        k = key_tuple(table, e.key)
        before = existing.get(k)
        if k in replaced:   # delete + insert of one key: replace the row, a no-op once it matches
            same = before is not None and all(before.get(c) == v for c, v in inserts[k].items()
                                              if c in before)
            if e.action == "delete":
                changes.append(Change(e, "skip" if before is None else
                                      "noop" if same else "apply", before, None))
            else:
                changes.append(Change(e, "skip" if same else "apply", before, inserts[k]))
            continue
        if e.action == "update":
            if before is None:
                errors.append(f"{where}: update of {fmt_key(e.key)}: no such row")
                continue
            changed = [c for c, v in e.values.items() if norm(table, c, v) != before.get(c)]
            after = {**before, **{c: norm(table, c, e.values[c]) for c in changed}}
            changes.append(Change(e, "apply" if changed else "noop", before, after, changed))
        elif e.action == "delete":
            changes.append(Change(e, "skip" if before is None else "apply", before, None))
        else:
            row = inserts[k]
            if before is None:
                changes.append(Change(e, "apply", None, row))
                continue
            differ = [c for c in row if c in before and row[c] != before[c]]
            if differ:
                errors.append(f"{where}: insert of {fmt_key(e.key)}: a different row is already "
                              f"there ({', '.join(differ)}); use update, or delete + insert")
                continue
            changes.append(Change(e, "skip", before, row))
    if errors:
        raise PatchError(errors)
    return changes


def insert_row(e: Entry, table: Table) -> dict:
    """The full, normalized row an ``insert`` entry writes."""
    row = {c: None for c in table.columns}
    row.update(e.key)
    row.update(e.values)
    row = {c: norm(table, c, v) for c, v in row.items()}
    fill_insert_defaults(row)
    return row


def fill_insert_defaults(row: dict) -> None:
    """Blank signs from ``PnC`` (the loaders' rule) and a blank ``quantity`` as the table's 1."""
    pnc = row.get("PnC")
    if pnc in PNC_VALUES:
        entry = "<" if pnc == "P" else ">"
        defaults = {"Entry_Sign": entry, "Target_Sign": ">" if pnc == "P" else "<",
                    "Stop_Sign": entry}
        for col, sign in defaults.items():
            if col in row and row[col] is None:
                row[col] = sign
    if "quantity" in row and row["quantity"] is None:
        row["quantity"] = 1


def describe(change: Change) -> str:
    e = change.entry
    head = f"  {e.action:<6} {fmt_key(e.key)}"
    if change.status == "noop":
        return f"{head}  (already has these values)"
    if change.status == "skip":
        return f"{head}  ({'already there' if e.action == 'insert' else 'no such row'})"
    if e.action == "update":
        return head + "  " + ", ".join(f"{c}: {change.before.get(c)} -> {change.after[c]}"
                                       for c in change.changed)
    if e.action == "insert" and change.before is not None:   # the delete + insert of one key
        return head + "  (replaces row) " + ", ".join(
            f"{c}: {change.before.get(c)} -> {v}" for c, v in change.after.items()
            if c in change.before and change.before[c] != v)
    if e.action == "insert":
        filled = {c: v for c, v in change.after.items()
                  if v is not None and c not in e.key}
        return head + "  (new row) " + ", ".join(f"{c}={v}" for c, v in filled.items())
    return head + "  (row removed)"


# --------------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------------
def apply(changes: list[Change], db: str, tbl: str, table: Table) -> tuple[int, int, int]:
    """Send the ``apply`` changes to ``DU.ApplyPatch`` (one transaction, all or nothing)."""
    todo = [c for c in changes if c.status == "apply"]
    deletes = [c.entry.key for c in todo if c.entry.action == "delete"]
    updates = [(c.entry.key, {col: c.after[col] for col in c.changed})
               for c in todo if c.entry.action == "update"]
    inserts = [c.after for c in todo if c.entry.action == "insert"]
    return DU.ApplyPatch(updates, inserts, deletes, db, tbl)


def audit_path(patch: Path) -> Path:
    return patch.with_suffix(".applied.json")


def write_audit(patch: Path, changes: list[Change], table: Table, db: str, tbl: str,
                counts: tuple[int, int, int]) -> Path:
    """Append this run to ``<patch>.applied.json``: who, when, and each row before and after."""
    path = audit_path(patch)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {"patch": patch.name, "runs": []}
    data["runs"].append({
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "user": hil._whoami(),
        "table": f"{db}.{tbl}",
        "deleted": counts[0], "updated": counts[1], "inserted": counts[2],
        "changes": [{"line": c.entry.line, "action": c.entry.action,
                     "key": c.entry.key, "before": c.before, "after": c.after}
                    for c in changes if c.status == "apply"],
    })
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    return path


# --------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------
def patch_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.glob("*.csv"))
    return [target]


def process_file(path: Path, table: Table, db: str, tbl: str, args: argparse.Namespace) -> bool:
    """Dry-run or apply one patch file; False if it has errors (and so was not applied)."""
    try:
        entries = parse_patch(path, table, args.date)
        existing = load_existing(db, tbl, table, [e.key["Date"] for e in entries])
        changes = plan(entries, existing, table, path.name)
    except PatchError as e:
        log.error("%s: not applied, %d error(s):", path.name, len(e.errors))
        for err in e.errors:
            log.error("  %s", err)
        return False
    todo = [c for c in changes if c.status == "apply"]
    print(f"{path.name}: {len(todo)} change(s), {len(changes) - len(todo)} already in place "
          f"-> {db}.{tbl}")
    for c in changes:
        print(describe(c))
    if not args.apply:
        return True
    if not todo:
        log.info("%s: nothing to apply", path.name)
        return True
    counts = apply(changes, db, tbl, table)
    log.info("%s: deleted %d, updated %d, inserted %d", path.name, *counts)
    log.info("%s: audit written to %s", path.name,
             write_audit(path, changes, table, db, tbl, counts))
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("target", type=Path, help="a patch CSV, or a folder of them (*.csv)")
    p.add_argument("--table", choices=sorted(TABLES), required=True,
                   help="etf = ETF_Options_v1 (TBLETFOPTIONS), stock = Stock_Options_v1 "
                        "(TBLSTOCKOPTIONS)")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="only the patch rows of this date")
    p.add_argument("--apply", action="store_true",
                   help="write the changes (default: dry run — show the diff only)")
    p.add_argument("--env-file", default=".env", help="dotenv file (default: .env)")
    p.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ... (default: INFO)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not load_dotenv(args.env_file):
        log.warning("env file %s not found; relying on the process environment.", args.env_file)
    table = TABLES[args.table]
    db = os.environ.get("DBTRADING", "Trading")
    tbl = os.environ.get(table.env_var, table.default)

    if not (args.target.is_dir() or args.target.is_file()):
        log.error("%s is not a file or directory", args.target)
        return 2
    files = patch_files(args.target)
    if not files:
        log.warning("No patch CSVs in %s", args.target)
        return 0
    ok = [process_file(f, table, db, tbl, args) for f in files]
    if not args.apply:
        log.info("Dry run: nothing written (add --apply to write).")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
