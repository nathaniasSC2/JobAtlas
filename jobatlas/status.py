"""`jobatlas status`: boards by ATS, failures, jobs seen, new today."""
import datetime
import os

from . import config, ledger, profile


def collect(conn):
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    by_ats = []
    for r in conn.execute("SELECT ats, COUNT(*) n, SUM(active) active, SUM(CASE WHEN last_ok IS NOT NULL THEN 1 ELSE 0 END) ok_ever,"
                          " SUM(CASE WHEN failures>0 THEN 1 ELSE 0 END) failing, SUM(jobs_open) jobs_open FROM companies GROUP BY ats ORDER BY n DESC"):
        by_ats.append(dict(r))
    j = conn.execute("SELECT COUNT(*) total, SUM(CASE WHEN closed_at IS NULL THEN 1 ELSE 0 END) open,"
                     " SUM(CASE WHEN first_seen>=? AND baseline=0 THEN 1 ELSE 0 END) new_today,"
                     " SUM(CASE WHEN first_seen>=? THEN 1 ELSE 0 END) first_seen_today,"
                     " SUM(CASE WHEN closed_at IS NULL AND tier IN ('strong','good','fair') THEN 1 ELSE 0 END) open_matches,"
                     " SUM(CASE WHEN closed_at IS NULL AND tier='strong' THEN 1 ELSE 0 END) open_strong,"
                     " SUM(CASE WHEN first_seen>=? AND baseline=0 AND tier IN ('strong','good','fair') THEN 1 ELSE 0 END) new_matches_today"
                     " FROM jobs", (today, today, today)).fetchone()
    tiers = {r["t"]: r["n"] for r in conn.execute(
        "SELECT CASE WHEN prefilter<>'pass' THEN 'dropped by title/location prefilter' ELSE COALESCE(tier, 'not scored yet') END t,"
        " COUNT(*) n FROM jobs WHERE closed_at IS NULL GROUP BY t")}
    fails = [dict(r) for r in conn.execute(
        "SELECT company, ats, token, failures, last_error, active FROM companies WHERE failures>0 OR (last_error IS NOT NULL AND last_error<>'')"
        " ORDER BY failures DESC, company LIMIT 25")]
    cand = conn.execute("SELECT COUNT(*) n, SUM(CASE WHEN probed_at IS NULL THEN 1 ELSE 0 END) unprobed,"
                        " SUM(CASE WHEN result LIKE 'found%' THEN 1 ELSE 0 END) found FROM candidates").fetchone()
    runs = [dict(r) for r in conn.execute("SELECT kind, started, finished, boards, ok, failed, not_modified, jobs_seen, new_jobs, closed, new_matches"
                                          " FROM runs ORDER BY id DESC LIMIT 5")]
    p = profile.load()
    alerts_cfg = config.cfg()["alerts"]
    return {"boards_by_ats": by_ats, "boards_total": sum(r["n"] for r in by_ats),
            "boards_active": sum(r["active"] or 0 for r in by_ats), "jobs": dict(j), "open_by_tier": tiers,
            "failing_boards": fails, "candidates": dict(cand), "recent_runs": runs,
            "profile": {"file": p.src or "NONE (no profile found)", "example": p.src.endswith("profile.example.json"), "hash": p.hash,
                        "fields": [f["name"] for f in p.fields], "home_state": p.home_state,
                        "floor": [p.floor_year, p.floor_hour], "minimum": [p.min_year, p.min_hour],
                        "problems": profile.check_file(config.PROFILE_FILE) if os.path.exists(config.PROFILE_FILE) else []},
            "ledger": ledger.source(), "alerts_enabled": bool(alerts_cfg.get("enabled"))}


def render(s):
    out = ["JobAtlas status", ""]
    out.append("Boards: %d (%d active)" % (s["boards_total"], s["boards_active"]))
    out.append("  %-16s %6s %6s %6s %7s %9s" % ("ats", "boards", "active", "ok", "failing", "jobs_open"))
    for r in s["boards_by_ats"]:
        out.append("  %-16s %6d %6d %6d %7d %9d" % (r["ats"], r["n"], r["active"] or 0, r["ok_ever"] or 0, r["failing"] or 0, r["jobs_open"] or 0))
    j = s["jobs"]
    out.append("")
    out.append("Jobs: %s seen, %s open; first seen today %s (new on known boards %s)" %
               (j["total"], j["open"], j["first_seen_today"], j["new_today"]))
    out.append("Matches open: %s (strong %s); new matches today: %s" % (j["open_matches"], j["open_strong"], j["new_matches_today"]))
    out.append("Open by tier: " + ", ".join("%s %d" % (k, v) for k, v in sorted(s["open_by_tier"].items(), key=lambda kv: -kv[1])))
    c = s["candidates"]
    out.append("Discovery queue: %s names, %s not probed yet, %s found boards" % (c["n"], c["unprobed"], c["found"]))
    out.append("")
    pr = s["profile"]
    out.append("Profile: %s%s (hash %s); fields %s; home %s; floor $%d/$%.2fhr, minimum $%d/$%.2fhr" %
               (pr["file"], " [THE EXAMPLE: run `jobatlas init` and edit <data>/profile.json]" if pr["example"] else "", pr["hash"],
                ", ".join(pr["fields"]), pr["home_state"] or "-", pr["floor"][0], pr["floor"][1], pr["minimum"][0], pr["minimum"][1]))
    if pr["problems"]:
        out.append("  %d profile problem(s), see `jobatlas profile check`: %s" % (len(pr["problems"]), pr["problems"][0]))
    out.append("Ledger: %s" % s["ledger"])
    out.append("Alerts: %s" % ("ENABLED" if s["alerts_enabled"] else "disabled (nothing is sent)"))
    if s["failing_boards"]:
        out.append("")
        out.append("Failing / blocked boards (top 25):")
        for f in s["failing_boards"]:
            out.append("  %-28s %-12s %-30s fails=%d %s%s" % ((f["company"] or "")[:28], f["ats"], f["token"][:30], f["failures"] or 0,
                                                            (f["last_error"] or "")[:60], "" if f["active"] else " [inactive]"))
    out.append("")
    out.append("Recent runs:")
    for r in s["recent_runs"]:
        out.append("  %s %s -> %s boards=%s ok=%s failed=%s 304=%s seen=%s new=%s closed=%s new_matches=%s" %
                   (r["kind"], r["started"], r["finished"], r["boards"], r["ok"], r["failed"], r["not_modified"], r["jobs_seen"],
                    r["new_jobs"], r["closed"], r["new_matches"]))
    return "\n".join(out)
