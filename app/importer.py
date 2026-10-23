"""Credit card CSV import using saved, named CSV formats.

Banks export CSVs in different shapes: some have one signed amount column,
others split debits and credits; some have no header row at all (TD, for
example). A saved format says where to find each field. Formats are shared:
several cards can use one, and a card can use several. An uploaded file is
matched to a format by its header row (or column count when headerless).
Without a header, columns are referred to by 1-based position ("1", "2", ...).
"""

import csv
import hashlib
import io
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%m/%d/%y",
    "%d-%b-%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%Y%m%d",
]

HEADER_HINTS = {
    "date_col": ["transaction date", "trans date", "date", "posted date", "posting date"],
    "description_col": ["description", "merchant", "payee", "details", "name", "memo"],
    "amount_col": ["amount", "cad$", "usd$", "amount (cad)", "amount (usd)"],
    "debit_col": ["debit", "withdrawal", "charges", "purchases"],
    "credit_col": ["credit", "deposit", "payments", "credits"],
}


@dataclass
class Mapping:
    date_col: str
    description_col: str
    amount_col: str | None = None
    debit_col: str | None = None
    credit_col: str | None = None
    date_format: str | None = None
    charges_positive: bool = True
    has_header: bool = True

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Mapping":
        return cls(
            date_col=row["date_col"],
            description_col=row["description_col"],
            amount_col=row["amount_col"] or None,
            debit_col=row["debit_col"] or None,
            credit_col=row["credit_col"] or None,
            date_format=row["date_format"] or None,
            charges_positive=bool(row["charges_positive"]),
            has_header=bool(row["has_header"]),
        )


@dataclass
class ParsedRow:
    txn_date: date
    description: str
    amount_cents: int  # positive = expense


class CsvFormatError(ValueError):
    pass


def read_rows(text: str, has_header: bool) -> tuple[list[str], list[dict[str, str]]]:
    """Return (column names, rows as dicts). Columns are numbered when there is no header."""
    text = text.lstrip("﻿")
    raw = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not raw:
        return [], []
    if has_header:
        header = [h.strip() for h in raw[0]]
        body = raw[1:]
    else:
        width = max(len(r) for r in raw)
        header = [str(i + 1) for i in range(width)]
        body = raw
    rows = [{h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(header)} for r in body]
    return header, rows


def looks_like_header(text: str) -> bool:
    """True if the first row has no parseable date or amount (i.e. it's labels)."""
    _, rows = read_rows(text, has_header=False)
    if not rows:
        return True
    first = list(rows[0].values())
    return not any(guess_date_format([c]) for c in first) and not any(
        _try_amount(c) is not None for c in first
    )


def signature(text: str, has_header: bool) -> str:
    """Fingerprint used to match a file to a saved format."""
    header, _ = read_rows(text, has_header)
    if has_header:
        return "|".join(h.lower() for h in header)
    return f"cols:{len(header)}"


def match_format(
    conn: sqlite3.Connection, text: str, prefer_id: int | None = None
) -> sqlite3.Row | None:
    """Saved format whose header (or column count) matches the file.
    If several match, prefer_id (the card's last-used format) wins."""
    candidates = {signature(text, True), signature(text, False)}
    matches = [
        f
        for f in conn.execute("SELECT * FROM csv_formats ORDER BY name")
        if f["signature"] in candidates
        and (f["has_header"] or not looks_like_header(text))
    ]
    for f in matches:
        if f["id"] == prefer_id:
            return f
    return matches[0] if matches else None


def save_format(conn: sqlite3.Connection, name: str, m: Mapping, text: str) -> int:
    """Create or update the named format. Returns its id."""
    values = (
        signature(text, m.has_header),
        m.date_col,
        m.description_col,
        m.amount_col,
        m.debit_col,
        m.credit_col,
        m.date_format,
        int(m.charges_positive),
        int(m.has_header),
    )
    conn.execute(
        "INSERT INTO csv_formats (signature, date_col, description_col, amount_col, debit_col, "
        "credit_col, date_format, charges_positive, has_header, name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET signature=excluded.signature, date_col=excluded.date_col, "
        "description_col=excluded.description_col, amount_col=excluded.amount_col, "
        "debit_col=excluded.debit_col, credit_col=excluded.credit_col, date_format=excluded.date_format, "
        "charges_positive=excluded.charges_positive, has_header=excluded.has_header",
        (*values, name.strip()),
    )
    return conn.execute("SELECT id FROM csv_formats WHERE name = ?", (name.strip(),)).fetchone()["id"]


def guess_mapping(text: str) -> Mapping:
    has_header = looks_like_header(text)
    header, rows = read_rows(text, has_header)
    picks: dict[str, str | None] = {k: None for k in HEADER_HINTS}

    if has_header:
        lowered = {h.lower(): h for h in header}
        for field, hints in HEADER_HINTS.items():
            for hint in hints:
                match = lowered.get(hint) or next(
                    (orig for low, orig in lowered.items() if hint in low), None
                )
                if match and match not in picks.values():
                    picks[field] = match
                    break
    else:
        # Infer from content: first date column, first mostly-text column,
        # then numeric columns as debit/credit (TD-style: date, desc, debit, credit, balance).
        sample = rows[:20]
        for h in header:
            values = [r[h] for r in sample if r[h]]
            if not values:
                continue
            if picks["date_col"] is None and guess_date_format(values):
                picks["date_col"] = h
            elif all(_try_amount(v) is not None for v in values):
                if picks["debit_col"] is None:
                    picks["debit_col"] = h
                elif picks["credit_col"] is None:
                    picks["credit_col"] = h
            elif picks["description_col"] is None:
                picks["description_col"] = h

    if picks["amount_col"]:
        picks["debit_col"] = picks["credit_col"] = None

    date_values = [r.get(picks["date_col"] or "", "") for r in rows[:50]]
    return Mapping(
        date_col=picks["date_col"] or "",
        description_col=picks["description_col"] or "",
        amount_col=picks["amount_col"],
        debit_col=picks["debit_col"],
        credit_col=picks["credit_col"],
        date_format=guess_date_format([v for v in date_values if v]),
        charges_positive=True,
        has_header=has_header,
    )


def guess_date_format(values: list[str]) -> str | None:
    """First format that parses every value. Ambiguous dd/mm vs mm/dd picks mm/dd first."""
    if not values:
        return None
    for fmt in DATE_FORMATS:
        try:
            for v in values:
                datetime.strptime(v.strip(), fmt)
            return fmt
        except ValueError:
            continue
    return None


def _try_amount(s: str) -> Decimal | None:
    s = s.strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    cleaned = re.sub(r"[^\d.\-]", "", s)
    if cleaned in ("", "-", "."):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def to_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1")))


def parse(text: str, m: Mapping) -> list[ParsedRow]:
    header, rows = read_rows(text, m.has_header)
    needed = [m.date_col, m.description_col] + (
        [m.amount_col] if m.amount_col else [c for c in (m.debit_col, m.credit_col) if c]
    )
    missing = [c for c in needed if c not in header]
    if missing:
        raise CsvFormatError(f"Column(s) not found in file: {', '.join(missing)}")
    if not m.amount_col and not m.debit_col:
        raise CsvFormatError("Map either an amount column or a debit column.")

    out: list[ParsedRow] = []
    for i, r in enumerate(rows, start=2 if m.has_header else 1):
        raw_date = r[m.date_col]
        if not raw_date:
            continue
        fmt = m.date_format or guess_date_format([raw_date])
        try:
            d = datetime.strptime(raw_date, fmt).date() if fmt else None
        except ValueError:
            d = None
        if d is None:
            raise CsvFormatError(f"Row {i}: can't read date '{raw_date}'")

        if m.amount_col:
            amt = _try_amount(r[m.amount_col])
            if amt is None:
                continue
            if not m.charges_positive:
                amt = -amt
        else:
            debit = _try_amount(r[m.debit_col]) if m.debit_col else None
            credit = _try_amount(r[m.credit_col]) if m.credit_col else None
            if debit is None and credit is None:
                continue
            amt = abs(debit or 0) - abs(credit or 0)

        out.append(ParsedRow(d, r[m.description_col], to_cents(amt)))
    return out


def dedupe_keys(account_id: int, rows: list[ParsedRow]) -> list[str]:
    """Stable key per row; identical same-day charges get an occurrence number
    so both are kept, while re-importing the same file adds nothing."""
    seen: Counter = Counter()
    keys = []
    for r in rows:
        base = (account_id, r.txn_date.isoformat(), r.description.upper(), r.amount_cents)
        seen[base] += 1
        keys.append(hashlib.sha1(repr((*base, seen[base])).encode()).hexdigest())
    return keys


def save(
    conn: sqlite3.Connection, account: sqlite3.Row, rows: list[ParsedRow], filename: str
) -> tuple[int, int]:
    """Insert rows. Returns (added, skipped_as_duplicate)."""
    cur = conn.execute(
        "INSERT INTO import_batches (account_id, filename) VALUES (?, ?)",
        (account["id"], filename),
    )
    batch_id = cur.lastrowid
    is_cad = account["currency"] == "CAD"
    added = 0
    for r, key in zip(rows, dedupe_keys(account["id"], rows)):
        cur = conn.execute(
            "INSERT OR IGNORE INTO transactions "
            "(account_id, batch_id, txn_date, tax_date, description, amount_cents, currency, cad_cents, "
            "dedupe_key, excluded) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account["id"],
                batch_id,
                r.txn_date.isoformat(),
                r.txn_date.isoformat(),
                r.description,
                r.amount_cents,
                account["currency"],
                r.amount_cents if is_cad else None,
                key,
                0 if account["include_by_default"] else 1,
            ),
        )
        added += cur.rowcount
    conn.execute("UPDATE import_batches SET row_count = ? WHERE id = ?", (added, batch_id))
    conn.commit()
    return added, len(rows) - added
