"""Vertex Desktop — native-window launcher.

Starts the existing HTTP/SSE server in the background and hosts the UI in a
real Windows window via pywebview + Microsoft Edge WebView2 (pre-installed
on Windows 11, available on Windows 10 via a free runtime download).

Launch directly:   python vertex_desktop.py
Build an .exe:     .\build.ps1   (see that file)
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

import webview

# Import module-level state from the existing server
from vertex_app_v2 import Handler, worker, PORT, HTML_PATH


WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def _webview2_installed() -> bool:
    """Return True if the Microsoft Edge WebView2 Runtime is present on this
    machine. Checks the three registry locations Microsoft uses
    (per-machine x64, per-machine x86, and per-user)."""
    if sys.platform != "win32":
        return True  # non-Windows path: pywebview picks its own backend
    try:
        import winreg
    except ImportError:
        return False

    GUID = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + GUID),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + GUID),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + GUID),
    ]
    for hive, sub in paths:
        try:
            with winreg.OpenKey(hive, sub) as k:
                ver, _ = winreg.QueryValueEx(k, "pv")
                if ver and ver != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _prompt_install_webview2() -> None:
    """Show a native message box explaining the WebView2 dependency and
    offer to open the Microsoft download page. Blocks until dismissed."""
    MB_YESNO = 0x04
    MB_ICON_INFO = 0x40
    IDYES = 6
    msg = (
        "Vertex Desktop needs the Microsoft Edge WebView2 Runtime to display "
        "its interface. It's a free ~2 MB install from Microsoft and ships "
        "with Windows 11 by default.\n\n"
        "Open the download page now?"
    )
    if sys.platform == "win32":
        result = ctypes.windll.user32.MessageBoxW(
            0, msg, "Vertex Desktop — missing component", MB_YESNO | MB_ICON_INFO
        )
        if result == IDYES:
            webbrowser.open(WEBVIEW2_DOWNLOAD_URL)
    else:
        print(msg)
        print(f"Download: {WEBVIEW2_DOWNLOAD_URL}")


def start_server() -> ThreadingHTTPServer:
    """Bind and serve on localhost in a daemon thread. Returns the server."""
    if not HTML_PATH.exists():
        raise FileNotFoundError(f"ui.html not found at {HTML_PATH}")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    # Preflight: WebView2 is the only non-bundled dependency on Windows.
    # Guide the user to install it instead of crashing with a cryptic backend error.
    if not _webview2_installed():
        _prompt_install_webview2()
        return 1

    try:
        server = start_server()
    except OSError as e:
        # Port already in use → an instance is already running. Just show its window.
        if "10048" in str(e) or "already in use" in str(e).lower():
            print("Vertex is already running on 127.0.0.1:8765")
        else:
            print(f"Server bind failed: {e}", file=sys.stderr)
            return 1
        server = None

    # Give the server a beat to warm up before we point the webview at it
    time.sleep(0.3)

    url = f"http://127.0.0.1:{PORT}/"
    webview.create_window(
        "Vertex Desktop",
        url,
        width=1360,
        height=940,
        min_size=(960, 700),
        confirm_close=False,
    )

    try:
        # edgechromium = Microsoft Edge WebView2 backend; stable on Win10/11.
        webview.start(gui="edgechromium", debug=False)
    finally:
        # Window closed → shut the BLE worker down cleanly and stop the server.
        try:
            worker.stop()
        except Exception:
            pass
        if server is not None:
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
