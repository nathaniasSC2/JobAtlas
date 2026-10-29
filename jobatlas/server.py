"""Small read-only HTTP API. It has no auth of its own: bind it to localhost (the
default) or a private network interface, never the open internet.

  GET /health
  GET /latest.json   ?hours=72&min_tier=fair&include_seen=1&limit=500&q=
  GET /jobs/<id>     one job incl. description
  GET /status        same numbers as `jobatlas status`
  GET /discover      ?company=...&title=...&website=...  (live lookup; one at a time)
"""
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, export, status as status_mod

log = logging.getLogger("jobatlas.api")
_discover_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "JobAtlas/0.1"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code, obj, ctype="application/json"):
        body = (json.dumps(obj, ensure_ascii=False, indent=1) if ctype == "application/json" else obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        conn = db.connect()
        try:
            if u.path in ("/", "/health"):
                last = conn.execute("SELECT finished, new_matches FROM runs WHERE kind='poll' AND finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
                return self._send(200, {"ok": True, "last_poll": last["finished"] if last else None,
                                        "last_new_matches": last["new_matches"] if last else None})
            if u.path in ("/latest.json", "/latest"):
                rows = export.query(conn, hours=int(qs.get("hours", config.cfg()["export"]["hours"])), min_tier=qs.get("min_tier"),
                                    include_seen=qs.get("include_seen", "1") != "0", limit=int(qs.get("limit", 500)), q=qs.get("q"))
                return self._send(200, {"generated": db.now(), "count": len(rows), "jobs": [export.row_out(r) for r in rows]})
            if u.path.startswith("/jobs/"):
                r = conn.execute("SELECT * FROM jobs WHERE id=?", (u.path.rsplit("/", 1)[-1],)).fetchone()
                if not r:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, export.row_out(r, desc_chars=20000))
            if u.path == "/status":
                return self._send(200, status_mod.collect(conn))
            if u.path == "/discover":
                if not qs.get("company"):
                    return self._send(400, {"error": "company= is required"})
                if not _discover_lock.acquire(timeout=1):
                    return self._send(429, {"error": "a discovery is already running"})
                try:
                    from . import discover
                    from .net import Client
                    out = discover.discover(conn, Client(), qs["company"], website=qs.get("website", ""), title=qs.get("title", ""),
                                            source="api")
                    for b in out["boards"]:
                        b.pop("board_id", None)
                    return self._send(200, out)
                finally:
                    _discover_lock.release()
            return self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            return self._send(500, {"error": str(e)[:300]})
        finally:
            conn.close()


def serve(host=None, port=None):
    c = config.cfg()["api"]
    host, port = host or c["host"], int(port or c["port"])
    httpd = ThreadingHTTPServer((host, port), Handler)
    log.info("JobAtlas API on %s:%d", host, port)
    httpd.serve_forever()
