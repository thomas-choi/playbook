#!/usr/bin/env python
"""Parse-confidence reporting and an LLM repair pass for the Playbook/Top20 PDF parsers.

The regex parsers in ``playbook_etf.py`` / ``top20_stock.py`` stay the primary path. They record
what confused them in a :class:`ParseReport` (one entry per *block* — a ``[SYMBOL]`` section, or a
``[SYMBOL]`` + Trend group for the Playbook), and only those flagged blocks are sent to an LLM for
re-interpretation, together with the rows the regexes already produced.

The model is reached through the **OpenAI-standard** chat-completions API, so any compatible
endpoint works (OpenAI, a gateway, or a local ollama / vLLM / LM Studio server)::

    LLM_BASE_URL=http://localhost:11434/v1   # or https://api.openai.com/v1
    LLM_API_KEY=...                          # ignored by most local servers
    LLM_MODEL=qwen2.5:32b                    # any model id the endpoint serves
    LLM_TEMPERATURE=0  LLM_TIMEOUT=60  LLM_MAX_RETRY=3

Repaired rows are accepted only after :func:`validate_rows` passes: schema-valid, symbol and every
number present verbatim in the block text, parseable expiration. Otherwise the regex rows are kept.
Every call and its verdict is written to ``<stem>.repairs.json``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("llm_repair")

# A line that looks like trade content: if one of these is seen and nothing comes of it, the block
# is flagged (the parser read a line it should have understood).
CONTENT_HINT_RE = re.compile(
    r"\b(BTO|STO|BTC|STC)\b|Entry\s*@|Entry of a net|Target\s*@|\bStop\b|\bExit\b|Trend\s*\[|\[\w+\]",
    re.IGNORECASE)

NUMBER_IN_TEXT_RE = re.compile(r"\d*\.?\d+")

# "conditional Target @ WMT 114.45", "Exit to BTC Naked Call @ UBER 27.87 Stop"
AT_TICKER_RE = re.compile(r"@\s*([A-Z]{1,6})\s+[\d.]")


# --------------------------------------------------------------------------------------------
# Parse report
# --------------------------------------------------------------------------------------------
@dataclass
class Missing:
    """A required column the regexes could not read, labelled where they gave up.

    ``suggest`` is the parser's candidate and ``source`` says where it came from, so the person
    asked at ``hil_review.confirm_missing`` can accept it with one keystroke instead of reading the
    value out of the block themselves. ``answer`` is what they chose.
    """
    field: str
    reason: str
    suggest: str = ""
    source: str = ""
    answer: str = ""
    answered_by: str = ""

    def as_dict(self) -> dict:
        d = {"field": self.field, "reason": self.reason}
        for k in ("suggest", "source", "answer", "answered_by"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


def ticker_candidate(text: str) -> str:
    """The ticker in a ``@ WMT 114.45`` / ``@ UBER 27.87 Stop`` reference, or ``""``.

    Both publications print the underlying's symbol beside a conditional price, which is the only
    place a section names its ticker when the heading does not (``Walmart Inc
    [Bullish/Countertrend]``, or a Top20 stock heading whose ``[TICKER]`` the PDF dropped).
    """
    m = AT_TICKER_RE.search(text)
    return m.group(1) if m else ""


@dataclass
class Flag:
    """One thing the parser could not do with a line."""
    kind: str          # exception | unconsumed | incomplete | unknown_header
    line: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "line": self.line.strip(), "detail": self.detail}


@dataclass
class Block:
    """A ``[SYMBOL]`` (+ Trend) section of the extracted text and the rows parsed out of it."""
    block_id: str
    symbol: str = ""
    trend: str = ""
    header_line: int = -1        # index of the "Company Name [SYM]" line, -1 if unknown
    start: int = 0               # first line index of the block
    end: int = 0                 # one past the last line index
    row_start: int = 0           # first index into the parser's row list
    row_end: int = 0             # one past the last row index
    closed: bool = False         # its end line is known, not just "wherever the next block starts"
    flags: list[Flag] = field(default_factory=list)
    repair: str = ""             # "", "accepted", "rejected: <reason>", "unavailable"
    decision: str = ""           # a human answer from hil_review, e.g. "keep_regex", "no_value"
    # {column: Missing} — required columns the parser could not read, labelled at the line where
    # it gave up and answered by confirm_missing.
    missing: dict[str, Missing] = field(default_factory=dict)

    @property
    def unanswered(self) -> list[str]:
        """Labelled columns still without an answer, in the order they were labelled."""
        return [f for f, m in self.missing.items() if not m.answer]

    @property
    def flagged(self) -> bool:
        """Something to ask about: a line the parser could not use, or a column it never read."""
        return bool(self.flags) or bool(self.unanswered)

    @property
    def settled(self) -> bool:
        """Nothing left to ask: not flagged, repaired and accepted, or decided by a person."""
        if self.unanswered:
            return False      # no repair or decision can stand in for a column nobody supplied
        return not self.flags or self.repair == "accepted" or bool(self.decision)

    def answers(self) -> dict[str, str]:
        """``{column: answer}`` for the labels a person has answered."""
        return {f: m.answer for f, m in self.missing.items() if m.answer}

    def text(self, lines: list[str]) -> str:
        kept = [l.rstrip() for l in lines[self.start:self.end]]
        while kept and not kept[-1].strip():      # the last block of a page runs into blank lines
            kept.pop()
        body = "\n".join(kept).strip("\n")
        if 0 <= self.header_line < self.start:
            body = lines[self.header_line].rstrip() + "\n" + body
        return body

    def as_dict(self) -> dict:
        d = {"block": self.block_id, "symbol": self.symbol, "trend": self.trend,
             "lines": [self.start, self.end], "rows": [self.row_start, self.row_end],
             "repair": self.repair, "decision": self.decision,
             "flags": [f.as_dict() for f in self.flags]}
        if self.missing:
            d["missing"] = {f: m.as_dict() for f, m in self.missing.items()}
        return d


class ParseReport:
    """Blocks the parser walked through, and the flags raised inside each of them."""

    def __init__(self, source: str):
        self.source = source
        self.blocks: list[Block] = []
        self.orphans: list[Flag] = []     # flags raised outside any block

    # -- building ----------------------------------------------------------------------------
    def open_block(self, block_id: str, *, symbol: str = "", trend: str = "",
                   header_line: int = -1, start: int = 0, row_start: int = 0) -> Block:
        self.close_block(end=start, row_end=row_start)
        # Some issues repeat a section (a stale boilerplate block beside the current one), so the
        # id has to stay unique for the logs and the review queue: "IWM/Trader", "IWM/Trader#2".
        taken = sum(1 for b in self.blocks if b.block_id.split("#")[0] == block_id)
        if taken:
            block_id = f"{block_id}#{taken + 1}"
        b = Block(block_id=block_id, symbol=symbol, trend=trend, header_line=header_line,
                  start=start, end=start, row_start=row_start, row_end=row_start)
        self.blocks.append(b)
        return b

    def close_block(self, *, end: int, row_end: int, final: bool = False) -> None:
        """Extend the open block to ``end``/``row_end``.

        ``final=True`` is the parser saying "this line ends the block" (a section header, the next
        symbol heading): the block keeps that end even though the next block opens further down, so
        its text — and therefore its decision fingerprint — does not swallow the following section.
        """
        if not self.blocks or self.blocks[-1].closed:
            return
        b = self.blocks[-1]
        b.end = max(b.end, end)
        b.row_end = max(b.row_end, row_end)
        b.closed = final

    @property
    def current(self) -> Block | None:
        return self.blocks[-1] if self.blocks else None

    def flag(self, kind: str, line: str, detail: str = "") -> None:
        f = Flag(kind, line, detail)
        (self.current.flags if self.current else self.orphans).append(f)

    def mark_missing(self, field_name: str, reason: str, *, suggest: str = "",
                     source: str = "") -> Missing | None:
        """Label the open block as missing a required column; ``None`` if no block is open.

        Called where the regexes give up, not afterwards: only the parser knows *why* the column
        is not there, and the reason is what the person asked for it reads first.
        """
        if self.current is None:
            return None
        m = Missing(field=field_name, reason=reason, suggest=suggest, source=source)
        self.current.missing[field_name] = m
        return m

    def missing_blocks(self) -> list[Block]:
        """Blocks with a labelled column nobody has answered yet."""
        return [b for b in self.blocks if b.unanswered]

    # -- reading -----------------------------------------------------------------------------
    def flagged_blocks(self) -> list[Block]:
        """Blocks with a flag and no recorded human decision — what the LLM is asked about."""
        return [b for b in self.blocks if b.flagged and not b.decision]

    def unsettled_blocks(self) -> list[Block]:
        return [b for b in self.blocks if not b.settled]

    def replace_rows(self, rows: list[dict], block: Block, new_rows: list[dict]) -> None:
        """Splice ``new_rows`` into ``block``'s slice of ``rows``.

        A repair or a human decision can change how many rows a block has, which moves every later
        block's slice — without this shift, the indices in ``.parse-report.json`` and the next
        review would point at the wrong rows.
        """
        old_len = block.row_end - block.row_start
        rows[block.row_start:block.row_end] = new_rows
        block.row_end = block.row_start + len(new_rows)
        delta = len(new_rows) - old_len
        if delta:
            for b in self.blocks[self.blocks.index(block) + 1:]:
                b.row_start += delta
                b.row_end += delta

    def decided(self) -> list[Block]:
        return [b for b in self.blocks if b.decision]

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for f in [f for b in self.blocks for f in b.flags] + self.orphans:
            c[f.kind] = c.get(f.kind, 0) + 1
        return c

    def summary(self) -> str:
        c = self.counts()
        miss = sum(len(b.unanswered) for b in self.blocks)
        return (f"{len(self.blocks)} blocks, {len([b for b in self.blocks if b.flagged])} "
                f"flagged, {len(self.unsettled_blocks())} unsettled"
                + (f", {miss} column(s) missing" if miss else "")
                + (f" ({', '.join(f'{k}={v}' for k, v in sorted(c.items()))})" if c else ""))

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(
            {"source": self.source, "counts": self.counts(), "summary": self.summary(),
             "unsettled": [b.block_id for b in self.unsettled_blocks()],
             "orphan_flags": [f.as_dict() for f in self.orphans],
             "blocks": [b.as_dict() for b in self.blocks]}, indent=2))


# --------------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------------
def spec_path(name: str) -> Path:
    """Extraction spec shared with the Claude Code skill (``LLM_SPEC_<NAME>`` overrides)."""
    env = os.environ.get(f"LLM_SPEC_{name.upper()}")
    if env:
        return Path(env)
    return (Path(__file__).resolve().parent / ".claude" / "skills" / f"lcr-{name}-pdf"
            / "reference" / f"{name}-extraction-spec.md")


def load_spec(name: str) -> str:
    p = spec_path(name)
    if not p.is_file():
        log.warning("extraction spec %s not found; sending a minimal instruction instead.", p)
        return ("You convert option-trade newsletter text into rows. Return only values present "
                "in the text; never invent a number.")
    return p.read_text()


def client():
    """OpenAI-standard client, or ``None`` when no endpoint is configured / SDK is missing."""
    base_url = os.environ.get("LLM_BASE_URL")
    if not base_url:
        log.info("LLM_BASE_URL not set; skipping LLM repair.")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("the 'openai' package is not installed; skipping LLM repair "
                    "(pip install -r requirements.txt).")
        return None
    return OpenAI(base_url=base_url, api_key=os.environ.get("LLM_API_KEY") or "not-needed",
                  timeout=float(os.environ.get("LLM_TIMEOUT", "60")))


def model_name() -> str:
    return os.environ.get("LLM_MODEL", "gpt-4o-mini")


# --------------------------------------------------------------------------------------------
# Repair
# --------------------------------------------------------------------------------------------
def _schema_envelope(row_schema: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["rows"],
            "properties": {"rows": {"type": "array", "items": row_schema}}}


def _prompt(block: Block, block_text: str, regex_rows: list[dict], columns: list[str],
            row_schema: dict) -> str:
    flags = "\n".join(f"- [{f.kind}] {f.line.strip()}" + (f"  ({f.detail})" if f.detail else "")
                      for f in block.flags) or "- (none; full re-read requested)"
    return (
        f"The text below is one block of a trade newsletter (symbol {block.symbol or '?'}"
        + (f", {block.trend} Trend" if block.trend else "") + ").\n\n"
        "--- BLOCK TEXT ---\n" + block_text + "\n--- END BLOCK TEXT ---\n\n"
        "A regex parser produced these rows from it:\n"
        + json.dumps(regex_rows, indent=2) + "\n\n"
        "It reported these problems:\n" + flags + "\n\n"
        "Re-read the block text and return the COMPLETE, corrected set of rows for this block "
        f"(not just the problem lines), as {{\"rows\": [...]}} with these keys in each row: "
        + ", ".join(columns) + ".\n"
        "Rules: one row per option leg; use \"\" for a value the text does not state; copy numbers "
        "exactly as written in the text; never invent a value. Return JSON only.\n\n"
        "JSON schema for one row:\n" + json.dumps(row_schema))


def repair_block(cli, spec: str, block: Block, block_text: str, regex_rows: list[dict],
                 columns: list[str], row_schema: dict) -> list[dict] | None:
    """Ask the model for this block's rows. Returns ``None`` when the call or the JSON failed."""
    envelope = _schema_envelope(row_schema)
    messages = [{"role": "system", "content": spec},
                {"role": "user", "content": _prompt(block, block_text, regex_rows, columns,
                                                    row_schema)}]
    kwargs = dict(model=model_name(), messages=messages,
                  temperature=float(os.environ.get("LLM_TEMPERATURE", "0")))
    formats = [{"type": "json_schema",
                "json_schema": {"name": "rows", "strict": True, "schema": envelope}},
               {"type": "json_object"}]
    max_retry = int(os.environ.get("LLM_MAX_RETRY", "3"))
    for attempt in range(1, max_retry + 1):
        fmt = formats[min(attempt - 1, len(formats) - 1)]
        try:
            resp = cli.chat.completions.create(response_format=fmt, **kwargs)
            content = resp.choices[0].message.content or ""
            data = json.loads(_strip_fences(content))
            rows = data.get("rows") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError(f"expected a list of rows, got {type(rows).__name__}")
            return rows
        except Exception as e:  # noqa: BLE001 - any failure falls back to the regex rows
            log.warning("LLM repair attempt %d/%d failed for %s: %s", attempt, max_retry,
                        block.block_id, _one_line(e))
            if attempt < max_retry:
                time.sleep(min(2 * attempt, 10))
    return None


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _one_line(e: BaseException) -> str:
    msg = str(e).strip().split("\n", 1)[0]
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------
def _number_in_text(value: str, text: str) -> bool:
    """Is ``value`` one of the numbers actually printed in ``text``? (``.25`` == ``0.25``)"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    # abs(): a net Credit entry is stored negative (-0.15) but printed as ".15".
    return any(abs(abs(float(m.group())) - abs(v)) < 1e-9
               for m in NUMBER_IN_TEXT_RE.finditer(text))


def validate_rows(rows: list[dict], row_schema: dict, block_text: str, columns: list[str],
                  numeric_fields: tuple[str, ...]) -> tuple[bool, str]:
    """Schema + semantic gates. Returns ``(ok, reason)``; ``reason`` is empty when ok."""
    if not rows:
        return False, "no rows returned"
    try:
        import jsonschema
        for r in rows:
            jsonschema.validate(r, row_schema)
    except ImportError:
        log.debug("jsonschema not installed; skipping schema validation.")
    except Exception as e:  # noqa: BLE001 - jsonschema.ValidationError and friends
        return False, f"schema: {_one_line(e)}"

    lowered = block_text.lower()
    for i, r in enumerate(rows):
        missing = [c for c in columns if c not in r]
        if missing:
            return False, f"row {i}: missing keys {missing}"
        if r.get("PnC") not in ("P", "C", ""):
            return False, f"row {i}: PnC={r.get('PnC')!r}"
        sym = str(r.get("Symbol", "")).strip()
        if sym and sym.lower() not in lowered:
            return False, f"row {i}: symbol {sym!r} is not in the block text"
        for fld in numeric_fields:
            val = str(r.get(fld, "") or "").strip()
            if val and not _number_in_text(val, block_text):
                return False, f"row {i}: {fld}={val!r} is not a number printed in the block text"
        exp = str(r.get("Expiration", "") or "").strip()
        if exp and not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", exp):
            return False, f"row {i}: Expiration={exp!r} is not M/D/YY"
    return True, ""


# --------------------------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------------------------
class RepairLog:
    """``<stem>.repairs.json`` — every block sent, what came back, and whether it was taken."""

    def __init__(self, source: str):
        self.source = source
        self.entries: list[dict] = []

    def add(self, block: Block, before: list[dict], after: list[dict] | None, verdict: str,
            reason: str = "") -> None:
        self.entries.append({"block": block.block_id, "symbol": block.symbol,
                             "trend": block.trend, "verdict": verdict, "reason": reason,
                             "flags": [f.as_dict() for f in block.flags],
                             "rows_before": before, "rows_after": after})
        block.repair = verdict if not reason else f"{verdict}: {reason}"

    @property
    def accepted(self) -> int:
        return sum(1 for e in self.entries if e["verdict"] == "accepted")

    @property
    def rejected(self) -> int:
        return sum(1 for e in self.entries if e["verdict"] == "rejected")

    @property
    def unavailable(self) -> int:
        """Flagged blocks nothing was asked about — no endpoint configured, no client."""
        return sum(1 for e in self.entries if e["verdict"] == "unavailable")

    @property
    def unrepaired(self) -> int:
        return self.rejected + self.unavailable

    def write(self, path: Path) -> None:
        path.write_text(json.dumps({"source": self.source, "model": model_name(),
                                    "endpoint": os.environ.get("LLM_BASE_URL", ""),
                                    "accepted": self.accepted, "rejected": self.rejected,
                                    "unavailable": self.unavailable,
                                    "calls": self.entries}, indent=2))


# --------------------------------------------------------------------------------------------
# Scratch artefacts
# --------------------------------------------------------------------------------------------
def clean_scratch(paths: list[Path]) -> list[Path]:
    """Delete a run's rebuildable debugging artefacts; returns the ones that went.

    Only the files the *next* run of the same code recreates byte for byte belong here — the
    extracted text and the parse report. Not the CSV (the record of what loaded), not
    ``.repairs.json`` (an LLM call is not reproducible), and never the decisions store, which is
    the one artefact nothing can recompute. Callers decide *when* it is safe: on a run that did
    not finish its job these files are the evidence, so they stay.
    """
    gone = []
    for p in paths:
        try:
            p.unlink()
            gone.append(p)
        except FileNotFoundError:
            pass
        except OSError as e:  # noqa: BLE001 - tidying up must not fail a finished load
            log.warning("could not remove %s (%s)", p, _one_line(e))
    return gone
