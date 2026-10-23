# Trader Expense Tracker

A local browser app for a self-employed Canadian trader (futures, stocks, options) to track business expenses and income and prepare the T2125 statement.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app
```

On Windows, just double-click **Start Expense Tracker.bat**: it installs anything missing, starts the app and opens your browser. Close the black window to stop the app. For a desktop shortcut, right-click the file → **Show more options** → **Send to** → **Desktop (create shortcut)**. (Manually: `py -m pip install -r requirements.txt` then `py -m app`.)

Then open http://127.0.0.1:8000. Your data is stored in `Documents/ExpenseTracker/expenses.db` in your user folder, so every downloaded copy of the app uses the same data (set `EXPENSE_DB=/path/to/file.db` to put it somewhere else). If an older version left an `expenses.db` in the folder you start the app from, it is copied there automatically the first time. The data never leaves your machine; the only network calls fetch USD→CAD rates from the Bank of Canada.

## Step 1: import

- **Cards**: add each credit card as CAD or USD.
- **CSV import with saved formats**: upload a statement, check the guessed column mapping (date, description, signed amount or debit/credit, date format, header or not), preview, then import. The mapping is saved as a named format. Later files are matched to a saved format by their header row (or column count when headerless), so one format can serve several cards and a card can use several formats. Manage them under "CSV formats". Re-importing overlapping statements doesn't create duplicates, and an import can be undone from the home page.
- **USD→CAD**: USD transactions are converted with the Bank of Canada daily rate for the transaction date (prior business day on weekends and holidays). Rates are cached locally; if you're offline, use "Fetch rates now" later.
- **Transactions**: filter by card and month, with CAD totals.

## Step 2: grouping and totals

- **Vendors and rules**: each transaction is assigned to a vendor by matching text in its description (e.g. `TOPSTEP`). Common prop firms, platforms and data vendors come preloaded; card payments are excluded automatically. The longest matching rule wins, so `APEXTRADERFUNDING RESET` can be its own vendor.
- **Summary**: a vendor × month grid in CAD with monthly and yearly totals, grouped by category, filterable to recurring or one-time, exportable as CSV.
- **One-time vs recurring**: detected automatically (charged in 3+ months, or two months in a row) and overridable per vendor.
- **Tax date and year-end check**: card dates are often UTC, so an evening charge on Dec 31 can post as Jan 1. Each transaction has a "counts on" date (defaults to the card date, editable on Transactions) that sets its tax year, month and USD rate. The summary lists Jan 1–2 charges with a one-click move to Dec 31 of the prior year.
- **Annual average rate**: per tax year, convert USD at the daily Bank of Canada rate or at the annual average rate (fetched from the Bank of Canada once published, or typed in).
- **Prepaid charges**: set "Covers" on a transaction (e.g. 12 months for an annual TradingView plan) and the amount is spread by day over that period, so only the part used in each tax year counts there. The summary shows how much moved into or out of the year.
- **Include only business lines**: each transaction has an "Include" checkbox; "Include all" in the header ticks or clears every transaction matching the current card/month filter. On the Cards page, set whether new imports from a card start included (e.g. a business USD card) or excluded (e.g. a mostly personal card). Only included transactions count in totals or ask for a vendor. To exclude every charge from a merchant, give its vendor the category "Personal (exclude)".
- **Unassigned transactions**: listed under the summary with a one-click form that saves a rule; mark personal spending as "Personal (exclude)".

## Roadmap

1. ~~Data model, CSV import, USD→CAD conversion~~
2. ~~Vendor grouping, monthly and yearly totals, one-time vs recurring tags~~
3. Prop firm payout entry and T5008 entry (trading reported as business income)
4. T2125 line mapping and year-end summary

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
