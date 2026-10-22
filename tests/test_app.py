from fastapi.testclient import TestClient

from app.main import create_app

CSV = "Date,Description,Amount\n2026-03-02,NINJATRADER,99.00\n2026-03-09,CME DATA,12.50\n"


def test_import_flow(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=lambda s, e: {"2026-03-02": "1.35", "2026-03-09": "1.36"}))
    client.post("/accounts", data={"name": "Amex USD", "currency": "USD"})

    r = client.post("/import/preview", data={"account_id": 1}, files={"file": ("mar.csv", CSV, "text/csv")})
    assert r.status_code == 200 and "NINJATRADER" in r.text and "Import 2 rows" in r.text

    form = {"account_id": 1, "filename": "mar.csv", "csv_text": CSV, "date_col": "Date",
            "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
            "format_name": "Amex"}
    r = client.post("/import/confirm", data=form)
    assert "Added <strong>2</strong>" in r.text

    r = client.get("/transactions?account_id=1")
    assert "133.65" in r.text  # 99.00 * 1.35
    assert "17.00" in r.text   # 12.50 * 1.36

    # saved format is matched on the next upload
    r = client.post("/import/preview", data={"account_id": 1}, files={"file": ("mar.csv", CSV, "text/csv")})
    assert "Matched saved format" in r.text and 'value="Amex"' in r.text
    assert "Amex" in client.get("/formats").text


TD = "01/03/2026,TOPSTEP,165.00,,1200.00\n"


def test_formats_shared_across_cards_and_several_per_card(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    client.post("/accounts", data={"name": "Visa 2", "currency": "CAD"})
    base = {"filename": "f.csv", "charges_positive": 1}
    client.post("/import/confirm", data={**base, "account_id": 1, "csv_text": CSV, "date_col": "Date",
                "description_col": "Description", "amount_col": "Amount", "has_header": 1, "format_name": "Header style"})
    client.post("/import/confirm", data={**base, "account_id": 1, "csv_text": TD, "date_col": "1",
                "description_col": "2", "debit_col": "3", "credit_col": "4", "has_header": 0, "format_name": "TD style"})

    # card 1 has used both formats; each file picks the right one
    r = client.post("/import/preview", data={"account_id": 1}, files={"file": ("a.csv", CSV, "text/csv")})
    assert "Header style" in r.text and "Matched" in r.text
    r = client.post("/import/preview", data={"account_id": 1}, files={"file": ("b.csv", TD, "text/csv")})
    assert 'value="TD style"' in r.text and "Matched" in r.text
    # a different card reuses the same format
    r = client.post("/import/preview", data={"account_id": 2}, files={"file": ("c.csv", TD, "text/csv")})
    assert 'value="TD style"' in r.text and "Matched" in r.text


def test_summary_and_assign(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    data = "Date,Description,Amount\n2026-03-02,NINJATRADER,99.00\n2026-03-09,ZEN FUTURES*EVAL 50K 8443,80.00\n"
    client.post("/import/confirm", data={"account_id": 1, "filename": "a.csv", "csv_text": data, "date_col": "Date",
                "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
                "format_name": "Amex"})
    r = client.get("/summary?year=2026")
    assert "NinjaTrader" in r.text and "Transactions that need a vendor" in r.text and 'value="ZEN FUTURES"' in r.text

    client.post("/assign", data={"pattern": "ZEN FUTURES", "vendor_id": "new", "new_vendor": "MyFF", "category": "Prop firm evaluations"})
    r = client.get("/summary?year=2026")
    assert "MyFF" in r.text and "Transactions that need a vendor" not in r.text
    assert "179.00" in r.text

    csv_text = client.get("/summary.csv?year=2026").text
    assert "Prop firm evaluations,MyFF,one-time" in csv_text
    assert client.get("/vendors").status_code == 200
