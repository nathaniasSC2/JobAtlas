"""A real browser for application forms that only exist once a page's JavaScript has run.

Ashby's form and Workday's apply page are built in the browser from calls JobAtlas doesn't make
itself (Ashby's /api/ is closed to crawlers in robots.txt). For those, JobAtlas asks a
camofox-browser server (https://github.com/jo-inc/camofox-browser, a REST API around a headless
Firefox) to load the public page, runs a read-only script in it and closes the tab. Configure it
under forms.browser in config.json; without it those forms are simply not read.

The same manners as every other request: robots.txt must allow the page, the host's spacing
applies, one tab at a time, closed after each page. Pages are only read: nothing is typed or
submitted and no account is used. (Ashby's script opens each dropdown to list its options and
closes it again; that is all it does.)

The server's access key comes from an environment variable (forms.browser.key_env, by default
CAMOFOX_ACCESS_KEY) or, on a box where the server runs in Docker, from the container's own
environment at call time (forms.browser.docker_container), so it is never written anywhere.
"""
import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("jobatlas.browser")

DEFAULTS = {"url": "", "key_env": "CAMOFOX_ACCESS_KEY", "docker_container": "", "user": "jobatlas",
            "session": "jobatlas-forms", "timeout": 25}


class BrowserError(Exception):
    pass


class Browser:
    def __init__(self, cfg, client=None, env=None):
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.base = self.cfg["url"].rstrip("/")
        self.client = client
        self.env = env
        self.lock = threading.Lock()      # one tab at a time
        self._key = None
        self.stats = {"pages": 0, "errors": 0, "robots_blocked": 0}

    # ------------------------------------------------------------- the access key
    def key(self):
        if self._key is not None:
            return self._key
        import os
        env = self.env if self.env is not None else os.environ
        k = env.get(self.cfg["key_env"] or "", "")
        if not k and self.cfg.get("docker_container"):
            try:
                p = subprocess.run(["docker", "inspect", self.cfg["docker_container"], "--format",
                                    "{{range .Config.Env}}{{println .}}{{end}}"], capture_output=True, text=True, timeout=10)
                name = (self.cfg["key_env"] or "CAMOFOX_ACCESS_KEY") + "="
                k = next((ln[len(name):] for ln in p.stdout.splitlines() if ln.startswith(name)), "")
            except (OSError, subprocess.SubprocessError):
                k = ""
        self._key = k
        return k

    # ------------------------------------------------------------- the REST API
    def _call(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"}
        if self.key():
            h["Authorization"] = "Bearer " + self.key()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.cfg["timeout"]) as r:
                txt = r.read().decode("utf8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            txt, status = (e.read() or b"").decode("utf8", "replace"), e.code
        except Exception as e:  # noqa: BLE001
            raise BrowserError("%s: %s" % (type(e).__name__, e))
        try:
            d = json.loads(txt) if txt else {}
        except ValueError:
            d = {"text": txt}
        if status >= 400:
            raise BrowserError("HTTP %s %s" % (status, str(d.get("error") or d)[:120]))
        return d

    def close_stale(self):
        """Close tabs this user left open (a crash mid-page); the server only idles down with none open."""
        try:
            d = self._call("GET", "/tabs?userId=" + urllib.parse.quote(self.cfg["user"]), timeout=10)
        except BrowserError:
            return 0
        tabs = d.get("tabs") if isinstance(d, dict) else d
        n = 0
        for t in tabs or []:
            tid = t.get("tabId") or t.get("id") if isinstance(t, dict) else t
            if tid:
                self._close(tid)
                n += 1
        return n

    def _close(self, tid):
        try:
            self._call("DELETE", "/tabs/%s?userId=%s" % (urllib.parse.quote(str(tid)), urllib.parse.quote(self.cfg["user"])), timeout=10)
        except BrowserError as e:
            log.info("browser: closing tab %s failed: %s", tid, e)

    def _turn(self, url):
        """Wait for the host's turn (the HTTP client's spacing for that host), then hold it for this page."""
        if self.client is None or not hasattr(self.client, "_gate"):
            return
        g = self.client._gate(urllib.parse.urlsplit(url).netloc.lower())
        with g.lock:
            wait = g.next_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            g.next_at = time.monotonic() + max(g.spacing, 2.0)

    def read(self, url, script, timeout=None):
        """Load url in a new tab, evaluate script (an expression; a Promise is awaited), close the tab.
        -> the script's result. Raises BrowserError (robots.txt, the server, the page)."""
        if not self.base:
            raise BrowserError("no browser configured (forms.browser.url)")
        if self.client is not None and not self.client.allowed(url):
            self.stats["robots_blocked"] += 1
            raise BrowserError("robots.txt disallows")
        with self.lock:
            self._turn(url)
            t = self._call("POST", "/tabs", {"userId": self.cfg["user"], "sessionKey": self.cfg["session"], "url": url})
            tid = t.get("tabId") or t.get("id")
            if not tid:
                raise BrowserError("no tab: %s" % str(t)[:120])
            try:
                r = self._call("POST", "/tabs/%s/evaluate" % urllib.parse.quote(str(tid)),
                               {"userId": self.cfg["user"], "expression": script}, timeout=timeout or self.cfg["timeout"])
            except BrowserError:
                self.stats["errors"] += 1
                raise
            finally:
                self._close(tid)
            self.stats["pages"] += 1
        if isinstance(r, dict) and r.get("ok") is False:
            raise BrowserError("script: %s" % str(r.get("error") or r)[:160])
        return r.get("result") if isinstance(r, dict) and "result" in r else r


def from_config(cfg, client=None):
    """forms.browser -> Browser, or None when no browser is configured."""
    b = (cfg or {}).get("browser") or {}
    if not b.get("url"):
        return None
    return Browser(b, client)
