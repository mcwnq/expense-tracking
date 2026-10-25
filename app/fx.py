"""USD -> CAD conversion using Bank of Canada rates (Valet API).

Each tax year uses either daily rates or the Bank of Canada annual average
rate (both are accepted by CRA). Daily rates are cached in the fx_rates
table; a transaction on a weekend or holiday uses the most recent prior
business-day rate.
"""

import json
import sqlite3
import urllib.request
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

VALET_URL = (
    "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
    "?start_date={start}&end_date={end}"
)

# How far back to look for a prior business-day rate (covers long weekends).
LOOKBACK_DAYS = 7

ANNUAL_URL = (
    "https://www.bankofcanada.ca/valet/observations/FXAUSDCAD/json"
    "?start_date={year}-01-01&end_date={year}-12-31"
)

Fetcher = Callable[[date, date], dict[str, str]]


def fetch_boc(start: date, end: date) -> dict[str, str]:
    """Return {iso_date: rate} from the Bank of Canada for the given range."""
    url = VALET_URL.format(start=start.isoformat(), end=end.isoformat())
    with urllib.request.urlopen(url, timeout=15) as resp:
        payload = json.load(resp)
    return {
        obs["d"]: obs["FXUSDCAD"]["v"]
        for obs in payload.get("observations", [])
        if obs.get("FXUSDCAD", {}).get("v")
    }


def fetch_boc_annual(year: int) -> str | None:
    """Bank of Canada annual average USD->CAD for a year, once published (early January)."""
    with urllib.request.urlopen(ANNUAL_URL.format(year=year), timeout=15) as resp:
        payload = json.load(resp)
    for obs in payload.get("observations", []):
        value = obs.get("FXAUSDCAD", {}).get("v")
        if value:
            return value
    return None


def year_setting(conn: sqlite3.Connection, year: int) -> tuple[str, Decimal | None]:
    row = conn.execute("SELECT fx_method, annual_rate FROM tax_years WHERE year = ?", (year,)).fetchone()
    if not row:
        return "daily", None
    return row["fx_method"], Decimal(row["annual_rate"]) if row["annual_rate"] else None


def set_year_method(
    conn: sqlite3.Connection, year: int, method: str, annual_rate: str | None, fetcher: Fetcher | None = fetch_boc
) -> int:
    """Switch a tax year between daily and annual-average conversion and re-convert
    its USD transactions and payouts. Returns how many are still unconverted."""
    rate = Decimal(annual_rate) if annual_rate else None
    conn.execute(
        "INSERT INTO tax_years (year, fx_method, annual_rate) VALUES (?, ?, ?) "
        "ON CONFLICT(year) DO UPDATE SET fx_method = excluded.fx_method, annual_rate = excluded.annual_rate",
        (year, method, str(rate) if rate else None),
    )
    for table, date_col, _ in CONVERTED:
        conn.execute(
            f"UPDATE {table} SET fx_rate = NULL, cad_cents = NULL "
            f"WHERE currency = 'USD' AND substr({date_col}, 1, 4) = ?",
            (str(year),),
        )
    conn.commit()
    return fill_missing_cad(conn, fetcher)


def store_rates(conn: sqlite3.Connection, rates: dict[str, str], source: str = "boc") -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO fx_rates (rate_date, usd_cad, source) VALUES (?, ?, ?)",
        [(d, r, source) for d, r in rates.items()],
    )
    conn.commit()


def _cached_rate(conn: sqlite3.Connection, on: date) -> Decimal | None:
    row = conn.execute(
        "SELECT usd_cad FROM fx_rates WHERE rate_date <= ? AND rate_date >= ? "
        "ORDER BY rate_date DESC LIMIT 1",
        (on.isoformat(), (on - timedelta(days=LOOKBACK_DAYS)).isoformat()),
    ).fetchone()
    return Decimal(row["usd_cad"]) if row else None


def ensure_rates(
    conn: sqlite3.Connection, start: date, end: date, fetcher: Fetcher = fetch_boc
) -> int:
    """Fetch and cache rates covering [start, end]. Returns number of rates stored."""
    rates = fetcher(start - timedelta(days=LOOKBACK_DAYS), end)
    store_rates(conn, rates)
    return len(rates)


def usd_to_cad_rate(conn: sqlite3.Connection, on: date) -> Decimal | None:
    """Cached rate for a date, or None if it hasn't been fetched yet."""
    return _cached_rate(conn, on)


def convert_cents(cents: int, rate: Decimal) -> int:
    return int((Decimal(cents) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# Tables holding USD amounts to convert: (table, date column, amount column)
CONVERTED = [("transactions", "tax_date", "amount_cents"), ("payouts", "received_on", "received_cents")]


def fill_missing_cad(conn: sqlite3.Connection, fetcher: Fetcher | None = fetch_boc) -> int:
    """Set cad_cents on any USD transactions and payouts that don't have it yet.

    If a fetcher is given, missing rates are downloaded first. Returns the
    number still lacking a rate (e.g. when offline).
    """
    rows = [
        (table, r["id"], r["day"], r["cents"])
        for table, date_col, amount_col in CONVERTED
        for r in conn.execute(
            f"SELECT id, {date_col} AS day, {amount_col} AS cents FROM {table} "
            "WHERE currency = 'USD' AND cad_cents IS NULL"
        )
    ]
    if not rows:
        return 0

    annual: dict[int, Decimal | None] = {}
    for _, _, on, _ in rows:
        year = int(on[:4])
        if year not in annual:
            method, rate = year_setting(conn, year)
            annual[year] = rate if method == "annual" else None

    needs_daily = [date.fromisoformat(on) for _, _, on, _ in rows if annual[int(on[:4])] is None]
    if fetcher is not None and needs_daily:
        try:
            ensure_rates(conn, min(needs_daily), max(needs_daily), fetcher)
        except OSError:
            pass  # offline; leave unconverted and report below

    missing = 0
    for table, row_id, on, cents in rows:
        rate = annual[int(on[:4])] or _cached_rate(conn, date.fromisoformat(on))
        if rate is None:
            missing += 1
            continue
        conn.execute(
            f"UPDATE {table} SET fx_rate = ?, cad_cents = ? WHERE id = ?",
            (str(rate), convert_cents(cents, rate), row_id),
        )
    conn.commit()
    return missing
