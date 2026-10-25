"""Prop firm payouts, reported as business income.

Only the firm, the received date and the amount received are required. When
both my share % and the approved amount are given, the amount received is
calculated as approved x share % - other fees.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .importer import parse_amount, to_cents

FIRM_CATEGORY = "Prop firm evaluations"
PAID_VIA = ["Rise", "Wise", "Bank transfer", "PayPal", "Crypto"]


@dataclass
class PayoutForm:
    """The form's text as typed, so it can be shown again with an error."""

    firm: str = ""
    currency: str = "USD"
    paid_via: str = ""
    split_pct: str = ""
    approved: str = ""
    approved_on: str = ""
    fees: str = ""
    received_on: str = ""
    received: str = ""
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "PayoutForm":
        def money(c):
            return "" if c is None else f"{c / 100:.2f}"

        return cls(
            firm=r["firm"], currency=r["currency"], paid_via=r["paid_via"] or "", split_pct=r["split_pct"] or "",
            approved=money(r["approved_cents"]), approved_on=r["approved_on"] or "", fees=money(r["fees_cents"]),
            received_on=r["received_on"], received=money(r["received_cents"]),
        )


def firms(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM vendors WHERE category = ? ORDER BY name", (FIRM_CATEGORY,))]


def calculated_cents(split_pct: Decimal, approved_cents: int, fees_cents: int | None) -> int:
    share = (Decimal(approved_cents) * split_pct / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(share) - (fees_cents or 0)


def _money(text: str, label: str, errors: list[str]) -> int | None:
    if not text.strip():
        return None
    value = parse_amount(text)
    if value is None or value < 0:
        errors.append(f"{label} isn't a valid amount.")
        return None
    return to_cents(value)


def _date(text: str, label: str, errors: list[str]) -> str | None:
    if not text.strip():
        return None
    try:
        return date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        errors.append(f"{label} isn't a valid date.")
        return None


def save(conn: sqlite3.Connection, f: PayoutForm, payout_id: int | None = None) -> int | None:
    """Validate and insert (or update) a payout. Returns its id, or None with f.errors set."""
    errors = f.errors
    split = None
    if f.split_pct.strip():
        try:
            split = Decimal(f.split_pct.strip().rstrip("%"))
        except InvalidOperation:
            split = None
        if split is None or not 0 < split <= 100:
            errors.append("My share % must be a number from 1 to 100.")
            split = None
    approved = _money(f.approved, "Payout amount approved", errors)
    fees = _money(f.fees, "Other fees", errors)
    approved_on = _date(f.approved_on, "Payout approved date", errors)
    received_on = _date(f.received_on, "Payout received date", errors)
    if split is not None and approved is not None:
        received = calculated_cents(split, approved, fees)
        if received < 0:
            errors.append("Other fees are more than your share of the payout.")
    else:
        received = _money(f.received, "Amount received", errors)

    missing = [
        name
        for name, value in (("prop firm", f.firm.strip()), ("payout received date", received_on), ("amount received", received))
        if value in (None, "")
    ]
    if missing and not errors:
        errors.append(f"Enter the {', '.join(missing)}.")
    if f.currency not in ("USD", "CAD"):
        errors.append("Pick USD or CAD.")
    if errors:
        return None

    values = (
        f.firm.strip(), f.currency, f.paid_via.strip() or None, str(split) if split is not None else None,
        approved, approved_on, fees, received_on, received,
        None, received if f.currency == "CAD" else None,
    )
    cols = ("firm, currency, paid_via, split_pct, approved_cents, approved_on, fees_cents, "
            "received_on, received_cents, fx_rate, cad_cents")
    if payout_id is None:
        payout_id = conn.execute(f"INSERT INTO payouts ({cols}) VALUES ({', '.join('?' * 11)})", values).lastrowid
    else:
        sets = ", ".join(f"{c.strip()} = ?" for c in cols.split(","))
        conn.execute(f"UPDATE payouts SET {sets} WHERE id = ?", (*values, payout_id))
    conn.commit()
    return payout_id


def for_year(conn: sqlite3.Connection, year: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM payouts WHERE substr(received_on, 1, 4) = ? ORDER BY received_on, id", (str(year),)
    ).fetchall()


def years(conn: sqlite3.Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT DISTINCT substr(received_on, 1, 4) FROM payouts ORDER BY 1 DESC")]


def totals(rows: list[sqlite3.Row]) -> dict:
    """Amount received per original currency, and all of it in CAD (None if a rate is missing)."""
    by_cur = {c: sum(r["received_cents"] for r in rows if r["currency"] == c) for c in ("USD", "CAD")}
    cad = None if any(r["cad_cents"] is None for r in rows) else sum(r["cad_cents"] for r in rows)
    return {"USD": by_cur["USD"], "CAD": by_cur["CAD"], "cad_total": cad}
