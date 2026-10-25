"""The two trading businesses, Futures and Options & Stocks, each filing its own T2125.

Each vendor belongs to one business or is Shared. A Shared vendor's expenses
are split by the year's Futures % (Options & Stocks gets the rest). Prop firm
payouts are Futures income; T5008 entries pick their business.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import payouts, vendors
from .importer import parse_amount, to_cents

FUTURES = "Futures"
OPTIONS = "Options & Stocks"
SHARED = "Shared"
BUSINESSES = [FUTURES, OPTIONS]
VENDOR_BUSINESSES = [FUTURES, OPTIONS, SHARED]
KINDS = ["Stock", "Option", "Futures"]
DEFAULT_FUTURES_PCT = Decimal(50)

# T2125 Part 4 line for each expense category: (line, name). Capital categories
# (see cca.CAPITAL_CATEGORIES) go through CCA instead.
LINES = {
    "Prop firm evaluations": ("9270", "Other expenses"),
    "Platform fees": ("9270", "Other expenses"),
    "Market data": ("9270", "Other expenses"),
    "Software": ("8810", "Office expenses"),
    "Internet and phone": ("9220", "Utilities"),
    "Education and courses": ("9270", "Other expenses"),
    "Office supplies": ("8811", "Office stationery and supplies"),
    "Bank and card fees": ("8710", "Interest and bank charges"),
    "Other business": ("9270", "Other expenses"),
}


def futures_pct(conn: sqlite3.Connection, year: int) -> Decimal:
    row = conn.execute("SELECT futures_pct FROM business_split WHERE year = ?", (year,)).fetchone()
    return Decimal(row[0]) if row else DEFAULT_FUTURES_PCT


def is_split_set(conn: sqlite3.Connection, year: int) -> bool:
    return conn.execute("SELECT 1 FROM business_split WHERE year = ?", (year,)).fetchone() is not None


def set_futures_pct(conn: sqlite3.Connection, year: int, text: str) -> str | None:
    """Save the year's Futures % for Shared vendors. Returns an error message, or None."""
    try:
        pct = Decimal(text.strip().rstrip("%"))
    except InvalidOperation:
        pct = None
    if pct is None or not 0 <= pct <= 100:
        return "The Futures % must be a number from 0 to 100."
    conn.execute(
        "INSERT INTO business_split (year, futures_pct) VALUES (?, ?) "
        "ON CONFLICT(year) DO UPDATE SET futures_pct = excluded.futures_pct",
        (year, pct_text(pct)),
    )
    conn.commit()
    return None


def split_cents(cents: int, pct: Decimal) -> tuple[int, int]:
    """(Futures, Options & Stocks) parts of a Shared amount; they always add up to it."""
    fut = int((Decimal(cents) * pct / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return fut, cents - fut


def pct_text(pct: Decimal) -> str:
    return f"{pct.normalize():f}"


# ---- T5008 entries ----

@dataclass
class T5008Form:
    """The form's text as typed, so it can be shown again with an error."""

    business: str = OPTIONS
    broker: str = ""
    kind: str = "Stock"
    security: str = ""
    settled_on: str = ""
    currency: str = "USD"
    proceeds: str = ""
    cost: str = ""
    net: str = ""
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "T5008Form":
        def money(c):
            return "" if c is None else f"{c / 100:.2f}"

        return cls(
            business=r["business"], broker=r["broker"], kind=r["kind"], security=r["security"] or "",
            settled_on=r["settled_on"], currency=r["currency"], proceeds=money(r["proceeds_cents"]),
            cost=money(r["cost_cents"]), net=money(r["net_cents"]),
        )


def _money(text: str, label: str, errors: list[str], allow_negative: bool = False) -> int | None:
    if not text.strip():
        return None
    value = parse_amount(text)
    if value is None or (value < 0 and not allow_negative):
        errors.append(f"{label} isn't a valid amount.")
        return None
    return to_cents(value)


def save_t5008(conn: sqlite3.Connection, f: T5008Form, entry_id: int | None = None) -> int | None:
    """Validate and insert (or update) a T5008 entry. Returns its id, or None with f.errors set."""
    errors = f.errors
    if f.business not in BUSINESSES:
        errors.append("Pick Futures or Options & Stocks.")
    if f.kind not in KINDS:
        errors.append("Pick Stock, Option or Futures.")
    if f.currency not in ("USD", "CAD"):
        errors.append("Pick USD or CAD.")
    settled_on = None
    if f.settled_on.strip():
        try:
            settled_on = date.fromisoformat(f.settled_on.strip()).isoformat()
        except ValueError:
            errors.append("The date isn't a valid date.")
    proceeds = _money(f.proceeds, "Proceeds", errors)
    cost = _money(f.cost, "Cost", errors)
    if proceeds is not None and cost is not None:
        net = proceeds - cost
    else:
        net = _money(f.net, "Net gain or loss", errors, allow_negative=True)
    missing = [
        name
        for name, value in (("broker", f.broker.strip()), ("date", settled_on),
                            ("proceeds and cost, or the net gain or loss", net))
        if value in (None, "")
    ]
    if missing and not errors:
        errors.append(f"Enter the {', '.join(missing)}.")
    if errors:
        return None

    values = (
        f.business, f.broker.strip(), f.kind, f.security.strip() or None, settled_on, f.currency,
        proceeds, cost, net, None, net if f.currency == "CAD" else None,
    )
    cols = ("business, broker, kind, security, settled_on, currency, proceeds_cents, cost_cents, "
            "net_cents, fx_rate, cad_cents")
    if entry_id is None:
        entry_id = conn.execute(f"INSERT INTO t5008 ({cols}) VALUES ({', '.join('?' * 11)})", values).lastrowid
    else:
        sets = ", ".join(f"{c.strip()} = ?" for c in cols.split(","))
        conn.execute(f"UPDATE t5008 SET {sets} WHERE id = ?", (*values, entry_id))
    conn.commit()
    return entry_id


def t5008_for(conn: sqlite3.Connection, year: int, business: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM t5008 WHERE substr(settled_on, 1, 4) = ?"
    args: list = [str(year)]
    if business:
        sql += " AND business = ?"
        args.append(business)
    return conn.execute(sql + " ORDER BY settled_on, id", args).fetchall()


def t5008_years(conn: sqlite3.Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT DISTINCT substr(settled_on, 1, 4) FROM t5008 ORDER BY 1 DESC")]


def net_cad(rows: list[sqlite3.Row]) -> int | None:
    """Total net in CAD, or None while a USD entry has no rate yet."""
    if any(r["cad_cents"] is None for r in rows):
        return None
    return sum(r["cad_cents"] for r in rows)


# ---- T2125 per business ----

@dataclass
class ExpenseItem:
    vendor: str
    business: str  # Futures, Options & Stocks, or "Shared · 60% of 812.60"
    cents: int


@dataclass
class ExpenseLine:
    code: str
    name: str
    items: list[ExpenseItem] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(i.cents for i in self.items)


@dataclass
class T2125:
    year: int
    business: str
    payouts: int  # line 8000
    payout_count: int
    trading: int | None  # line 8230; None while a USD entry has no rate yet
    trading_count: int
    lines: list[ExpenseLine]
    unconverted: int  # expense lines or payouts left out for lack of a rate
    pct: Decimal
    cca: "cca.Schedule"  # Area A; its total is on line 9936, recapture on line 8230

    @property
    def other_income(self) -> int:  # line 8230
        return (self.trading or 0) + self.cca.recapture

    @property
    def gross(self) -> int:
        return self.payouts + self.other_income

    @property
    def expenses(self) -> int:
        return sum(line.total for line in self.lines)

    @property
    def net(self) -> int:
        return self.gross - self.expenses


def t2125(conn: sqlite3.Connection, year: int, business: str) -> T2125:
    from . import cca

    pct = futures_pct(conn, year)
    biz = {r["id"]: r["business"] for r in conn.execute("SELECT id, business FROM vendors")}
    s = vendors.summary(conn, year)
    lines: dict[str, ExpenseLine] = {}
    for v in s.all_vendors:
        owner = biz.get(v.id, SHARED)
        if owner == SHARED:
            fut, opt = split_cents(v.total, pct)
            cents = fut if business == FUTURES else opt
            share = pct if business == FUTURES else 100 - pct
            label = f"Shared · {pct_text(share)}% of {v.total / 100:,.2f}"
        elif owner == business:
            cents, label = v.total, business
        else:
            continue
        if v.category in cca.CAPITAL_CATEGORIES:
            continue  # claimed through CCA
        code, name = LINES.get(v.category, ("9270", "Other expenses"))
        lines.setdefault(code, ExpenseLine(code, name)).items.append(ExpenseItem(v.name, label, cents))

    if business == FUTURES:
        paid = payouts.for_year(conn, year)
        payout_cents = sum(p["cad_cents"] or 0 for p in paid)
        unconverted = s.unconverted + sum(1 for p in paid if p["cad_cents"] is None)
    else:
        paid, payout_cents, unconverted = [], 0, s.unconverted
    sched = cca.schedule(conn, year, business)
    for r in sched.rows:
        if r.claim:
            lines.setdefault("9936", ExpenseLine("9936", "Capital cost allowance (CCA)")).items.append(
                ExpenseItem(f"Class {r.cls} · {r.name}", "Area A", r.claim))
        if r.terminal_loss:
            lines.setdefault("9270", ExpenseLine("9270", "Other expenses")).items.append(
                ExpenseItem(f"Terminal loss, class {r.cls}", "Area A", r.terminal_loss))
    entries = t5008_for(conn, year, business)
    return T2125(
        year, business, payout_cents, len(paid), net_cad(entries), len(entries),
        [lines[c] for c in sorted(lines)], unconverted, pct, sched,
    )
