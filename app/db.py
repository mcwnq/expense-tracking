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
    txn_date     TEXT NOT NULL,          -- ISO yyyy-mm-dd
    description  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,       -- original currency; positive = expense, negative = refund/payment
    currency     TEXT NOT NULL,
    fx_rate      TEXT,                   -- USD->CAD rate used (decimal string); NULL for CAD
    cad_cents    INTEGER,                -- NULL until a rate is available
    dedupe_key   TEXT NOT NULL UNIQUE
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
    return conn
