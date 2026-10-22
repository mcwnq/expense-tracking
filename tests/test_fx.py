from datetime import date
from decimal import Decimal

from app import db, fx


def fake_fetcher(calls):
    def fetch(start, end):
        calls.append((start, end))
        # Friday rate only; the weekend should fall back to it
        return {"2026-01-02": "1.3700", "2026-01-05": "1.3800"}
    return fetch


def test_weekend_uses_prior_business_day_and_converts(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO accounts (name, currency) VALUES ('USD card', 'USD')")
    for i, (d, cents) in enumerate([("2026-01-03", 10000), ("2026-01-05", 1999)]):
        conn.execute(
            "INSERT INTO transactions (account_id, txn_date, description, amount_cents, currency, dedupe_key) "
            "VALUES (1, ?, 'X', ?, 'USD', ?)", (d, cents, str(i)))
    calls = []
    assert fx.fill_missing_cad(conn, fake_fetcher(calls)) == 0
    rows = conn.execute("SELECT fx_rate, cad_cents FROM transactions ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("1.3700", 13700), ("1.3800", 2759)]
    assert len(calls) == 1


def test_offline_leaves_rows_unconverted(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO accounts (name, currency) VALUES ('USD card', 'USD')")
    conn.execute(
        "INSERT INTO transactions (account_id, txn_date, description, amount_cents, currency, dedupe_key) "
        "VALUES (1, '2026-01-03', 'X', 100, 'USD', 'k')")

    def offline(start, end):
        raise OSError("no network")

    assert fx.fill_missing_cad(conn, offline) == 1


def test_convert_rounds_half_up():
    assert fx.convert_cents(1, Decimal("1.5")) == 2
