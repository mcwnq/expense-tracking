"""Vendor grouping, monthly/yearly totals and one-time vs recurring tagging.

Each transaction is assigned to a vendor by rules: a rule is an upper-case
substring of the card description ("TOPSTEP" matches "TOPSTEP* COMBINE 150K").
When several rules match, the longest pattern wins, so "APEX RESET" can map
to a different vendor than "APEX".
"""

import re
import sqlite3
from dataclasses import dataclass, field

CATEGORIES = [
    "Prop firm evaluations",
    "Platform fees",
    "Market data",
    "Software",
    "Internet and phone",
    "Education and courses",
    "Computer equipment",
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
    ("Card payment", "Card payment (exclude)", None, ["PAYMENT THANK YOU", "PAYMENT - THANK YOU", "PAYMENT RECEIVED", "PAIEMENT MERCI"]),
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


def match_vendor(rules: list[tuple[str, int]], description: str) -> int | None:
    """rules must be sorted longest pattern first."""
    desc = description.upper()
    for pattern, vendor_id in rules:
        if pattern in desc:
            return vendor_id
    return None


def apply_rules(conn: sqlite3.Connection) -> None:
    """Re-assign every transaction's vendor from the current rules."""
    rules = [
        (r["pattern"], r["vendor_id"])
        for r in conn.execute("SELECT pattern, vendor_id FROM vendor_rules ORDER BY length(pattern) DESC, pattern")
    ]
    updates = [
        (match_vendor(rules, t["description"]), t["id"])
        for t in conn.execute("SELECT id, description FROM transactions")
    ]
    conn.executemany("UPDATE transactions SET vendor_id = ? WHERE id = ?", updates)
    conn.commit()


def suggest_pattern(description: str) -> str:
    """A starting rule for an unassigned description: its leading words, without
    reference numbers. "MFFU*EVAL 50K 8443" -> "MFFU"."""
    first = re.split(r"[*#]", description.upper(), maxsplit=1)[0]
    words = [w for w in re.findall(r"[A-Z][A-Z.&'-]*", first) if len(w) > 1]
    return " ".join(words[:2]) or description.upper().strip()


def add_rule(conn: sqlite3.Connection, pattern: str, vendor_id: int) -> None:
    conn.execute(
        "INSERT INTO vendor_rules (pattern, vendor_id) VALUES (?, ?) "
        "ON CONFLICT(pattern) DO UPDATE SET vendor_id = excluded.vendor_id",
        (pattern.strip().upper(), vendor_id),
    )
    apply_rules(conn)


def get_or_create_vendor(conn: sqlite3.Connection, name: str, category: str) -> int:
    conn.execute("INSERT OR IGNORE INTO vendors (name, category) VALUES (?, ?)", (name.strip(), category))
    return conn.execute("SELECT id FROM vendors WHERE name = ?", (name.strip(),)).fetchone()[0]


def detect_recurrence(months_charged: set[int]) -> str:
    """Recurring if charged in 3+ months of the year, or in two consecutive months."""
    if len(months_charged) >= 3:
        return "recurring"
    if any(m + 1 in months_charged for m in months_charged):
        return "recurring"
    return "one-time"


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


def summary(conn: sqlite3.Connection, year: int, only: str | None = None) -> Summary:
    """Business expenses for a year in CAD cents, grouped by category then vendor.

    Refunds net against the vendor's charges in the month they post. Excluded
    categories and unassigned transactions are left out (see unassigned()).
    """
    rows = conn.execute(
        "SELECT v.id, v.name, v.category, v.recurrence, CAST(substr(t.txn_date, 6, 2) AS INTEGER) AS m, "
        "SUM(t.cad_cents) AS cents, SUM(CASE WHEN t.amount_cents > 0 THEN 1 ELSE 0 END) AS charges "
        "FROM transactions t JOIN vendors v ON v.id = t.vendor_id "
        "WHERE substr(t.txn_date, 1, 4) = ? AND t.cad_cents IS NOT NULL "
        "GROUP BY v.id, m",
        (str(year),),
    ).fetchall()

    vendors: dict[int, VendorRow] = {}
    charged: dict[int, set[int]] = {}
    for r in rows:
        if r["category"] in EXCLUDED:
            continue
        v = vendors.setdefault(
            r["id"], VendorRow(r["id"], r["name"], r["category"], r["recurrence"] or "", r["recurrence"] is None)
        )
        v.months[r["m"] - 1] += r["cents"]
        if r["charges"]:
            charged.setdefault(r["id"], set()).add(r["m"])
    for v in vendors.values():
        if v.auto:
            v.recurrence = detect_recurrence(charged.get(v.id, set()))

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
    )


def unassigned(conn: sqlite3.Connection, year: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.*, a.name AS account FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE t.vendor_id IS NULL AND substr(t.txn_date, 1, 4) = ? ORDER BY t.txn_date",
        (str(year),),
    ).fetchall()


def years(conn: sqlite3.Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT DISTINCT substr(txn_date, 1, 4) FROM transactions ORDER BY 1 DESC")]
