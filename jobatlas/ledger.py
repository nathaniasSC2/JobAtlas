"""Optional "already seen" ledger, so exports can drop jobs you've already looked at.

ledger.path (config.json) names a JSONL file that another tool writes: an
application tracker, a spreadsheet export, a digest that remembers what it
sent. Each line is {"company", "title", "url", "alt_urls"?, "feed"?, "date"?}.
JobAtlas only reads it. A job matches by URL (normalised, keeping job-id query
parameters) or by company + the same set of title words.
"""
import json
import re
import urllib.parse

from . import config, names

# Query parameters that identify a posting (dropping them would merge different jobs)
JOB_ID_PARAMS = {"gh_jid", "jobid", "job_id", "jobopeningid", "id", "jid", "requisitionid", "lever-source"}


def norm_url(u):
    p = urllib.parse.urlsplit((u or "").strip())
    host = re.sub(r"^www\.", "", p.netloc.lower())
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k.lower() in JOB_ID_PARAMS]
    q = ("?" + urllib.parse.urlencode(sorted(keep))) if keep else ""
    return host + p.path.rstrip("/").lower() + q


def title_words(t):
    return " ".join(sorted(set(re.findall(r"[a-z0-9]+", (t or "").lower())) - {"remote", "us", "usa", "hybrid"}))


class Ledger:
    def __init__(self, path=""):
        self.path = path
        self.urls, self.fp = {}, {}
        self.entries = 0
        if not path:
            return
        try:
            with open(path, encoding="utf8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    self.entries += 1
                    for u in [e.get("url")] + list(e.get("alt_urls") or []):
                        if u:
                            self.urls[norm_url(u)] = e
                    self.fp[(names.norm_company(e.get("company")), title_words(e.get("title")))] = e
        except OSError:
            pass

    def match(self, company, title, urls=()):
        """-> (entry, how) or (None, None)."""
        for u in urls:
            if u and norm_url(u) in self.urls:
                return self.urls[norm_url(u)], "url"
        e = self.fp.get((names.norm_company(company), title_words(title)))
        return (e, "company+title") if e else (None, None)


_LEDGER = None


def ledger():
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = Ledger((config.cfg().get("ledger") or {}).get("path") or "")
    return _LEDGER


def source():
    L = ledger()
    return "%s (%d entries)" % (L.path, L.entries) if L.path else "none configured"


def seen(company, title, urls):
    """-> note like "digest 2026-10-20 (url)" or None."""
    e, how = ledger().match(company or "", title or "", [u for u in urls if u])
    if not e:
        return None
    return ("%s %s (%s)" % (e.get("feed", "seen"), (e.get("date") or "")[:10], how)).replace("  ", " ")
