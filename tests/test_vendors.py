from app import db, vendors


def setup(tmp_path, txns):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO accounts (name, currency) VALUES ('Visa', 'CAD')")
    for i, (d, desc, cents) in enumerate(txns):
        conn.execute(
            "INSERT INTO transactions (account_id, txn_date, tax_date, description, amount_cents, currency, cad_cents, dedupe_key) "
            "VALUES (1, ?, ?, ?, ?, 'CAD', ?, ?)", (d, d, desc, cents, cents, str(i)))
    vendors.apply_rules(conn)
    return conn


def test_default_rules_group_and_exclude_payments(tmp_path):
    conn = setup(tmp_path, [
        ("2026-01-03", "TOPSTEP* COMBINE 150K", 22350),
        ("2026-02-03", "TOPSTEP* COMBINE 150K", 22350),
        ("2026-02-10", "TOPSTEP* COMBINE 150K", 22350),
        ("2026-02-15", "PAYMENT THANK YOU", -50000),
        ("2026-03-01", "NINJATRADER LIFETIME", 204000),
        ("2026-03-02", "UBER EATS", 2800),
    ])
    s = vendors.summary(conn, 2026)
    by_name = {v.name: v for v in s.all_vendors}
    assert set(by_name) == {"Topstep", "NinjaTrader"}
    assert by_name["Topstep"].months[:3] == [22350, 44700, 0]
    assert by_name["Topstep"].recurrence == "recurring"   # Jan and Feb are consecutive
    assert by_name["NinjaTrader"].recurrence == "one-time"
    assert s.total == 22350 * 3 + 204000
    assert s.month_totals[2] == 204000
    assert [t["description"] for t in vendors.unassigned(conn, 2026)] == ["UBER EATS"]


def test_longest_rule_wins_and_manual_recurrence(tmp_path):
    conn = setup(tmp_path, [
        ("2026-01-05", "APEXTRADERFUNDING EVAL", 11840),
        ("2026-02-05", "APEXTRADERFUNDING RESET", 11840),
    ])
    reset = vendors.get_or_create_vendor(conn, "Apex resets", "Prop firm evaluations")
    vendors.add_rule(conn, "apextraderfunding reset", reset)
    conn.execute("UPDATE vendors SET recurrence = 'one-time' WHERE id = ?", (reset,))
    s = vendors.summary(conn, 2026)
    by_name = {v.name: v for v in s.all_vendors}
    assert by_name["Apex resets"].months[1] == 11840
    assert by_name["Apex resets"].recurrence == "one-time" and not by_name["Apex resets"].auto
    assert by_name["Apex Trader Funding"].months[0] == 11840
    assert [g.name for g in vendors.summary(conn, 2026, only="one-time").groups] == ["Prop firm evaluations"]


def test_refund_nets_against_charge(tmp_path):
    conn = setup(tmp_path, [("2026-04-01", "TRADOVATE", 13500), ("2026-04-09", "TRADOVATE REFUND", -13500)])
    v = vendors.summary(conn, 2026).all_vendors[0]
    assert v.months[3] == 0 and v.recurrence == "one-time"


def test_suggest_pattern():
    assert vendors.suggest_pattern("MFFU*EVAL 50K 8443") == "MFFU"
    assert vendors.suggest_pattern("AMZN MKTP CA 2K91") == "AMZN MKTP"
    # merchants behind a payment processor are kept apart
    assert vendors.suggest_pattern("PAYPAL *TRADINGVIEW 4029357733 CA") == "TRADINGVIEW"
    assert vendors.suggest_pattern("PAYPAL *ACME DATA FEED") == "ACME DATA"
    assert vendors.suggest_pattern("PP*BOOKMAP") == "BOOKMAP"
    assert vendors.suggest_pattern("SQ *COFFEE HUT") == "COFFEE HUT"
    assert vendors.suggest_pattern("PAYPAL ACMEDATA") == "ACMEDATA"
    assert vendors.suggest_pattern("PAYPAL *") == "PAYPAL"
    assert vendors.suggest_pattern("7-ELEVEN #123") == "ELEVEN"


def test_detect_recurrence():
    assert vendors.detect_recurrence({1, 5, 9}) == "recurring"
    assert vendors.detect_recurrence({4, 5}) == "recurring"
    assert vendors.detect_recurrence({2, 7}) == "one-time"
    assert vendors.detect_recurrence({3}) == "one-time"


def test_migrates_step1_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)  # step 1 schema: no vendor_id column
    old.execute("INSERT INTO accounts (name, currency) VALUES ('Visa', 'CAD')")
    old.execute("INSERT INTO transactions (account_id, txn_date, description, amount_cents, currency, cad_cents, dedupe_key) "
                "VALUES (1, '2026-01-01', 'TRADINGVIEW', 100, 'CAD', 100, 'k')")
    old.commit(); old.close()
    conn = db.connect(path)
    row = conn.execute("SELECT v.name, t.tax_date FROM transactions t JOIN vendors v ON v.id = t.vendor_id").fetchone()
    assert tuple(row) == ("TradingView", "2026-01-01")


def test_year_end_move_changes_tax_year_and_fx(tmp_path):
    from app import fx
    conn = setup(tmp_path, [("2026-01-01", "TOPSTEP* COMBINE", 22350), ("2026-01-02", "PAYMENT THANK YOU", -100)])
    conn.execute("INSERT INTO accounts (name, currency) VALUES ('USD', 'USD')")
    conn.execute("INSERT INTO transactions (account_id, txn_date, tax_date, description, amount_cents, currency, fx_rate, cad_cents, dedupe_key) "
                 "VALUES (2, '2026-01-01', '2026-01-01', 'TRADOVATE', 10000, 'USD', '1.40', 14000, 'u')")
    vendors.apply_rules(conn)
    # both years' views list the Jan 1 charges (not the payment)
    assert [r["description"] for r in vendors.year_end_check(conn, 2025)] == ["TOPSTEP* COMBINE", "TRADOVATE"]
    assert len(vendors.year_end_check(conn, 2026)) == 2

    for r in vendors.year_end_check(conn, 2025):
        vendors.set_tax_date(conn, r["id"], "2025-12-31")
    fx.fill_missing_cad(conn, lambda s, e: {"2025-12-31": "1.3700"})
    s25 = vendors.summary(conn, 2025)
    assert s25.month_totals[11] == 22350 + 13700
    assert vendors.summary(conn, 2026).total == 0


def test_excluded_line_left_out(tmp_path):
    conn = setup(tmp_path, [("2026-05-01", "AMAZON BOOK", 3000), ("2026-05-02", "AMAZON GIFT", 5000), ("2026-01-01", "UBER", 900)])
    vid = vendors.get_or_create_vendor(conn, "Amazon", "Office supplies")
    vendors.add_rule(conn, "AMAZON", vid)
    gift = conn.execute("SELECT id FROM transactions WHERE description = 'AMAZON GIFT'").fetchone()[0]
    uber = conn.execute("SELECT id FROM transactions WHERE description = 'UBER'").fetchone()[0]
    vendors.set_excluded(conn, gift, True)
    vendors.set_excluded(conn, uber, True)
    assert vendors.summary(conn, 2026).total == 3000
    assert vendors.unassigned(conn, 2026) == [] and vendors.year_end_check(conn, 2026) == []
    vendors.set_excluded(conn, gift, False)
    assert vendors.summary(conn, 2026).total == 8000


def test_allocate_by_day_adds_up():
    from datetime import date
    parts = vendors.allocate(date(2026, 6, 30), 12, 36500)
    assert sum(parts.values()) == 36500
    assert sum(c for (y, _), c in parts.items() if y == 2026) == 18500   # Jun 30 - Dec 31 = 185 days
    assert parts[(2026, 6)] == 100 and parts[(2027, 6)] == 2900          # through Jun 29, 2027
    assert vendors.allocate(date(2026, 3, 5), None, 999) == {(2026, 3): 999}
    assert sum(vendors.allocate(date(2026, 1, 31), 1, 1001).values()) == 1001


def test_prepaid_annual_subscription_spans_years(tmp_path):
    conn = setup(tmp_path, [("2026-06-30", "TRADINGVIEW ANNUAL", 36500)])
    tid = conn.execute("SELECT id FROM transactions").fetchone()[0]
    vendors.set_service_months(conn, tid, 12)
    s26, s27 = vendors.summary(conn, 2026), vendors.summary(conn, 2027)
    assert s26.total == 18500 and s26.deferred_out == 18000 and s26.carried_in == 0
    assert s27.total == 18000 and s27.carried_in == 18000
    assert s26.all_vendors[0].recurrence == "recurring"
    assert vendors.years(conn) == [2027, 2026]
    vendors.set_service_months(conn, tid, None)
    assert vendors.summary(conn, 2026).total == 36500


def test_default_db_path_moves_legacy_file(tmp_path, monkeypatch):
    monkeypatch.delenv("EXPENSE_DB", raising=False)
    home_db = tmp_path / "home" / "Documents" / "ExpenseTracker" / "expenses.db"
    monkeypatch.setattr(db, "HOME_DB_PATH", home_db)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "expenses.db").write_bytes(b"old data")
    assert db.default_db_path() == home_db and home_db.read_bytes() == b"old data"
    # an existing home database is never overwritten
    (tmp_path / "expenses.db").write_bytes(b"newer copy")
    assert db.default_db_path() == home_db and home_db.read_bytes() == b"old data"
    monkeypatch.setenv("EXPENSE_DB", str(tmp_path / "x.db"))
    assert db.default_db_path() == tmp_path / "x.db"


def test_rule_must_start_a_word():
    rules = [("APEX TRADER", 1), ("NINJA", 2), ("*EVAL", 3), ("FL", 4)]
    assert vendors.match_vendor(rules, "PAYPAL *NETFLIX 4029357733") is None
    assert vendors.match_vendor(rules, "PAYPAL *FLEXTRADE 4029357733") == 4
    assert vendors.match_vendor(rules, "WWW.APEX TRADER FUNDING") == 1
    assert vendors.match_vendor(rules, "NINJATRADER LLC") == 2
    assert vendors.match_vendor(rules, "MFFU*EVAL 50K") == 3


def test_migration_rematches_netflix(tmp_path):
    conn = setup(tmp_path, [("2026-03-02", "PAYPAL *NETFLIX 4029357733", 1699)])
    vid = vendors.get_or_create_vendor(conn, "FL Trading", "Software")
    conn.execute("INSERT INTO vendor_rules (pattern, vendor_id) VALUES ('FL', ?)", (vid,))
    conn.execute("UPDATE transactions SET vendor_id = ?", (vid,))  # as the old substring match left it
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    conn = db.connect(tmp_path / "t.db")
    assert conn.execute("SELECT vendor_id FROM transactions").fetchone()[0] is None


def test_suggested_pattern_always_matches_its_line():
    for d in ["1PASSWORD 1PASSWORD.COM", "TOPSTEP/COMBINE 50K", "APPLE.COM/BILL", "3COMMAS",
              "99DESIGNS", "CHATGPT  SUBSCRIPTION", "I AM A DAY TRADER LLC", "PAYPAL *FL 4029357733", "AMZN MKTP CA*2X3"]:
        assert vendors.rule_matches(vendors.suggest_pattern(d), d.upper()), d
    assert not vendors.rule_matches("FL", "PAYPAL *NETFLIX.COM")
