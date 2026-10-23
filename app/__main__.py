import threading
import webbrowser

import uvicorn

from .main import create_app

URL = "http://127.0.0.1:8000"

if __name__ == "__main__":
    app = create_app()
    print(f"Expense Tracker is running at {URL} - close this window to stop it.")
    threading.Timer(1.5, webbrowser.open, args=(URL,)).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
