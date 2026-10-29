"""Polite HTTP: per-host spacing, robots.txt, conditional requests, 429 back-off.

Rules (good citizen):
  * one request at a time per host, spaced by config http.host_spacing;
  * robots.txt is honoured for every host (RFC 9309: 4xx robots = allowed,
    5xx/unreachable = skip the host this run);
  * If-None-Match / If-Modified-Since when we hold an ETag / Last-Modified;
  * a 429 or Cloudflare 1015 pauses the host (cooldown) and after max_429
    drops it for the rest of the run. Bot walls are never worked around.
"""
import datetime
import gzip
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib

from . import config

BOT_WALL_MARKERS = ("_Incapsula_Resource", "cf-chl-", "challenge-platform", "Attention Required! | Cloudflare",
                    "g-recaptcha", "h-captcha", "px-captcha", "error code: 1015")


class Response:
    __slots__ = ("status", "headers", "body", "url", "error")

    def __init__(self, status, headers=None, body=b"", url="", error=""):
        self.status, self.headers, self.body, self.url, self.error = status, headers or {}, body, url, error

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        return self.body.decode("utf-8", "replace") if isinstance(self.body, (bytes, bytearray)) else (self.body or "")

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError:
            return None

    def header(self, name):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None

    def __repr__(self):
        return "<Response %s %s %s>" % (self.status, self.url[:80], self.error[:60])


class HostGate:
    def __init__(self, spacing):
        self.lock = threading.Lock()
        self.spacing = spacing
        self.next_at = 0.0
        self.strikes = 0
        self.dead = False


# Multi-tenant ATSs serve every customer subdomain from one platform: share one
# politeness gate and one robots.txt per platform instead of per subdomain.
SHARED = ("bamboohr.com", "recruitee.com", "breezy.hr", "teamtailor.com", "pinpointhq.com", "applicantpro.com")


def gate_key(host):
    for base in SHARED:
        if host == base or host.endswith("." + base):
            return base
    return host


class Client:
    """Thread-safe polite client. One per process run."""

    def __init__(self, cfg=None, log=None):
        self.cfg = (cfg or config.cfg())["http"]
        self.gates = {}
        self.glock = threading.Lock()
        self.robots = {}
        self.rlock = threading.Lock()
        self.log = log or (lambda *a: None)
        self.blocks_path = os.path.join(config.CACHE, "host_blocks.json")
        self.blocks = self._load_blocks()
        self.stats = {"requests": 0, "not_modified": 0, "errors": 0, "rate_limited": 0, "robots_blocked": 0, "bytes": 0}
        self.slock = threading.Lock()

    # ------------------------------------------------------------- long back-offs (Retry-After > cooldown)
    def _load_blocks(self):
        try:
            with open(self.blocks_path, encoding="utf8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return {h: u for h, u in d.items() if u > now}

    def block_host(self, host, seconds, why=""):
        """Back a host off across runs (persisted in cache/host_blocks.json)."""
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()
        with self.glock:
            self.blocks[gate_key(host)] = until
            try:
                os.makedirs(os.path.dirname(self.blocks_path), exist_ok=True)
                with open(self.blocks_path + ".tmp", "w", encoding="utf8") as f:
                    json.dump(self.blocks, f, indent=1)
                os.replace(self.blocks_path + ".tmp", self.blocks_path)
            except OSError:
                pass
        self.log("net: %s backed off until %s %s" % (host, until, why))

    def blocked_until(self, host):
        u = self.blocks.get(gate_key(host))
        if u and u > datetime.datetime.now(datetime.timezone.utc).isoformat():
            return u
        return None

    # ------------------------------------------------------------- gates
    def _gate(self, host):
        host = gate_key(host)
        with self.glock:
            g = self.gates.get(host)
            if g is None:
                sp = self.cfg["host_spacing"].get(host)
                if sp is None and host in SHARED:
                    sp = 0.3
                if sp is None:
                    sp = self.cfg.get("workday_spacing", 1.5) if host.endswith("myworkdayjobs.com") else self.cfg["default_spacing"]
                g = self.gates[host] = HostGate(float(sp))
            return g

    def host_dead(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        return self._gate(host).dead or bool(self.blocked_until(host))

    def _bump(self, k, n=1):
        with self.slock:
            self.stats[k] = self.stats.get(k, 0) + n

    # ------------------------------------------------------------- robots
    def allowed(self, url):
        p = urllib.parse.urlsplit(url)
        host = p.netloc.lower()
        if host in (self.cfg.get("robots_override") or []):
            return True
        key = gate_key(host)
        with self.rlock:
            rp = self.robots.get(key)
        if rp is None:
            rp = self._load_robots(p.scheme or "https", host)
            with self.rlock:
                self.robots[key] = rp
        if rp == "allow":
            return True
        if rp == "deny":
            return False
        return rp.can_fetch(config.ROBOTS_TOKEN, url)

    def _load_robots(self, scheme, host):
        r = self._raw("GET", f"{scheme}://{host}/robots.txt", accept="text/plain,*/*", timeout=15)
        if r.status == 0 or r.status >= 500:
            self.log("robots: %s unreachable (%s): skipping host this run" % (host, r.status or r.error[:40]))
            return "deny"
        if r.status >= 400 or "html" in (r.header("content-type") or "").lower() and "user-agent" not in r.text.lower():
            return "allow"
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp

    # ------------------------------------------------------------- requests
    def _raw(self, method, url, data=None, ctype=None, accept="application/json", headers=None, timeout=None):
        h = {"User-Agent": self.cfg.get("user_agent") or config.UA, "Accept": accept, "Accept-Encoding": "gzip, deflate", "Accept-Language": "en-US,en;q=0.8"}
        if ctype:
            h["Content-Type"] = ctype
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.cfg["timeout"]) as resp:
                body = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                body = _decode(body, enc)
                self._bump("bytes", len(body))
                return Response(resp.status, dict(resp.headers), body, resp.geturl())
        except urllib.error.HTTPError as e:
            try:
                body = _decode(e.read(), (e.headers.get("Content-Encoding") or "").lower()) if e.fp else b""
            except Exception:  # noqa: BLE001
                body = b""
            return Response(e.code, dict(e.headers or {}), body, url)
        except Exception as e:  # noqa: BLE001
            return Response(0, {}, b"", url, error=str(e))

    def request(self, method, url, data=None, ctype=None, accept="application/json", etag=None, last_modified=None,
                headers=None, check_robots=True, timeout=None):
        host = urllib.parse.urlsplit(url).netloc.lower()
        g = self._gate(host)
        if g.dead:
            return Response(0, url=url, error="host dropped this run (rate limited)")
        until = self.blocked_until(host)
        if until:
            return Response(0, url=url, error="rate limited: host backed off until " + until)
        if check_robots and not self.allowed(url):
            self._bump("robots_blocked")
            return Response(0, url=url, error="robots.txt disallows")
        hdr = dict(headers or {})
        if etag:
            hdr["If-None-Match"] = etag
        if last_modified:
            hdr["If-Modified-Since"] = last_modified
        for attempt in range(2):
            with g.lock:
                wait = g.next_at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                r = self._raw(method, url, data=data, ctype=ctype, accept=accept, headers=hdr, timeout=timeout)
                g.next_at = time.monotonic() + g.spacing
                self._bump("requests")
                limited = r.status == 429 or (r.status in (403, 503) and "1015" in r.text[:2000] and "error code" in r.text[:2000])
                if limited:
                    self._bump("rate_limited")
                    g.strikes += 1
                    ra = r.header("Retry-After")
                    cool = self.cfg["cooldown_429"]
                    try:
                        ra_s = int(ra) if ra else 0
                    except ValueError:
                        ra_s = 0
                    if ra_s > 900:  # e.g. Workable's ~24 h Retry-After: honour it across runs
                        g.dead = True
                        self.block_host(host, ra_s, "(Retry-After %ss)" % ra_s)
                        return Response(429, r.headers, r.body, url, error="rate limited (Retry-After %ss)" % ra_s)
                    cool = max(cool, ra_s)
                    g.spacing = min(g.spacing * 2, 30)
                    if g.strikes >= self.cfg["max_429"]:
                        g.dead = True
                        self.log("net: %s rate-limited %d times, dropped for this run" % (host, g.strikes))
                        return Response(429, r.headers, r.body, url, error="rate limited")
                    self.log("net: %s rate-limited, cooling %ds" % (host, cool))
                    g.next_at = time.monotonic() + cool
                    continue
            if r.status == 304:
                self._bump("not_modified")
            elif r.status == 0 or r.status >= 500:
                self._bump("errors")
                if attempt == 0 and r.status >= 500:
                    time.sleep(2)
                    continue
            return r
        return r

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post_json(self, url, payload, **kw):
        return self.request("POST", url, data=json.dumps(payload).encode(), ctype="application/json", **kw)


def _decode(body, enc):
    if not body:
        return body
    try:
        if "gzip" in enc:
            return gzip.GzipFile(fileobj=io.BytesIO(body)).read()
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001
        return body
    return body


def is_bot_wall(r):
    if r.status in (401, 403, 406, 429, 503):
        return True
    t = r.text[:20000] if r.body else ""
    return len(t) < 20000 and any(m in t for m in BOT_WALL_MARKERS)
