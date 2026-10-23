"""SQLite storage. Money is stored as integer cents to avoid float rounding."""

import os
import shutil
import sqlite3
from pathlib import Path

# One fixed home for the data, so every downloaded copy of the app shares it.
# EXPENSE_DB overrides it.
HOME_DB_PATH = Path.home() / "Documents" / "ExpenseTracker" / "expenses.db"


def default_db_path() -> Path:
    if os.environ.get("EXPENSE_DB"):
        return Path(os.environ["EXPENSE_DB"])
    if not HOME_DB_PATH.exists():
        HOME_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Earlier versions kept the database in the folder the app was started from
        legacy = Path("expenses.db")
        if legacy.exists():
            shutil.copy2(legacy, HOME_DB_PATH)
            print(f"Copied {legacy.resolve()} to {HOME_DB_PATH}")
    return HOME_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS csv_formats (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    signature       TEXT NOT NULL,  -- header row (or column count) used to auto-match files
    date_col        TEXT NOT NULL,
    description_col TEXT NOT NULL,
    amount_col      TEXT,           -- single signed amount column, or
    debit_col       TEXT,           -- separate debit / credit columns
    credit_col      TEXT,
    date_format     TEXT,           -- strptime format, e.g. %Y-%m-%d
    charges_positive INTEGER NOT NULL DEFAULT 1,  -- 1 if purchases appear as positive numbers
    has_header      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    currency        TEXT NOT NULL CHECK (currency IN ('CAD', 'USD')),
    last_format_id  INTEGER REFERENCES csv_formats(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    filename    TEXT,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    row_count   INTEGER NOT NULL DEFAULT 0
);

-- Every line each import's file contained, including ones skipped because an
-- earlier import already had them, so removing an import keeps shared lines.
CREATE TABLE IF NOT EXISTS batch_lines (
    batch_id   INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL,
    PRIMARY KEY (batch_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    batch_id     INTEGER REFERENCES import_batches(id) ON DELETE CASCADE,
    txn_date     TEXT NOT NULL,          -- ISO yyyy-mm-dd, as printed on the statement
    description  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,       -- original currency; positive = expense, negative = refund/payment
    currency     TEXT NOT NULL,
    fx_rate      TEXT,                   -- USD->CAD rate used (decimal string); NULL for CAD
    cad_cents    INTEGER,                -- NULL until a rate is available
    dedupe_key   TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS vendors (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    category   TEXT NOT NULL,
    recurrence TEXT CHECK (recurrence IN ('recurring', 'one-time'))  -- NULL = detect automatically
);

-- A transaction belongs to the vendor of the longest rule pattern found in its description.
CREATE TABLE IF NOT EXISTS vendor_rules (
    id        INTEGER PRIMARY KEY,
    pattern   TEXT NOT NULL UNIQUE,  -- upper-case substring
    vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE
);

-- How USD converts to CAD for a tax year. No row = daily rates.
CREATE TABLE IF NOT EXISTS tax_years (
    year        INTEGER PRIMARY KEY,
    fx_method   TEXT NOT NULL DEFAULT 'daily' CHECK (fx_method IN ('daily', 'annual')),
    annual_rate TEXT              -- Bank of Canada annual average USD->CAD, decimal string
);

CREATE TABLE IF NOT EXISTS fx_rates (
    rate_date TEXT PRIMARY KEY,          -- ISO date of the Bank of Canada observation
    usd_cad   TEXT NOT NULL,             -- decimal string
    source    TEXT NOT NULL DEFAULT 'boc'
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = path or default_db_path()
    print(f"Using database {Path(path).resolve()}")
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by earlier versions up to date (tracked in PRAGMA user_version)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN vendor_id INTEGER REFERENCES vendors(id) ON DELETE SET NULL"
        )
        from .vendors import apply_rules, seed_defaults

        seed_defaults(conn)
        apply_rules(conn)
        conn.execute("PRAGMA user_version = 1")
    if version < 2:
        # The date that counts for tax year, month and FX rate. Starts as the card
        # date; editable because card dates are often UTC, so a late-evening charge
        # on Dec 31 can post as Jan 1.
        conn.execute("ALTER TABLE transactions ADD COLUMN tax_date TEXT")
        conn.execute("UPDATE transactions SET tax_date = txn_date")
        conn.execute("PRAGMA user_version = 2")
    if version < 3:
        # Per-line exclusion, e.g. a personal purchase from a vendor you also buy business items from.
        conn.execute("ALTER TABLE transactions ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
        conn.execute("PRAGMA user_version = 3")
    if version < 4:
        # Prepaid charges (e.g. an annual subscription) are spread by day over this
        # many months starting on the tax date. NULL = expensed on the tax date.
        conn.execute("ALTER TABLE transactions ADD COLUMN service_months INTEGER")
        conn.execute("PRAGMA user_version = 4")
    if version < 5:
        # Whether newly imported transactions from a card start out included in
        # business totals (a mostly-personal card can default to excluded).
        conn.execute("ALTER TABLE accounts ADD COLUMN include_by_default INTEGER NOT NULL DEFAULT 1")
        conn.execute("PRAGMA user_version = 5")
    if version < 6:
        # More card-payment wordings, and exclude payments already imported
        from .vendors import DEFAULT_VENDORS, PAYMENT_CATEGORY, apply_rules, exclude_card_payments

        row = conn.execute("SELECT id FROM vendors WHERE category = ? ORDER BY id LIMIT 1", (PAYMENT_CATEGORY,)).fetchone()
        if row:
            patterns = next(p for _, c, _, p in DEFAULT_VENDORS if c == PAYMENT_CATEGORY)
            conn.executemany(
                "INSERT OR IGNORE INTO vendor_rules (pattern, vendor_id) VALUES (?, ?)", [(p, row[0]) for p in patterns]
            )
            apply_rules(conn)
        exclude_card_payments(conn)
        conn.execute("PRAGMA user_version = 6")
    if version < 7:
        # Each import's file shape (header or column count), to warn when a file
        # doesn't look like the card's earlier ones. Older imports get the card's last-used format.
        if "signature" not in [c["name"] for c in conn.execute("PRAGMA table_info(import_batches)")]:
            conn.execute("ALTER TABLE import_batches ADD COLUMN signature TEXT")
        conn.execute(
            "UPDATE import_batches SET signature = (SELECT f.signature FROM accounts a "
            "JOIN csv_formats f ON f.id = a.last_format_id WHERE a.id = import_batches.account_id) "
            "WHERE signature IS NULL"
        )
        conn.execute("PRAGMA user_version = 7")
    if version < 8:
        # Older imports only know the lines they added
        conn.execute(
            "INSERT OR IGNORE INTO batch_lines (batch_id, dedupe_key) "
            "SELECT batch_id, dedupe_key FROM transactions WHERE batch_id IS NOT NULL"
        )
        conn.execute("PRAGMA user_version = 8")
    if version < 9:
        # Data rows in the imported file (header not counted). Unknown for older imports.
        if "file_rows" not in [c["name"] for c in conn.execute("PRAGMA table_info(import_batches)")]:
            conn.execute("ALTER TABLE import_batches ADD COLUMN file_rows INTEGER")
        conn.execute("PRAGMA user_version = 9")
    if version < 10:
        # Rules now match only where a word starts ("FL" no longer matches NETFLIX)
        from .vendors import apply_rules

        apply_rules(conn)
        conn.execute("PRAGMA user_version = 10")
    if version < 11:
        # Rules allow any gap between words and a digit before them ("APPLE.COM BILL", "1PASSWORD")
        from .vendors import apply_rules

        apply_rules(conn)
        conn.execute("PRAGMA user_version = 11")
    conn.commit()
