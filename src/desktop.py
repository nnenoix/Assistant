"""Desktop launcher.

Runs uvicorn in a background thread, waits until it responds, then opens a
native window (Microsoft Edge WebView2 on Windows) pointing at the local UI.
Closing the window terminates the process — uvicorn shuts down with it
because the server thread is a daemon.

Usage:
    uv run python -m src.desktop
"""
import threading
import time
import urllib.error
import urllib.request

import uvicorn
import webview

from src.app import app


HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"

WINDOW_TITLE = "Workspace Agent"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860
MIN_SIZE = (900, 600)


def _run_server() -> None:
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.run()


def _wait_for_server(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{URL}/api/accounts", timeout=1) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(0.15)
    return False


def main() -> int:
    threading.Thread(target=_run_server, daemon=True, name="uvicorn").start()
    if not _wait_for_server():
        print(f"Server did not start within 30s on {URL}", flush=True)
        return 1

    webview.create_window(
        WINDOW_TITLE,
        URL,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=MIN_SIZE,
    )
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
