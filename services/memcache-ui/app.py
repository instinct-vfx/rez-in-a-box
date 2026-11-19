# SPDX-License-Identifier: Apache-2.0
"""rez memcache inspector: tiny standard-library web server.

Serves index.html plus a JSON API on top of rezbox.memcache.Inspector:

  GET  /api/stats             server statistics (+ hit ratio)
  GET  /api/entries?limit=N   all entries with rez-aware summaries
  GET  /api/entry?key=K       one entry, payload decoded
  POST /api/delete            {"key": K}
  POST /api/flush             flush_all
  POST /api/reset-stats       stats reset
  GET  /healthz
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
# repo layout (running from a checkout) and container layout (/app/rezbox)
for candidate in (HERE, HERE.parent.parent):
    if (candidate / "rezbox" / "memcache.py").is_file():
        sys.path.insert(0, str(candidate))
        break

from rezbox.memcache import Inspector, MemcacheError  # noqa: E402

MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "rezbox-memcache-ui"
    inspector: Inspector
    index_html: bytes

    def log_message(self, fmt, *args):  # quieter than the default
        if os.environ.get("REZBOX_UI_ACCESS_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _error(self, status: int, msg: str) -> None:
        self._json({"error": msg}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        url = urlsplit(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, self.index_html, "text/html; charset=utf-8")
            elif url.path == "/healthz":
                self._json({"ok": True, "memcached": self.inspector.server})
            elif url.path == "/api/stats":
                self._json(self.inspector.stats())
            elif url.path == "/api/entries":
                limit = int(qs.get("limit", ["5000"])[0])
                self._json(self.inspector.entries(limit=max(1, min(limit, 100000))))
            elif url.path == "/api/entry":
                key = qs.get("key", [""])[0]
                entry = self.inspector.entry(key) if key else None
                if entry is None:
                    self._error(404, "no such key (expired or evicted?)")
                else:
                    self._json(entry)
            else:
                self._error(404, "not found")
        except MemcacheError as e:
            self._error(HTTPStatus.BAD_GATEWAY, str(e))
        except (ValueError, TypeError) as e:
            self._error(400, str(e))

    def do_POST(self):
        url = urlsplit(self.path)
        # the UI always sends JSON; refusing other types blocks simple
        # cross-site form posts from other pages in the browser
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "expected application/json")
        try:
            body = self._body()
            if url.path == "/api/delete":
                self._json({"deleted": self.inspector.delete(str(body["key"]))})
            elif url.path == "/api/flush":
                self.inspector.flush()
                self._json({"flushed": True})
            elif url.path == "/api/reset-stats":
                self.inspector.reset_stats()
                self._json({"reset": True})
            else:
                self._error(404, "not found")
        except MemcacheError as e:
            self._error(HTTPStatus.BAD_GATEWAY, str(e))
        except (ValueError, KeyError, TypeError) as e:
            self._error(400, str(e))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--memcached", default=os.environ.get("REZBOX_MEMCACHED", "127.0.0.1:11211"))
    args = p.parse_args(argv)

    Handler.inspector = Inspector(args.memcached)
    Handler.index_html = (HERE / "index.html").read_bytes()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"memcache inspector on http://{args.host}:{args.port} -> memcached {args.memcached}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
