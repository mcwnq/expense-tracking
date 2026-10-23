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
import uuid
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


def parse_amount(s: str) -> Decimal | None:
    """An amount typed by hand: "$1,234.50", "-20" or "(20.00)"."""
    return _try_amount(s)


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
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    rows: list[ParsedRow],
    filename: str,
    signature: str | None = None,
    file_rows: int | None = None,
) -> tuple[int, int, int]:
    """Insert rows. Returns (added, skipped_as_duplicate, batch_id)."""
    cur = conn.execute(
        "INSERT INTO import_batches (account_id, filename, signature, file_rows) VALUES (?, ?, ?, ?)",
        (account["id"], filename, signature, file_rows),
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
        conn.execute("INSERT OR IGNORE INTO batch_lines (batch_id, dedupe_key) VALUES (?, ?)", (batch_id, key))
    conn.execute("UPDATE import_batches SET row_count = ? WHERE id = ?", (added, batch_id))
    conn.commit()
    return added, len(rows) - added, batch_id


def format_warning(conn: sqlite3.Connection, account_id: int, sig: str) -> str | None:
    """A message when the card has earlier imports and none had this file's shape."""
    used = {
        r["signature"]
        for r in conn.execute(
            "SELECT DISTINCT signature FROM import_batches WHERE account_id = ? AND signature IS NOT NULL",
            (account_id,),
        )
    }
    if not used or sig in used:
        return None
    names = [
        r["name"]
        for r in conn.execute(
            f"SELECT name FROM csv_formats WHERE signature IN ({','.join('?' * len(used))}) ORDER BY name",
            sorted(used),
        )
    ]
    earlier = f" (earlier files used {', '.join(names)})" if names else ""
    return f"This file's columns don't match the files you imported to this card before{earlier}. Check that you picked the right card."


def found_on_other_cards(conn: sqlite3.Connection, account_id: int, rows: list[ParsedRow]) -> list[tuple[str, int]]:
    """(card name, line count) for cards that already have lines with the same date,
    description and amount, a sign the file was meant for one of them."""
    counts: Counter = Counter()
    for r in rows:
        for hit in conn.execute(
            "SELECT DISTINCT a.name FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE t.account_id != ? AND t.txn_date = ? AND upper(t.description) = ? AND t.amount_cents = ?",
            (account_id, r.txn_date.isoformat(), r.description.upper(), r.amount_cents),
        ):
            counts[hit["name"]] += 1
    return counts.most_common()


def remove_batch(conn: sqlite3.Connection, batch_id: int) -> int:
    """Undo an import. A line that another remaining import's file also had is
    kept and moved to that import; the rest are deleted. Returns lines deleted."""
    conn.execute(
        "UPDATE transactions SET batch_id = (SELECT max(l.batch_id) FROM batch_lines l "
        "  WHERE l.dedupe_key = transactions.dedupe_key AND l.batch_id != ?) "
        "WHERE batch_id = ? AND EXISTS (SELECT 1 FROM batch_lines l "
        "  WHERE l.dedupe_key = transactions.dedupe_key AND l.batch_id != ?)",
        (batch_id, batch_id, batch_id),
    )
    deleted = conn.execute("DELETE FROM transactions WHERE batch_id = ?", (batch_id,)).rowcount
    conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
    conn.commit()
    return deleted


def fill_file_rows(conn: sqlite3.Connection, text: str) -> list[sqlite3.Row] | None:
    """Set Rows in file on every import of this file (matched by its lines, on any
    card) and return those imports. None if no saved format reads the file."""
    fmt = match_format(conn, text)
    if fmt is None:
        return None
    m = Mapping.from_row(fmt)
    rows = parse(text, m)
    n = len(read_rows(text, m.has_header)[1])
    batches = conn.execute(
        "SELECT b.*, a.name AS account FROM import_batches b JOIN accounts a ON a.id = b.account_id ORDER BY b.id"
    ).fetchall()
    # an import whose lines all appear in the file; on each card the one with the most
    # lines wins, so a shorter statement inside this one (e.g. Sep inside the year) is left alone
    best: dict[int, tuple[int, list[sqlite3.Row]]] = {}
    for b in batches:
        lines = {r[0] for r in conn.execute("SELECT dedupe_key FROM batch_lines WHERE batch_id = ?", (b["id"],))}
        if not lines or not lines <= set(dedupe_keys(b["account_id"], rows)):
            continue
        size, same = best.get(b["account_id"], (0, []))
        if len(lines) > size:
            best[b["account_id"]] = (len(lines), [b])
        elif len(lines) == size:
            same.append(b)
    found = [b for _, bs in best.values() for b in bs]
    for b in found:
        conn.execute("UPDATE import_batches SET file_rows = ? WHERE id = ?", (n, b["id"]))
    conn.commit()
    return found


def add_manual(conn: sqlite3.Connection, account: sqlite3.Row, on: date, description: str, amount_cents: int) -> int:
    """Add a transaction typed in by hand (no import). Returns its id."""
    cur = conn.execute(
        "INSERT INTO transactions (account_id, batch_id, txn_date, tax_date, description, amount_cents, currency, "
        "cad_cents, dedupe_key, excluded) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            account["id"],
            on.isoformat(),
            on.isoformat(),
            description.strip(),
            amount_cents,
            account["currency"],
            amount_cents if account["currency"] == "CAD" else None,
            f"manual:{uuid.uuid4().hex}",
        ),
    )
    conn.commit()
    return cur.lastrowid
