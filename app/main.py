"""Local web app: run with `python -m app` and open http://127.0.0.1:8000."""

from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, fx, importer

HERE = Path(__file__).parent


def cents(value: int | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value) / 100:,.2f}"


def create_app(db_path: Path | str | None = None, fetcher: fx.Fetcher | None = fx.fetch_boc) -> FastAPI:
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

    @app.get("/import", response_class=HTMLResponse)
    def import_form(request: Request, account_id: int | None = None):
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        return render(request, "import.html", accounts=accounts, selected=account_id)

    @app.post("/import/preview", response_class=HTMLResponse)
    async def import_preview(request: Request, account_id: int = Form(...), file: UploadFile = Form(...)):
        text = (await file.read()).decode("utf-8-sig", errors="replace")
        account = get_account(account_id)
        fmt = importer.match_format(conn, text, prefer_id=account["last_format_id"])
        if fmt:
            mapping, name, note = importer.Mapping.from_row(fmt), fmt["name"], f"Matched saved format \u201c{fmt['name']}\u201d."
        else:
            mapping, name, note = importer.guess_mapping(text), f"{account['name']} format", None
        return _preview(request, account, text, file.filename or "upload.csv", mapping, name, note)

    @app.post("/import/confirm", response_class=HTMLResponse)
    def import_confirm(
        request: Request,
        account_id: int = Form(...),
        filename: str = Form(...),
        csv_text: str = Form(...),
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
        if action == "load" and load_format_id:
            fmt = conn.execute("SELECT * FROM csv_formats WHERE id = ?", (load_format_id,)).fetchone()
            return _preview(request, account, csv_text, filename, importer.Mapping.from_row(fmt), fmt["name"])
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
            return _preview(request, account, csv_text, filename, mapping, format_name)
        if not format_name.strip():
            return _preview(request, account, csv_text, filename, mapping, format_name, error="Give the format a name.")
        try:
            rows = importer.parse(csv_text, mapping)
        except importer.CsvFormatError as e:
            return _preview(request, account, csv_text, filename, mapping, format_name, error=str(e))

        format_id = importer.save_format(conn, format_name, mapping, csv_text)
        conn.execute("UPDATE accounts SET last_format_id = ? WHERE id = ?", (format_id, account_id))
        added, skipped = importer.save(conn, account, rows, filename)
        missing = fx.fill_missing_cad(conn, fetcher) if account["currency"] == "USD" else 0
        return render(
            request, "imported.html", account=account, added=added, skipped=skipped, missing=missing
        )

    def _preview(request, account, text, filename, mapping, format_name, note=None, error=None):
        header, _ = importer.read_rows(text, mapping.has_header)
        rows, parse_error = [], error
        if not parse_error:
            try:
                rows = importer.parse(text, mapping)
            except importer.CsvFormatError as e:
                parse_error = str(e)
        return render(
            request,
            "preview.html",
            account=account,
            filename=filename,
            csv_text=text,
            header=header,
            mapping=mapping,
            rows=rows[:25],
            total_rows=len(rows),
            total_cents=sum(r.amount_cents for r in rows),
            error=parse_error,
            date_formats=importer.DATE_FORMATS,
            format_name=format_name,
            note=note,
            formats=conn.execute("SELECT id, name FROM csv_formats ORDER BY name").fetchall(),
        )

    @app.get("/transactions", response_class=HTMLResponse)
    def transactions(request: Request, account_id: int | None = None, month: str | None = None):
        sql = (
            "SELECT t.*, a.name AS account FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE 1=1"
        )
        args: list = []
        if account_id:
            sql += " AND t.account_id = ?"
            args.append(account_id)
        if month:
            sql += " AND substr(t.txn_date, 1, 7) = ?"
            args.append(month)
        sql += " ORDER BY t.txn_date DESC, t.id DESC"
        rows = conn.execute(sql, args).fetchall()
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        months = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT substr(txn_date, 1, 7) FROM transactions ORDER BY 1 DESC"
            )
        ]
        total_cad = sum(r["cad_cents"] or 0 for r in rows)
        return render(
            request,
            "transactions.html",
            rows=rows,
            accounts=accounts,
            months=months,
            account_id=account_id,
            month=month,
            total_cad=total_cad,
        )

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

    @app.post("/fx/refresh")
    def refresh_fx():
        fx.fill_missing_cad(conn, fetcher)
        return RedirectResponse("/", status_code=303)

    @app.post("/batches/{batch_id}/delete")
    def delete_batch(batch_id: int):
        conn.execute("DELETE FROM transactions WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
        conn.commit()
        return RedirectResponse("/", status_code=303)

    return app
