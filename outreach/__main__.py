"""Start the dashboard:  python -m outreach"""

import os
import threading
import webbrowser

import uvicorn

from .web import create_app


def main() -> None:
    port = int(os.environ.get("OUTREACH_PORT", "8000"))
    url = f"http://localhost:{port}"
    print("\n  Outreach dashboard is running at", url)
    print("  Keep this window open while you want emails to be sent.")
    print("  To stop: close this window (or press Ctrl+C).\n")
    if os.environ.get("OUTREACH_NO_BROWSER") != "1":
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    # 127.0.0.1 = only reachable from this computer.
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
