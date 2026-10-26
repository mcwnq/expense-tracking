"""Vendor grouping, monthly/yearly totals and one-time vs recurring tagging.

Each transaction is assigned to a vendor by rules: a rule is upper-case text
that starts a word of the card description ("TOPSTEP" matches "TOPSTEP* COMBINE
150K", but "FL" doesn't match "PAYPAL *NETFLIX"). See rule_matches.
When several rules match, the longest pattern wins, so "APEX RESET" can map
to a different vendor than "APEX".
"""

import calendar
import itertools
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

CATEGORIES = [
    "Prop firm evaluations",
    "Platform fees",
    "Market data",
    "Software",
    "Internet and phone",
    "Education and courses",
    "Computer equipment",
    "Software (capital)",
    "Office supplies",
    "Bank and card fees",
    "Other business",
    "Personal (exclude)",
    "Card payment (exclude)",
]

# Categories left out of business totals.
EXCLUDED = {"Personal (exclude)", "Card payment (exclude)"}

# (vendor, category, recurrence or None for auto, [patterns])
DEFAULT_VENDORS = [
    ("Topstep", "Prop firm evaluations", None, ["TOPSTEP"]),
    ("Apex Trader Funding", "Prop firm evaluations", None, ["APEX TRADER", "APEXTRADER"]),
    ("Take Profit Trader", "Prop firm evaluations", None, ["TAKEPROFITTRADER", "TAKE PROFIT TRADER"]),
    ("MyFundedFutures", "Prop firm evaluations", None, ["MYFUNDEDFUTURES", "MFFU"]),
    ("Tradeify", "Prop firm evaluations", None, ["TRADEIFY"]),
    ("Bulenox", "Prop firm evaluations", None, ["BULENOX"]),
    ("Earn2Trade", "Prop firm evaluations", None, ["EARN2TRADE"]),
    ("TradeDay", "Prop firm evaluations", None, ["TRADEDAY"]),
    ("Alpha Futures", "Prop firm evaluations", None, ["ALPHA FUTURES", "ALPHAFUTURES"]),
    ("Lucid Trading", "Prop firm evaluations", None, ["LUCID TRADING", "LUCIDTRADING"]),
    ("FTMO", "Prop firm evaluations", None, ["FTMO"]),
    ("Tradovate", "Platform fees", None, ["TRADOVATE"]),
    ("NinjaTrader", "Platform fees", None, ["NINJATRADER", "NINJA TRADER"]),
    ("Rithmic", "Market data", None, ["RITHMIC"]),
    ("CQG", "Market data", None, ["CQG"]),
    ("CME Group", "Market data", None, ["CME GROUP", "CMEGROUP"]),
    ("Databento", "Market data", None, ["DATABENTO"]),
    ("TradingView", "Software", None, ["TRADINGVIEW"]),
    ("Bookmap", "Software", None, ["BOOKMAP"]),
    ("Sierra Chart", "Software", None, ["SIERRA CHART", "SIERRACHART"]),
    ("Quantower", "Software", None, ["QUANTOWER"]),
    ("ATAS", "Software", None, ["ATAS PLATFORM", "ATAS.NET"]),
    ("MotiveWave", "Software", None, ["MOTIVEWAVE"]),
    ("Card payment", "Card payment (exclude)", None, [
        "PAYMENT THANK YOU", "PAYMENT - THANK YOU", "PAYMENT RECEIVED", "PAIEMENT MERCI", "PAIEMENT - MERCI",
        "ONLINE PAYMENT", "AUTOPAY PAYMENT", "AUTOMATIC PAYMENT", "PAYMENT FROM", "PAYMENT - ONLINE",
    ]),
]


def seed_defaults(conn: sqlite3.Connection) -> None:
    for name, category, recurrence, patterns in DEFAULT_VENDORS:
        conn.execute(
            "INSERT OR IGNORE INTO vendors (name, category, recurrence) VALUES (?, ?, ?)",
            (name, category, recurrence),
        )
        vid = conn.execute("SELECT id FROM vendors WHERE name = ?", (name,)).fetchone()[0]
        for p in patterns:
            conn.execute("INSERT OR IGNORE INTO vendor_rules (pattern, vendor_id) VALUES (?, ?)", (p, vid))


PAYMENT_CATEGORY = "Card payment (exclude)"


def exclude_card_payments(conn: sqlite3.Connection, batch_id: int | None = None) -> int:
    """Exclude payments made to the card (credits matched to a card-payment vendor).
    Refunds are credits from a merchant, so they don't match and stay included."""
    sql = (
        "UPDATE transactions SET excluded = 1 WHERE amount_cents < 0 AND vendor_id IN "
        "(SELECT id FROM vendors WHERE category = ?)"
    )
    args: list = [PAYMENT_CATEGORY]
    if batch_id is not None:
        sql += " AND batch_id = ?"
        args.append(batch_id)
    n = conn.execute(sql, args).rowcount
    conn.commit()
    return n


def match_vendor(rules: list[tuple[str, int]], description: str) -> int | None:
    """rules must be sorted longest pattern first."""
    desc = description.upper()
    for pattern, vendor_id in rules:
        if rule_matches(pattern, desc):
            return vendor_id
    return None


def rule_matches(pattern: str, desc: str) -> bool:
    """True if pattern appears in desc where a word starts, i.e. not right after
    a letter (it may run on into the word, so "NINJA" matches "NINJATRADER", and
    "PASSWORD" matches "1PASSWORD"). A space in the pattern stands for any gap
    between words, so "APPLE.COM BILL" matches "APPLE.COM/BILL"."""
    words = pattern.split()
    if not words:
        return False
    start = r"(?<![A-Z])" if pattern[:1].isalpha() else ""
    return re.search(start + r"[^A-Z]+".join(map(re.escape, words)), desc) is not None


def apply_rules(conn: sqlite3.Connection) -> None:
    """Re-assign every transaction's vendor from the current rules, except ones
    whose vendor was picked by hand."""
    rules = [
        (r["pattern"], r["vendor_id"])
        for r in conn.execute("SELECT pattern, vendor_id FROM vendor_rules ORDER BY length(pattern) DESC, pattern")
    ]
    updates = [
        (match_vendor(rules, t["description"]), t["id"])
        for t in conn.execute("SELECT id, description FROM transactions WHERE NOT vendor_manual")
    ]
    conn.executemany("UPDATE transactions SET vendor_id = ? WHERE id = ?", updates)
    conn.commit()


# Payment processors that put their own name before the merchant's,
# e.g. "PAYPAL *TRADINGVIEW" or "SQ *COFFEE HUT".
PROCESSORS = {"PAYPAL", "PP", "SQ", "SP", "TST", "PADDLE", "PADDLE.NET", "FS", "STRIPE", "GOOGLE", "2CO", "DRI"}


def suggest_pattern(description: str) -> str:
    """A starting rule for an unassigned description: its leading words, without
    reference numbers. "MFFU*EVAL 50K 8443" -> "MFFU". Behind a payment processor
    it's the merchant: "PAYPAL *TRADINGVIEW 4029357733" -> "TRADINGVIEW"."""
    text = description.upper()
    parts = [p.strip() for p in re.split(r"[*#]", text)]
    head = parts[0].split()
    if len(parts) > 1 and parts[0] in PROCESSORS and parts[1]:
        text = "*".join(parts[1:])  # "PAYPAL *MERCHANT ..."
    elif len(head) > 1 and head[0] in PROCESSORS:
        text = text.split(None, 1)[1]  # "PAYPAL MERCHANT ..."
    first = re.split(r"[*#]", text, maxsplit=1)[0]
    # stop at the first reference or phone number
    first = " ".join(itertools.takewhile(lambda t: not re.search(r"\d", t), first.split())) or first
    # the first two words of 2+ letters, with any short words between them kept,
    # so the rule still matches: "I AM A DAY TRADER" -> "AM A DAY"
    words = [w for w in re.finditer(r"[A-Z][A-Z.&'-]*", first) if len(w[0]) > 1][:2]
    if not words:
        return description.upper().strip()
    return " ".join(first[words[0].start() : words[-1].end()].split())


def add_rule(conn: sqlite3.Connection, pattern: str, vendor_id: int) -> None:
    conn.execute(
        "INSERT INTO vendor_rules (pattern, vendor_id) VALUES (?, ?) "
        "ON CONFLICT(pattern) DO UPDATE SET vendor_id = excluded.vendor_id",
        (pattern.strip().upper(), vendor_id),
    )
    apply_rules(conn)


def assign_vendor(conn: sqlite3.Connection, ids: list[int], vendor_id: int | None) -> None:
    """Pick the vendor for these lines by hand, so rules leave them alone. None puts
    them back under the rules."""
    marks = ",".join("?" * len(ids))
    if vendor_id is None:
        conn.execute(f"UPDATE transactions SET vendor_manual = 0 WHERE id IN ({marks})", ids)
        apply_rules(conn)
    else:
        conn.execute(f"UPDATE transactions SET vendor_id = ?, vendor_manual = 1 WHERE id IN ({marks})", [vendor_id, *ids])
        conn.commit()


def duplicate_vendor(conn: sqlite3.Connection, vendor_id: int) -> int:
    """A copy named "<name> - Copy" with the same category and type and no rules,
    for charges from one vendor that belong on different T2125 lines."""
    v = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    taken = {r[0] for r in conn.execute("SELECT name FROM vendors")}
    name = f"{v['name']} - Copy"
    n = 2
    while name in taken:
        name, n = f"{v['name']} - Copy {n}", n + 1
    cur = conn.execute(
        "INSERT INTO vendors (name, category, recurrence, business) VALUES (?, ?, ?, ?)",
        (name, v["category"], v["recurrence"], v["business"]),
    )
    conn.commit()
    return cur.lastrowid


def delete_vendor(conn: sqlite3.Connection, vendor_id: int) -> None:
    """Delete a vendor; its lines go back to the rules (unassigned if none match)."""
    conn.execute("UPDATE transactions SET vendor_manual = 0 WHERE vendor_id = ?", (vendor_id,))
    conn.execute("DELETE FROM vendors WHERE id = ?", (vendor_id,))
    apply_rules(conn)


def default_business(category: str) -> str:
    """Prop firm evaluations are Futures; anything else starts Shared between the two businesses."""
    return "Futures" if category == "Prop firm evaluations" else "Shared"


def get_or_create_vendor(conn: sqlite3.Connection, name: str, category: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO vendors (name, category, business) VALUES (?, ?, ?)",
        (name.strip(), category, default_business(category)),
    )
    return conn.execute("SELECT id FROM vendors WHERE name = ?", (name.strip(),)).fetchone()[0]


def detect_recurrence(months_charged: set[int]) -> str:
    """Recurring if charged in 3+ months of the year, or in two consecutive months."""
    if len(months_charged) >= 3:
        return "recurring"
    if any(m + 1 in months_charged for m in months_charged):
        return "recurring"
    return "one-time"


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def allocate(start: date, months: int | None, cents: int) -> dict[tuple[int, int], int]:
    """Split an amount across calendar months, by day, over the service period
    [start, start + months). Without a period the whole amount falls on start.
    Rounding is carried so the parts always add up to the original amount."""
    if not months:
        return {(start.year, start.month): cents}
    end = add_months(start, months)  # exclusive
    days = (end - start).days
    out: dict[tuple[int, int], int] = {}
    done, cursor = 0, start
    while cursor < end:
        month_end = min(add_months(cursor.replace(day=1), 1), end)
        done_after = done + (month_end - cursor).days
        out[(cursor.year, cursor.month)] = round(cents * done_after / days) - round(cents * done / days)
        done, cursor = done_after, month_end
    return out


def service_end(start: date, months: int) -> date:
    """Last day covered by a prepaid charge."""
    return add_months(start, months) - timedelta(days=1)


@dataclass
class VendorRow:
    id: int
    name: str
    category: str
    recurrence: str
    auto: bool
    months: list[int] = field(default_factory=lambda: [0] * 12)

    @property
    def total(self) -> int:
        return sum(self.months)


@dataclass
class CategoryGroup:
    name: str
    vendors: list[VendorRow]

    @property
    def total(self) -> int:
        return sum(v.total for v in self.vendors)


@dataclass
class Summary:
    year: int
    groups: list[CategoryGroup]
    all_vendors: list[VendorRow]

    @property
    def month_totals(self) -> list[int]:
        return [sum(v.months[i] for g in self.groups for v in g.vendors) for i in range(12)]

    @property
    def total(self) -> int:
        return sum(g.total for g in self.groups)

    def total_for(self, recurrence: str) -> int:
        return sum(v.total for v in self.all_vendors if v.recurrence == recurrence)

    def count_for(self, recurrence: str) -> int:
        return sum(1 for v in self.all_vendors if v.recurrence == recurrence)

    # Prepaid amounts crossing the year boundary, in cents of `currency`
    deferred_out: int = 0  # paid this year, counted in later years
    carried_in: int = 0  # paid in earlier years, counted this year
    currency: str = "CAD"
    unconverted: int = 0  # lines left out because no exchange rate is available yet


def summary(
    conn: sqlite3.Connection,
    year: int,
    only: str | None = None,
    currency: str = "CAD",
    charged_in: str | None = None,
    expenses_only: bool = False,
) -> Summary:
    """Business expenses for a year in cents, grouped by category then vendor.

    Amounts are in `currency` (CAD or USD), converting at the Bank of Canada rate
    the year uses. With `charged_in`, only lines charged in that currency are
    counted, in their own currency. With `expenses_only`, charges moved to CCA by
    themselves are left out. Refunds net against the vendor's charges in
    the month they post. Prepaid charges are spread over their service period,
    so part of a charge can fall in another year. Excluded categories and
    unassigned transactions are left out (see unassigned()).
    """
    if charged_in:
        currency = charged_in
    sql = (
        "SELECT v.id, v.name, v.category, v.recurrence, t.tax_date, t.service_months, t.cad_cents, "
        "t.amount_cents, t.currency "
        "FROM transactions t JOIN vendors v ON v.id = t.vendor_id "
        "WHERE t.tax_date <= ? AND (substr(t.tax_date, 1, 4) = ? OR t.service_months IS NOT NULL) "
        "AND NOT t.excluded"
    )
    if expenses_only:
        sql += " AND t.capital_cls IS NULL"  # charges moved to CCA are claimed as assets
    args: list = [f"{year}-12-31", str(year)]
    if charged_in:
        sql += " AND t.currency = ?"
        args.append(charged_in)
    rows = conn.execute(sql, args).fetchall()
    to = _converter(conn, currency)
    unconverted = 0

    vendors: dict[int, VendorRow] = {}
    charged: dict[int, set[int]] = {}
    subscriptions: set[int] = set()
    deferred_out = carried_in = 0
    for r in rows:
        if r["category"] in EXCLUDED:
            continue
        paid = date.fromisoformat(r["tax_date"])
        cents = to(r)
        if cents is None:
            unconverted += 1
            continue
        parts = allocate(paid, r["service_months"], cents)
        in_year = {m: c for (y, m), c in parts.items() if y == year}
        if paid.year == year:
            deferred_out += sum(c for (y, _), c in parts.items() if y > year)
        else:
            carried_in += sum(in_year.values())
        if not in_year:
            continue
        v = vendors.setdefault(
            r["id"], VendorRow(r["id"], r["name"], r["category"], r["recurrence"] or "", r["recurrence"] is None)
        )
        for m, c in in_year.items():
            v.months[m - 1] += c
        if paid.year == year and r["amount_cents"] > 0:
            charged.setdefault(r["id"], set()).add(paid.month)
        if (r["service_months"] or 0) >= 12:
            subscriptions.add(r["id"])
    for v in vendors.values():
        if v.auto:
            # A charge prepaid for a year or more is a subscription, so recurring
            v.recurrence = "recurring" if v.id in subscriptions else detect_recurrence(charged.get(v.id, set()))

    all_vendors = sorted(vendors.values(), key=lambda v: v.name.lower())
    shown = [v for v in all_vendors if not only or v.recurrence == only]
    order = {c: i for i, c in enumerate(CATEGORIES)}
    groups: dict[str, list[VendorRow]] = {}
    for v in shown:
        groups.setdefault(v.category, []).append(v)
    return Summary(
        year,
        [CategoryGroup(c, vs) for c, vs in sorted(groups.items(), key=lambda kv: order.get(kv[0], 99))],
        all_vendors,
        deferred_out,
        carried_in,
        currency,
        unconverted,
    )


def _converter(conn: sqlite3.Connection, currency: str):
    """A function giving a transaction row's amount in `currency` cents, or None
    when the rate it needs hasn't been fetched yet."""
    from . import fx

    if currency == "CAD":
        return lambda r: r["cad_cents"]
    rates: dict[str, Decimal | None] = {}

    def usd(r):
        if r["currency"] == "USD":
            return r["amount_cents"]
        day = r["tax_date"]
        if day not in rates:
            method, annual = fx.year_setting(conn, int(day[:4]))
            rates[day] = annual if method == "annual" else fx.usd_to_cad_rate(conn, date.fromisoformat(day))
        rate = rates[day]
        if rate is None:
            return None
        return int((Decimal(r["cad_cents"]) / rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return usd


def unassigned(conn: sqlite3.Connection, year: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.*, a.name AS account, NULL AS vendor, NULL AS vendor_category "
        "FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE t.vendor_id IS NULL AND NOT t.excluded AND substr(t.tax_date, 1, 4) = ? ORDER BY t.tax_date",
        (str(year),),
    ).fetchall()


def years(conn: sqlite3.Connection) -> list[int]:
    """Years with transactions, plus later years that prepaid charges reach into."""
    found = {int(r[0]) for r in conn.execute("SELECT DISTINCT substr(tax_date, 1, 4) FROM transactions")}
    for r in conn.execute("SELECT tax_date, service_months FROM transactions WHERE service_months IS NOT NULL"):
        found.add(service_end(date.fromisoformat(r["tax_date"]), r["service_months"]).year)
    return sorted(found, reverse=True)


def year_end_check(conn: sqlite3.Connection, year: int) -> list[sqlite3.Row]:
    """Charges with a card date of Jan 1-2 at either end of the tax year. Card dates
    are often UTC, so some of these were really made on Dec 31 of the year before."""
    days = [f"{y}-01-0{d}" for y in (year, year + 1) for d in (1, 2)]
    return conn.execute(
        "SELECT t.*, a.name AS account, v.name AS vendor FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id LEFT JOIN vendors v ON v.id = t.vendor_id "
        f"WHERE t.txn_date IN ({','.join('?' * len(days))}) AND t.amount_cents > 0 AND NOT t.excluded ORDER BY t.txn_date",
        days,
    ).fetchall()


def set_service_months(conn: sqlite3.Connection, txn_id: int, months: int | None) -> None:
    """Spread a prepaid charge over months starting on its tax date (None = expense it at once)."""
    conn.execute("UPDATE transactions SET service_months = ? WHERE id = ?", (months or None, txn_id))
    conn.commit()


@dataclass
class MerchantGroup:
    """Lines from one merchant: its vendor when assigned, otherwise the suggested rule text."""

    label: str
    vendor: str | None
    category: str | None
    ids: list[int] = field(default_factory=list)
    included: int = 0
    cad_cents: int = 0
    first: str = ""
    last: str = ""
    sample: str = ""

    @property
    def n(self) -> int:
        return len(self.ids)


def group_by_merchant(rows: list[sqlite3.Row]) -> list[MerchantGroup]:
    """Group transaction rows (needing id, description, vendor, vendor_category, excluded,
    cad_cents, tax_date) by merchant, biggest groups first."""
    groups: dict[str, MerchantGroup] = {}
    for r in rows:
        label = r["vendor"] or suggest_pattern(r["description"])
        g = groups.get(label)
        if g is None:
            g = groups[label] = MerchantGroup(label, r["vendor"], r["vendor_category"], sample=r["description"])
        g.ids.append(r["id"])
        g.cad_cents += r["cad_cents"] or 0
        if not r["excluded"]:
            g.included += 1
        g.first = min(g.first or r["tax_date"], r["tax_date"])
        g.last = max(g.last, r["tax_date"])
    return sorted(groups.values(), key=lambda g: (-g.n, g.label))


def set_included_ids(conn: sqlite3.Connection, ids: list[int], included: bool) -> None:
    """Include or exclude several transactions at once, remembering the old
    values so the change can be undone with undo_include."""
    if len(ids) > 1:
        conn.execute("DELETE FROM include_undo")
        conn.executemany(
            "INSERT INTO include_undo (txn_id, excluded) SELECT id, excluded FROM transactions WHERE id = ?",
            [(i,) for i in ids],
        )
        label = f"{'Included' if included else 'Excluded'} {len(ids)} transactions"
        conn.execute(
            "INSERT OR REPLACE INTO include_undo_info (id, label, at) VALUES (1, ?, datetime('now', 'localtime'))",
            (label,),
        )
    conn.executemany("UPDATE transactions SET excluded = ? WHERE id = ?", [(0 if included else 1, i) for i in ids])
    conn.commit()


def last_include_change(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The bulk include/exclude that undo_include would reverse, if any."""
    if not conn.execute("SELECT 1 FROM include_undo LIMIT 1").fetchone():
        return None
    return conn.execute("SELECT label, substr(at, 1, 16) AS at FROM include_undo_info WHERE id = 1").fetchone()


def undo_include(conn: sqlite3.Connection) -> int:
    """Put back the Include values from before the last bulk include/exclude."""
    n = conn.execute(
        "UPDATE transactions SET excluded = (SELECT u.excluded FROM include_undo u WHERE u.txn_id = transactions.id) "
        "WHERE id IN (SELECT txn_id FROM include_undo)"
    ).rowcount
    conn.execute("DELETE FROM include_undo")
    conn.execute("DELETE FROM include_undo_info")
    conn.commit()
    return n


def set_excluded(conn: sqlite3.Connection, txn_id: int, excluded: bool) -> None:
    """Leave one transaction out of business totals (or bring it back)."""
    conn.execute("UPDATE transactions SET excluded = ? WHERE id = ?", (int(excluded), txn_id))
    conn.commit()


def set_tax_date(conn: sqlite3.Connection, txn_id: int, tax_date: str) -> None:
    """Change the date a transaction counts on. USD amounts are re-converted at that date's rate."""
    conn.execute(
        "UPDATE transactions SET tax_date = ?, "
        "fx_rate = CASE WHEN currency = 'USD' THEN NULL ELSE fx_rate END, "
        "cad_cents = CASE WHEN currency = 'USD' THEN NULL ELSE cad_cents END WHERE id = ?",
        (tax_date, txn_id),
    )
    conn.commit()
