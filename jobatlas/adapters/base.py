"""Shared adapter types and helpers.

Every adapter module exposes:
    ATS            short name ("greenhouse")
    fetch(client, token, etag=None, last_modified=None) -> Result
    detail(client, token, job) -> dict of fields to merge into the job (or {} )
    public_url(token) -> str
    PARTIAL        True when fetch() only sees part of the board (Workday search),
                   so a job missing from one poll is not proof that it closed
Jobs are plain dicts (see new_job()).
"""
import datetime
import html as htmlmod
import re
import xml.etree.ElementTree as ET


class Result:
    __slots__ = ("status", "jobs", "name", "url", "etag", "last_modified", "error")

    def __init__(self, status, jobs=None, name="", url="", etag=None, last_modified=None, error=""):
        # status: ok | not_modified | none (no such board) | blocked (robots/bot wall/rate limit) | error
        self.status, self.jobs, self.name, self.url = status, jobs or [], name, url
        self.etag, self.last_modified, self.error = etag, last_modified, error

    def __repr__(self):
        return "<Result %s jobs=%d name=%r %s>" % (self.status, len(self.jobs), self.name, self.error[:60])


def new_job(ext_id, title, url, **kw):
    j = {"ext_id": str(ext_id), "title": (title or "").strip(), "url": url or "", "apply_url": "", "location": "",
         "workplace": "", "department": "", "employment_type": "", "posted_at": "", "description": "",
         "pay_text": "", "questions": [], "raw": {}, "needs_detail": False, "company": ""}
    j.update({k: v for k, v in kw.items() if v is not None})
    return j


def from_response(r, empty_is_none=False):
    """Map a failed Response to a Result, or None when the caller should parse r."""
    if r.status == 304:
        return Result("not_modified", etag=r.header("ETag"), last_modified=r.header("Last-Modified"))
    if r.error and "robots" in r.error:
        return Result("blocked", error="robots.txt disallows")
    if r.status == 429 or (r.error and "rate limited" in r.error):
        return Result("blocked", error="rate limited")
    if r.status in (404, 410):
        return Result("none", error="HTTP %d" % r.status)
    if r.status in (401, 403):
        return Result("blocked", error="HTTP %d" % r.status)
    if not r.ok:
        return Result("error", error=("HTTP %d " % r.status if r.status else "") + (r.error or "")[:120])
    return None


_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article)\b[^>]*>", re.I)


def html_text(s):
    """HTML (possibly entity-escaped twice, as Greenhouse does) -> readable plain text."""
    if not s:
        return ""
    s = str(s)
    if "&lt;" in s and "<" not in s[:200]:
        s = htmlmod.unescape(s)
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<li\b[^>]*>", "\n- ", s)
    s = _BLOCK.sub("\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmlmod.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", s)
    return "\n".join(ln.strip() for ln in s.split("\n")).strip()


def iso_date(v):
    """Many date shapes -> 'YYYY-MM-DD' (or '' when unknown)."""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e11 else v
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()
    s = str(v).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S Z", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return relative_date(s)


def relative_date(s, today=None):
    """Workday's 'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    today = today or datetime.date.today()
    t = (s or "").lower()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - datetime.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*days?\s*ago", t)
    if m:
        return (today - datetime.timedelta(days=int(m.group(1)))).isoformat()
    return ""


def money(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    return "${:,.0f}".format(n) if n >= 1000 else "${:,.2f}".format(n)


def pay_range_text(lo, hi, currency="USD", period=""):
    if lo is None and hi is None:
        return ""
    cur = "" if (currency or "USD").upper() == "USD" else (currency.upper() + " ")
    per = {"year": "/yr", "yearly": "/yr", "annual": "/yr", "annually": "/yr", "per-year-salary": "/yr", "hour": "/hr",
           "hourly": "/hr", "per-hour-wage": "/hr", "month": "/mo", "monthly": "/mo"}.get((period or "").lower(), "")
    if lo and hi and lo != hi:
        return "%s%s - %s%s" % (cur, money(lo), money(hi), per)
    return "%s%s%s" % (cur, money(hi or lo), per)


def rss_items(text):
    """Parse RSS/Atom into [{tag: text}] with namespaces stripped. Tolerant of junk."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {}
        for ch in it:
            # namespaced extension tags (job:description, tt:locations) get an "x:" prefix so they
            # never collide with the plain RSS element of the same name
            k = ("x:" if ch.tag.startswith("{") and "w3.org/2005/Atom" not in ch.tag else "") + ch.tag.split("}")[-1]
            v = (ch.text or "").strip()
            if k == "link" and not v:
                v = ch.attrib.get("href", "")
            if k in d and v:
                d[k] = d[k] + " | " + v
            elif v or k not in d:
                d[k] = v
            for sub in ch:  # e.g. <tt:locations><tt:location><tt:city>
                sk = sub.tag.split("}")[-1]
                sv = " ".join(x.strip() for x in sub.itertext() if x.strip())
                if sv:
                    d.setdefault(k + "." + sk, sv)
        out.append(d)
    return out


REMOTE_NEG = re.compile(r"(?:not (?:be )?(?:eligible|available|approved) for (?:full[- ]time |any )?(?:remote|telework)(?: work)?|(?:remote|telework)(?: work)? (?:is|are) not (?:available|an option|permitted|offered|eligible)|no (?:remote|telework)(?: work)?(?: options?)?\b|not a remote (?:role|position|job)|is not remote|cannot be (?:performed|done|worked) remotely|not (?:a )?(?:remote|telework)[- ]eligible)", re.I)


def workplace_of(*texts):
    t = " ".join(str(x or "") for x in texts).lower()
    if REMOTE_NEG.search(t):
        t = REMOTE_NEG.sub(" ", t)
        if not re.search(r"\bhybrid\b|\bremote\b|work from home|telecommut|\bwfh\b|anywhere", t):
            return "onsite"
    if re.search(r"\bhybrid\b", t):
        return "hybrid"
    if re.search(r"\bremote\b|work from home|telecommut|\bwfh\b|anywhere", t):
        return "remote"
    if re.search(r"on-?site|in[- ]office|in person", t):
        return "onsite"
    return ""
