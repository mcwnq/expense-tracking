"""SQLite storage. Money is stored as integer cents to avoid float rounding."""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("EXPENSE_DB", "expenses.db"))

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
    conn = sqlite3.connect(str(path or DEFAULT_DB_PATH), check_same_thread=False)
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
    conn.commit()
