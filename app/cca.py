"""Capital cost allowance (T2125 Area A), one schedule per business.

Charges whose vendor category is capital (Computer equipment, Software (capital))
become assets instead of expenses; assets can also be added by hand. Each asset
has a CCA class, a business (or Shared, split by the Futures % of the year it
became available for use) and a business-use %. The schedule runs year by year
from the first asset, so the undepreciated capital cost (UCC) carries over.

First-year rules follow Area A of the 2025 T2125: no half-year rule for
assets acquired after November 20, 2018, and an extra "UCC adjustment" (column
15/17) for some classes: 9/11 for class 50 (acquired after April 15, 2024), 1/2
for other classes acquired after 2024, nothing for class 12 or earlier assets.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import business
from .importer import parse_amount, to_cents

# class: (name, rate)
CLASSES = {
    "50": ("Computers and systems software", Decimal("0.55")),
    "12": ("Software, tools under $500", Decimal("1")),
    "8": ("Furniture and other equipment", Decimal("0.20")),
}

# Vendor categories whose charges are assets, and the class they start in
CAPITAL_CATEGORIES = {"Computer equipment": "50", "Software (capital)": "12"}


def first_year_factor(cls: str, acquired: date) -> Decimal:
    """Area A column 15/17 factor; the half-year rule (column 18) never applies to these."""
    if cls == "12":
        return Decimal(0)
    if cls == "50":
        return Decimal(9) / 11 if acquired > date(2024, 4, 15) else Decimal(0)
    return Decimal(1) / 2 if acquired.year >= 2025 else Decimal(0)


def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def sync_assets(conn: sqlite3.Connection) -> None:
    """Keep one asset per included capital charge: add new ones, drop ones whose
    charge was excluded, deleted or moved to a non-capital vendor, and refresh
    cost and date. The class, business, business-use % and name you set stay."""
    cats = list(CAPITAL_CATEGORIES)
    rows = conn.execute(
        "SELECT t.id, t.tax_date, t.cad_cents, v.name, v.category, v.business FROM transactions t "
        f"JOIN vendors v ON v.id = t.vendor_id WHERE NOT t.excluded AND t.amount_cents > 0 "
        f"AND t.cad_cents IS NOT NULL AND v.category IN ({', '.join('?' * len(cats))})",
        cats,
    ).fetchall()
    keep = {r["id"] for r in rows}
    for (txn_id,) in conn.execute("SELECT txn_id FROM assets WHERE txn_id IS NOT NULL").fetchall():
        if txn_id not in keep:
            conn.execute("DELETE FROM assets WHERE txn_id = ?", (txn_id,))
    for r in rows:
        conn.execute(
            "INSERT INTO assets (txn_id, name, available_on, cost_cents, cls, business) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(txn_id) DO UPDATE SET available_on = excluded.available_on, cost_cents = excluded.cost_cents",
            (r["id"], r["name"], r["tax_date"], r["cad_cents"], CAPITAL_CATEGORIES[r["category"]], r["business"]),
        )
    conn.commit()


@dataclass
class AssetForm:
    """An asset's editable fields as typed, so they can be shown again with an error."""

    name: str = ""
    available_on: str = ""
    cost: str = ""
    cls: str = "50"
    business: str = business.SHARED
    use_pct: str = "100"
    disposed_on: str = ""
    proceeds: str = ""
    errors: list[str] = field(default_factory=list)


def save_asset(conn: sqlite3.Connection, f: AssetForm, asset_id: int | None = None) -> int | None:
    """Insert an asset added by hand, or update one. Cost and date of an asset that
    comes from a charge follow the charge, so they're ignored. Returns the id, or
    None with f.errors set."""
    errors = f.errors
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone() if asset_id else None
    from_charge = bool(row and row["txn_id"])
    if f.cls not in CLASSES:
        errors.append("Pick a CCA class.")
    if f.business not in business.VENDOR_BUSINESSES:
        errors.append("Pick Futures, Options & Stocks or Shared.")
    try:
        use = Decimal(f.use_pct.strip().rstrip("%") or "100")
    except InvalidOperation:
        use = Decimal(-1)
    if not 0 < use <= 100:
        errors.append("Business use must be a number from 1 to 100.")

    def day(text: str, label: str) -> str | None:
        if not text.strip():
            return None
        try:
            return date.fromisoformat(text.strip()).isoformat()
        except ValueError:
            errors.append(f"{label} isn't a valid date.")
            return None

    def money(text: str, label: str) -> int | None:
        if not text.strip():
            return None
        value = parse_amount(text)
        if value is None or value < 0:
            errors.append(f"{label} isn't a valid amount.")
            return None
        return to_cents(value)

    available_on = row["available_on"] if from_charge else day(f.available_on, "The date")
    cost = row["cost_cents"] if from_charge else money(f.cost, "Cost")
    disposed_on = day(f.disposed_on, "The date sold")
    proceeds = money(f.proceeds, "Proceeds")
    if (disposed_on is None) != (proceeds is None) and not errors:
        errors.append("Enter both the date sold and the proceeds, or neither.")
    missing = [n for n, v in (("name", f.name.strip()), ("date", available_on), ("cost", cost)) if v in (None, "")]
    if missing and not errors:
        errors.append(f"Enter the {', '.join(missing)}.")
    if errors:
        return None
    values = (f.name.strip(), available_on, cost, f.cls, f.business, business.pct_text(use), disposed_on, proceeds)
    cols = "name, available_on, cost_cents, cls, business, use_pct, disposed_on, proceeds_cents"
    if asset_id is None:
        asset_id = conn.execute(f"INSERT INTO assets ({cols}) VALUES ({', '.join('?' * 8)})", values).lastrowid
    else:
        sets = ", ".join(f"{c.strip()} = ?" for c in cols.split(","))
        conn.execute(f"UPDATE assets SET {sets} WHERE id = ?", (*values, asset_id))
    conn.commit()
    return asset_id


def set_claim(conn: sqlite3.Connection, year: int, biz: str, cls: str, text: str) -> str | None:
    """Claim less than the most allowed for a class (empty = the most). Returns an error, or None."""
    if not text.strip():
        conn.execute("DELETE FROM cca_claims WHERE year = ? AND business = ? AND cls = ?", (year, biz, cls))
        conn.commit()
        return None
    value = parse_amount(text)
    if value is None or value < 0:
        return "The claim isn't a valid amount."
    conn.execute(
        "INSERT INTO cca_claims (year, business, cls, claim_cents) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(year, business, cls) DO UPDATE SET claim_cents = excluded.claim_cents",
        (year, biz, cls, to_cents(value)),
    )
    conn.commit()
    return None


@dataclass
class AssetShare:
    asset: sqlite3.Row
    cents: int  # this business's part of the cost
    proceeds: int  # and of the proceeds when sold (capped at cost)
    label: str


def asset_shares(conn: sqlite3.Connection, biz: str) -> list[AssetShare]:
    """Every asset's part for one business: business-use %, then the Shared split of
    the year the asset became available for use."""
    shares = []
    for a in conn.execute("SELECT * FROM assets ORDER BY available_on, id"):
        if a["business"] not in (biz, business.SHARED):
            continue
        use = Decimal(a["use_pct"])
        cost = _round(Decimal(a["cost_cents"]) * use / 100)
        proceeds = _round(Decimal(min(a["proceeds_cents"] or 0, a["cost_cents"])) * use / 100)
        label = business.pct_text(use) + "% business use" if use < 100 else ""
        if a["business"] == business.SHARED:
            pct = business.futures_pct(conn, int(a["available_on"][:4]))
            fut, opt = business.split_cents(cost, pct)
            fut_p, opt_p = business.split_cents(proceeds, pct)
            share = pct if biz == business.FUTURES else 100 - pct
            cost, proceeds = (fut, fut_p) if biz == business.FUTURES else (opt, opt_p)
            label = ", ".join(x for x in (f"Shared · {business.pct_text(share)}%", label) if x)
        shares.append(AssetShare(a, cost, proceeds, label or biz))
    return shares


@dataclass
class ClassRow:
    cls: str
    name: str
    rate: Decimal
    opening: int = 0
    additions: int = 0
    proceeds: int = 0
    boost: int = 0
    most: int = 0
    claim: int = 0
    recapture: int = 0
    terminal_loss: int = 0
    claim_set: bool = False

    @property
    def after(self) -> int:  # column 7
        return self.opening + self.additions - self.proceeds

    @property
    def base(self) -> int:  # column 19
        return max(self.after, 0) + self.boost

    @property
    def closing(self) -> int:  # column 22
        if self.recapture or self.terminal_loss:
            return 0
        return self.after - self.claim


@dataclass
class Schedule:
    year: int
    business: str
    rows: list[ClassRow]
    assets: list[AssetShare]

    @property
    def total(self) -> int:  # amount ii, to line 9936
        return sum(r.claim for r in self.rows)

    @property
    def recapture(self) -> int:  # to line 8230
        return sum(r.recapture for r in self.rows)

    @property
    def terminal_loss(self) -> int:  # to line 9270
        return sum(r.terminal_loss for r in self.rows)


def schedule(conn: sqlite3.Connection, year: int, biz: str) -> Schedule:
    shares = asset_shares(conn, biz)
    claims = {
        (r["year"], r["cls"]): r["claim_cents"]
        for r in conn.execute("SELECT * FROM cca_claims WHERE business = ?", (biz,))
    }
    first = min([int(s.asset["available_on"][:4]) for s in shares] + [year])
    closing: dict[str, int] = {}
    rows: dict[str, ClassRow] = {}
    for y in range(first, year + 1):
        rows = {}
        for cls, (name, rate) in CLASSES.items():
            row = ClassRow(cls, name, rate, opening=closing.get(cls, 0))
            boost_base = Decimal(0)
            held = False  # any asset of the class still owned at the end of the year
            for s in shares:
                a = s.asset
                if a["cls"] != cls or int(a["available_on"][:4]) > y:
                    continue
                sold = a["disposed_on"] and int(a["disposed_on"][:4]) <= y
                held |= not sold
                if int(a["available_on"][:4]) == y:
                    row.additions += s.cents
                    boost_base += Decimal(s.cents) * first_year_factor(cls, date.fromisoformat(a["available_on"]))
                if a["disposed_on"] and int(a["disposed_on"][:4]) == y:
                    row.proceeds += s.proceeds
            # Proceeds first reduce the additions that get the boost (Area A columns 14 to 17)
            if row.additions:
                factor = boost_base / row.additions
                row.boost = _round(max(Decimal(row.additions - row.proceeds), Decimal(0)) * factor)
            if row.after < 0:
                row.recapture = -row.after
            elif not held and row.after > 0 and (row.opening or row.additions):
                row.terminal_loss = row.after
            else:
                row.most = min(_round(Decimal(row.base) * rate), row.after)
                row.claim_set = (y, cls) in claims
                row.claim = min(claims.get((y, cls), row.most), row.most)
            closing[cls] = row.closing
            if row.opening or row.additions or row.proceeds:
                rows[cls] = row
    in_year = [
        s for s in shares
        if int(s.asset["available_on"][:4]) <= year
        and not (s.asset["disposed_on"] and int(s.asset["disposed_on"][:4]) < year)
    ]
    return Schedule(year, biz, list(rows.values()), in_year)
