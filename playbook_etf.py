#!/usr/bin/env python
"""Batch-process LCR Playbook PDFs: parse trade tables, load to MySQL, send commentary to llmwiki.

Script version of ``ETF_2_Code.ipynb``. Given a folder, every ``*Playbook YYYY-MM-DD.pdf`` in it
is parsed with the same logic as the notebook (one function per notebook stage — see the
function docstrings for the originating cells), the resulting rows are appended to
``Trading.ETF_Options_v1`` (dates already present in the table are skipped unless ``--force``),
and physical page 2's market commentary ("Market Expectations ..." block) is posted to llmwiki's ``POST /ingest`` with
the date in the title and body.

    python playbook_etf.py /path/to/pdfs [--since 2026-09-01] [--dry-run] ...

Configuration comes from ``DB_Config.env`` (``--env-file``); see ``.env.example`` for every key.
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

# "912 Playbook 2026-09-07.pdf" (Google-Drive naming) or "Playbook-2026-09-07.pdf" (repo sample)
PDF_NAME_RE = re.compile(r"Playbook[ -](\d{4}-\d{2}-\d{2})\.pdf$", re.IGNORECASE)
SECTION_MARKER = "Trade & Maintenance Suggestions"
COMMENTARY_SPLIT_MARKER = "Copyright"
COMMENTARY_START_MARKER = "Market Expectations"

ROW_COLUMNS = ["Type", "Symbol", "Trend", "Status", "Expiration", "PnC", "L_Strike", "H_Strike",
               "Entry", "Target", "Stop", "quantity"]
OUTPUT_COLUMNS = ["Date", "Type", "Trend", "Symbol", "Status", "Expiration", "PnC", "Low-High",
                  "L_Strike", "H_Strike", "Entry_Sign", "Entry", "Target_Sign", "Target",
                  "Stop_Sign", "Stop", "quantity"]

NUM_RE = r"\d+(\.)?\d+"          # the notebook's number matcher (needs >= 2 digits), kept as-is
EXP_RE = r"\((\d{1,2}/\d{1,2}/\d{2,4})\s*exp\)"
ACTION_RE = r"\b(BTO|STO)\b\s*(\d+)?"
NET_ENTRY_RE = r"Entry of a net\s+([\d.]+)\s+(Credit|Debit)"

log = logging.getLogger("playbook_etf")


# --------------------------------------------------------------------------------------------
# 1. Locate PDFs
# --------------------------------------------------------------------------------------------
def find_playbook_pdfs(folder: Path) -> list[tuple[str, Path]]:
    """Return ``[(date_str, path), ...]`` for every Playbook PDF in ``folder``, oldest first."""
    found = []
    for p in folder.iterdir():
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
                if SECTION_MARKER in lines[i] or COMMENTARY_SPLIT_MARKER in lines[i]), len(lines))
    commentary = "\n".join(lines[start:end]).strip()
    table = "\n".join(lines[:start] + lines[end:])
    return table, commentary


# --------------------------------------------------------------------------------------------
# 3. Parse trade rows (notebook cells 5, 7, 9)
# --------------------------------------------------------------------------------------------
def _new_row() -> dict:
    return {c: (1 if c == "quantity" else "") for c in ROW_COLUMNS}


def _numbers(text: str) -> list[str]:
    return [m[0] for m in re.finditer(NUM_RE, text)]


def parse_trades(table_text: str) -> pd.DataFrame:
    """Turn the concatenated trade-table text into one row per ``STO``/``BTO`` leg.

    Same branch structure as the notebook's main loop; the only structural change is that rows
    are collected as dicts and turned into a DataFrame once at the end (the notebook wrote into
    a pre-seeded DataFrame with chained assignment, which pandas >= 2 no longer supports).
    Per-line exceptions are logged and skipped, exactly as in the notebook.
    """
    lines = table_text.split("\n")
    # Pre-scan: parsing starts at the first section header (notebook cell 7).
    start = next((i for i, l in enumerate(lines) if SECTION_MARKER in l), None)
    if start is None:
        raise ValueError(f"no '{SECTION_MARKER}' header found in extracted text")

    rows: list[dict] = []
    type_name = None
    symbol = None
    status = None
    trend = None
    line_num = -1
    group_start = 0      # index into ``rows`` of the current Trend group (for combo broadcast)
    is_combo = False     # set once an "Entry of a net ... Credit/Debit" line is seen

    for line in lines[start:]:
        try:
            if line.find(SECTION_MARKER) >= 0:
                type_name = line[:line.find(SECTION_MARKER)].strip()
                log.debug("Type_name = %s", type_name)
                continue
            if line.strip() == "" or line.find("Aggressive Traders:") > -1:
                continue
            if line.find("Trend") == -1 and re.search(r"\[\w*\]", line):
                symbol = re.findall(r"\[\w*\]", line)[0][1:-1]
                line_num = 0
                log.debug("Symbol = %s", symbol)
                continue
            if line_num == 0 or line.find("Trend") > 0:
                trend = re.findall(r"Investor|Trader", line)[0]
                status = re.findall(r"\[\S*\]", line)[0][1:-1]
                line_num = 1
                group_start = len(rows)
                is_combo = False
            elif line.find("STO") > -1 or line.find("BTO") > -1:
                row = _new_row()
                rows.append(row)          # appended first so a partial parse still keeps the row
                row["Type"] = type_name
                row["Symbol"] = symbol
                row["Trend"] = trend
                row["Status"] = status
                row["PnC"] = re.findall(r"Put|Call", line)[0][0]
                # BTO=Long/Buy, STO=Short/Sell; trailing number is the contract count, signed
                # by direction (STO 2 -> -2).
                action_match = re.search(ACTION_RE, line)
                action = action_match.group(1)
                qty = int(action_match.group(2)) if action_match.group(2) else 1
                row["quantity"] = qty if action == "BTO" else -qty
                row["Expiration"] = re.findall(EXP_RE, line)[0]
                # Strikes come from the text *after* the action/count prefix so a leading
                # contract count is never mistaken for a strike.
                strikes = _numbers(line[action_match.end():].split("(")[0])
                row["L_Strike"] = strikes[0] if len(strikes) == 2 else ""
                row["H_Strike"] = strikes[1] if len(strikes) == 2 else strikes[0]
            elif line.find("Entry of a net") > -1:
                # Multi-leg combo: one net Entry (negative for Credit) broadcast to every leg
                # parsed since the current Trend group started.
                is_combo = True
                net_match = re.search(NET_ENTRY_RE, line, re.IGNORECASE)
                net_val = float(net_match.group(1))
                entry_val = -net_val if net_match.group(2).lower() == "credit" else net_val
                target_val = None
                if "," in line:
                    trgt = _numbers(line.split(",")[1])
                    if trgt:
                        target_val = trgt[0]
                for r in rows[group_start:]:
                    r["Entry"] = entry_val
                    if target_val is not None:
                        r["Target"] = target_val
            elif line.find("Entry @ ") > -1:
                rows[-1]["Entry"] = _numbers(line.split(",")[0])[0]
                rows[-1]["Target"] = _numbers(line.split(",")[1])[0]
            elif line.find("Stop") > -1:
                stop = line.split("@", 2)[2] if line.count("@") > 1 else line.split("@")[1]
                stp = _numbers(stop)[0]
                for r in (rows[group_start:] if is_combo else rows[-1:]):
                    r["Stop"] = stp
        except Exception as e:  # noqa: BLE001 - mirror the notebook: log the line, keep going
            log.warning("Exception on line: %r => %r", line, e)

    return pd.DataFrame(rows, columns=ROW_COLUMNS)


# --------------------------------------------------------------------------------------------
# 4. Derived columns (notebook cells 11, 14-17)
# --------------------------------------------------------------------------------------------
def _same_group(df: pd.DataFrame, i: int, j: int) -> bool:
    return (df.at[i, "Type"] == df.at[j, "Type"] and df.at[i, "Symbol"] == df.at[j, "Symbol"]
            and df.at[i, "Trend"] == df.at[j, "Trend"])


def derive_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``Low``/``High``/``Low-High``, the three sign columns, and back-fill ``Stop``.

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
    if not isinstance(value, str):
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
    csv_df = pd.concat([investor, trader], ignore_index=True, sort=False).fillna("")

    db_df = csv_df.replace(r"^\s*$", np.nan, regex=True)
    # "Low-High" is a NOT NULL column in Trading.ETF_Options_v1, but it's legitimately blank
    # for strike-less rows (e.g. "Major US Market" index-summary lines with no option leg) —
    # unlike Expiration/PnC/L_Strike/H_Strike/Entry/Target/Stop, which are nullable. Undo the
    # blanket NaN-ification above for this one column so those rows insert as "" instead of
    # tripping a "Column 'Low-High' cannot be null" IntegrityError.
    db_df["Low-High"] = csv_df["Low-High"]
    db_df["Date"] = pd.to_datetime(db_df["Date"], format="%Y-%m-%d")
    db_df["Expiration"] = db_df["Expiration"].map(_parse_expiration)
    db_df = db_df.astype({"L_Strike": "float", "H_Strike": "float", "Target": "float",
                          "Entry": "float", "Stop": "float", "quantity": "int"})
    return csv_df, db_df


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


def store(db_df: pd.DataFrame, db: str, table: str) -> None:
    log.info("Appending %d rows to %s.%s", len(db_df), db, table)
    DU.StoreEOD(db_df, db, table)   # logs (does not raise) on failure


# --------------------------------------------------------------------------------------------
# 9. Per-PDF driver
# --------------------------------------------------------------------------------------------
def process_pdf(date_str: str, pdf_path: Path, args: argparse.Namespace, db: str, table: str) -> None:
    out_dir = Path(args.out_dir) if args.out_dir else pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / (pdf_path.stem + ".csv")
    commentary_path = out_dir / (pdf_path.stem + "_commentary.txt")

    table_text, commentary_text = extract_text(pdf_path)
    df = parse_trades(table_text)
    df = derive_columns(df)
    csv_df, db_df = finalize(df, date_str)

    csv_df.to_csv(csv_path, sep=",", index=None)
    log.info("%s: parsed %d rows -> %s", pdf_path.name, len(csv_df), csv_path)

    if commentary_text:
        commentary_path.write_text(commentary_text)
        log.info("%s: saved market commentary -> %s", pdf_path.name, commentary_path)
    else:
        log.warning("%s: no market commentary captured from page 2.", pdf_path.name)

    if args.skip_db:
        log.info("%s: --skip-db, not uploading %d rows.", pdf_path.name, len(db_df))
    else:
        store(db_df, db, table)

    if args.skip_llmwiki:
        log.info("%s: --skip-llmwiki, not sending commentary.", pdf_path.name)
    elif commentary_text:
        upload_commentary(commentary_text, date_str)


# --------------------------------------------------------------------------------------------
# 10. CLI
# --------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("folder", type=Path, help="folder to scan for '*Playbook YYYY-MM-DD.pdf'")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only process PDFs dated on/after this")
    p.add_argument("--force", action="store_true",
                   help="process dates already in the DB (rows are appended, not replaced)")
    p.add_argument("--skip-db", action="store_true", help="parse and write CSV only, no DB upload")
    p.add_argument("--skip-llmwiki", action="store_true", help="do not send commentary to llmwiki")
    p.add_argument("--dry-run", action="store_true", help="same as --skip-db --skip-llmwiki")
    p.add_argument("--env-file", default=".env", help="dotenv file (default: DB_Config.env)")
    p.add_argument("--out-dir", help="where to write CSV/commentary files (default: next to the PDF)")
    p.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ... (default: INFO)")
    args = p.parse_args(argv)
    if args.dry_run:
        args.skip_db = args.skip_llmwiki = True
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not load_dotenv(args.env_file):
        log.warning("env file %s not found; relying on the process environment.", args.env_file)
    db = os.environ.get("DBTRADING", "Trading")
    table = os.environ.get("TBLETFOPTIONS", "ETF_Options_v1")

    if not args.folder.is_dir():
        log.error("%s is not a directory", args.folder)
        return 2
    pdfs = find_playbook_pdfs(args.folder)
    if args.since:
        pdfs = [(d, p) for d, p in pdfs if d >= args.since]
    if not pdfs:
        log.warning("No Playbook PDFs found in %s", args.folder)
        return 0
    log.info("Found %d Playbook PDF(s) in %s", len(pdfs), args.folder)

    done = set() if (args.skip_db or args.force) else loaded_dates(db, table)

    processed = 0
    skipped: list[tuple[str, str]] = []   # (filename, one-line reason)
    failed: list[tuple[str, str]] = []
    for date_str, pdf_path in pdfs:
        if date_str in done:
            reason = f"date {date_str} already in {db}.{table} (use --force to re-load)"
            log.info("%s: %s, skipping", pdf_path.name, reason)
            skipped.append((pdf_path.name, reason))
            continue
        try:
            process_pdf(date_str, pdf_path, args, db, table)
            processed += 1
        except Exception as e:  # noqa: BLE001
            log.error("%s: failed", pdf_path.name, exc_info=True)
            failed.append((pdf_path.name, _one_line(e)))
    log.info("Done: %d processed, %d skipped, %d failed", processed, len(skipped), len(failed))
    for name, reason in skipped:
        log.info("  skipped: %s — %s", name, reason)
    for name, reason in failed:
        log.error("  failed:  %s — %s", name, reason)
    return 1 if failed else 0


def _one_line(e: BaseException) -> str:
    """``ValueError: time data ... doesn't match`` — type + first line of the message."""
    msg = str(e).strip().split("\n", 1)[0]
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


if __name__ == "__main__":
    sys.exit(main())
