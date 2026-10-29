"""Seed the registry. Every source is read-only.

  remoteintech  the remoteintech/remote-jobs company list (an ISC-licensed GitHub repo)
  employers     a curated JSON list of employers you care about (<data>/employers.json,
                format in examples/employers.example.json): boards you already know,
                and names (+ websites) for discovery to resolve
  --file F      any JSON list of {company, url, apply_url} rows (an application tracker
                export, a saved search); boards in the URLs are added, names are queued
  --seedmap M   {company: {ats: {slug, jobs_today}}} from a research probe

Sources whose terms forbid scraping are not used: YC's directory and Work at a
Startup (YC's terms ban "data mining, robots, scraping or similar data gathering";
WaaS needs a login), and job aggregators whose terms ban harvesting.
"""
import io
import json
import os
import re
import tarfile

from . import boards as boards_mod, config, db, registry

def _read_json(path, default=None):
    try:
        with open(path, encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _add_urls(conn, company, urls, source, stats):
    for u in urls:
        for b in boards_mod.boards_in(u or ""):
            _, created = registry.add_board(conn, company or b["token"], b["ats"], b["token"], source)
            stats["boards_new" if created else "boards_known"] += 1


def from_remoteintech(conn, client, stats):
    r = client.get("https://codeload.github.com/remoteintech/remote-jobs/tar.gz/refs/heads/main", accept="*/*", timeout=120)
    if not r.ok:
        stats["notes"].append("remoteintech: HTTP %s %s" % (r.status, r.error[:60]))
        return
    n = 0
    with tarfile.open(fileobj=io.BytesIO(r.body), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not re.search(r"/src/companies/[^/]+\.md$", m.name):
                continue
            text = tf.extractfile(m).read().decode("utf-8", "replace")
            fm = re.match(r"---\n(.*?)\n---", text, re.S)
            if not fm:
                continue
            meta = dict(re.findall(r"^(\w+):\s*\"?(.*?)\"?\s*$", fm.group(1), re.M))
            name = meta.get("title") or ""
            if not name:
                continue
            region = (meta.get("region") or "").lower()
            careers = meta.get("careers_url") or ""
            _add_urls(conn, name, [careers], "remoteintech", stats)
            pri = 3 if any(x in region for x in ("worldwide", "us", "america", "usa")) or not region else 6
            if registry.add_candidate(conn, name, "remoteintech", website=careers or meta.get("website") or "", priority=pri):
                stats["candidates_new"] += 1
            n += 1
    stats["notes"].append("remoteintech: %d companies" % n)


def from_employers(conn, stats, path=None):
    path = path or os.path.join(config.DATA, "employers.json")
    d = _read_json(path, {}) or {}
    if not d:
        stats["notes"].append("employers: %s missing (see examples/employers.example.json)" % path)
        return
    for e in d.get("employers") or []:
        for b in e.get("boards") or []:
            _, created = registry.add_board(conn, e["company"], b["ats"], b["token"], "curated", notes=e.get("note", ""))
            stats["boards_new" if created else "boards_known"] += 1
        if not e.get("boards"):
            if registry.add_candidate(conn, e["company"], "curated", website=e.get("website", ""), priority=1):
                stats["candidates_new"] += 1


ROBOTS_BLOCKED_ATS = {"pinpoint", "smartrecruiters"}


def from_seedmap(conn, client, stats, path):
    """{company: {ats: {"slug", "jobs_today", "note"}}} from a research probe.

    BambooHR hits are validated here (unknown subdomains can answer with another
    tenant's jobs): the board's careers page title must name the company.
    Pinpoint/SmartRecruiters boards are recorded inactive (their robots.txt
    disallows crawlers). Lever entries for companies that also have a
    Greenhouse/Ashby board are kept but marked, since Lever's API can be stale.
    """
    from . import names
    d = _read_json(path, {}) or {}
    for company, boards in d.items():
        if not isinstance(boards, dict):
            continue
        for ats, b in boards.items():
            if ats == "own" or not isinstance(b, dict) or not b.get("slug"):
                continue
            slug = b["slug"]
            if ats == "bamboohr":
                if not b.get("jobs_today"):
                    stats.setdefault("bamboohr_skipped", 0)
                    stats["bamboohr_skipped"] += 1
                    continue
                r = client.get("https://%s.bamboohr.com/careers" % slug, accept="text/html,*/*")
                t = re.search(r"<title>(.*?)</title>", r.text or "", re.S)
                if not (t and names.mentions(company, t.group(1))):
                    stats.setdefault("bamboohr_rejected", 0)
                    stats["bamboohr_rejected"] += 1
                    continue
            active = 0 if ats in ROBOTS_BLOCKED_ATS else 1
            note = "robots.txt disallows" if not active else (b.get("note") or "")
            if ats == "lever" and ("greenhouse" in boards or "ashby" in boards):
                note = (note + "; also on greenhouse/ashby (Lever API may be stale)").strip("; ")
            _, created = registry.add_board(conn, company, ats, slug, "research-seed", notes=note, active=active)
            stats["boards_new" if created else "boards_known"] += 1


def from_files(conn, stats, paths):
    for p in paths:
        d = _read_json(p)
        rows = d if isinstance(d, list) else (d or {}).get("jobs") or (d or {}).get("rows") or []
        for r in rows:
            if isinstance(r, dict) and r.get("company"):
                _add_urls(conn, r["company"], [r.get("url"), r.get("apply_url")], "files", stats)
                registry.add_candidate(conn, r["company"], "files", priority=2)


def run(conn, client, sources=("remoteintech", "employers"), files=(), seedmaps=()):
    stats = {"boards_new": 0, "boards_known": 0, "candidates_new": 0, "notes": []}
    for p in seedmaps or ():
        from_seedmap(conn, client, stats, p)
        conn.commit()
    if "remoteintech" in sources:
        from_remoteintech(conn, client, stats)
        conn.commit()
    if "employers" in sources:
        from_employers(conn, stats)
        conn.commit()
    if files:
        from_files(conn, stats, files)
        conn.commit()
    # a company that already has a board doesn't need probing
    conn.execute("UPDATE candidates SET result=COALESCE(result,'has-board'), probed_at=COALESCE(probed_at, ?) "
                 "WHERE norm IN (SELECT norm FROM companies)", (db.now(),))
    conn.commit()
    return stats
