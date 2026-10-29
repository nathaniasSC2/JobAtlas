"""Exports for whatever consumes the matches (a digest, an automation, a tracker).

<data>/export/latest.json: open jobs that passed the profile's filters and are
NEW in the last 72 h (first seen in that window on an already-known board, or
on a board's baseline poll when the posting's own date is within the window),
best first. Rows already in the optional ledger are kept but marked
`already_seen` so consumers can drop them.
"""
import datetime
import json
import os

from . import config, db, score as score_mod

FIELDS = ("id", "company", "title", "url", "apply_url", "ats", "location", "workplace", "posted_at", "first_seen", "score",
          "tier", "field", "department", "employment_type")


def row_out(r, desc_chars=0):
    facts = db.jload(r["facts"], {}) or {}
    d = {k: r[k] for k in FIELDS}
    d["direct_url"] = r["url"]
    d["pay"] = r["pay_text"] or facts.get("pay_text") or ""
    d["pay_top"] = facts.get("pay_top")
    d["pay_kind"] = facts.get("pay_kind")
    d["reasons"] = db.jload(r["reasons"], [])
    d["flags"] = db.jload(r["flags"], [])
    d["location_class"] = facts.get("location_class")
    d["home_state_remote"] = facts.get("location_class") == "home_state_remote"
    d["weekend"] = facts.get("weekend")
    d["health_insurance_stated"] = facts.get("health")
    d["years_asked"] = facts.get("years")
    d["questions"] = db.jload(r["questions"], [])
    d["already_seen"] = bool(r["seen"])
    d["seen_note"] = r["seen"] or ""
    d["baseline"] = bool(r["baseline"])
    if desc_chars:
        d["description"] = (r["description"] or "")[:desc_chars]
    return d


def query(conn, hours=None, min_tier=None, include_seen=True, limit=500, fresh_days=None, q=None):
    cfg = config.cfg()["export"]
    hours = cfg["hours"] if hours is None else hours
    min_tier = min_tier or cfg["min_tier"]
    tiers = [t for t in score_mod.TIERS if score_mod.TIER_RANK[t] <= score_mod.TIER_RANK.get(min_tier, 2) and t != "skip"]
    since_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    since = since_dt.replace(microsecond=0).isoformat()
    since_day = since_dt.date().isoformat()
    sql = ("SELECT * FROM jobs WHERE closed_at IS NULL AND tier IN (%s) AND first_seen>=? AND "
           "(baseline=0 OR (posted_at<>'' AND posted_at>=?))" % ",".join("?" * len(tiers)))
    args = tiers + [since, (datetime.date.today() - datetime.timedelta(days=fresh_days)).isoformat() if fresh_days else since_day]
    if not include_seen:
        sql += " AND (seen IS NULL OR seen='')"
    if q:
        sql += " AND (title LIKE ? OR company LIKE ?)"
        args += ["%" + q + "%", "%" + q + "%"]
    sql += " ORDER BY score DESC, posted_at DESC LIMIT %d" % int(limit)
    return conn.execute(sql, args).fetchall()


def write(conn, path=None):
    path = path or os.path.join(config.EXPORT, "latest.json")
    rows = query(conn)
    out = {"generated": db.now(), "window_hours": config.cfg()["export"]["hours"], "min_tier": config.cfg()["export"]["min_tier"],
           "count": len(rows), "not_yet_seen": sum(1 for r in rows if not r["seen"]),
           "jobs": [row_out(r) for r in rows]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return {"path": path, "count": len(rows)}
