#!/usr/bin/env python3
"""Development server for the ads admin console (standard library only).

    python apps/ads-admin/serve.py --port 4300 --api http://localhost:8000

Serves this folder and generates config.js from --api / ADS_ADMIN_API_BASE — the same thing the
Docker image's entrypoint does — so the console can run on its own origin next to any API.
"""

from __future__ import annotations

import argparse
import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    api_base = ""

    def do_GET(self):  # noqa: N802 — http.server API
        if self.path.split("?")[0] == "/config.js":
            body = f"window.ADS_ADMIN_CONFIG = {json.dumps({'apiBase': self.api_base})};\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, fmt, *args):  # quiet unless asked
        if os.environ.get("ADS_ADMIN_VERBOSE"):
            super().log_message(fmt, *args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=int(os.environ.get("ADS_ADMIN_PORT", "4300")))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--api", default=os.environ.get("ADS_ADMIN_API_BASE", "http://localhost:8000"))
    args = parser.parse_args()
    handler = partial(Handler, directory=str(HERE))
    Handler.api_base = args.api.rstrip("/")
    with ThreadingHTTPServer((args.bind, args.port), handler) as httpd:
        print(
            f"Ads admin console: http://localhost:{args.port}/  (API: {Handler.api_base or 'same origin'})", flush=True
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
