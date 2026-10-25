"""Local web app: run with `python -m app` and open http://127.0.0.1:8000."""

import csv
import io
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, fx, importer, payouts, vendors

HERE = Path(__file__).parent


def cents(value: int | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value) / 100:,.2f}"


def create_app(
    db_path: Path | str | None = None,
    fetcher: fx.Fetcher | None = fx.fetch_boc,
    annual_fetcher=fx.fetch_boc_annual,
) -> FastAPI:
    app = FastAPI(title="Trader Expense Tracker")
    conn = db.connect(db_path)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["money"] = cents
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    def get_account(account_id: int):
        return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        accounts = conn.execute(
            "SELECT a.*, COUNT(t.id) AS n, MIN(t.txn_date) AS first, MAX(t.txn_date) AS last "
            "FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id "
            "GROUP BY a.id ORDER BY a.name"
        ).fetchall()
        unconverted = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE cad_cents IS NULL"
        ).fetchone()[0]
        batches = conn.execute(
            "SELECT b.*, a.name AS account FROM import_batches b JOIN accounts a ON a.id = b.account_id "
            "ORDER BY b.id DESC LIMIT 10"
        ).fetchall()
        return render(request, "index.html", accounts=accounts, unconverted=unconverted, batches=batches)

    @app.post("/accounts")
    def add_account(name: str = Form(...), currency: str = Form(...)):
        conn.execute(
            "INSERT OR IGNORE INTO accounts (name, currency) VALUES (?, ?)",
            (name.strip(), currency),
        )
        conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/edit")
    def edit_account(account_id: int, name: str = Form(...), currency: str = Form("")):
        conn.execute("UPDATE OR IGNORE accounts SET name = ? WHERE id = ?", (name.strip(), account_id))
        if currency in ("CAD", "USD"):
            # Fixing a card set up with the wrong currency: its transactions are
            # re-labelled and USD ones re-converted to CAD.
            conn.execute("UPDATE accounts SET currency = ? WHERE id = ?", (currency, account_id))
            conn.execute(
                "UPDATE transactions SET currency = ?, fx_rate = NULL, "
                "cad_cents = CASE WHEN ? = 'CAD' THEN amount_cents ELSE NULL END "
                "WHERE account_id = ? AND currency != ?",
                (currency, currency, account_id, currency),
            )
        conn.commit()
        fx.fill_missing_cad(conn, fetcher)
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/delete")
    def delete_account(account_id: int):
        conn.execute("DELETE FROM transactions WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM import_batches WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.get("/import", response_class=HTMLResponse)
    def import_form(request: Request, account_id: int | None = None):
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        return render(request, "import.html", accounts=accounts, selected=account_id)

    @app.post("/import/preview", response_class=HTMLResponse)
    async def import_preview(request: Request, account_id: int = Form(...), file: list[UploadFile] = File(...)):
        files = [(f.filename or "upload.csv", (await f.read()).decode("utf-8-sig", errors="replace")) for f in file]
        account = get_account(account_id)
        first = files[0][1]
        fmt = importer.match_format(conn, first, prefer_id=account["last_format_id"])
        if fmt:
            mapping, name, note = importer.Mapping.from_row(fmt), fmt["name"], f"Matched saved format \u201c{fmt['name']}\u201d."
        else:
            mapping, name, note = importer.guess_mapping(first), f"{account['name']} format", None
        return _preview(request, account, files, mapping, name, note)

    @app.post("/import/confirm", response_class=HTMLResponse)
    def import_confirm(
        request: Request,
        account_id: int = Form(...),
        filename: list[str] = Form(...),
        csv_text: list[str] = Form(...),
        date_col: str = Form(...),
        description_col: str = Form(...),
        amount_col: str = Form(""),
        debit_col: str = Form(""),
        credit_col: str = Form(""),
        date_format: str = Form(""),
        charges_positive: int = Form(1),
        has_header: int = Form(1),
        format_name: str = Form(""),
        load_format_id: int | None = Form(None),
        action: str = Form("import"),
    ):
        account = get_account(account_id)
        files = list(zip(filename, csv_text))
        if action == "load" and load_format_id:
            fmt = conn.execute("SELECT * FROM csv_formats WHERE id = ?", (load_format_id,)).fetchone()
            return _preview(request, account, files, importer.Mapping.from_row(fmt), fmt["name"])
        mapping = importer.Mapping(
            date_col=date_col,
            description_col=description_col,
            amount_col=amount_col or None,
            debit_col=debit_col or None,
            credit_col=credit_col or None,
            date_format=date_format or None,
            charges_positive=bool(charges_positive),
            has_header=bool(has_header),
        )
        if action == "preview":
            return _preview(request, account, files, mapping, format_name)
        if not format_name.strip():
            return _preview(request, account, files, mapping, format_name, error="Give the format a name.")
        error = _mixed_shapes(files, mapping)
        parsed = []
        for name, text in files:
            try:
                parsed.append((name, text, importer.parse(text, mapping)))
            except importer.CsvFormatError as e:
                error = error or (f"{name}: {e}" if len(files) > 1 else str(e))
        if error:
            return _preview(request, account, files, mapping, format_name, error=error)

        format_id = importer.save_format(conn, format_name, mapping, files[0][1])
        conn.execute("UPDATE accounts SET last_format_id = ? WHERE id = ?", (format_id, account_id))
        results = []
        for name, text, rows in parsed:
            added, skipped, batch_id = importer.save(
                conn, account, rows, name, importer.signature(text, mapping.has_header),
                file_rows=len(importer.read_rows(text, mapping.has_header)[1]),
            )
            results.append({"filename": name, "added": added, "skipped": skipped, "batch_id": batch_id})
        vendors.apply_rules(conn)
        for r in results:
            vendors.exclude_card_payments(conn, r["batch_id"])
        missing = fx.fill_missing_cad(conn, fetcher) if account["currency"] == "USD" else 0
        return render(
            request, "imported.html", account=account, results=results, missing=missing,
            added=sum(r["added"] for r in results), skipped=sum(r["skipped"] for r in results),
        )

    def _mixed_shapes(files, mapping) -> str | None:
        """Files imported together share one mapping, so they need the same columns."""
        shapes = {importer.signature(text, mapping.has_header) for _, text in files}
        if len(shapes) > 1:
            return "These files don't all have the same columns. Import files from different formats separately."
        return None

    def _preview(request, account, files, mapping, format_name, note=None, error=None):
        header, _ = importer.read_rows(files[0][1], mapping.has_header)
        parse_error = error or _mixed_shapes(files, mapping)
        all_rows, per_file, warnings = [], [], []
        for name, text in files:
            rows, file_error = [], None
            if not parse_error:
                try:
                    rows = importer.parse(text, mapping)
                except importer.CsvFormatError as e:
                    file_error = str(e)
            all_rows += rows
            per_file.append({"filename": name, "rows": len(rows), "total_cents": sum(r.amount_cents for r in rows),
                             "error": file_error})
        if not parse_error:
            parse_error = next(
                (f"{f['filename']}: {f['error']}" if len(files) > 1 else f["error"] for f in per_file if f["error"]),
                None,
            )
        format_warning = importer.format_warning(conn, account["id"], importer.signature(files[0][1], mapping.has_header))
        if format_warning:
            warnings.append(format_warning)
        for card, n in importer.found_on_other_cards(conn, account["id"], all_rows):
            warnings.append(
                f"{n} of these lines are already on {card} (same date, description and amount). "
                f"{'These files' if len(files) > 1 else 'This file'} may belong to that card."
            )
        return render(
            request,
            "preview.html",
            account=account,
            files=files,
            per_file=per_file,
            header=header,
            mapping=mapping,
            rows=all_rows[:25],
            total_rows=len(all_rows),
            total_cents=sum(r.amount_cents for r in all_rows),
            error=parse_error,
            date_formats=importer.DATE_FORMATS,
            format_name=format_name,
            note=note,
            warnings=warnings,
            formats=conn.execute("SELECT id, name FROM csv_formats ORDER BY name").fetchall(),
        )

    @app.get("/transactions", response_class=HTMLResponse)
    def transactions(
        request: Request, account_id: str = "", month: str = "", q: str = "", show: str = "", view: str = ""
    ):
        # The filter form sends empty strings for "All"
        account_id = int(account_id) if account_id.isdigit() else None
        sql = (
            "SELECT t.*, a.name AS account, v.name AS vendor, v.category AS vendor_category, "
            # other included charges from the same vendor, to warn before excluding this one
            "(SELECT COUNT(*) FROM transactions o WHERE o.vendor_id = t.vendor_id AND o.id != t.id "
            " AND NOT o.excluded) AS vendor_included "
            "FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id LEFT JOIN vendors v ON v.id = t.vendor_id WHERE 1=1"
        )
        args: list = []
        if account_id:
            sql += " AND t.account_id = ?"
            args.append(account_id)
        if month:
            sql += " AND substr(t.tax_date, 1, 7) = ?"
            args.append(month)
        if q.strip():
            sql += " AND (upper(t.description) LIKE ? OR upper(coalesce(v.name, '')) LIKE ?)"
            args += [f"%{q.strip().upper()}%"] * 2
        if show in ("included", "excluded"):
            sql += " AND t.excluded = ?"
            args.append(int(show == "excluded"))
        sql += " ORDER BY t.tax_date DESC, t.id DESC"
        rows = conn.execute(sql, args).fetchall()
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        months = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT substr(tax_date, 1, 7) FROM transactions ORDER BY 1 DESC"
            )
        ]
        total_cad = sum(r["cad_cents"] or 0 for r in rows if not r["excluded"])
        view = "merchants" if view == "merchants" else "lines"
        here = f"/transactions?{urlencode({'account_id': account_id or '', 'month': month, 'q': q, 'show': show, 'view': view})}"
        return render(
            request,
            "transactions.html",
            rows=rows,
            groups=vendors.group_by_merchant(rows) if view == "merchants" else [],
            accounts=accounts,
            months=months,
            account_id=account_id,
            month=month,
            q=q,
            show=show,
            view=view,
            here=here,
            here_for=lambda v: here.replace(f"view={view}", f"view={v}"),
            all_ids=",".join(str(r["id"]) for r in rows),
            total_cad=total_cad,
            all_included=bool(rows) and not any(r["excluded"] for r in rows),
            included_count=sum(1 for r in rows if not r["excluded"]),
            last_change=vendors.last_include_change(conn),
        )

    @app.post("/transactions/add")
    def add_transaction(
        account_id: int = Form(...),
        txn_date: date = Form(...),
        description: str = Form(...),
        amount: str = Form(...),
        back: str = Form("/transactions"),
    ):
        account = get_account(account_id)
        value = importer.parse_amount(amount)
        if account and description.strip() and value is not None:
            importer.add_manual(conn, account, txn_date, description, importer.to_cents(value))
            vendors.apply_rules(conn)
            fx.fill_missing_cad(conn, fetcher)
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/transactions", status_code=303)

    @app.post("/transactions/{txn_id}/delete")
    def delete_transaction(txn_id: int, back: str = Form("/transactions")):
        # Only lines added by hand; imported lines are removed with their import
        conn.execute("DELETE FROM transactions WHERE id = ? AND batch_id IS NULL", (txn_id,))
        conn.commit()
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/transactions", status_code=303)

    @app.post("/transactions/set-included")
    def set_included_many(ids: str = Form(""), included: int = Form(1), back: str = Form("/transactions")):
        vendors.set_included_ids(conn, [int(i) for i in ids.split(",") if i.isdigit()], bool(included))
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/transactions", status_code=303)

    @app.post("/transactions/undo-include")
    def undo_include(back: str = Form("/transactions")):
        vendors.undo_include(conn)
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/transactions", status_code=303)

    @app.get("/formats", response_class=HTMLResponse)
    def formats(request: Request):
        rows = conn.execute(
            "SELECT f.*, GROUP_CONCAT(a.name, ', ') AS cards FROM csv_formats f "
            "LEFT JOIN accounts a ON a.last_format_id = f.id GROUP BY f.id ORDER BY f.name"
        ).fetchall()
        return render(request, "formats.html", formats=rows)

    @app.post("/formats/{format_id}/rename")
    def rename_format(format_id: int, name: str = Form(...)):
        conn.execute("UPDATE OR IGNORE csv_formats SET name = ? WHERE id = ?", (name.strip(), format_id))
        conn.commit()
        return RedirectResponse("/formats", status_code=303)

    @app.post("/formats/{format_id}/delete")
    def delete_format(format_id: int):
        conn.execute("DELETE FROM csv_formats WHERE id = ?", (format_id,))
        conn.commit()
        return RedirectResponse("/formats", status_code=303)

    def pick_year(year: int | None) -> int:
        if year:
            return year
        known = all_years()
        return known[0] if known else date.today().year

    def all_years() -> list[int]:
        return sorted(set(vendors.years(conn)) | set(payouts.years(conn)), reverse=True)

    @app.get("/summary", response_class=HTMLResponse)
    def summary(request: Request, year: int | None = None, show: str = "", cur: str = "", fx_error: str = "", assign_error: str = ""):
        year = pick_year(year)
        fx_method, annual_rate = fx.year_setting(conn, year)
        only = show if show in ("recurring", "one-time") else None
        # Charges in each original currency, then everything in one currency (CAD unless toggled to USD)
        cur = "USD" if cur == "USD" else "CAD"
        s = summary_in(year, only, cur)
        tables = [(c, vendors.summary(conn, year, only, charged_in=c)) for c in ("USD", "CAD")]
        return render(
            request,
            "summary.html",
            s=s,
            tables=tables,
            cur=cur,
            show=show,
            years=all_years() or [year],
            firm_payouts=summary_payouts(year),
            unassigned=(unassigned := vendors.unassigned(conn, year)),
            unassigned_groups=vendors.group_by_merchant(unassigned),
            year_end=vendors.year_end_check(conn, year),
            fx_method=fx_method,
            annual_rate=annual_rate,
            fx_error=fx_error,
            assign_error=assign_error,
            all_vendors=conn.execute("SELECT * FROM vendors ORDER BY name").fetchall(),
            categories=vendors.CATEGORIES,
            suggest=vendors.suggest_pattern,
            unconverted=conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE cad_cents IS NULL AND substr(tax_date, 1, 4) = ?",
                (str(year),),
            ).fetchone()[0],
        )

    def summary_payouts(year: int) -> dict:
        """Payouts by firm and month: received in USD, received in CAD, and all in CAD."""
        if payouts.monthly(conn, year)["unconverted"]:
            fx.fill_missing_cad(conn, fetcher)
        return {"USD": payouts.monthly(conn, year, "USD"), "CAD": payouts.monthly(conn, year, "CAD"),
                "all": payouts.monthly(conn, year)}

    def summary_in(year: int, only: str | None, currency: str) -> vendors.Summary:
        s = vendors.summary(conn, year, only, currency)
        if s.unconverted and currency == "USD" and fetcher is not None:
            # CAD lines need rates for their own dates, which USD imports may not have fetched
            try:
                fx.ensure_rates(conn, date(year, 1, 1), min(date(year, 12, 31), date.today()), fetcher)
            except OSError:
                return s
            s = vendors.summary(conn, year, only, currency)
        return s

    @app.post("/tax-years/{year}/fx")
    def set_fx_method(year: int, method: str = Form(...), annual_rate: str = Form("")):
        rate = annual_rate.strip() or None
        if method == "annual" and not rate and annual_fetcher:
            try:
                rate = annual_fetcher(year)
            except OSError:
                rate = None
        if method == "annual" and not rate:
            msg = "The Bank of Canada annual rate isn't available yet; type it in."
            return RedirectResponse(f"/summary?year={year}&fx_error={quote(msg)}", status_code=303)
        fx.set_year_method(conn, year, method, rate, fetcher)
        return RedirectResponse(f"/summary?year={year}", status_code=303)

    @app.get("/summary.csv")
    def summary_csv(year: int | None = None, cur: str = "CAD"):
        s = summary_in(pick_year(year), None, "USD" if cur == "USD" else "CAD")
        out = io.StringIO()
        w = csv.writer(out)
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        w.writerow(["Category", "Vendor", "Type", *months, "Year"])
        for g in s.groups:
            for v in g.vendors:
                w.writerow([g.name, v.name, v.recurrence, *[f"{c / 100:.2f}" for c in v.months], f"{v.total / 100:.2f}"])
        w.writerow(["", "Monthly total", "", *[f"{c / 100:.2f}" for c in s.month_totals], f"{s.total / 100:.2f}"])
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="expenses-{s.year}-{s.currency}.csv"'},
        )

    @app.post("/transactions/{txn_id}/tax-date")
    def change_tax_date(txn_id: int, tax_date: date = Form(...), back: str = Form("/transactions")):
        vendors.set_tax_date(conn, txn_id, tax_date.isoformat())
        fx.fill_missing_cad(conn, fetcher)
        return RedirectResponse(back, status_code=303)

    @app.post("/transactions/{txn_id}/covers")
    def covers(txn_id: int, months: str = Form(""), back: str = Form("/transactions")):
        vendors.set_service_months(conn, txn_id, int(months) if months.isdigit() else None)
        return RedirectResponse(back, status_code=303)

    @app.post("/accounts/{account_id}/default")
    def account_default(account_id: int, include_by_default: int = Form(1)):
        conn.execute("UPDATE accounts SET include_by_default = ? WHERE id = ?", (include_by_default, account_id))
        conn.commit()
        return RedirectResponse("/", status_code=303)

    @app.post("/transactions/{txn_id}/exclude")
    def exclude(txn_id: int, excluded: int = Form(0), back: str = Form("/transactions")):
        vendors.set_excluded(conn, txn_id, bool(excluded))
        return RedirectResponse(back, status_code=303)

    @app.post("/assign")
    def assign(
        pattern: str = Form(...),
        vendor_id: str = Form(...),
        new_vendor: str = Form(""),
        category: str = Form("Other business"),
        back: str = Form("/summary"),
    ):
        def error(msg):
            sep = "&" if "?" in back else "?"
            return RedirectResponse(f"{back}{sep}{urlencode({'assign_error': msg})}#unassigned", status_code=303)

        rule = pattern.strip().upper()
        descs = [r[0].upper() for r in conn.execute("SELECT description FROM transactions")]
        if not any(vendors.rule_matches(rule, d) for d in descs):
            return error(f'No transaction description matches "{pattern.strip()}". Try a shorter match text.')
        if vendor_id == "new":
            if not new_vendor.strip():
                return error("Type a name for the new vendor, or pick an existing vendor from the list.")
            vid = vendors.get_or_create_vendor(conn, new_vendor, category)
        else:
            vid = int(vendor_id)
        vendors.add_rule(conn, pattern, vid)
        return RedirectResponse(back, status_code=303)

    @app.get("/vendors", response_class=HTMLResponse)
    def vendor_list(request: Request):
        rows = conn.execute(
            "SELECT v.*, (SELECT COUNT(*) FROM transactions t WHERE t.vendor_id = v.id) AS n "
            "FROM vendors v ORDER BY v.category, v.name"
        ).fetchall()
        rules: dict[int, list] = {}
        for r in conn.execute("SELECT * FROM vendor_rules ORDER BY pattern"):
            rules.setdefault(r["vendor_id"], []).append(r)
        return render(request, "vendors.html", vendors=rows, rules=rules, categories=vendors.CATEGORIES)

    @app.post("/vendors")
    def add_vendor(name: str = Form(...), category: str = Form(...), pattern: str = Form("")):
        vid = vendors.get_or_create_vendor(conn, name, category)
        if pattern.strip():
            vendors.add_rule(conn, pattern, vid)
        conn.commit()
        return RedirectResponse("/vendors", status_code=303)

    @app.post("/vendors/{vendor_id}")
    def update_vendor(vendor_id: int, category: str = Form(...), recurrence: str = Form("")):
        conn.execute(
            "UPDATE vendors SET category = ?, recurrence = ? WHERE id = ?",
            (category, recurrence or None, vendor_id),
        )
        conn.commit()
        return RedirectResponse("/vendors", status_code=303)

    @app.post("/vendors/{vendor_id}/delete")
    def delete_vendor(vendor_id: int):
        conn.execute("DELETE FROM vendors WHERE id = ?", (vendor_id,))
        conn.commit()
        vendors.apply_rules(conn)
        return RedirectResponse("/vendors", status_code=303)

    @app.post("/rules")
    def add_rule(pattern: str = Form(...), vendor_id: int = Form(...)):
        vendors.add_rule(conn, pattern, vendor_id)
        return RedirectResponse("/vendors", status_code=303)

    @app.post("/rules/{rule_id}/delete")
    def delete_rule(rule_id: int):
        conn.execute("DELETE FROM vendor_rules WHERE id = ?", (rule_id,))
        conn.commit()
        vendors.apply_rules(conn)
        return RedirectResponse("/vendors", status_code=303)

    @app.post("/fx/refresh")
    def refresh_fx():
        fx.fill_missing_cad(conn, fetcher)
        return RedirectResponse("/", status_code=303)

    def _payouts_page(request, year: int | None, form=None, editing: int | None = None):
        known = payouts.years(conn)
        year = year or (known[0] if known else date.today().year)
        rows = payouts.for_year(conn, year)
        if any(r["cad_cents"] is None for r in rows):
            fx.fill_missing_cad(conn, fetcher)
            rows = payouts.for_year(conn, year)
        firms = payouts.firms(conn)
        form = form or payouts.PayoutForm()
        if form.firm and form.firm not in firms:
            firms = sorted(firms + [form.firm])  # a firm whose vendor was renamed or recategorized
        return render(
            request, "payouts.html", year=year, years=sorted(set(known) | {year, date.today().year}, reverse=True),
            rows=rows, totals=payouts.totals(rows), firms=firms, paid_via=payouts.PAID_VIA, form=form,
            editing=editing, firm_category=payouts.FIRM_CATEGORY,
        )

    @app.get("/payouts", response_class=HTMLResponse)
    def payouts_page(request: Request, year: int | None = None, edit: int | None = None):
        if edit:
            row = conn.execute("SELECT * FROM payouts WHERE id = ?", (edit,)).fetchone()
            if row:
                return _payouts_page(request, int(row["received_on"][:4]), payouts.PayoutForm.from_row(row), edit)
        return _payouts_page(request, year)

    @app.post("/payouts", response_class=HTMLResponse)
    def save_payout(
        request: Request,
        payout_id: int | None = Form(None),
        firm: str = Form(""),
        currency: str = Form("USD"),
        paid_via: str = Form(""),
        split_pct: str = Form(""),
        approved: str = Form(""),
        approved_on: str = Form(""),
        fees: str = Form(""),
        received_on: str = Form(""),
        received: str = Form(""),
        year: int | None = Form(None),
    ):
        form = payouts.PayoutForm(firm, currency, paid_via, split_pct, approved, approved_on, fees, received_on, received)
        if payouts.save(conn, form, payout_id) is None:
            return _payouts_page(request, year, form, payout_id)
        fx.fill_missing_cad(conn, fetcher)
        return RedirectResponse(f"/payouts?year={received_on[:4]}", status_code=303)

    @app.post("/payouts/{payout_id}/delete")
    def delete_payout(payout_id: int, year: int = Form(...)):
        conn.execute("DELETE FROM payouts WHERE id = ?", (payout_id,))
        conn.commit()
        return RedirectResponse(f"/payouts?year={year}", status_code=303)

    @app.get("/imports", response_class=HTMLResponse)
    def imports(request: Request):
        return _imports_page(request)

    def _imports_page(request, results=None):
        batches = conn.execute(
            "SELECT b.*, a.name AS account, a.currency, "
            "(SELECT COUNT(*) FROM transactions t WHERE t.batch_id = b.id) AS n, "
            # lines in this file that belong to another import (overlapping statements)
            "(SELECT COUNT(*) FROM batch_lines l JOIN transactions t ON t.dedupe_key = l.dedupe_key "
            " WHERE l.batch_id = b.id AND t.batch_id != b.id) AS shared "
            "FROM import_batches b JOIN accounts a ON a.id = b.account_id ORDER BY b.id DESC"
        ).fetchall()
        return render(request, "imports.html", batches=batches, results=results)

    @app.post("/imports/row-counts", response_class=HTMLResponse)
    async def fill_row_counts(request: Request, file: list[UploadFile] = File(...)):
        results = []
        for f in file:
            text = (await f.read()).decode("utf-8-sig", errors="replace")
            try:
                found = importer.fill_file_rows(conn, text)
            except importer.CsvFormatError as e:
                found, error = None, str(e)
            else:
                error = None if found is not None else "No saved format matches this file's columns."
            if found == []:
                error = "Doesn't match any import. Its lines may have been removed, or it was never imported."
            results.append({"filename": f.filename, "imports": found or [], "error": error})
        return _imports_page(request, results)

    @app.post("/batches/{batch_id}/delete")
    def delete_batch(batch_id: int, back: str = Form("/")):
        importer.remove_batch(conn, batch_id)
        return RedirectResponse("/imports" if back == "/imports" else "/", status_code=303)

    return app
