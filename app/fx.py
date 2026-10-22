"""USD -> CAD conversion using Bank of Canada daily rates (Valet API).

Rates are cached in the fx_rates table. A transaction on a weekend or holiday
uses the most recent prior business-day rate, which is what CRA accepts.
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


def fill_missing_cad(conn: sqlite3.Connection, fetcher: Fetcher | None = fetch_boc) -> int:
    """Set cad_cents on any USD transactions that don't have it yet.

    If a fetcher is given, missing rates are downloaded first. Returns the
    number of transactions still lacking a rate (e.g. when offline).
    """
    rows = conn.execute(
        "SELECT id, tax_date, amount_cents FROM transactions "
        "WHERE currency = 'USD' AND cad_cents IS NULL"
    ).fetchall()
    if not rows:
        return 0

    if fetcher is not None:
        dates = [date.fromisoformat(r["tax_date"]) for r in rows]
        try:
            ensure_rates(conn, min(dates), max(dates), fetcher)
        except OSError:
            pass  # offline; leave unconverted and report below

    missing = 0
    for r in rows:
        rate = _cached_rate(conn, date.fromisoformat(r["tax_date"]))
        if rate is None:
            missing += 1
            continue
        conn.execute(
            "UPDATE transactions SET fx_rate = ?, cad_cents = ? WHERE id = ?",
            (str(rate), convert_cents(r["amount_cents"], rate), r["id"]),
        )
    conn.commit()
    return missing
