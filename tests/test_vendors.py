from app import db, vendors


def setup(tmp_path, txns):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO accounts (name, currency) VALUES ('Visa', 'CAD')")
    for i, (d, desc, cents) in enumerate(txns):
        conn.execute(
            "INSERT INTO transactions (account_id, txn_date, description, amount_cents, currency, cad_cents, dedupe_key) "
            "VALUES (1, ?, ?, ?, 'CAD', ?, ?)", (d, desc, cents, cents, str(i)))
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
    assert conn.execute("SELECT v.name FROM transactions t JOIN vendors v ON v.id = t.vendor_id").fetchone()[0] == "TradingView"
