from datetime import date

import pytest

from app import db, importer

TD_STYLE = """01/03/2026,TOPSTEP EVAL,165.00,,1200.00
01/15/2026,PAYMENT THANK YOU,,500.00,700.00
01/20/2026,TRADINGVIEW,"1,014.95",,1714.95
"""

HEADER_STYLE = """Transaction Date,Description,Amount
2026-02-01,RITHMIC DATA,-25.00
2026-02-01,RITHMIC DATA,-25.00
2026-02-05,REFUND APEX,40.00
"""


def test_guess_mapping_without_header_uses_debit_credit():
    m = importer.guess_mapping(TD_STYLE)
    assert not m.has_header
    assert (m.date_col, m.description_col, m.debit_col, m.credit_col) == ("1", "2", "3", "4")
    assert m.date_format == "%m/%d/%Y"


def test_parse_debit_credit():
    rows = importer.parse(TD_STYLE, importer.guess_mapping(TD_STYLE))
    assert [(r.txn_date, r.amount_cents) for r in rows] == [
        (date(2026, 1, 3), 16500),
        (date(2026, 1, 15), -50000),
        (date(2026, 1, 20), 101495),
    ]


def test_guess_mapping_with_header_and_negative_charges():
    m = importer.guess_mapping(HEADER_STYLE)
    assert m.has_header
    assert (m.date_col, m.description_col, m.amount_col) == ("Transaction Date", "Description", "Amount")
    m.charges_positive = False
    rows = importer.parse(HEADER_STYLE, m)
    assert [r.amount_cents for r in rows] == [2500, 2500, -4000]


def test_parentheses_and_currency_symbols():
    assert importer._try_amount("($1,234.50)") == -1234.50
    assert importer._try_amount("US$ 12.00") == 12


def test_missing_column_is_reported():
    m = importer.guess_mapping(HEADER_STYLE)
    m.amount_col = "Nope"
    with pytest.raises(importer.CsvFormatError, match="Nope"):
        importer.parse(HEADER_STYLE, m)


def test_reimport_skips_duplicates_but_keeps_identical_same_day_charges(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO accounts (name, currency) VALUES ('Visa', 'CAD')")
    account = conn.execute("SELECT * FROM accounts").fetchone()
    m = importer.guess_mapping(HEADER_STYLE)
    rows = importer.parse(HEADER_STYLE, m)

    assert importer.save(conn, account, rows, "feb.csv")[:2] == (3, 0)
    assert importer.save(conn, account, rows, "feb.csv")[:2] == (0, 3)
    cad = [r[0] for r in conn.execute("SELECT cad_cents FROM transactions ORDER BY id")]
    assert cad == [-2500, -2500, 4000]
