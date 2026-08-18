"""
fileserver.py - update server for PXM_LU4. Run on the DEV machine.

Serves this folder to the other machines so they can run run_updater.bat.
Start it with start_server.bat, or directly:

    python fileserver.py            # 0.0.0.0:8081
    python fileserver.py --port 9000 --bind 192.168.0.156

It serves ONLY the files listed in manifest.json, plus manifest.json itself.
That is deliberate, and it is the reason this exists instead of a one-line
`python -m http.server`: this project tree also contains real secrets that have
no business on the wire, among them PXM_RB/accounts.json (game logins) and
PXM_RB/*/config_local.py (coordinator API token, Telegram bot token). A plain
directory server publishes every one of them to anything on the LAN. Driving the
allow-list off the manifest means the set of readable files is exactly the set
the updater needs, and adding a file to the tree does not silently publish it -
it has to be tracked in updater.py and re-manifested first.

Traversal is impossible by construction: a request path is looked up as a key in
the allow-list, never resolved against the filesystem.

Read-only: GET and HEAD only, everything else is refused.
"""

import argparse
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.json"
DEFAULT_PORT = 8081        # matches start_server.bat and run_updater.bat

# Text types are labelled so a browser shows them instead of downloading them,
# which makes "is my .ino actually being served?" a one-click question.
TEXT_SUFFIXES = {".py", ".bat", ".ino", ".json", ".txt", ".md", ".cfg", ".ini"}


class Allowlist:
    """Serveable paths, taken from manifest.json and refreshed when it changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._names = set()
        self.reload()

    def reload(self):
        try:
            mtime = MANIFEST.stat().st_mtime
        except OSError:
            with self._lock:
                self._names = set()
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [!] manifest.json unreadable: {e}")
            return
        with self._lock:
            self._mtime = mtime
            self._names = set(data.keys()) | {"manifest.json"}
        print(f"  manifest loaded: {len(self._names)} file(s) served")

    def __contains__(self, rel):
        # Picked up without a restart if make_manifest.bat is re-run mid-session.
        self.reload()
        with self._lock:
            return rel in self._names

    def sorted_names(self):
        self.reload()
        with self._lock:
            return sorted(self._names)


ALLOW = Allowlist()


class Handler(BaseHTTPRequestHandler):
    server_version = "PXM_LU4-fileserver/1.0"
    protocol_version = "HTTP/1.1"        # keep-alive; 9 slaves pull many files

    # ---- helpers -------------------------------------------------------------
    def _rel_from_path(self):
        """Request path -> manifest key, or None if it is not serveable."""
        rel = unquote(urlparse(self.path).path).lstrip("/")
        if not rel or rel.endswith("/"):
            return None
        rel = rel.replace("\\", "/")
        if rel not in ALLOW:
            return None
        return rel

    def _send_bytes(self, body, ctype, head_only=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _fail(self, code, msg):
        body = f"{code} {msg}\n".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- verbs ---------------------------------------------------------------
    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _serve(self, head_only):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_bytes(self._index_html(), "text/html; charset=utf-8", head_only)
            return

        rel = self._rel_from_path()
        if rel is None:
            self._fail(404, "Not served. Only files listed in manifest.json are "
                            "available; run make_manifest.bat if you just added one.")
            return

        target = ROOT / rel
        if not target.is_file():
            self._fail(404, f"{rel} is in the manifest but missing on disk")
            return
        try:
            body = target.read_bytes()
        except OSError as e:
            self._fail(500, f"cannot read {rel}: {e}")
            return

        if target.suffix.lower() in TEXT_SUFFIXES:
            ctype = "text/plain; charset=utf-8"
        else:
            ctype = "application/octet-stream"
        self._send_bytes(body, ctype, head_only)

    def _index_html(self):
        names = ALLOW.sorted_names()
        rows = "\n".join(f'<li><a href="/{n}">{n}</a></li>' for n in names)
        return (
            "<!doctype html><meta charset='utf-8'>"
            "<title>PXM_LU4 update server</title>"
            "<style>body{font:13px Consolas,monospace;background:#161616;color:#ddd;"
            "margin:24px}a{color:#6cf;text-decoration:none}a:hover{text-decoration:"
            "underline}h1{font-size:15px}li{line-height:1.5}</style>"
            f"<h1>PXM_LU4 update server &mdash; {len(names)} file(s)</h1>"
            "<p>Point the other machines at this address with run_updater.bat.</p>"
            f"<ul>{rows}</ul>"
        ).encode("utf-8")

    # ---- logging -------------------------------------------------------------
    def log_message(self, fmt, *args):
        print(f"  [{time.strftime('%H:%M:%S')}] {self.address_string():<15} "
              f"{fmt % args}")

    def log_error(self, fmt, *args):
        self.log_message(fmt, *args)


def local_ips():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       family=socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def main():
    ap = argparse.ArgumentParser(description="PXM_LU4 update server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    # A console line-buffers on its own, but a redirected stream block-buffers,
    # so `start_server.bat > server.log` would show nothing until it exited.
    try:
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    except Exception:
        pass

    if not MANIFEST.exists():
        print(f"\n  [ERROR] {MANIFEST.name} not found in {ROOT}")
        print("          Run make_manifest.bat first, then start this again.\n")
        return 1

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    try:
        httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as e:
        print(f"\n  [ERROR] cannot bind {args.bind}:{args.port} - {e}")
        print("          Something else may already be using that port.\n")
        return 1

    print(f"\n{'=' * 62}")
    print("  PXM_LU4 update server")
    print(f"  Serving : {ROOT}  (manifest files only)")
    print(f"  Listening on {args.bind}:{args.port}")
    for ip in local_ips():
        print(f"    other machines use:  http://{ip}:{args.port}")
    print("  Ctrl+C to stop.")
    print(f"{'=' * 62}\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
