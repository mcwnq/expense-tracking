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

def test_transactions_filter_all(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    assert client.get("/transactions?account_id=&month=").status_code == 200


def test_annual_average_rate(tmp_path):
    daily = lambda s, e: {"2026-03-02": "1.35"}
    client = TestClient(create_app(tmp_path / "t.db", fetcher=daily, annual_fetcher=lambda y: None))
    client.post("/accounts", data={"name": "USD", "currency": "USD"})
    data = "Date,Description,Amount\n2026-03-02,NINJATRADER,100.00\n"
    client.post("/import/confirm", data={"account_id": 1, "filename": "a.csv", "csv_text": data, "date_col": "Date",
                "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
                "format_name": "x"})
    assert "135.00" in client.get("/summary?year=2026").text

    r = client.post("/tax-years/2026/fx", data={"method": "annual", "annual_rate": ""})
    assert "isn" in r.text and "available yet" in r.text   # no published rate, nothing changed
    r = client.post("/tax-years/2026/fx", data={"method": "annual", "annual_rate": "1.40"})
    assert "140.00" in r.text and "use 1.40" in r.text
    r = client.post("/tax-years/2026/fx", data={"method": "daily", "annual_rate": ""})
    assert "135.00" in r.text


def test_exclude_route(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    data = "Date,Description,Amount\n2026-03-02,NINJATRADER,99.00\n2026-03-03,NINJATRADER MERCH,25.00\n"
    client.post("/import/confirm", data={"account_id": 1, "filename": "a.csv", "csv_text": data, "date_col": "Date",
                "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
                "format_name": "x"})
    r = client.post("/transactions/2/exclude", data={"excluded": 1})
    assert 'class="excluded"' in r.text and "net 99.00 CAD" in r.text
    assert "124.00" not in client.get("/summary?year=2026").text


def test_covers_route(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    data = "Date,Description,Amount\n2026-06-30,TRADINGVIEW,365.00\n"
    client.post("/import/confirm", data={"account_id": 1, "filename": "a.csv", "csv_text": data, "date_col": "Date",
                "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
                "format_name": "x"})
    r = client.post("/transactions/1/covers", data={"months": "12"})
    assert '<option value="12" selected>' in r.text
    r = client.get("/summary?year=2026")
    assert "185.00" in r.text and "$180.00 paid in 2026 counts in later years" in r.text
    assert "$180.00 paid in earlier years counts in 2027" in client.get("/summary?year=2027").text


def test_include_all_and_card_default(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "TD", "currency": "CAD"})
    client.post("/accounts", data={"name": "Amex", "currency": "CAD"})
    client.post("/accounts/1/default", data={"include_by_default": 0})
    base = {"filename": "a.csv", "date_col": "Date", "description_col": "Description", "amount_col": "Amount",
            "charges_positive": 1, "has_header": 1, "format_name": "x"}
    td = "Date,Description,Amount\n2026-03-02,GROCERY STORE,50.00\n2026-03-03,MYSTERY SHOP,20.00\n"
    amex = "Date,Description,Amount\n2026-03-04,OTHER SHOP,10.00\n"
    client.post("/import/confirm", data={**base, "account_id": 1, "csv_text": td})
    client.post("/import/confirm", data={**base, "account_id": 2, "csv_text": amex})

    # TD lines start excluded, so they don't ask for a vendor; Amex does
    r = client.get("/summary?year=2026")
    assert "OTHER SHOP" in r.text and "GROCERY STORE" not in r.text and "MYSTERY SHOP" not in r.text

    r = client.get("/transactions?account_id=1&month=")
    assert "0 included" in r.text and 'aria-label="Include all shown transactions">' in r.text

    # include all TD lines, then the "needs a vendor" list picks them up
    r = client.post("/transactions/include-all", data={"account_id": "1", "month": "", "included": 1})
    assert "2 included" in r.text
    assert "GROCERY STORE" in client.get("/summary?year=2026").text
    # include-all only touched the filtered card
    client.post("/transactions/include-all", data={"account_id": "1", "month": "", "included": 0})
    assert "1 included" in client.get("/transactions?account_id=&month=").text


def test_edit_and_delete_card(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=lambda s, e: {}))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    client.post("/accounts/1/edit", data={"name": "RBC Visa USD", "currency": "USD"})
    r = client.get("/")
    assert 'value="RBC Visa USD"' in r.text and "<option selected>USD</option>" in r.text

    data = "Date,Description,Amount\n2026-03-02,NINJATRADER,99.00\n"
    client.post("/import/confirm", data={"account_id": 1, "filename": "a.csv", "csv_text": data, "date_col": "Date",
                "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
                "format_name": "x"})
    assert "99.00</td>" in client.get("/transactions").text and "—" in client.get("/transactions").text  # no USD rate offline
    client.post("/accounts/1/edit", data={"name": "Renamed", "currency": "CAD"})  # fix a wrong currency
    r = client.get("/")
    assert 'value="Renamed"' in r.text and "<option selected>CAD</option>" in r.text
    t = client.get("/transactions").text
    assert "<td>CAD</td>" in t and "net 99.00 CAD" in t

    client.post("/accounts/1/delete")
    assert "Renamed" not in client.get("/").text
    assert "NINJATRADER" not in client.get("/transactions").text
