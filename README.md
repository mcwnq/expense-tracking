# Trader Expense Tracker

A local browser app for a self-employed Canadian trader (futures, stocks, options) to track business expenses and income and prepare the T2125 statement.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app
```

On Windows with the Python install manager, use `py` instead: `py -m pip install -r requirements.txt` then `py -m app`.

Then open http://127.0.0.1:8000. Your data is stored in `expenses.db` in the folder you run from (set `EXPENSE_DB=/path/to/file.db` to put it somewhere else). It never leaves your machine; the only network call is fetching USD→CAD rates from the Bank of Canada.

## What works so far (step 1)

- **Cards**: add each credit card as CAD or USD.
- **CSV import with saved formats**: upload a statement, check the guessed column mapping (date, description, signed amount or debit/credit, date format, header or not), preview, then import. The mapping is saved as a named format. Later files are matched to a saved format by their header row (or column count when headerless), so one format can serve several cards and a card can use several formats. Manage them under "CSV formats". Re-importing overlapping statements doesn't create duplicates, and an import can be undone from the home page.
- **USD→CAD**: USD transactions are converted with the Bank of Canada daily rate for the transaction date (prior business day on weekends and holidays). Rates are cached locally; if you're offline, use "Fetch rates now" later.
- **Transactions**: filter by card and month, with CAD totals.

## Roadmap

1. ~~Data model, CSV import, USD→CAD conversion~~
2. Vendor grouping, monthly and yearly totals, one-time vs recurring tags
3. Prop firm payout entry and T5008 entry (trading reported as business income)
4. T2125 line mapping and year-end summary

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
