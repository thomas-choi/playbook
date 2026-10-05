#!/usr/bin/env python
"""Batch-process LCR/912 Top20 PDFs: parse per-stock levels, load to MySQL, send commentary to llmwiki.

Script version of ``Top20_2_Code.ipynb`` (the Top20 counterpart of ``playbook_etf.py``). Given a
folder *or a single PDF*, every ``*Top20 YYYY-MM-DD.pdf`` is parsed with the same logic as the
notebook (one function per notebook stage — see the function docstrings for the originating cells),
the resulting rows are appended to ``Trading.Stock_Options_v1`` (dates already present in the table
are skipped unless ``--force``/``--replace-date``), and the market commentary on the printed page 1
(physical page 2: "Market Expectations ..." block) is posted to llmwiki's ``POST /ingest`` with the
date in the title and body.

The regexes below are the primary parser. Where they get confused — the PDFs do not always follow
their own layout — the stock's block is flagged in a ``llm_repair.ParseReport`` and, with
``--llm repair`` (the default), re-read by an LLM over the OpenAI-standard API; see
``docs/PDFreader.md``.

    python top20_stock.py /path/to/pdfs [--since 2025-01-01] [--csv-only] ...
    python top20_stock.py "/path/to/912 Top20 2025-07-21.pdf" --csv-only
    python top20_stock.py /path/to/pdfs --date 2022-12-19 --replace-date

Configuration comes from ``.env`` (``--env-file``); see ``.env.example`` for every key.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 name
except ImportError:  # older PyMuPDF (e.g. the Python 3.8 venv)
    import fitz

import dataUtil as DU
import hil_review as hil
import llm_repair as lr

# "912 Top20 2025-07-21.pdf" / "LCR Top20 2022-11-21.pdf" (Google-Drive naming) or "Top20-2025-07-21.pdf"
PDF_NAME_RE = re.compile(r"Top20[ -](\d{4}-\d{2}-\d{2})\.pdf$", re.IGNORECASE)
COMMENTARY_START_MARKER = "Market Expectations"
COMMENTARY_END_RE = re.compile(r"Vol\s+\d+\s+Issue\s+\d+")   # "<date>  912 Top 20  Vol N Issue N Page 1" footer

ROW_COLUMNS = ["Symbol", "Status", "Expiration", "PnC", "Price", "Entry1", "Entry2", "Target", "Stop"]
OUTPUT_COLUMNS = ["Date", "Symbol", "Status", "Expiration", "PnC", "Strike", "Entry_Sign", "Entry1",
                  "Entry2", "Target_Sign", "Target", "Stop_Sign", "Stop"]
# NOT NULL in Trading.Stock_Options_v1, and its primary key.
KEY_COLUMNS = ["Date", "Symbol"]
# The key column a person can supply (Date comes from the filename). No fixed choices: the answer
# is a ticker, typed or accepted from the candidate the parser found.
CONFIRM_FIELDS = ("Symbol",)
CONFIRM_CHOICES: dict[str, tuple[str, ...]] = {}
NUMERIC_FIELDS = ("Price", "Entry1", "Entry2", "Target", "Stop")

NUM_RE = r"\d+(\.)?\d+"          # strike matcher (needs >= 2 digits), kept as in the notebook
PRICE_RE = re.compile(r"\d*\.?\d+")   # prices, which the PDF also writes as ".25"
EXP_RE = re.compile(r"[({]?\s*(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})\s*exp", re.IGNORECASE)
CYCLE_RE = re.compile(r"\((?:Quarterly|Monthly|Weekly)\)", re.IGNORECASE)
ACTION_RE = re.compile(r"\b(BTO|STO)\b\s*(\d+)?")
SYMBOL_RE = re.compile(r"\[(\w+)\]")
PNC_RE = re.compile(r"\b(put|call)", re.IGNORECASE)
ENTRY_RE = re.compile(r"Entry\s*@", re.IGNORECASE)
TARGET_RE = re.compile(r"Target\s*@", re.IGNORECASE)   # some issues print "Target @250.00"
STOP_RE = re.compile(r"\b(Stop|Exit)\b", re.IGNORECASE)
# The rating line under a stock heading: "Perform/Buy/Hold", "Underperform /Sell",
# "Market Perform/Hold", "(Speculative) Outperform" — letters/slashes only, and short.
STATUS_LINE_RE = re.compile(r"^[A-Za-z()/ ]{3,40}$")

# Masthead, footers and the "Trend Trade Suggestion" sub-heading: never data.
SKIP_RE = re.compile(r"""
      ^\s*(Conservative|Aggressive)\s+(Trade|Traders|Investor|Investors)\b
    | ^\s*Best\s+practice
    | practice:\s*Close\s+positions
    | short/sell\s+units\s+under
    | ^\s*Table\s+of\s+Contents
    | ^\s*P\s*\d+\s+\S
    | Copyright\s*©
    | all\s+rights\s+reserved
    | General@
    | Vol\s+\d+\s+Issue\s+\d+
    | Financial\s+Group
    | Asset\s+Management
    | ^\s*(Intra-)?Trend\s+Trade\s+Suggestion\s*$
""", re.VERBOSE | re.IGNORECASE)

ROW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ROW_COLUMNS,
    "properties": {c: {"type": "string"} for c in ROW_COLUMNS},
}

log = logging.getLogger("top20_stock")


# --------------------------------------------------------------------------------------------
# 1. Locate PDFs
# --------------------------------------------------------------------------------------------
def find_top20_pdfs(target: Path) -> list[tuple[str, Path]]:
    """``[(date_str, path), ...]`` for a single Top20 PDF or every one in a folder, oldest first."""
    if target.is_file():
        m = PDF_NAME_RE.search(target.name)
        if not m:
            raise ValueError(f"{target.name} is not named '*Top20 YYYY-MM-DD.pdf'")
        return [(m.group(1), target)]
    found = []
    for p in sorted(target.iterdir()):
        m = PDF_NAME_RE.search(p.name)
        if p.is_file() and m:
            found.append((m.group(1), p))
    found.sort()
    return found


# --------------------------------------------------------------------------------------------
# 2. Extract text (notebook cells 5-6)
# --------------------------------------------------------------------------------------------
def extract_text(pdf_path: Path) -> tuple[str, str]:
    """Return ``(stock_text, commentary_text)``.

    Physical page 1 (Disclosure Statement) is skipped. Physical page 2 is the printed "Page 1":
    masthead, "Market Expectations for <month>: ..." and "Notes on current edition: ...", then
    the "<date>  912 Top 20  Vol N Issue N Page 1" footer — only the commentary block is taken
    from it. Pages 3+ hold the per-stock sections and are concatenated for ``parse_stocks``.
    """
    commentary_text = ""
    text_full = ""
    with fitz.open(str(pdf_path)) as doc:
        for page_no, page in enumerate(doc, start=1):
            if page_no == 2:
                commentary_text = split_commentary(page.get_text(), pdf_path.name)
                continue
            if page_no < 3:
                continue
            text = page.get_text()
            log.debug("==> page %d\n%s", page_no, text)
            text_full += text
    return text_full, commentary_text


def split_commentary(page_text: str, pdf_name: str) -> str:
    """The block from the "Market Expectations" line up to (not including) the page footer."""
    lines = page_text.split("\n")
    start = next((i for i, l in enumerate(lines)
                  if l.strip().startswith(COMMENTARY_START_MARKER)), None)
    if start is None:
        log.warning("%s: '%s' not found on page 2; no commentary captured.",
                    pdf_name, COMMENTARY_START_MARKER)
        return ""
    end = next((i for i in range(start + 1, len(lines)) if COMMENTARY_END_RE.search(lines[i])),
               len(lines))
    return "\n".join(l.rstrip() for l in lines[start:end]).strip()


# --------------------------------------------------------------------------------------------
# 3. Parse stock rows (notebook cells 7-8)
# --------------------------------------------------------------------------------------------
def _new_row() -> dict:
    return {c: "" for c in ROW_COLUMNS}


def _is_status_line(line: str) -> bool:
    s = line.strip()
    return bool(STATUS_LINE_RE.match(s)) and "perform" in s.lower()


def _numbers(text: str) -> list[str]:
    return [m[0] for m in re.finditer(NUM_RE, text)]


def _symbol_from_section(lines: list[str], start: int) -> str:
    """The ticker a heading without a ``[TICKER]`` refers to: its own ``@ UBER 27.87 Stop``.

    ``912 Top20 2022-12-19`` prints "Uber Technologies Inc" with no bracket, but its conditional
    exit names the symbol — the only place the section does.
    """
    for line in lines[start + 1:]:
        if SYMBOL_RE.search(line):      # the next stock heading ends this section
            break
        cand = lr.ticker_candidate(line)
        if cand:
            return cand
    return ""


def _prices(text: str) -> list[str]:
    return [m.group() for m in PRICE_RE.finditer(text)]


def parse_stocks(stock_text: str,
                 report: lr.ParseReport | None = None) -> tuple[list[dict], list[str]]:
    """Turn the concatenated page-3+ text into one row per stock.

    Same branch structure as the notebook's main loop, with three differences: rows are collected
    as dicts (the notebook's chained assignment into a pre-seeded DataFrame is gone), a line that
    cannot be read is logged *and* recorded in ``report`` instead of aborting, and a Target/leg/Stop
    line arriving before the stock's ``Entry @`` line opens that stock's row instead of overwriting
    the previous stock's row (~57 of the 78 issues have a section with no ``Entry @`` line).

    Returns ``(rows, lines)`` — the lines are needed to quote a block back to the model.
    """
    report = report if report is not None else lr.ParseReport("<text>")
    lines = stock_text.split("\n")
    rows: list[dict] = []
    symbol = None
    status = ""
    row: dict | None = None      # the row of the stock being read
    status_line = -1

    def ensure_row() -> dict:
        """The current stock's row, created on first use."""
        nonlocal row
        if row is None:
            row = _new_row()
            row["Symbol"] = symbol or ""
            row["Status"] = status
            rows.append(row)
        return row

    def set_field(r: dict, col: str, value: str, line: str) -> None:
        """First value wins. A second one means two stocks have been merged into one block —
        912 Top20 2022-12-19 prints "Uber Technologies Inc" with no ticker, and its levels used
        to be written over Comcast's row."""
        if str(r[col]).strip():
            report.flag("unconsumed", line,
                        f"{col} is already {r[col]!r} — a second stock may be in this block")
            return
        r[col] = value

    for i, line in enumerate(lines):
        try:
            if line.strip() == "" or i == status_line or SKIP_RE.search(line):
                continue
            nxt = next((lines[j] for j in range(i + 1, min(i + 4, len(lines)))
                        if lines[j].strip()), "")
            sm = SYMBOL_RE.search(line)
            if (sm is None and not _is_status_line(line) and _is_status_line(nxt)
                    and 3 < len(line.strip()) < 60 and not re.search(r"[\[\]\d@]", line)):
                # A stock heading whose ticker the PDF dropped: open the section anyway so the
                # next stock's levels do not land on the previous stock's row. ``Symbol`` is in
                # the table's primary key, so it is *labelled* missing — with the ticker from the
                # section's own "@ UBER 27.87 Stop" as the candidate — and confirmed by a person
                # (``confirm_missing``) or supplied by the LLM repair.
                report.close_block(end=i, row_end=len(rows), final=True)
                symbol, row = "", None
                status, status_line = nxt.split()[0], lines.index(nxt, i + 1)
                report.open_block(line.strip()[:30], header_line=i, start=i, row_start=len(rows))
                cand = _symbol_from_section(lines, i)
                report.mark_missing(
                    "Symbol", f"the heading {line.strip()[:40]!r} has no [TICKER]",
                    suggest=cand,
                    source=f"a '@ {cand} price' in this section" if cand else "")
                continue
            if sm:
                # "Bank of America Corp [BAC]" opens a section; the next non-blank line is the
                # status ("Perform/Buy/Hold").
                report.close_block(end=i, row_end=len(rows), final=True)
                symbol, row = sm.group(1), None
                status, status_line = "", -1
                for j in range(i + 1, min(i + 4, len(lines))):
                    tok = lines[j].split()
                    if tok:
                        status, status_line = tok[0], j
                        break
                report.open_block(symbol, symbol=symbol, header_line=i, start=i,
                                  row_start=len(rows))
                log.debug("Symbol = %s, Status = %s", symbol, status)
                if not status:
                    report.flag("incomplete", line, "no status line after the symbol")
                continue
            if symbol is None:
                continue
            if ENTRY_RE.search(line):
                # "... range of Entry @ 41.00-43.00 before provides acceptable risk reward ..."
                ent = _prices(line[ENTRY_RE.search(line).end():])
                if not ent:
                    report.flag("unconsumed", line, "Entry line with no price")
                    continue
                r = ensure_row()
                set_field(r, "Entry1", ent[0], line)
                if len(ent) > 1:
                    set_field(r, "Entry2", ent[1], line)
                else:
                    report.flag("incomplete", line, "Entry gives one price, not a range")
                if TARGET_RE.search(line):
                    tgt = _prices(line[TARGET_RE.search(line).end():])
                    if tgt:
                        set_field(r, "Target", tgt[0], line)
                continue
            if TARGET_RE.search(line):
                tgt = _prices(line[TARGET_RE.search(line).end():])
                if not tgt:
                    report.flag("unconsumed", line, "Target line with no price")
                    continue
                set_field(ensure_row(), "Target", tgt[0], line)
                continue
            if "STO" in line or "BTO" in line:
                # e.g. "STO August 43 Naked Put (8/15/25 exp) using"
                r = ensure_row()
                pc = PNC_RE.search(line)
                if pc:
                    set_field(r, "PnC", pc.group(1)[0].upper(), line)
                else:
                    report.flag("incomplete", line, "no Put/Call on the leg line")
                exp = EXP_RE.search(line)
                if exp:
                    set_field(r, "Expiration", re.sub(r"\s+", "", exp.group(1)), line)
                else:
                    report.flag("incomplete", line, "no '(M/D/YY exp)'")
                action = ACTION_RE.search(line)
                tail = line[action.end():] if action else line
                segment = CYCLE_RE.sub(" ", tail).split("(")[0]
                strikes = _numbers(segment) or _prices(segment)
                if strikes:
                    set_field(r, "Price", strikes[0], line)
                else:
                    report.flag("incomplete", line, "no strike on the leg line")
                log.debug("%s: %s", r["Symbol"], line.strip())
                continue
            if STOP_RE.search(line):
                # e.g. "conditional position Exit to BTC Naked Put @ BAC 40.91 Stop"
                prices = _prices(line.rsplit("@", 1)[-1]) or _prices(line)
                if not prices:
                    report.flag("unconsumed", line, "Stop/Exit line with no price")
                    continue
                set_field(ensure_row(), "Stop", prices[0], line)
                continue
        except Exception as e:  # noqa: BLE001 - log the line, keep going
            log.warning("Exception on line: %r => %r", line, e)
            report.flag("exception", line, lr._one_line(e))

    report.close_block(end=len(lines), row_end=len(rows))
    return rows, lines


def flag_duplicate_symbols(rows: list[dict], report: lr.ParseReport) -> None:
    """Flag the blocks of a symbol that appears twice in one issue.

    ``Stock_Options_v1``'s key is ``(Date, Symbol)``, so a second row for the same ticker cannot
    load. Flagging both blocks puts the choice in the review queue instead of dropping a row at
    load time.
    """
    first: dict[str, lr.Block] = {}
    for b in report.blocks:
        for i in range(b.row_start, min(b.row_end, len(rows))):
            sym = rows[i]["Symbol"]
            if not sym:
                continue
            if sym in first and first[sym] is not b:
                detail = f"{sym} also appears in block {first[sym].block_id} — only one can load"
                for blk in (first[sym], b):
                    blk.flags.append(lr.Flag("duplicate", f"[row {i}] {sym}", detail))
            else:
                first.setdefault(sym, b)


# --------------------------------------------------------------------------------------------
# 3b. LLM repair of the flagged blocks
# --------------------------------------------------------------------------------------------
def _coerce_row(row: dict, fallback: list[dict]) -> dict:
    """Shape an LLM row like ``_new_row()``: every column, strings, symbol/status kept."""
    out = _new_row()
    for c in ROW_COLUMNS:
        if c in row and row[c] is not None:
            out[c] = str(row[c]).strip()
    for c in ("Symbol", "Status"):
        if not out[c] and fallback:
            out[c] = fallback[0][c]
    return out


def confirm_missing(rows: list[dict], lines: list[str], report: lr.ParseReport,
                    source: str) -> int:
    """Ask a person for every Symbol the parser labelled as missing; returns blocks answered.

    ``Symbol`` is part of ``Trading.Stock_Options_v1``'s primary key, so ``split_loadable``
    rejects a row without one and the stock never reaches the table.
    """
    return hil.confirm_missing(rows, lines, report, source=source, fields=CONFIRM_FIELDS,
                               choices=CONFIRM_CHOICES, columns=ROW_COLUMNS,
                               numeric_fields=NUMERIC_FIELDS, row_schema=ROW_SCHEMA)


def run_repairs(rows: list[dict], lines: list[str], report: lr.ParseReport,
                mode: str, source: str) -> lr.RepairLog | None:
    """Re-read flagged blocks (``--llm repair``) or every block (``force``) with the LLM."""
    if mode == "off":
        return None
    blocks = report.blocks if mode == "force" else report.flagged_blocks()
    if not blocks:
        return None
    rlog = lr.RepairLog(source)
    cli = lr.client()
    if cli is None:
        for b in blocks:
            rlog.add(b, [dict(r) for r in rows[b.row_start:b.row_end]], None, "unavailable",
                     "no LLM endpoint configured (LLM_BASE_URL)")
        return rlog
    spec = lr.load_spec("top20")
    for b in list(blocks):
        before = [dict(r) for r in rows[b.row_start:b.row_end]]
        block_text = b.text(lines)
        new = lr.repair_block(cli, spec, b, block_text, before, ROW_COLUMNS, ROW_SCHEMA)
        if new is None:
            rlog.add(b, before, None, "rejected", "no usable response")
            continue
        new = [_coerce_row(r, before) for r in new if isinstance(r, dict)]
        ok, reason = lr.validate_rows(new, ROW_SCHEMA, block_text, ROW_COLUMNS, NUMERIC_FIELDS)
        if not ok:
            log.warning("%s: LLM repair rejected (%s)", b.block_id, reason)
            rlog.add(b, before, new, "rejected", reason)
            continue
        report.replace_rows(rows, b, new)
        hil.reapply_answers(rows, b)   # a repair must not drop what a person just confirmed
        log.info("%s: LLM repair accepted (%d rows -> %d)", b.block_id, len(before), len(new))
        rlog.add(b, before, new, "accepted")
    return rlog


# --------------------------------------------------------------------------------------------
# 4. Derived columns (notebook cells 9, 11)
# --------------------------------------------------------------------------------------------
def derive_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add the three sign columns and put the entry range in trade order for Calls."""
    df = df.copy().fillna("")
    df["Entry_Sign"] = df["PnC"].apply(lambda x: "<" if x == "P" else ">")
    df["Target_Sign"] = df["PnC"].apply(lambda x: ">" if x == "P" else "<")
    df["Stop_Sign"] = df["Entry_Sign"]
    # Calls (bearish): swap so Entry1 is the near level and Entry2 the far one.
    df["Entry1"], df["Entry2"] = np.where(df["PnC"] == "C",
                                          [df["Entry2"], df["Entry1"]],
                                          [df["Entry1"], df["Entry2"]])
    return df


# --------------------------------------------------------------------------------------------
# 5. Final shape + types (notebook cells 12, 15)
# --------------------------------------------------------------------------------------------
def _parse_expiration(value):
    """'8/15/25' or '8/15/2025' -> Timestamp (the PDF's '(M/D/YY exp)' form); NaN stays NaN."""
    if not isinstance(value, str) or not value.strip():
        return pd.NaT
    fmt = "%m/%d/%y" if len(value.rsplit("/", 1)[-1]) == 2 else "%m/%d/%Y"
    return pd.to_datetime(value, format=fmt)


def finalize(df: pd.DataFrame, date_str: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(csv_df, db_df)``: the notebook's CSV layout (strings) and its typed DB frame."""
    df = df.copy()
    df["Date"] = date_str
    df = df.rename(columns={"Price": "Strike"})
    csv_df = df[OUTPUT_COLUMNS].reset_index(drop=True)

    db_df = csv_df.replace(r"^\s*$", np.nan, regex=True)
    db_df["Date"] = pd.to_datetime(db_df["Date"], format="%Y-%m-%d")
    db_df["Expiration"] = db_df["Expiration"].map(_parse_expiration)
    db_df = db_df.astype({"Strike": "float", "Entry1": "float", "Entry2": "float",
                          "Target": "float", "Stop": "float"})
    return csv_df, db_df


def split_loadable(db_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split off rows ``Trading.Stock_Options_v1`` cannot take (its key is ``(Date, Symbol)``).

    A row with no Symbol, or a second row for a symbol, used to raise an ``IntegrityError`` that
    ``DU.StoreEOD`` swallowed — taking the whole date's rows with it.
    """
    missing = db_df[KEY_COLUMNS].isna().any(axis=1)
    rejected = db_df[missing].assign(_reason="NULL in " + ", ".join(KEY_COLUMNS))
    good = db_df[~missing]
    dup = good.duplicated(subset=KEY_COLUMNS, keep="first")
    if dup.any():
        rejected = pd.concat([rejected, good[dup].assign(_reason="duplicate primary key")],
                             sort=False)
    return good[~dup], rejected


# --------------------------------------------------------------------------------------------
# 6. llmwiki (notebook cell 19; same policy as playbook_etf.py)
# --------------------------------------------------------------------------------------------
def upload_commentary(text: str, date_str: str) -> bool:
    """POST the market commentary to llmwiki ``/ingest``; retry then log-and-skip, never raise."""
    base_url = os.environ.get("LLMWIKI_BASE_URL")
    if not base_url:
        log.info("LLMWIKI_BASE_URL not set; skipping llmwiki upload for %s.", date_str)
        return False
    token = os.environ.get("LLMWIKI_API_TOKEN", "")
    max_retry = int(os.environ.get("MAX_LLMWIKI_RETRY", "5"))
    timeout = float(os.environ.get("LLMWIKI_TIMEOUT", "30"))

    url = f"{base_url.rstrip('/')}/ingest"
    title = f"LCR Top20 Market Commentary {date_str}"
    payload = {"title": title, "text": f"Date: {date_str}\n\n{text}"}
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(1, max_retry + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            log.info("llmwiki ingest succeeded for %s on attempt %d: %s", date_str, attempt, resp.text)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("llmwiki ingest attempt %d/%d failed for %s: %s", attempt, max_retry,
                      date_str, e, exc_info=(attempt == max_retry))
            if attempt < max_retry:
                time.sleep(min(2 * attempt, 10))
    log.error("llmwiki ingest failed after %d attempts for %s; skipping.", max_retry, date_str)
    return False


# --------------------------------------------------------------------------------------------
# 7/8. Database
# --------------------------------------------------------------------------------------------
def loaded_dates(db: str, table: str) -> set[str]:
    """Dates already present in ``db.table`` as ``YYYY-MM-DD`` strings (empty set on failure)."""
    df = DU.load_df_SQL(f"SELECT DISTINCT Date FROM {db}.{table}")
    if df is None:
        log.warning("Could not read loaded dates from %s.%s; no dates will be skipped.", db, table)
        return set()
    return set(pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d"))


def store(db_df: pd.DataFrame, db: str, table: str, date_str: str, replace: bool) -> int:
    """Append (or replace the date's rows) and return the number of rows that did not load."""
    if replace:
        log.info("Replacing %s in %s.%s with %d rows", date_str, db, table, len(db_df))
        deleted, inserted, failed = DU.ReplaceDate(db_df, db, table, date_str)
        log.info("%s: deleted %d existing row(s), inserted %d", date_str, deleted, inserted)
    else:
        log.info("Appending %d rows to %s.%s", len(db_df), db, table)
        inserted, failed = DU.StoreEOD_strict(db_df, db, table)
    for idx, err in failed:
        log.error("row %s did not load: %s", idx, err)
    return len(failed)


# --------------------------------------------------------------------------------------------
# 9. Per-PDF driver
# --------------------------------------------------------------------------------------------
def process_pdf(date_str: str, pdf_path: Path, args: argparse.Namespace, db: str,
                table: str) -> dict:
    out_dir = Path(args.out_dir) if args.out_dir else pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem
    csv_path = out_dir / (stem + ".csv")
    text_path = out_dir / (stem + ".txt")
    report_path = out_dir / (stem + ".parse-report.json")
    repairs_path = out_dir / (stem + ".repairs.json")
    rejected_path = out_dir / (stem + ".rejected.csv")
    commentary_path = out_dir / (stem + "_commentary.txt")

    stock_text, commentary_text = extract_text(pdf_path)
    text_path.write_text(stock_text)

    report = lr.ParseReport(pdf_path.name)
    rows, lines = parse_stocks(stock_text, report)
    flag_duplicate_symbols(rows, report)
    hil.label_missing_keys(rows, lines, report, CONFIRM_FIELDS)
    decisions = hil.get_store(hil.store_path(pdf_path, args.decisions))
    hil.apply_decisions(rows, lines, report, decisions, "top20")
    if not rows:
        raise ValueError("no stock rows parsed (no '[SYMBOL]' sections found on pages 3+)")
    # Asked before the LLM: a ticker the PDF never printed in brackets is quicker to confirm off
    # the section text than to wait for a repair round-trip.
    if args.confirm:
        confirm_missing(rows, lines, report, pdf_path.name)
    repairs = run_repairs(rows, lines, report, args.llm, pdf_path.name)
    if args.review:
        if hil.review(rows, lines, report, decisions, repairs, publication="top20",
                      source=pdf_path.name, date=date_str, columns=ROW_COLUMNS,
                      numeric_fields=NUMERIC_FIELDS, row_schema=ROW_SCHEMA, new_row=_new_row):
            decisions.save()
    # Recorded last, holding each confirmed block's rows as they finally stand, so a replay (or a
    # --replace-date reload) reproduces what this run loaded without an LLM call.
    if hil.record_confirmations(rows, lines, report, decisions, publication="top20",
                                source=pdf_path.name, date=date_str):
        decisions.save()
    blank_rows = [i for i, r in enumerate(rows)
                  if any(not str(r.get(f) or "").strip() for f in CONFIRM_FIELDS)]
    if blank_rows:
        log.warning("%s: %d row(s) still have no %s and cannot load (rows %s)%s",
                    pdf_path.name, len(blank_rows), "/".join(CONFIRM_FIELDS),
                    ", ".join(str(i) for i in blank_rows),
                    "" if args.confirm else " — drop --no-confirm to be asked about them")
    unsettled = report.unsettled_blocks()
    if unsettled and not args.review:
        log.warning("%s: %d block(s) still need a human decision (--review): %s", pdf_path.name,
                    len(unsettled), ", ".join(b.block_id for b in unsettled))
    report.write(report_path)
    log.info("%s: %s", pdf_path.name, report.summary())
    if repairs is not None:
        repairs.write(repairs_path)
        log.info("%s: LLM repair — %d accepted, %d rejected, %d unavailable -> %s",
                 pdf_path.name, repairs.accepted, repairs.rejected, repairs.unavailable,
                 repairs_path)

    df = derive_columns(pd.DataFrame(rows, columns=ROW_COLUMNS))
    csv_df, db_df = finalize(df, date_str)
    loadable, rejected = split_loadable(db_df)

    csv_df.to_csv(csv_path, sep=",", index=None)
    log.info("%s: parsed %d rows -> %s", pdf_path.name, len(csv_df), csv_path)
    if len(rejected):
        rejected.to_csv(rejected_path, index=None)
        log.warning("%s: %d row(s) are not loadable (see %s)", pdf_path.name, len(rejected),
                    rejected_path)
    elif rejected_path.exists():
        rejected_path.unlink()

    if commentary_text:
        commentary_path.write_text(commentary_text)
        log.info("%s: saved market commentary -> %s", pdf_path.name, commentary_path)
    else:
        log.warning("%s: no market commentary captured from page 2.", pdf_path.name)

    failed_rows = 0
    if args.skip_db:
        log.info("%s: --csv-only/--skip-db, not uploading %d rows.", pdf_path.name, len(loadable))
    else:
        failed_rows = store(loadable, db, table, date_str, args.replace_date)

    sent = False
    if args.skip_llmwiki:
        log.info("%s: --skip-llmwiki, not sending commentary.", pdf_path.name)
    elif commentary_text:
        sent = upload_commentary(commentary_text, date_str)
    # Configured, attempted and failed — the date is not finished, whatever the DB says.
    llmwiki_pending = bool(commentary_text) and not args.skip_llmwiki and not sent \
        and bool(os.environ.get("LLMWIKI_BASE_URL"))

    if args.clean:
        clean_scratch_files(pdf_path, [text_path, report_path], args, failed_rows=failed_rows,
                            rejected=len(rejected), unsettled=len(unsettled),
                            blank_rows=len(blank_rows), llmwiki_pending=llmwiki_pending)

    return {"rows": len(csv_df), "loaded": len(loadable), "rejected": len(rejected),
            "failed_rows": failed_rows, "unsettled": len(unsettled)}


def clean_scratch_files(pdf_path: Path, paths: list[Path], args: argparse.Namespace, *,
                        failed_rows: int, rejected: int, unsettled: int, blank_rows: int,
                        llmwiki_pending: bool) -> None:
    """Drop the rebuildable reports once the date is actually done. The default; see ``--no-clean``.

    "Done" is the whole job, not just the last step: every parsed row loaded, none rejected, no
    column left for a person to answer, and the commentary delivered. Anything else and the text
    and parse report are exactly what you would want to read next, so they stay and the run says
    why. A verify pass (``--csv-only``) loads nothing, so it never cleans — its reports are the
    point of it.
    """
    if args.skip_db:
        log.debug("%s: nothing was loaded, keeping the run's reports.", pdf_path.name)
        return
    reasons = []
    if failed_rows:
        reasons.append(f"{failed_rows} row(s) did not load")
    if rejected:
        reasons.append(f"{rejected} row(s) were rejected")
    if unsettled:
        reasons.append(f"{unsettled} block(s) are unsettled")
    if blank_rows:
        reasons.append(f"{blank_rows} row(s) have no {'/'.join(CONFIRM_FIELDS)}")
    if llmwiki_pending:
        reasons.append("the commentary did not reach llmwiki")
    if reasons:
        log.info("%s: keeping %s and the parse report — %s.", pdf_path.name,
                 paths[0].name, "; ".join(reasons))
        return
    gone = lr.clean_scratch(paths)
    if gone:
        log.info("%s: cleaned %s", pdf_path.name, ", ".join(p.name for p in gone))


# --------------------------------------------------------------------------------------------
# 10. CLI
# --------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("target", type=Path,
                   help="a single '*Top20 YYYY-MM-DD.pdf' or a folder to scan for them")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="only process this date")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only process PDFs dated on/after this")
    p.add_argument("--force", action="store_true",
                   help="process dates already in the DB (rows are appended, not replaced)")
    p.add_argument("--replace-date", action="store_true",
                   help="replace each processed date's rows (DELETE then INSERT in one "
                        "transaction) — implies --force, and is how to re-fix a loaded date")
    p.add_argument("--llm", choices=("off", "repair", "force"), default="repair",
                   help="LLM re-read of blocks the regexes flagged: repair (default), off, or "
                        "force (every block). Needs LLM_BASE_URL/LLM_MODEL; see .env.example")
    p.add_argument("--review", action="store_true",
                   help="ask about every block the regexes flagged and the LLM did not settle, and "
                        "remember each answer (see --decisions)")
    p.add_argument("--no-confirm", dest="confirm", action="store_false",
                   help="do not ask about a Symbol the parser could not read (it is in the "
                        "table's primary key, so such a row is rejected instead of loaded)")
    p.add_argument("--decisions", metavar="PATH",
                   help="decisions store for --review answers "
                        "(default: .pdfreader-decisions.json next to the PDF)")
    p.add_argument("--csv-only", action="store_true",
                   help="write CSV + text + reports only — no DB, no llmwiki (verify first)")
    p.add_argument("--clean", action="store_true", default=True,
                   help="the default: once a date has loaded completely, delete its rebuildable "
                        "reports (<stem>.txt, <stem>.parse-report.json)")
    p.add_argument("--no-clean", dest="clean", action="store_false",
                   help="keep a loaded date's <stem>.txt and <stem>.parse-report.json. They are "
                        "kept anyway whenever a row was rejected, a block is unsettled or the "
                        "commentary did not post — that is when they are worth reading")
    p.add_argument("--skip-db", action="store_true", help="parse and write CSV only, no DB upload")
    p.add_argument("--skip-llmwiki", action="store_true", help="do not send commentary to llmwiki")
    p.add_argument("--dry-run", action="store_true", help="same as --csv-only")
    p.add_argument("--env-file", default=".env", help="dotenv file (default: .env)")
    p.add_argument("--out-dir", help="where to write CSV/commentary files (default: next to the PDF)")
    p.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ... (default: INFO)")
    args = p.parse_args(argv)
    if args.dry_run or args.csv_only:
        args.skip_db = args.skip_llmwiki = True
    if args.replace_date:
        args.force = True
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not load_dotenv(args.env_file):
        log.warning("env file %s not found; relying on the process environment.", args.env_file)
    db = os.environ.get("DBTRADING", "Trading")
    table = os.environ.get("TBLSTOCKOPTIONS", "Stock_Options_v1")

    if not (args.target.is_dir() or args.target.is_file()):
        log.error("%s is not a file or directory", args.target)
        return 2
    try:
        pdfs = find_top20_pdfs(args.target)
    except ValueError as e:
        log.error("%s", e)
        return 2
    if args.since:
        pdfs = [(d, p) for d, p in pdfs if d >= args.since]
    if args.date:
        pdfs = [(d, p) for d, p in pdfs if d == args.date]
    if not pdfs:
        log.warning("No Top20 PDFs to process in %s", args.target)
        return 0
    log.info("Found %d Top20 PDF(s) in %s", len(pdfs), args.target)

    done = set() if (args.skip_db or args.force) else loaded_dates(db, table)

    processed = 0
    problems = 0
    skipped: list[tuple[str, str]] = []   # (filename, one-line reason)
    failed: list[tuple[str, str]] = []
    for date_str, pdf_path in pdfs:
        if date_str in done:
            reason = f"date {date_str} already in {db}.{table} (use --replace-date to re-load)"
            log.info("%s: %s, skipping", pdf_path.name, reason)
            skipped.append((pdf_path.name, reason))
            continue
        try:
            stats = process_pdf(date_str, pdf_path, args, db, table)
            processed += 1
            problems += stats["rejected"] + stats["failed_rows"] + stats["unsettled"]
        except Exception as e:  # noqa: BLE001
            log.error("%s: failed", pdf_path.name, exc_info=True)
            failed.append((pdf_path.name, _one_line(e)))
    hil.save_all()
    log.info("Done: %d processed, %d skipped, %d failed, %d unresolved item(s)",
             processed, len(skipped), len(failed), problems)
    for name, reason in skipped:
        log.info("  skipped: %s — %s", name, reason)
    for name, reason in failed:
        log.error("  failed:  %s — %s", name, reason)
    return 1 if (failed or problems) else 0


def _one_line(e: BaseException) -> str:
    """``ValueError: time data ... doesn't match`` — type + first line of the message."""
    msg = str(e).strip().split("\n", 1)[0]
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


if __name__ == "__main__":
    sys.exit(main())
