#!/usr/bin/env python
"""Batch-process LCR Top20 PDFs: parse per-stock levels, load to MySQL, send commentary to llmwiki.

Script version of ``Top20_2_Code.ipynb`` (the Top20 counterpart of ``playbook_etf.py``). Given a
folder, every ``*Top20 YYYY-MM-DD.pdf`` in it is parsed with the same logic as the notebook (one
function per notebook stage — see the function docstrings for the originating cells), the
resulting rows are appended to ``Trading.Stock_Options`` (dates already present in the table are
skipped unless ``--force``), and the market commentary on the printed page 1 (physical page 2:
"Market Expectations ..." block) is posted to llmwiki's ``POST /ingest`` with the date in the
title and body.

    python top20_stock.py /path/to/pdfs [--since 2025-01-01] [--dry-run] ...

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

# "912 Top20 2025-07-21.pdf" / "LCR Top20 2022-11-21.pdf" (Google-Drive naming) or "Top20-2025-07-21.pdf"
PDF_NAME_RE = re.compile(r"Top20[ -](\d{4}-\d{2}-\d{2})\.pdf$", re.IGNORECASE)
COMMENTARY_START_MARKER = "Market Expectations"
COMMENTARY_END_RE = re.compile(r"Vol\s+\d+\s+Issue\s+\d+")   # "<date>  912 Top 20  Vol N Issue N Page 1" footer

ROW_COLUMNS = ["Symbol", "Status", "Expiration", "PnC", "Price", "Entry1", "Entry2", "Target", "Stop"]
OUTPUT_COLUMNS = ["Date", "Symbol", "Status", "Expiration", "PnC", "Strike", "Entry_Sign", "Entry1",
                  "Entry2", "Target_Sign", "Target", "Stop_Sign", "Stop"]

NUM_RE = r"\d+(\.)?\d+"          # the notebook's number matcher (needs >= 2 digits), kept as-is

log = logging.getLogger("top20_stock")


# --------------------------------------------------------------------------------------------
# 1. Locate PDFs
# --------------------------------------------------------------------------------------------
def find_top20_pdfs(folder: Path) -> list[tuple[str, Path]]:
    """Return ``[(date_str, path), ...]`` for every Top20 PDF in ``folder``, oldest first."""
    found = []
    for p in folder.iterdir():
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
def _numbers(text: str) -> list[str]:
    return [m[0] for m in re.finditer(NUM_RE, text)]


def parse_stocks(stock_text: str) -> pd.DataFrame:
    """Turn the concatenated page-3+ text into one row per stock (its ``Entry @`` line).

    Same branch structure as the notebook's main loop; the only structural changes are that rows
    are collected as dicts and turned into a DataFrame once at the end (the notebook wrote into
    a pre-seeded DataFrame with chained assignment, which pandas >= 2 no longer supports), and
    that a line which fails to parse is logged and skipped instead of aborting the run.
    """
    iterates = iter(stock_text.split("\n"))
    rows: list[dict] = []
    symbol = None
    status = None
    for line in iterates:
        try:
            if line.strip() == "":
                continue
            if re.search(r"\[\w*\]", line):
                symbol = re.findall(r"\[\w*\]", line)[0][1:-1]
                # the line after "Company Name [SYM]" is the status, e.g. "Perform/Buy/Hold"
                status = re.findall(r"\S*", next(iterates))[0]
                log.debug("Symbol = %s, Status = %s", symbol, status)
            elif line.find("Aggressive Traders:") > -1:
                pass
            elif line.find("Entry @") > -1 or line.find("range of") > -1:
                ent = _numbers(line.split("@")[1])
                # The notebook appended the row before reading the numbers; here the row is only
                # added once the entry range has parsed, so a "range of" description line without
                # an "@" (a warning below) never leaves an empty row behind.
                rows.append({c: "" for c in ROW_COLUMNS})
                rows[-1]["Symbol"] = symbol
                rows[-1]["Status"] = status
                rows[-1]["Entry1"] = ent[0]
                rows[-1]["Entry2"] = ent[1]
            elif line.find("Target @") > -1:   # no trailing space: some issues print "Target @250.00"
                rows[-1]["Target"] = _numbers(line.split("@")[1])[0]
            elif line.find("STO") > -1 or line.find("BTO") > -1:
                # e.g. "STO August 43 Naked Put (8/15/25 exp) using"
                rows[-1]["PnC"] = re.findall(r"Put|Call", line)[0][0]
                rows[-1]["Expiration"] = re.findall(r"\(...*\)", line)[0][1:-4].strip()
                rows[-1]["Price"] = _numbers(line.split("(")[0])[0]
                log.debug("%s: %s", rows[-1]["Symbol"], line.strip())
            elif line.find("Stop") > -1:
                # e.g. "conditional position Exit to BTC Naked Put @ BAC 40.91 Stop"
                rows[-1]["Stop"] = _numbers(line.split("@")[1])[0]
        except Exception as e:  # noqa: BLE001 - log the line, keep going
            log.warning("Exception on line: %r => %r", line, e)
    return pd.DataFrame(rows, columns=ROW_COLUMNS)


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
    if not isinstance(value, str):
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

    stock_text, commentary_text = extract_text(pdf_path)
    df = parse_stocks(stock_text)
    if df.empty:
        raise ValueError("no stock rows parsed (no 'Entry @' lines found on pages 3+)")
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
    p.add_argument("folder", type=Path, help="folder to scan for '*Top20 YYYY-MM-DD.pdf'")
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
    table = os.environ.get("TBLSTOCKOPTIONS", "Stock_Options")

    if not args.folder.is_dir():
        log.error("%s is not a directory", args.folder)
        return 2
    pdfs = find_top20_pdfs(args.folder)
    if args.since:
        pdfs = [(d, p) for d, p in pdfs if d >= args.since]
    if not pdfs:
        log.warning("No Top20 PDFs found in %s", args.folder)
        return 0
    log.info("Found %d Top20 PDF(s) in %s", len(pdfs), args.folder)

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
