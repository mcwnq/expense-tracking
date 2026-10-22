from fastapi.testclient import TestClient

from app.main import create_app

CSV = "Date,Description,Amount\n2026-03-02,NINJATRADER,99.00\n2026-03-09,CME DATA,12.50\n"


def test_import_flow(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=lambda s, e: {"2026-03-02": "1.35", "2026-03-09": "1.36"}))
    client.post("/accounts", data={"name": "Amex USD", "currency": "USD"})

    r = client.post("/import/preview", data={"account_id": 1}, files={"file": ("mar.csv", CSV, "text/csv")})
    assert r.status_code == 200 and "NINJATRADER" in r.text and "Import 2 rows" in r.text

    form = {"account_id": 1, "filename": "mar.csv", "csv_text": CSV, "date_col": "Date",
            "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1}
    r = client.post("/import/confirm", data=form)
    assert "Added <strong>2</strong>" in r.text

    r = client.get("/transactions?account_id=1")
    assert "133.65" in r.text  # 99.00 * 1.35
    assert "17.00" in r.text   # 12.50 * 1.36

    # mapping remembered for next upload
    r = client.post("/import/preview", data={"account_id": 1}, files={"file": ("mar.csv", CSV, "text/csv")})
    assert 'value="Amount" selected' in r.text
