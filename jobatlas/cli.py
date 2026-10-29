"""jobatlas CLI.

  jobatlas seed [--sources remoteintech,employers] [--file F.json ...] [--seedmap M.json ...]
  jobatlas discover "Company" ["Title"] [--website URL] [--json]   resolve one company to its boards
  jobatlas discover-batch [--budget N] [--workers N]              probe queued candidates
  jobatlas poll [--all] [--limit N] [--ats greenhouse,lever] [--detail-budget N]
  jobatlas rescore                                                re-run the filter on open jobs
  jobatlas status [--json]
  jobatlas export                                                 rewrite export/latest.json
  jobatlas top [--n 20] [--hours 72] [--fresh-days N] [--exclude-seen] [--json]
  jobatlas serve [--port 8804]
  jobatlas check "title" [--location L] [--text-file F]           score an ad-hoc posting (debug)
"""
import argparse
import concurrent.futures as cf
import json
import logging
import logging.handlers
import os
import sys
import threading

from . import config, db


def setup_logging(name="jobatlas", quiet=False):
    config.ensure_dirs()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(os.path.join(config.LOGS, name + ".log"), maxBytes=5_000_000, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    if not quiet:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def cmd_seed(a):
    from . import seed
    from .net import Client
    conn = db.connect()
    srcs = tuple(s.strip() for s in a.sources.split(",") if s.strip())
    st = seed.run(conn, Client(), sources=srcs, files=a.file or (), seedmaps=a.seedmap or ())
    print(json.dumps(st, indent=1))
    n = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    c = conn.execute("SELECT COUNT(*) FROM candidates WHERE probed_at IS NULL").fetchone()[0]
    print("registry: %d boards; %d candidate names waiting for discovery" % (n, c))


def cmd_discover(a):
    from . import discover
    from .net import Client
    conn = db.connect()
    out = discover.discover(conn, Client(), a.company, website=a.website or "", title=a.title or "", source="cli")
    if a.json:
        print(json.dumps(out, indent=1))
        return
    if not out["boards"]:
        print("no board found for %s%s" % (a.company, (" (blocked: %s)" % "; ".join(out["blocked"][:3])) if out["blocked"] else ""))
        for r in out["rejected"][:5]:
            print("  rejected", r)
    for b in out["boards"]:
        print("%s %s %s jobs=%d (%s)%s" % (b["ats"], b["token"], b["url"], b["jobs"], b["how"], " NEW" if b["new"] else ""))
        if b.get("match"):
            m = b["match"]
            print("   match: %s  %s  [%s]" % (m["title"], m["url"], m["similarity"]))


def cmd_discover_batch(a):
    from . import discover
    from .net import Client
    log = logging.getLogger("jobatlas.discover")
    cfg = config.cfg()
    conn = db.connect()
    budget = a.budget or cfg["discover"]["budget"]
    rows = conn.execute("SELECT * FROM candidates WHERE probed_at IS NULL ORDER BY priority, first_seen LIMIT ?", (budget,)).fetchall()
    client = Client(cfg, log=log.info)
    local = threading.local()
    found = {"companies": 0, "boards": 0, "by_ats": {}, "blocked": 0, "none": 0}
    lock = threading.Lock()

    def one(r):
        if not hasattr(local, "conn"):
            local.conn = db.connect()
        try:
            srcs = db.jload(r["sources"], ["?"]) or ["?"]
            out = discover.discover(local.conn, client, r["company"], website=r["website"] or "", source="batch:" + srcs[0],
                                    sources=",".join(srcs))
        except Exception as e:  # noqa: BLE001
            log.warning("discover %s failed: %s", r["company"], e)
            return
        with lock:
            if out["boards"]:
                found["companies"] += 1
                for b in out["boards"]:
                    if b["new"]:
                        found["boards"] += 1
                        found["by_ats"][b["ats"]] = found["by_ats"].get(b["ats"], 0) + 1
            elif out["blocked"] or out["walled"]:
                found["blocked"] += 1
            else:
                found["none"] += 1
    log.info("discover-batch: %d candidates", len(rows))
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, rows))
    found["probed"] = len(rows)
    found["http"] = client.stats
    conn.execute("INSERT INTO runs(kind, started, finished, boards, notes) VALUES ('discover', ?, ?, ?, ?)",
                 (db.now(), db.now(), found["boards"], db.jdump(found)))
    conn.commit()
    log.info("discover-batch done: %s", found)
    print(json.dumps(found, indent=1))


def cmd_poll(a):
    from . import poll
    st = poll.run(limit=a.limit, ats=a.ats.split(",") if a.ats else None, all_boards=a.all, workers=a.workers,
                  detail_budget=a.detail_budget)
    print(json.dumps(st, indent=1, default=str))


def cmd_details(a):
    from . import poll
    from .net import Client
    conn = db.connect()
    poll.ensure_columns(conn)
    c = Client(log=logging.getLogger("jobatlas.details").info)
    n = poll.backfill_details(conn, c, a.budget, a.workers)
    ids = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE prefilter='pass' AND closed_at IS NULL")]
    print(json.dumps({"details": n, "scored": poll.score_jobs(conn, ids=ids), "verified": poll.verify_urls(conn, c, a.verify),
                      "http": c.stats}, indent=1))


def cmd_rescore(a):
    """Re-run the prefilter and the scorer on open jobs (after a rule change).
    Jobs that newly pass the prefilter get their details on the next poll (backfill)."""
    from . import export, poll, score
    conn = db.connect()
    prof = score.profile_mod.load()
    changed = 0
    for r in conn.execute("SELECT id, title, location, workplace, prefilter FROM jobs WHERE closed_at IS NULL").fetchall():
        pf = score.prefilter(dict(r), prof)
        if pf != r["prefilter"]:
            conn.execute("UPDATE jobs SET prefilter=?, tier=CASE WHEN ?='pass' THEN tier ELSE NULL END WHERE id=?", (pf, pf, r["id"]))
            changed += 1
    conn.commit()
    print(json.dumps({"prefilter_changed": changed}))
    print(json.dumps(poll.score_jobs(conn), indent=1))
    print(json.dumps(export.write(conn)))


def cmd_status(a):
    from . import status
    s = status.collect(db.connect())
    print(json.dumps(s, indent=1) if a.json else status.render(s))


def cmd_export(a):
    from . import export
    print(json.dumps(export.write(db.connect())))


def cmd_top(a):
    from . import export
    conn = db.connect()
    rows = export.query(conn, hours=a.hours, min_tier=a.min_tier, include_seen=not a.exclude_seen, limit=a.n * 5,
                        fresh_days=a.fresh_days)
    out = [export.row_out(r) for r in rows][: a.n]
    if a.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return
    for i, r in enumerate(out, 1):
        print("%2d. [%s %d] %s | %s | %s | %s | %s" % (i, r["tier"], r["score"], r["company"], r["title"], r["location"][:40],
                                                      r["pay"][:40] or "pay not listed", r["url"]))
        print("      " + "; ".join(r["reasons"][:4]) + ((" | flags: " + "; ".join(r["flags"][:3])) if r["flags"] else ""))


def cmd_serve(a):
    from . import server
    server.serve(port=a.port)


def cmd_check(a):
    from . import score
    desc = open(a.text_file, encoding="utf8").read() if a.text_file else ""
    print(json.dumps(score.assess({"title": a.title, "location": a.location or "", "description": desc, "workplace": a.workplace or ""}), indent=1))


def main(argv=None):
    p = argparse.ArgumentParser(prog="jobatlas", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--sources", default="remoteintech,employers")
    s.add_argument("--file", action="append", help="JSON list of {company, url, apply_url}")
    s.add_argument("--seedmap", action="append", help="JSON {company: {ats: {slug, jobs_today}}} from a research probe")
    s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("discover")
    s.add_argument("company")
    s.add_argument("title", nargs="?")
    s.add_argument("--website")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_discover)
    s = sub.add_parser("discover-batch")
    s.add_argument("--budget", type=int)
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_discover_batch)
    s = sub.add_parser("poll")
    s.add_argument("--all", action="store_true", help="poll every active board, due or not")
    s.add_argument("--limit", type=int)
    s.add_argument("--ats")
    s.add_argument("--workers", type=int)
    s.add_argument("--detail-budget", type=int)
    s.set_defaults(fn=cmd_poll)
    s = sub.add_parser("details", help="fetch missing job details, rescore, verify match pages")
    s.add_argument("--budget", type=int, default=3000)
    s.add_argument("--workers", type=int, default=12)
    s.add_argument("--verify", type=int, default=400)
    s.set_defaults(fn=cmd_details)
    s = sub.add_parser("rescore")
    s.set_defaults(fn=cmd_rescore)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("export")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("top")
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--hours", type=int, default=72)
    s.add_argument("--fresh-days", type=int)
    s.add_argument("--min-tier", default="fair")
    s.add_argument("--exclude-seen", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_top)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("check")
    s.add_argument("title")
    s.add_argument("--location")
    s.add_argument("--workplace")
    s.add_argument("--text-file")
    s.set_defaults(fn=cmd_check)
    a = p.parse_args(argv)
    setup_logging("api" if a.cmd == "serve" else "jobatlas", quiet=a.cmd in ("status", "top", "check", "export"))
    if a.cmd in ("poll", "discover-batch", "details"):
        from .runlock import Busy, RunLock
        try:
            # a poll that finds another job running just skips (the next hourly run catches up);
            # discovery and details wait up to an hour for the poll to finish
            with RunLock(wait=0 if a.cmd == "poll" else 3600):
                a.fn(a)
        except Busy as e:
            logging.getLogger("jobatlas").info("%s skipped: %s", a.cmd, e)
            print("skipped: %s" % e)
        return
    a.fn(a)


if __name__ == "__main__":
    main()
