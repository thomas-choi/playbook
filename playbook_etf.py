#!/usr/bin/env python
"""Batch-process LCR/912 Playbook PDFs: parse trade tables, load to MySQL, send commentary to llmwiki.

Script version of ``ETF_2_Code.ipynb``. Given a folder *or a single PDF*, every
``*Playbook YYYY-MM-DD.pdf`` is parsed with the same logic as the notebook (one function per
notebook stage — see the function docstrings for the originating cells), the resulting rows are
appended to ``Trading.ETF_Options_v1`` (dates already present in the table are skipped unless
``--force``/``--replace-date``), and physical page 2's market commentary ("Market Expectations ..."
block) is posted to llmwiki's ``POST /ingest`` with the date in the title and body.

The regexes below are the primary parser. Where they get confused — the PDFs do not always follow
their own layout — the block is flagged in a ``llm_repair.ParseReport`` and, with ``--llm repair``
(the default), re-read by an LLM over the OpenAI-standard API; see ``docs/PDFreader.md``.

    python playbook_etf.py /path/to/pdfs [--since 2026-09-01] [--csv-only] ...
    python playbook_etf.py "/path/to/912 Playbook 2026-09-07.pdf" --csv-only
    python playbook_etf.py /path/to/pdfs --date 2022-06-06 --replace-date

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

# "912 Playbook 2026-09-07.pdf" (Google-Drive naming), "Playbook-2026-09-07.pdf" (repo sample)
# or "Playbook_2026-09-07.pdf"
PDF_NAME_RE = re.compile(r"Playbook[ _-](\d{4}-\d{2}-\d{2})\.pdf$", re.IGNORECASE)
# "Bond ETF Trade & Maintenance Suggestions", "Featured Trade & ...", "Featured Investor & ..."
SECTION_RE = re.compile(r"(?:Trade|Investor)\s*&\s*Maintenance Suggestions")
COMMENTARY_SPLIT_MARKER = "Copyright"
COMMENTARY_START_MARKER = "Market Expectations"

ROW_COLUMNS = ["Type", "Symbol", "Trend", "Status", "Expiration", "PnC", "L_Strike", "H_Strike",
               "Entry", "Target", "Stop", "quantity"]
OUTPUT_COLUMNS = ["Date", "Type", "Trend", "Symbol", "Status", "Expiration", "PnC", "Low-High",
                  "L_Strike", "H_Strike", "Entry_Sign", "Entry", "Target_Sign", "Target",
                  "Stop_Sign", "Stop", "quantity"]
# NOT NULL in Trading.ETF_Options_v1, and its primary key.
KEY_COLUMNS = ["Date", "Trend", "Symbol", "Low-High"]
NUMERIC_FIELDS = ("L_Strike", "H_Strike", "Entry", "Target", "Stop")

NUM_RE = r"\d+(\.)?\d+"          # strike matcher (needs >= 2 digits), kept as in the notebook
PRICE_RE = re.compile(r"\d*\.?\d+")   # prices, which the PDF also writes as ".25"
# "(9/18/26 exp)", "{12/16/16 exp)", "8/15/25 exp)", "(6/30 /23 exp)" — all seen in the PDFs
EXP_RE = re.compile(r"[({]?\s*(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})\s*exp", re.IGNORECASE)
# "(Quarterly)" / "(Monthly)" can sit between the month and the strike
CYCLE_RE = re.compile(r"\((?:Quarterly|Monthly|Weekly)\)", re.IGNORECASE)
ACTION_RE = re.compile(r"\b(BTO|STO)\b\s*(\d+)?")
NET_ENTRY_RE = re.compile(r"Entry of a net\s+([\d.]+)\s+(Credit|Debit)", re.IGNORECASE)
SYMBOL_RE = re.compile(r"\[(\w+)\]")
TREND_RE = re.compile(r"\b(Investor|Trader)\b")
# "[Bullish/Counter Trend]" (spaces) and the line-wrapped "[Bullish/Hold" (no closing bracket)
STATUS_RE = re.compile(r"\[([^\]\n]*)\]?")
PNC_RE = re.compile(r"\b(put|call)", re.IGNORECASE)
STOP_RE = re.compile(r"\b(Stop|Exit)\b", re.IGNORECASE)

# Masthead, table of contents, footers and advice prose: never trade content, and left to the
# content branches below they land in the wrong one (this is most of the old "Exception on line").
SKIP_RE = re.compile(r"""
      ^\s*(Conservative|Aggressive)\s+(Trade|Traders|Investor|Investors)\b
    | ^\s*Best\s+practice
    | practice:\s*Close\s+positions          # 'Best' often wraps onto the line above
    | short/sell\s+units\s+under
    | Maintain\s+Cash\s+Position
    | ^\s*Table\s+of\s+Contents
    | ^\s*P\s*\d+\s+\S
    | Copyright\s*©
    | all\s+rights\s+reserved
    | General@
    | Vol\s+\d+\s+Issue\s+\d+
    | Financial\s+Group
    | Asset\s+Management
    | ^\s*PLAYBOOK\s*$
""", re.VERBOSE | re.IGNORECASE)

ROW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ROW_COLUMNS,
    "properties": {**{c: {"type": "string"} for c in ROW_COLUMNS if c != "quantity"},
                   "quantity": {"type": "integer"}},
}

log = logging.getLogger("playbook_etf")


# --------------------------------------------------------------------------------------------
# 1. Locate PDFs
# --------------------------------------------------------------------------------------------
def find_playbook_pdfs(target: Path) -> list[tuple[str, Path]]:
    """``[(date_str, path), ...]`` for a single Playbook PDF or every one in a folder, oldest first."""
    if target.is_file():
        m = PDF_NAME_RE.search(target.name)
        if not m:
            raise ValueError(f"{target.name} is not named '*Playbook YYYY-MM-DD.pdf' "
                             "(space, '-' or '_' before the date)")
        return [(m.group(1), target)]
    found = []
    for p in sorted(target.iterdir()):
        m = PDF_NAME_RE.search(p.name)
        if p.is_file() and m:
            found.append((m.group(1), p))
    found.sort()
    return found


# --------------------------------------------------------------------------------------------
# 2. Extract text (notebook cell 4)
# --------------------------------------------------------------------------------------------
def extract_text(pdf_path: Path) -> tuple[str, str]:
    """Return ``(table_text, commentary_text)``.

    Physical page 1 (Disclosure Statement) is skipped. Physical page 2 holds the TOC, the
    "Featured ..." trade sections and the market commentary, but PyMuPDF does not emit them in a
    fixed order (older issues: commentary *before* the trades; newer: after the "Copyright" line).
    The commentary is therefore cut out as the block starting at the "Market Expectations ..."
    line and ending at the next section header / "Copyright" line / end of page; the rest of the
    page joins the trade-table text fed to the parser.
    """
    commentary_text = ""
    text_full = ""
    with fitz.open(str(pdf_path)) as doc:
        for page_no, page in enumerate(doc, start=1):
            if page_no < 2:
                continue
            text = page.get_text()
            if page_no == 2:
                text, commentary_text = _split_commentary(text, pdf_path.name)
            log.debug("==> page %d\n%s", page_no, text)
            text_full += text
    return text_full, commentary_text


def _split_commentary(text: str, pdf_name: str) -> tuple[str, str]:
    """Split page-2 text into ``(table_text, commentary_text)``; see ``extract_text``."""
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines)
                  if l.strip().startswith(COMMENTARY_START_MARKER)), None)
    if start is None:
        # Fallback for issues without the heading: everything after "Copyright ..." (the
        # layout of the newer PDFs).
        idx = text.find(COMMENTARY_SPLIT_MARKER)
        if idx == -1:
            log.warning("%s: neither '%s' nor '%s' found on page 2; treating the whole page as "
                        "trade table text, no commentary captured.",
                        pdf_name, COMMENTARY_START_MARKER, COMMENTARY_SPLIT_MARKER)
            return text, ""
        return text[:idx], text[idx:].split("\n", 1)[-1].strip()
    end = next((i for i in range(start + 1, len(lines))
                if SECTION_RE.search(lines[i]) or COMMENTARY_SPLIT_MARKER in lines[i]), len(lines))
    commentary = "\n".join(lines[start:end]).strip()
    table = "\n".join(lines[:start] + lines[end:])
    return table, commentary


# --------------------------------------------------------------------------------------------
# 3. Parse trade rows (notebook cells 5, 7, 9)
# --------------------------------------------------------------------------------------------
def _new_row() -> dict:
    return {c: (1 if c == "quantity" else "") for c in ROW_COLUMNS}


def _numbers(text: str) -> list[str]:
    """Strikes: >= 2 digits, so a stray single digit is not mistaken for one."""
    return [m[0] for m in re.finditer(NUM_RE, text)]


def _prices(text: str) -> list[str]:
    """Prices, including the PDF's ``.25`` form."""
    return [m.group() for m in PRICE_RE.finditer(text)]


def _price_after(line: str, marker: str) -> str:
    """First price printed after ``marker`` ("Entry @", "Target @"), or ""."""
    idx = line.find(marker)
    if idx == -1:
        return ""
    p = _prices(line[idx + len(marker):])
    return p[0] if p else ""


def parse_trades(table_text: str,
                 report: lr.ParseReport | None = None) -> tuple[list[dict], list[str]]:
    """Turn the concatenated trade-table text into one row per ``STO``/``BTO`` leg.

    Same branch structure as the notebook's main loop; rows are collected as dicts and turned into
    a DataFrame by the caller (the notebook wrote into a pre-seeded DataFrame with chained
    assignment, which pandas >= 2 no longer supports). Lines that cannot be read are logged *and*
    recorded in ``report`` so ``run_repairs`` can hand the block to an LLM.

    Returns ``(rows, lines)`` — the lines are needed to quote a block back to the model.
    """
    report = report if report is not None else lr.ParseReport("<text>")
    lines = table_text.split("\n")
    # Pre-scan: parsing starts at the first section header (notebook cell 7).
    start = next((i for i, l in enumerate(lines) if SECTION_RE.search(l)), None)
    if start is None:
        raise ValueError("no '... & Maintenance Suggestions' header found in extracted text")

    rows: list[dict] = []
    type_name = None
    symbol = None
    status = None
    trend = None
    symbol_line = -1
    line_num = -1
    group_start = 0      # index into ``rows`` of the current Trend group (for combo broadcast)
    is_combo = False     # set once an "Entry of a net ... Credit/Debit" line is seen

    for i in range(start, len(lines)):
        line = lines[i]
        try:
            m = SECTION_RE.search(line)
            if m:
                report.close_block(end=i, row_end=len(rows), final=True)
                asset_class = line[:m.start()].strip()
                if asset_class:
                    type_name = asset_class
                    log.debug("Type_name = %s", type_name)
                else:
                    report.flag("unknown_header", line, "no asset class before the header")
                continue
            if line.strip() == "" or SKIP_RE.search(line):
                continue
            sm = SYMBOL_RE.search(line)
            if "Trend" not in line and sm:
                report.close_block(end=i, row_end=len(rows), final=True)
                symbol = sm.group(1)
                symbol_line, line_num = i, 0
                log.debug("Symbol = %s", symbol)
                continue
            if line_num == 0 or line.find("Trend") > 0:
                tm = TREND_RE.search(line)
                if tm is None:
                    report.flag("unconsumed", line, "Trend line without Investor/Trader")
                    continue
                trend = tm.group(1)
                st = STATUS_RE.search(line)
                status = st.group(1).strip() if st else ""
                line_num, group_start, is_combo = 1, len(rows), False
                report.open_block(f"{symbol}/{trend}", symbol=symbol or "", trend=trend,
                                  header_line=symbol_line, start=i, row_start=len(rows))
                if not status:
                    report.flag("incomplete", line, "no [status] on the Trend line")
                continue
            if "STO" in line or "BTO" in line:
                row = _new_row()
                rows.append(row)          # appended first so a partial parse still keeps the row
                row["Type"] = type_name
                row["Symbol"] = symbol
                row["Trend"] = trend
                row["Status"] = status
                pc = PNC_RE.search(line)      # the PDFs also print "put" / "call" lower-case
                if pc:
                    row["PnC"] = pc.group(1)[0].upper()
                else:
                    report.flag("incomplete", line, "no Put/Call on the leg line")
                # BTO=Long/Buy, STO=Short/Sell; trailing number is the contract count, signed
                # by direction (STO 2 -> -2).
                action_match = ACTION_RE.search(line)
                if action_match:
                    qty = int(action_match.group(2)) if action_match.group(2) else 1
                    row["quantity"] = qty if action_match.group(1) == "BTO" else -qty
                    tail = line[action_match.end():]
                else:
                    report.flag("incomplete", line, "no BTO/STO action word")
                    tail = line
                exp = EXP_RE.search(line)
                if exp:
                    row["Expiration"] = re.sub(r"\s+", "", exp.group(1))
                else:
                    report.flag("incomplete", line, "no '(M/D/YY exp)'")
                # Strikes come from the text *after* the action/count prefix so a leading
                # contract count is never mistaken for a strike. "(Quarterly)" is dropped first,
                # because it would otherwise end the segment before the strike.
                segment = CYCLE_RE.sub(" ", tail).split("(")[0]
                # A one-digit strike ("STO 2 January 9 Naked Puts") needs the price matcher; the
                # >= 2 digit matcher runs first so a stray digit elsewhere cannot win.
                strikes = _numbers(segment) or _prices(segment)
                if len(strikes) >= 2:
                    row["L_Strike"], row["H_Strike"] = strikes[0], strikes[1]
                elif strikes:
                    row["H_Strike"] = strikes[0]
                else:
                    report.flag("incomplete", line, "no strike on the leg line")
                continue
            if "Entry of a net" in line:
                # Multi-leg combo: one net Entry (negative for Credit) broadcast to every leg
                # parsed since the current Trend group started.
                net_match = NET_ENTRY_RE.search(line)
                if net_match is None or not rows:
                    report.flag("unconsumed", line, "net Entry without a value or a leg")
                    continue
                is_combo = True
                net_val = float(net_match.group(1))
                entry_val = -net_val if net_match.group(2).lower() == "credit" else net_val
                target_val = _price_after(line, "Target @")
                for r in rows[group_start:]:
                    r["Entry"] = entry_val
                    if target_val:
                        r["Target"] = target_val
                continue
            if "Entry @" in line or "Target @" in line:
                if not rows:
                    report.flag("unconsumed", line, "Entry/Target before any leg")
                    continue
                entry_val = _price_after(line, "Entry @")
                target_val = _price_after(line, "Target @")
                if entry_val:
                    rows[-1]["Entry"] = entry_val
                if target_val:
                    rows[-1]["Target"] = target_val
                if not (entry_val or target_val):
                    report.flag("unconsumed", line, "Entry/Target line with no price")
                continue
            if STOP_RE.search(line):
                if not rows:
                    report.flag("unconsumed", line, "Stop before any leg")
                    continue
                # "... Exit to BTC Naked Put @ EWW 47.61 Stop" -> the price after the last
                # "@"; "conditional position Exit 145.52 Stop" (no "@") -> the first one.
                prices = _prices(line.rsplit("@", 1)[-1]) or _prices(line)
                if not prices:
                    report.flag("unconsumed", line, "Stop/Exit line with no price")
                    continue
                for r in (rows[group_start:] if is_combo else rows[-1:]):
                    r["Stop"] = prices[0]
                continue
            if lr.CONTENT_HINT_RE.search(line):
                report.flag("unconsumed", line, "looks like trade content but matched no branch")
        except Exception as e:  # noqa: BLE001 - mirror the notebook: log the line, keep going
            log.warning("Exception on line: %r => %r", line, e)
            report.flag("exception", line, lr._one_line(e))

    report.close_block(end=len(lines), row_end=len(rows))
    return rows, lines


def flag_duplicate_legs(rows: list[dict], report: lr.ParseReport) -> None:
    """Flag the blocks whose legs collide on the table's primary key.

    ``(Date, Trend, Symbol, Low-High)`` holds one row per strike pair, so two legs with the same
    symbol, trend, strikes and Put/Call cannot both load. This is not always a parser mistake: some
    issues carry a stale boilerplate section alongside the current one (LCR Playbook 2020-06-01 and
    2020-06-22 both repeat SPY/QQQ/IWM at the 2016 strikes with `12/16/2016` expirations), and which
    one to keep is a judgement call. Flagging both blocks puts it in the review queue instead of
    dropping the second row at load time.
    """
    first: dict[tuple, tuple[lr.Block, int]] = {}
    for b in report.blocks:
        for i in range(b.row_start, min(b.row_end, len(rows))):
            r = rows[i]
            if not r["Symbol"]:
                continue
            key = (r["Symbol"], r["Trend"], r["PnC"], r["L_Strike"], r["H_Strike"])
            if key in first and first[key][0] is not b:
                other, j = first[key]
                leg = f"{r['L_Strike']}/{r['H_Strike']}{r['PnC']}".lstrip("/")
                for blk, exp in ((other, rows[j]["Expiration"]), (b, r["Expiration"])):
                    blk.flags.append(lr.Flag(
                        "duplicate", f"[row {i if blk is b else j}] {r['Symbol']} {leg} "
                                     f"({exp or 'no expiration'})",
                        f"{r['Symbol']}/{r['Trend']} {leg} is in both {other.block_id} "
                        f"({rows[j]['Expiration'] or '?'}) and {b.block_id} "
                        f"({r['Expiration'] or '?'}) — only one can load"))
            else:
                first.setdefault(key, (b, i))


# --------------------------------------------------------------------------------------------
# 3b. LLM repair of the flagged blocks
# --------------------------------------------------------------------------------------------
def _coerce_row(row: dict, fallback: list[dict]) -> dict:
    """Shape an LLM row like ``_new_row()``: every column, strings, int quantity, group fields kept."""
    out = _new_row()
    for c in ROW_COLUMNS:
        if c in row and row[c] is not None:
            out[c] = row[c]
    for c in ("Type", "Symbol", "Trend", "Status"):
        if not str(out[c]).strip() and fallback:
            out[c] = fallback[0][c]
    try:
        out["quantity"] = int(out["quantity"])
    except (TypeError, ValueError):
        out["quantity"] = 1
    for c in ROW_COLUMNS:
        if c != "quantity":
            out[c] = "" if out[c] is None else str(out[c]).strip()
    return out


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
    spec = lr.load_spec("playbook")
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
        log.info("%s: LLM repair accepted (%d rows -> %d)", b.block_id, len(before), len(new))
        rlog.add(b, before, new, "accepted")
    return rlog


# --------------------------------------------------------------------------------------------
# 4. Derived columns (notebook cells 11, 14-17)
# --------------------------------------------------------------------------------------------
def _same_group(df: pd.DataFrame, i: int, j: int) -> bool:
    return (df.at[i, "Type"] == df.at[j, "Type"] and df.at[i, "Symbol"] == df.at[j, "Symbol"]
            and df.at[i, "Trend"] == df.at[j, "Trend"])


def _broadcast_group_values(df: pd.DataFrame) -> None:
    """Fill empty Entry/Target/Stop from the leg of the same group that states them.

    The PDF prints one conditional Entry/Target/Stop per Trend group ("conditional Entry @ HYG
    78.95 or below, conditional Target @ HYG 82.06" under both of HYG's legs); only the leg whose
    line carried the text gets it during parsing. Empty cells only — a leg that states its own
    value keeps it.
    """
    idx = list(df.index)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and _same_group(df, idx[j + 1], idx[i]):
            j += 1
        for col in ("Entry", "Target", "Stop"):
            stated = [df.at[k, col] for k in idx[i:j + 1] if str(df.at[k, col]).strip() != ""]
            if stated:
                for k in idx[i:j + 1]:
                    if str(df.at[k, col]).strip() == "":
                        df.at[k, col] = stated[0]
        i = j + 1


def derive_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``Low``/``High``/``Low-High``, the three sign columns, and fill Entry/Target/Stop.

    The row loops keep the notebook's sequential semantics (each row reads the row before/after
    it *as already updated*) and its try/except-per-row, which is what handles the first/last
    row edge cases.
    """
    df = df.copy().fillna("")   # NaN is truthy, so the strike checks below need "" for empty
    df["Low"] = ""
    df["High"] = ""
    df["Low-High"] = ""
    for i in df.index:
        try:
            if df.at[i, "L_Strike"]:
                df.at[i, "Low"] = "+" + df.at[i, "L_Strike"] + df.at[i, "PnC"]
            if df.at[i, "H_Strike"]:
                df.at[i, "High"] = "-" + df.at[i, "H_Strike"] + df.at[i, "PnC"]
            df.at[i, "Low-High"] = df.at[i, "Low"] + df.at[i, "High"]
        except Exception:  # noqa: BLE001
            pass

    df["Entry_Sign"] = df["PnC"].apply(lambda x: "<" if x == "P" else ">")
    df["Target_Sign"] = df["PnC"].apply(lambda x: ">" if x == "P" else "<")
    df["Stop_Sign"] = df["Entry_Sign"]

    _broadcast_group_values(df)

    # Fill an empty Stop from the next row of the same Symbol/Expiration.
    for i in df.index:
        try:
            if not (df.at[i, "Stop"] or df.at[i, "Symbol"] != df.at[i + 1, "Symbol"]
                    or df.at[i, "Expiration"] != df.at[i + 1, "Expiration"]):
                df.at[i, "Stop"] = (df.at[i + 1, "Stop"] if df.at[i + 1, "PnC"] == "C"
                                    else df.at[i + 1, "Entry"])
        except Exception:  # noqa: BLE001
            pass

    # Carry each sign down through rows of the same Type/Symbol/Trend group (this is also what
    # gives every leg of a combo the same signs).
    for col in ("Stop_Sign", "Entry_Sign", "Target_Sign"):
        for i in df.index:
            try:
                if i - 1 in df.index and _same_group(df, i, i - 1):
                    df.at[i, col] = df.at[i - 1, col]
                else:
                    df.at[i, col] = df.at[i, "Entry_Sign" if col == "Stop_Sign" else col]
            except Exception:  # noqa: BLE001
                pass
    return df


# --------------------------------------------------------------------------------------------
# 5. Final shape + types (notebook cells 18-21, 25)
# --------------------------------------------------------------------------------------------
def _parse_expiration(value):
    """'9/18/26' or '9/18/2026' -> Timestamp (the PDF's '(M/D/YY exp)' form); NaN stays NaN."""
    if not isinstance(value, str) or not value.strip():
        return pd.NaT
    fmt = "%m/%d/%y" if len(value.rsplit("/", 1)[-1]) == 2 else "%m/%d/%Y"
    return pd.to_datetime(value, format=fmt)


def finalize(df: pd.DataFrame, date_str: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(csv_df, db_df)``: the notebook's CSV layout (strings) and its typed DB frame."""
    df = df.copy()
    df["Date"] = date_str
    df = df[OUTPUT_COLUMNS]
    investor = df[df["Trend"] == "Investor"].reset_index(drop=True)
    trader = df[df["Trend"] == "Trader"].reset_index(drop=True)
    other = df[~df["Trend"].isin(["Investor", "Trader"])].reset_index(drop=True)
    csv_df = pd.concat([investor, trader, other], ignore_index=True, sort=False).fillna("")

    db_df = csv_df.replace(r"^\s*$", np.nan, regex=True)
    db_df["Date"] = pd.to_datetime(db_df["Date"], format="%Y-%m-%d")
    db_df["Expiration"] = db_df["Expiration"].map(_parse_expiration)
    db_df = db_df.astype({"L_Strike": "float", "H_Strike": "float", "Target": "float",
                          "Entry": "float", "Stop": "float", "quantity": "int"})
    return csv_df, db_df


def split_loadable(db_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split off rows the table cannot take, instead of letting them fail the whole date.

    ``Trading.ETF_Options_v1`` has ``Date``/``Trend``/``Symbol``/``Low-High`` NOT NULL as its
    primary key, so a row with no parsed strike (``Low-High`` empty) used to raise
    ``IntegrityError (1048, "Column 'Low-High' cannot be null")`` and — because ``DU.StoreEOD``
    swallowed it — silently drop every row of that date. Duplicate keys would do the same.
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
# 6. llmwiki (notebook cell 27, switched from /upload to /ingest {text, title})
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
    title = f"LCR Playbook Market Commentary {date_str}"
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

    table_text, commentary_text = extract_text(pdf_path)
    text_path.write_text(table_text)

    report = lr.ParseReport(pdf_path.name)
    rows, lines = parse_trades(table_text, report)
    flag_duplicate_legs(rows, report)
    store = hil.get_store(hil.store_path(pdf_path, args.decisions))
    hil.apply_decisions(rows, lines, report, store, "playbook")
    repairs = run_repairs(rows, lines, report, args.llm, pdf_path.name)
    if args.review:
        if hil.review(rows, lines, report, store, repairs, publication="playbook",
                      source=pdf_path.name, date=date_str, columns=ROW_COLUMNS,
                      numeric_fields=NUMERIC_FIELDS, row_schema=ROW_SCHEMA, new_row=_new_row):
            store.save()
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

    if args.skip_llmwiki:
        log.info("%s: --skip-llmwiki, not sending commentary.", pdf_path.name)
    elif commentary_text:
        upload_commentary(commentary_text, date_str)

    return {"rows": len(csv_df), "loaded": len(loadable), "rejected": len(rejected),
            "failed_rows": failed_rows, "unsettled": len(unsettled)}


# --------------------------------------------------------------------------------------------
# 10. CLI
# --------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("target", type=Path,
                   help="a single '*Playbook YYYY-MM-DD.pdf' or a folder to scan for them")
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
    p.add_argument("--decisions", metavar="PATH",
                   help="decisions store for --review answers "
                        "(default: .pdfreader-decisions.json next to the PDF)")
    p.add_argument("--csv-only", action="store_true",
                   help="write CSV + text + reports only — no DB, no llmwiki (verify first)")
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
    table = os.environ.get("TBLETFOPTIONS", "ETF_Options_v1")

    if not (args.target.is_dir() or args.target.is_file()):
        log.error("%s is not a file or directory", args.target)
        return 2
    try:
        pdfs = find_playbook_pdfs(args.target)
    except ValueError as e:
        log.error("%s", e)
        return 2
    if args.since:
        pdfs = [(d, p) for d, p in pdfs if d >= args.since]
    if args.date:
        pdfs = [(d, p) for d, p in pdfs if d == args.date]
    if not pdfs:
        log.warning("No Playbook PDFs to process in %s", args.target)
        return 0
    log.info("Found %d Playbook PDF(s) in %s", len(pdfs), args.target)

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
