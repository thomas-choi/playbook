#!/usr/bin/env python
"""Human-in-the-loop review of the blocks neither the regexes nor the LLM could settle.

A flagged block that the LLM did not repair (rejected by validation, no endpoint, or a layout the
model got wrong) is a question for a person: *what does this text actually say?* ``--review`` asks
it in the terminal, one block at a time, and records the answer in a decisions store keyed by a
**fingerprint of the block text** — so the same question is never asked twice, and a re-run (or a
``--replace-date`` reload months later) produces the same rows without a human present.

The store lives next to the PDFs (``.pdfreader-decisions.json``) unless ``--decisions`` says
otherwise. Because the fingerprint covers the block's text verbatim, an answer stops applying the
moment the publisher changes that block — a new issue with different numbers is a new question.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import llm_repair as lr

log = logging.getLogger("hil_review")

STORE_NAME = ".pdfreader-decisions.json"
ACTIONS = ("accept_llm", "keep_regex", "set_fields", "no_value", "drop_row", "unresolvable")


# --------------------------------------------------------------------------------------------
# Fingerprint + store
# --------------------------------------------------------------------------------------------
def fingerprint(publication: str, block_text: str) -> str:
    """Stable id for *this exact block text* (whitespace-normalised) in this publication."""
    norm = "\n".join(" ".join(l.split()) for l in block_text.splitlines() if l.strip())
    return hashlib.sha1(f"{publication}\x00{norm}".encode()).hexdigest()[:16]


def store_path(pdf_path: Path, override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("PDFREADER_DECISIONS")
    return Path(env) if env else pdf_path.parent / STORE_NAME


class DecisionStore:
    """``{fingerprint: {action, rows, ...}}`` on disk; one file per PDF folder."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"version": 1, "decisions": {}}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded.get("decisions"), dict):
                    self.data = loaded
                else:
                    log.warning("%s has no 'decisions' object; starting a new store.", path)
            except Exception as e:  # noqa: BLE001 - a corrupt store must not stop a load
                log.warning("could not read %s (%s); starting a new store.", path, lr._one_line(e))
        self._dirty = False

    @property
    def decisions(self) -> dict:
        return self.data["decisions"]

    def get(self, fp: str) -> dict | None:
        return self.decisions.get(fp)

    def put(self, fp: str, action: str, rows: list[dict], *, block: lr.Block, source: str,
            date: str, note: str = "") -> None:
        self.decisions[fp] = {
            "action": action, "rows": rows, "block": block.block_id, "symbol": block.symbol,
            "trend": block.trend, "date": date, "source": source, "note": note,
            "flags": [f.as_dict() for f in block.flags],
            "decided_at": datetime.now().isoformat(timespec="seconds"),
            "decided_by": _whoami(),
        }
        self._dirty = True

    def save(self) -> bool:
        if not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)
        self._dirty = False
        log.info("decisions saved -> %s (%d recorded)", self.path, len(self.decisions))
        return True


_STORES: dict[Path, DecisionStore] = {}


def get_store(path: Path) -> DecisionStore:
    """One store per file, shared across the PDFs of a folder run."""
    if path not in _STORES:
        _STORES[path] = DecisionStore(path)
    return _STORES[path]


def save_all() -> None:
    for st in _STORES.values():
        st.save()


def _whoami() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


# --------------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------------
def apply_decisions(rows: list[dict], lines: list[str], report: lr.ParseReport,
                    store: DecisionStore, publication: str) -> int:
    """Apply recorded answers to the blocks they were made about. Returns how many applied."""
    applied = 0
    for b in list(report.flagged_blocks()):
        d = store.get(fingerprint(publication, b.text(lines)))
        if d is None:
            continue
        report.replace_rows(rows, b, [dict(r) for r in d["rows"]])
        b.decision = d["action"]
        applied += 1
        log.info("%s: applying recorded decision %s (%s)", b.block_id, d["action"],
                 d.get("decided_at", "?"))
    return applied


# --------------------------------------------------------------------------------------------
# Interactive review
# --------------------------------------------------------------------------------------------
BANNER = "─" * 78


def pending(report: lr.ParseReport) -> list[lr.Block]:
    """Flagged blocks still unsettled: no recorded decision, no accepted LLM repair."""
    return report.unsettled_blocks()


def review(rows: list[dict], lines: list[str], report: lr.ParseReport, store: DecisionStore,
           repairs: lr.RepairLog | None, *, publication: str, source: str, date: str,
           columns: list[str], numeric_fields: tuple[str, ...], row_schema: dict,
           new_row) -> int:
    """Ask about every unsettled block. Returns how many were decided this run.

    Reads from stdin; at EOF (cron, ``</dev/null``) it leaves the items pending and says so, so an
    unattended run can never block.
    """
    items = pending(report)
    if not items:
        return 0
    proposals = {e["block"]: e for e in (repairs.entries if repairs else [])}
    print(f"\n{BANNER}\n{source}: {len(items)} block(s) need a human decision\n{BANNER}")
    decided = 0
    for n, b in enumerate(items, start=1):
        block_text = b.text(lines)
        before = [dict(r) for r in rows[b.row_start:b.row_end]]
        _show(n, len(items), b, block_text, before, proposals.get(b.block_id))
        answer = _ask(block_text, before, proposals.get(b.block_id), columns,
                      numeric_fields, row_schema, new_row)
        if answer is None:            # skip this one
            continue
        if answer == "quit":
            print("  leaving the remaining block(s) pending.")
            break
        action, new_rows, note = answer
        report.replace_rows(rows, b, new_rows)
        b.decision = action
        store.put(fingerprint(publication, block_text), action, new_rows, block=b, source=source,
                  date=date, note=note)
        decided += 1
        print(f"  recorded: {action}" + (f" — {note}" if note else ""))
    return decided


def _show(n: int, total: int, b: lr.Block, block_text: str, before: list[dict],
          proposal: dict | None) -> None:
    print(f"\n[{n}/{total}] {b.block_id}"
          + (f"  ({b.symbol}{'/' + b.trend if b.trend else ''})" if b.symbol else ""))
    for f in b.flags:
        print(f"  flag: {f.kind} — {f.detail}")
        print(f"        {f.line.strip()}")
    print("  --- block text " + "-" * 59)
    flagged_lines = {f.line.strip() for f in b.flags}
    for line in block_text.splitlines():
        mark = ">>" if line.strip() in flagged_lines else "  "
        print(f"  {mark} {line.strip()}")
    print("  " + "-" * 74)
    print("  rows the regexes produced:")
    for i, r in enumerate(before):
        print(f"    [{i}] {_fmt(r)}")
    if not before:
        print("    (none)")
    if proposal:
        verdict = proposal["verdict"] + (f" — {proposal['reason']}" if proposal["reason"] else "")
        print(f"  LLM proposal ({verdict}):")
        for i, r in enumerate(proposal.get("rows_after") or []):
            print(f"    [{i}] {_fmt(r)}")
        if not (proposal.get("rows_after") or []):
            print("    (none)")


def _fmt(row: dict) -> str:
    return "  ".join(f"{k}={v}" for k, v in row.items() if str(v).strip() not in ("", "None"))


def _ask(block_text: str, before: list[dict], proposal: dict | None, columns: list[str],
         numeric_fields: tuple[str, ...], row_schema: dict, new_row):
    """Return ``(action, rows, note)``, ``None`` to skip, or ``"quit"``."""
    has_proposal = bool(proposal and (proposal.get("rows_after") or []))
    while True:
        print("  what should these rows be?")
        print("    [1] accept the LLM proposal"
              + ("" if has_proposal else "   (none offered)"))
        print("    [2] keep the regex rows as they are (they are right)")
        print("    [3] set a field on a row")
        print("    [4] confirm the value is not in the PDF (keep it empty, stop flagging)")
        print("    [5] drop a row")
        print("    [6] add a row")
        print("    [u] unresolvable — record that and move on")
        print("    [s] skip for now      [q] stop reviewing")
        choice = _input("  > ")
        if choice is None:
            print("\n  no input available; leaving this and the remaining block(s) pending.")
            return "quit"
        choice = choice.strip().lower()
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("s", "skip", ""):
            return None
        if choice == "1":
            if not has_proposal:
                print("  there is no LLM proposal for this block.")
                continue
            return "accept_llm", [dict(r) for r in proposal["rows_after"]], "LLM proposal confirmed"
        if choice == "2":
            return "keep_regex", [dict(r) for r in before], "regex rows confirmed"
        if choice == "4":
            return "no_value", [dict(r) for r in before], "value is not printed in the PDF"
        if choice in ("u", "unresolvable"):
            note = _input("  note (what is wrong?): ") or ""
            return "unresolvable", [dict(r) for r in before], note.strip()
        if choice in ("3", "5", "6"):
            rows = [dict(r) for r in before]
            if choice == "3":
                if not _edit_field(rows, block_text, columns, numeric_fields):
                    continue
                action = "set_fields"
            elif choice == "5":
                if not rows:
                    print("  there is no row to drop.")
                    continue
                idx = _ask_index("  drop which row", len(rows))
                if idx is None:
                    continue
                dropped = rows.pop(idx)
                print(f"  dropped [{idx}] {_fmt(dropped)}")
                action = "drop_row"
            else:
                rows.append(new_row())
                for c in ("Type", "Symbol", "Trend", "Status"):
                    if c in rows[-1] and before and str(before[0].get(c, "")).strip():
                        rows[-1][c] = before[0][c]
                print(f"  added row [{len(rows) - 1}]; set its fields next.")
                while _input("  set another field on it? [y/N] ").strip().lower() == "y":
                    if not _edit_field(rows, block_text, columns, numeric_fields, len(rows) - 1):
                        break
                action = "add_row"
            ok, reason = lr.validate_rows(rows, row_schema, block_text, columns, numeric_fields)
            if not ok:
                print(f"  ! these rows do not validate: {reason}")
                if _input("  use them anyway? [y/N] ").strip().lower() != "y":
                    continue
                reason = f"forced past validation: {reason}"
            else:
                reason = ""
            print("  resulting rows:")
            for i, r in enumerate(rows):
                print(f"    [{i}] {_fmt(r)}")
            if _input("  record this? [Y/n] ").strip().lower() in ("", "y", "yes"):
                return action, rows, reason
        print("  (not a choice)")


def _edit_field(rows: list[dict], block_text: str, columns: list[str],
                numeric_fields: tuple[str, ...], row_idx: int | None = None) -> bool:
    if not rows:
        print("  there is no row to edit — add one first.")
        return False
    idx = row_idx if row_idx is not None else _ask_index("  edit which row", len(rows))
    if idx is None:
        return False
    print(f"  fields: {', '.join(columns)}")
    col = (_input("  field: ") or "").strip()
    if col not in columns:
        print(f"  ! {col!r} is not one of the columns.")
        return False
    value = (_input(f"  value for {col} (empty = clear): ") or "").strip()
    if value and col in numeric_fields and not lr._number_in_text(value, block_text):
        print(f"  ! {value} is not a number printed in this block.")
        if _input("  use it anyway? [y/N] ").strip().lower() != "y":
            return False
    if col == "quantity":
        try:
            rows[idx][col] = int(value or 1)
        except ValueError:
            print("  ! quantity must be a whole number.")
            return False
    else:
        rows[idx][col] = value
    return True


def _ask_index(prompt: str, count: int) -> int | None:
    raw = _input(f"{prompt} [0-{count - 1}]: ")
    try:
        idx = int((raw or "").strip())
    except ValueError:
        print("  ! not a row number.")
        return None
    if not 0 <= idx < count:
        print(f"  ! there is no row [{idx}].")
        return None
    return idx


def _input(prompt: str) -> str | None:
    """``input()`` that returns ``None`` at EOF instead of raising."""
    try:
        return input(prompt)
    except EOFError:
        return None
