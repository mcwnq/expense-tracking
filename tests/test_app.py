import re

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
    assert "0 included" in r.text and "Include all 2 shown" in r.text and 'name="ids" value="2,1"' in r.text
    assert 'aria-label="Include all shown transactions"' in r.text
    assert 'aria-label="Include all shown transactions"' in client.get("/transactions?account_id=1&view=merchants").text

    # include all TD lines, then the "needs a vendor" list picks them up
    back = "/transactions?account_id=1&month="
    r = client.post("/transactions/set-included", data={"ids": "2,1", "included": 1, "back": back})
    assert "2 included" in r.text
    assert "GROCERY STORE" in client.get("/summary?year=2026").text
    # only the lines shown were touched
    client.post("/transactions/set-included", data={"ids": "2,1", "included": 0, "back": back})
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


def test_card_payments_excluded_refunds_kept_and_vendor_warning(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    data = ("Date,Description,Amount\n2026-03-02,TRADOVATE LLC,99.00\n2026-03-05,TRADOVATE LLC,99.00\n"
            "2026-03-09,TRADOVATE REFUND,-20.00\n2026-03-15,PAYMENT - THANK YOU,-500.00\n")
    client.post("/import/confirm", data={"account_id": 1, "filename": "a.csv", "csv_text": data, "date_col": "Date",
                "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
                "format_name": "x"})
    t = client.get("/transactions").text
    assert "3 included" in t and "net 178.00 CAD" in t   # payment excluded, refund kept
    assert t.count("Similar charges from") == 3           # every Tradovate line warns; the payment doesn't
    assert '\\&#34;Tradovate\\&#34; (2 more)' in t


def test_migration_excludes_existing_payments(tmp_path):
    import sqlite3
    from app import db
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO accounts (name, currency) VALUES ('Visa', 'CAD')")
    conn.execute("INSERT INTO transactions (account_id, txn_date, tax_date, description, amount_cents, currency, cad_cents, dedupe_key) "
                 "VALUES (1, '2026-01-01', '2026-01-01', 'ONLINE PAYMENT 123', -5000, 'CAD', -5000, 'k')")
    conn.execute("PRAGMA user_version = 5"); conn.commit(); conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT excluded FROM transactions").fetchone()[0] == 1


def test_wrong_card_warnings_and_undo_import(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=lambda s, e: {}))
    client.post("/accounts", data={"name": "TD CAD", "currency": "CAD"})
    client.post("/accounts", data={"name": "TD USD", "currency": "USD"})
    td = {"filename": "cad.csv", "csv_text": TD, "date_col": "1", "description_col": "2", "debit_col": "3",
          "credit_col": "4", "has_header": 0, "charges_positive": 1, "format_name": "TD"}
    client.post("/import/confirm", data={**td, "account_id": 1})
    amex = {"filename": "usd.csv", "csv_text": CSV, "date_col": "Date", "description_col": "Description",
            "amount_col": "Amount", "has_header": 1, "charges_positive": 1, "format_name": "Amex"}
    r = client.post("/import/confirm", data={**amex, "account_id": 2})
    assert "Undo this import" in r.text

    # same TD file on the USD card: lines already on TD CAD, and a different shape from that card's files
    r = client.post("/import/preview", data={"account_id": 2}, files={"file": ("cad.csv", TD, "text/csv")})
    assert "1 of these lines are already on TD CAD" in r.text
    assert "don&#39;t match the files you imported to this card before (earlier files used Amex)" in r.text
    # a matching file on the right card has no warning
    r = client.post("/import/preview", data={"account_id": 2}, files={"file": ("usd2.csv", CSV, "text/csv")})
    assert "Check the card" not in r.text

    # import it anyway, then remove just that import from the Imports page
    client.post("/import/confirm", data={**td, "account_id": 2, "filename": "oops.csv"})
    page = client.get("/imports").text
    assert "oops.csv" in page and "TD USD (USD)" in page
    r = client.post("/batches/3/delete", data={"back": "/imports"})
    assert r.url.path == "/imports" and "oops.csv" not in r.text
    assert "TOPSTEP" not in client.get("/transactions?account_id=2").text
    assert "TOPSTEP" in client.get("/transactions?account_id=1").text


def test_migration_records_batch_signatures(tmp_path):
    from app import db

    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO csv_formats (name, signature, date_col, description_col) VALUES ('Amex', 'date|description|amount', 'Date', 'Description')")
    conn.execute("INSERT INTO accounts (name, currency, last_format_id) VALUES ('Amex', 'USD', 1)")
    conn.execute("INSERT INTO import_batches (account_id, filename) VALUES (1, 'old.csv')")
    conn.execute("UPDATE import_batches SET signature = NULL")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT signature FROM import_batches").fetchone()[0] == "date|description|amount"


def test_search_merchant_view_and_grouped_needs_vendor(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "TD", "currency": "CAD"})
    data = ("Date,Description,Amount\n2026-03-02,GROCERY STORE #12,50.00\n2026-04-02,GROCERY STORE #40,30.00\n"
            "2026-03-05,TOPSTEP COMBINE,165.00\n2026-03-07,COFFEE HUT,4.00\n")
    client.post("/import/confirm", data={"account_id": 1, "filename": "a.csv", "csv_text": data, "date_col": "Date",
                "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
                "format_name": "x"})

    # search and filter
    r = client.get("/transactions?q=grocery")
    assert "GROCERY STORE #12" in r.text and "TOPSTEP COMBINE" not in r.text and "Include all 2 shown" in r.text
    r = client.get("/transactions?q=topstep")  # matches the vendor too
    assert "TOPSTEP COMBINE" in r.text

    # by-merchant view: one row per merchant, one tick excludes all its lines
    r = client.get("/transactions?view=merchants")
    assert "GROCERY STORE" in r.text and "2 of 2" in r.text and "Topstep" in r.text
    assert 'aria-label="Include all 2 lines from GROCERY STORE"' in r.text
    client.post("/transactions/set-included", data={"ids": "1,2", "included": 0, "back": "/transactions?view=merchants"})
    r = client.get("/transactions?show=excluded")
    assert "GROCERY STORE #12" in r.text and "GROCERY STORE #40" in r.text and "COFFEE HUT" not in r.text

    # bring them back; the summary groups lines needing a vendor with one Exclude for the group
    client.post("/transactions/set-included", data={"ids": "1,2", "included": 1, "back": "/"})
    r = client.get("/summary?year=2026")
    assert "Exclude all 2 lines" in r.text and 'name="ids" value="1,2"' in r.text and "Exclude this line" in r.text
    assert r.text.count('value="GROCERY STORE"') == 1


def test_import_several_files_together(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    jan = "Date,Description,Amount\n2026-01-05,NINJATRADER,99.00\n2026-01-20,CME DATA,12.50\n"
    feb = "Date,Description,Amount\n2026-01-20,CME DATA,12.50\n2026-02-05,NINJATRADER,99.00\n"  # overlaps jan
    files = [("file", ("jan.csv", jan, "text/csv")), ("file", ("feb.csv", feb, "text/csv"))]
    r = client.post("/import/preview", data={"account_id": 1}, files=files)
    assert "2 files use one mapping" in r.text and "Import 4 rows" in r.text
    assert "jan.csv" in r.text and "feb.csv" in r.text

    form = {"account_id": 1, "filename": ["jan.csv", "feb.csv"], "csv_text": [jan, feb], "date_col": "Date",
            "description_col": "Description", "amount_col": "Amount", "charges_positive": 1, "has_header": 1,
            "format_name": "Visa"}
    r = client.post("/import/confirm", data=form)
    assert "Added <strong>3</strong>" in r.text and "Skipped 1 already imported" in r.text
    page = client.get("/imports").text
    assert "jan.csv" in page and "feb.csv" in page  # one import per file

    # files with different columns can't share a mapping
    other = "Posted,Payee,Debit,Credit\n2026-03-01,TOPSTEP,165.00,\n"
    files = [("file", ("a.csv", jan, "text/csv")), ("file", ("b.csv", other, "text/csv"))]
    r = client.post("/import/preview", data={"account_id": 1}, files=files)
    assert "don&#39;t all have the same columns" in r.text and "disabled" in r.text


def test_add_and_delete_manual_transaction(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=lambda s, e: {"2026-03-02": "1.40"}))
    client.post("/accounts", data={"name": "Amex USD", "currency": "USD"})
    r = client.post("/transactions/add", data={"account_id": 1, "txn_date": "2026-03-02",
                    "description": "Topstep combine", "amount": "$100.00", "back": "/transactions"})
    assert "Topstep combine" in r.text and "manual" in r.text
    assert "140.00" in r.text  # converted to CAD, and the Topstep rule assigned the vendor
    assert "Topstep" in client.get("/summary?year=2026").text

    # bad amount is ignored
    client.post("/transactions/add", data={"account_id": 1, "txn_date": "2026-03-03", "description": "x", "amount": "abc"})
    assert "2026-03-03" not in client.get("/transactions").text

    r = client.post("/transactions/1/delete", data={"back": "/transactions"})
    assert "Topstep combine" not in r.text


def test_summary_split_by_currency_and_all_in_usd(tmp_path):
    rates = {"2026-03-02": "1.25", "2026-03-09": "1.25"}
    client = TestClient(create_app(tmp_path / "t.db", fetcher=lambda s, e: rates))
    client.post("/accounts", data={"name": "Amex USD", "currency": "USD"})
    client.post("/accounts", data={"name": "TD CAD", "currency": "CAD"})
    client.post("/transactions/add", data={"account_id": 1, "txn_date": "2026-03-02", "description": "TRADOVATE", "amount": "100"})
    client.post("/transactions/add", data={"account_id": 2, "txn_date": "2026-03-09", "description": "TRADOVATE", "amount": "50"})

    # always both currency sections, then the combined total in CAD by default
    r = client.get("/summary?year=2026")
    assert "Charged in USD · $100.00 USD" in r.text and "Charged in CAD · $50.00 CAD" in r.text
    assert "All charges in CAD · $175.00 CAD" in r.text  # 100 USD * 1.25 + 50 CAD
    r = client.get("/summary?year=2026&cur=USD")
    assert "Charged in USD · $100.00 USD" in r.text and "Charged in CAD · $50.00 CAD" in r.text
    assert "All charges in USD · $140.00 USD" in r.text  # 100 USD + 50 CAD / 1.25
    assert "140.00" in client.get("/summary.csv?year=2026&cur=USD").text


def test_removing_an_import_keeps_lines_a_later_import_also_had(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    base = {"account_id": 1, "date_col": "Date", "description_col": "Description", "amount_col": "Amount",
            "charges_positive": 1, "has_header": 1, "format_name": "Visa"}
    sep = "Date,Description,Amount\n2026-09-05,NINJATRADER,99.00\n2026-09-20,CME DATA,12.50\n"
    year = sep + "2026-10-05,NINJATRADER,99.00\n"  # overlaps September
    client.post("/import/confirm", data={**base, "filename": "sep.csv", "csv_text": sep})
    r = client.post("/import/confirm", data={**base, "filename": "year.csv", "csv_text": year})
    assert "Added <strong>1</strong>" in r.text and "Skipped 2" in r.text

    # removing September keeps its lines, because the year file has them too
    client.post("/batches/1/delete", data={"back": "/imports"})
    page = client.get("/transactions").text
    assert "3 transactions" in page and "2026-09-20" in page
    assert "year.csv" in client.get("/imports").text and "sep.csv" not in client.get("/imports").text
    # removing the year file then removes everything
    client.post("/batches/2/delete", data={"back": "/imports"})
    assert "0 transactions" in client.get("/transactions").text


def test_migration_records_lines_of_existing_imports(tmp_path):
    from app import db

    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO accounts (name, currency) VALUES ('Visa', 'CAD')")
    conn.execute("INSERT INTO import_batches (account_id, filename) VALUES (1, 'a.csv')")
    conn.execute("INSERT INTO transactions (account_id, batch_id, txn_date, tax_date, description, amount_cents, "
                 "currency, cad_cents, dedupe_key) VALUES (1, 1, '2026-01-01', '2026-01-01', 'X', 100, 'CAD', 100, 'k1')")
    conn.execute("DELETE FROM batch_lines")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    assert [tuple(r) for r in conn.execute("SELECT batch_id, dedupe_key FROM batch_lines")] == [(1, "k1")]


def test_imports_page_shows_rows_in_file(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    base = {"account_id": 1, "date_col": "Date", "description_col": "Description", "amount_col": "Amount",
            "charges_positive": 1, "has_header": 1, "format_name": "Visa"}
    sep = "Date,Description,Amount\n2026-09-05,NINJATRADER,99.00\n2026-09-20,CME DATA,12.50\n"
    year = sep + "2026-10-05,NINJATRADER,99.00\n2026-10-06,PENDING,\n"  # overlaps; last row has no amount
    client.post("/import/confirm", data={**base, "filename": "sep.csv", "csv_text": sep})
    client.post("/import/confirm", data={**base, "filename": "year.csv", "csv_text": year})
    page = client.get("/imports").text
    rows = re.findall(r'<td class="num[^"]*">([^<]*)</td>', page)
    assert rows[:3] == ["4", "1", "2"]  # year.csv: 4 data rows, 1 added, 2 in sep.csv
    assert rows[3:6] == ["2", "2", ""]  # sep.csv


def test_fill_row_counts_for_older_imports(tmp_path):
    from app import db
    path = tmp_path / "t.db"
    client = TestClient(create_app(path, fetcher=None))
    conn = db.connect(path)
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    client.post("/accounts", data={"name": "Amex", "currency": "CAD"})
    base = {"date_col": "Date", "description_col": "Description", "amount_col": "Amount",
            "charges_positive": 1, "has_header": 1, "format_name": "Visa"}
    sep = "Date,Description,Amount\n2026-09-05,NINJATRADER,99.00\n2026-09-20,CME DATA,12.50\n"
    year = sep + "2026-10-05,NINJATRADER,99.00\n2026-10-06,PENDING,\n"
    client.post("/import/confirm", data={**base, "account_id": 1, "filename": "sep.csv", "csv_text": sep})
    client.post("/import/confirm", data={**base, "account_id": 1, "filename": "year.csv", "csv_text": year})
    client.post("/import/confirm", data={**base, "account_id": 2, "filename": "amex.csv", "csv_text": sep})
    conn.execute("UPDATE import_batches SET file_rows = NULL")
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    other = "Date,Description,Amount\n2026-11-01,SOMETHING ELSE,5.00\n"
    page = client.post("/imports/row-counts", files=[
        ("file", ("year-again.csv", year, "text/csv")),
        ("file", ("sep-again.csv", sep, "text/csv")),
        ("file", ("other.csv", other, "text/csv")),
        ("file", ("odd.csv", "A,B\n1,2\n", "text/csv")),
    ]).text
    rows = {r["filename"]: (r["account_id"], r["file_rows"]) for r in
            conn.execute("SELECT filename, account_id, file_rows FROM import_batches")}
    # year-again fills only year.csv, though sep.csv's lines are inside it; sep-again fills sep + amex
    assert rows == {"sep.csv": (1, 2), "year.csv": (1, 4), "amex.csv": (2, 2)}
    assert "year.csv on Visa" in page and "amex.csv on Amex" in page
    assert "Doesn&#39;t match any import" in page and "No saved format matches" in page
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before


def test_assign_explains_when_nothing_matches(tmp_path):
    client = TestClient(create_app(tmp_path / "t.db", fetcher=None))
    client.post("/accounts", data={"name": "Visa", "currency": "CAD"})
    client.post("/import/confirm", data={
        "account_id": 1, "date_col": "Date", "description_col": "Description", "amount_col": "Amount",
        "charges_positive": 1, "has_header": 1, "format_name": "Visa", "filename": "a.csv",
        "csv_text": "Date,Description,Amount\n2026-03-02,TOPSTEP/COMBINE 50K,49.00\n"})
    back = "/summary?year=2026"
    page = client.post("/assign", data={"pattern": "NOPE", "vendor_id": "new", "new_vendor": "X", "back": back}).text
    assert "No transaction description matches" in page
    page = client.post("/assign", data={"pattern": "TOPSTEP COMBINE", "vendor_id": "new", "new_vendor": "", "back": back}).text
    assert "Type a name for the new vendor" in page
    page = client.post("/assign", data={"pattern": "TOPSTEP COMBINE", "vendor_id": "new", "new_vendor": "Topstep eval",
                                        "category": "Prop firm evaluations", "back": back}).text
    assert "Transactions that need a vendor" not in page
