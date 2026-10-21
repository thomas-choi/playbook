#!/usr/bin/env python
"""Human-in-the-loop review of the blocks neither the regexes nor the LLM could settle.

A flagged block that the LLM did not repair (rejected by validation, no endpoint, or a layout the
model got wrong) is a question for a person: *what does this text actually say?* ``--review`` asks
it in the terminal, one block at a time, and records the answer in a decisions store keyed by a
**fingerprint of the block text** — so the same question is never asked twice, and a re-run (or a
``--replace-date`` reload months later) produces the same rows without a human present.

One question is *not* opt-in. A row missing a column the table cannot take a NULL in (``Symbol``
and ``Trend`` in ``ETF_Options_v1``'s primary key, ``Symbol`` in ``Stock_Options_v1``'s) cannot load
at all, so the parsers *label* those columns where the regexes gave up and ``confirm_missing`` asks
for them on every interactive run — before the LLM, with the parser's candidate offered as the
Enter-default. ``record_confirmations`` writes the answers to the same store at the end of the run,
holding each block's rows as they finally stand, so a replay reproduces what was loaded.

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
ACTIONS = ("accept_llm", "keep_regex", "set_fields", "no_value", "drop_row", "add_row",
           "unresolvable")


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
        # The recorded rows *are* the answer to whatever the parser labelled as missing, so the
        # question is not asked again (confirm_missing runs after this).
        for f, m in b.missing.items():
            val = next((str(r.get(f) or "").strip() for r in d["rows"]
                        if str(r.get(f) or "").strip()), "")
            if val:
                m.answer, m.answered_by = val, d.get("decided_by", "a previous run")
        _retitle(b)   # so a replayed run's report reads WMT/Investor, like the run that asked
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


# --------------------------------------------------------------------------------------------
# Missing required columns: label -> confirm -> record
# --------------------------------------------------------------------------------------------
def _blank_rows(rows: list[dict], block: lr.Block, field: str) -> list[int]:
    """Indices of the block's rows with no value in ``field``."""
    return [i for i in range(block.row_start, min(block.row_end, len(rows)))
            if not str(rows[i].get(field) or "").strip()]


def label_missing_keys(rows: list[dict], lines: list[str], report: lr.ParseReport,
                       fields: list[str]) -> int:
    """Backstop after parsing: label any blank required column the parser did not label itself.

    The parser labels where it gives up, which is where the reason is known. This catches the rest
    — a leg parsed outside any group, a replayed or repaired row with a hole — and drops labels
    from a block that ended up with no rows at all, because there is then nothing to fill and
    nothing to ask.
    """
    labelled = 0
    for b in report.blocks:
        if b.row_end <= b.row_start:
            b.missing.clear()
            continue
        for f in fields:
            m = b.missing.get(f)
            if m is not None:
                # The parser labelled it where it gave up, before the block was complete; the
                # whole block's text may hold a candidate it could not see yet.
                if f == "Symbol" and not m.suggest:
                    cand = lr.ticker_candidate(b.text(lines))
                    if cand:
                        m.suggest, m.source = cand, f"a '@ {cand} price' in this block"
                continue
            blank = _blank_rows(rows, b, f)
            if blank:
                cand = lr.ticker_candidate(b.text(lines)) if f == "Symbol" else ""
                b.missing[f] = lr.Missing(
                    field=f, reason=f"{len(blank)} of this block's leg(s) have no {f}",
                    suggest=cand, source=f"a '@ {cand} price' in this block" if cand else "")
                labelled += 1
    return labelled


def reapply_answers(rows: list[dict], block: lr.Block) -> None:
    """Put a person's confirmed values back on a block's rows after an LLM repair replaced them.

    A repair returns the whole block, and the extraction spec tells the model to leave a column it
    cannot read empty — so without this an accepted repair would quietly drop (or contradict) the
    answer that was just given. Confirmed columns are group-wide, so they apply to every row.
    """
    answers = block.answers()
    if not answers:
        return
    for r in rows[block.row_start:block.row_end]:
        for f, v in answers.items():
            r[f] = v


def confirm_missing(rows: list[dict], lines: list[str], report: lr.ParseReport,
                    *, source: str, fields: tuple[str, ...],
                    choices: dict[str, tuple[str, ...]], columns: list[str],
                    numeric_fields: tuple[str, ...], row_schema: dict) -> int:
    """Ask a person for every labelled column nobody has answered. Returns the blocks answered.

    This runs before the LLM, not after: a column the regexes could not read is usually quicker
    for a person to confirm off the block text than it is to wait for a repair round-trip, and the
    answer then goes to the model as given. The answers are kept on the labels and on the rows;
    ``record_confirmations`` writes them to the decisions store at the end of the run, once repair
    and ``--review`` have had their say, so a replay reproduces the rows that were really loaded.

    Needs a terminal: with no TTY (cron, ``</dev/null``) it asks nothing and returns 0, leaving the
    labels for the caller to report.
    """
    items = report.missing_blocks()
    if not items or not sys.stdin.isatty():
        return 0
    cols = sorted({f for b in items for f in b.unanswered},
                  key=lambda f: fields.index(f) if f in fields else len(fields))
    print(f"\n{BANNER}\n{source}: {len(items)} block(s) are missing a required column "
          f"({', '.join(cols)})\n{BANNER}")
    answered = 0
    for n, b in enumerate(items, start=1):
        block_text = b.text(lines)
        _show_missing(n, len(items), b, block_text, rows)
        got = stop = False
        for f in fields:
            m = b.missing.get(f)
            if m is None or m.answer:
                continue
            outcome = _answer_field(rows, lines, report, b, block_text, f, m, choices.get(f, ()),
                                    columns, numeric_fields, row_schema)
            if outcome == "quit":
                stop = True
                break
            if outcome == "skip":
                break
            got = True
        if got:
            _retitle(b)
            answered += 1
        if stop:
            print("  leaving the remaining block(s) as they are.")
            break
    return answered


def record_confirmations(rows: list[dict], lines: list[str], report: lr.ParseReport,
                         store: DecisionStore, *, publication: str, source: str,
                         date: str) -> int:
    """Store one decision per confirmed block, holding its rows as they finally stand.

    Called after repair and ``--review``, so what is recorded is what the run actually loaded:
    replaying it on a later run (or a ``--replace-date`` reload) reproduces those rows exactly,
    with no LLM call and nobody present. Blocks ``--review`` already recorded are left alone —
    its record is the final one and holds the confirmed columns too.
    """
    stored = 0
    for b in report.blocks:
        answers = b.answers()
        if not answers or b.decision:
            continue
        note = ", ".join(f"{f}={v}" for f, v in answers.items()) + " confirmed by a person"
        store.put(fingerprint(publication, b.text(lines)), "set_fields",
                  [dict(r) for r in rows[b.row_start:b.row_end]], block=b, source=source,
                  date=date, note=note)
        b.decision = "set_fields"
        stored += 1
    return stored


def _show_missing(n: int, total: int, b: lr.Block, block_text: str, rows: list[dict]) -> None:
    print(f"\n[{n}/{total}] {b.block_id}"
          + (f"   rows {b.row_start}-{b.row_end - 1}" if b.row_end > b.row_start else ""))
    for f in b.flags:
        print(f"  flag: {f.kind} — {f.detail}")
    print("  --- block text " + "-" * 59)
    for line in block_text.splitlines():
        print(f"     {line.strip()}")
    print("  " + "-" * 74)
    for i in range(b.row_start, min(b.row_end, len(rows))):
        print(f"    [{i}] {_fmt(rows[i])}")


def _answer_field(rows: list[dict], lines: list[str], report: lr.ParseReport, b: lr.Block,
                  block_text: str, field_name: str, m: lr.Missing, choices: tuple[str, ...],
                  columns: list[str], numeric_fields: tuple[str, ...],
                  row_schema: dict) -> str:
    """Ask for one column until it is answered, skipped or the run is stopped.

    Returns ``"answered"``, ``"skip"`` (this block) or ``"quit"`` (all remaining blocks).
    """
    print(f"  {field_name} — {m.reason}")
    if m.suggest and m.source:
        print(f"    candidate: {m.suggest}   (from {m.source})")
    while True:
        value = _ask_choice(f"  {field_name} for these rows?", choices, field_name, m.suggest,
                            None if choices else FREE_TEXT_RE.get(field_name))
        if value is None:
            return "skip"
        if value == "quit":
            return "quit"
        new_rows = [dict(r) for r in rows[b.row_start:b.row_end]]
        for r in new_rows:
            if not str(r.get(field_name) or "").strip():
                r[field_name] = value
        # Validate a stringified copy: the parser legitimately leaves a net Entry as a float, and
        # the gates worth applying here are the semantic ones (symbol and numbers printed in the
        # block, PnC, expiration shape), not the row's Python types — ``finalize`` casts later.
        ok, reason = lr.validate_rows(_as_strings(new_rows), row_schema, block_text, columns,
                                      numeric_fields)
        if not ok:
            print(f"  ! {reason}")
            if (_input("  use it anyway? [y/N] ") or "").strip().lower() != "y":
                print()
                continue
            m.reason += f" [forced past validation: {reason}]"
        report.replace_rows(rows, b, new_rows)
        m.answer, m.answered_by = value, _whoami()
        print(f"  recorded: {field_name} = {value}")
        return "answered"


def _as_strings(rows: list[dict]) -> list[dict]:
    """A copy with every column but ``quantity`` as a string, the shape ``ROW_SCHEMA`` describes."""
    out = []
    for r in rows:
        out.append({k: (v if k == "quantity" else "" if v is None else str(v))
                    for k, v in r.items()})
    return out


def _retitle(b: lr.Block) -> None:
    """Carry the answers into the block's own identity, so the logs stop saying ``WMT/?``."""
    answers = b.answers()
    b.symbol = answers.get("Symbol", b.symbol)
    b.trend = answers.get("Trend", b.trend)
    head, sep, suffix = b.block_id.partition("#")
    parts = head.split("/")
    parts[0] = b.symbol or parts[0]
    if len(parts) > 1 and b.trend:
        parts[1] = b.trend
    b.block_id = "/".join(parts) + (sep + suffix if sep else "")


# A column with no fixed set of values: what a typed answer has to look like.
FREE_TEXT_RE = {"Symbol": re.compile(r"^[A-Z]{1,6}$")}


def _ask_choice(prompt: str, choices: tuple[str, ...], field: str, suggest: str = "",
                pattern: re.Pattern | None = None) -> str | None:
    """The chosen value, ``None`` to skip this block, or ``"quit"`` to stop asking.

    ``suggest`` is what the page hints at without stating it: it is marked in the list and taken by
    pressing Enter, which makes the question a confirmation rather than an open one. With no
    suggestion, Enter skips. ``pattern`` accepts a typed value where there is no fixed list.
    """
    while True:
        for i, c in enumerate(choices, start=1):
            print(f"    [{i}] {c}" + ("   <- the page suggests this" if c == suggest else ""))
        if pattern is not None:
            print(f"    [type a {field.lower()}]")
        print(("    [Enter] accept " + suggest + "      " if suggest else "    ")
              + "[s] skip for now      [q] stop asking")
        raw = _input(f"{prompt} > ")
        if raw is None:
            print(f"\n  no input available; leaving this and the remaining block(s) "
                  f"without a {field}.")
            return "quit"
        raw = raw.strip()
        if raw.lower() in ("q", "quit"):
            return "quit"
        if raw == "" and suggest:
            return suggest
        if raw.lower() in ("s", "skip", ""):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        match = [c for c in choices if c.lower() == raw.lower()]
        if match:
            return match[0]
        if pattern is not None:
            for candidate in (raw, raw.upper()):
                if pattern.fullmatch(candidate):
                    return candidate
            print(f"  ! {field} must look like {pattern.pattern}.")
            continue
        print(f"  ! {field} must be one of: {', '.join(choices)} "
              "(--review can set any other value).")
