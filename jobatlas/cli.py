"""jobatlas CLI.

  jobatlas init [--force]                                         data dir + example profile and employer list
  jobatlas seed [--sources remoteintech,employers] [--file F.json ...] [--seedmap M.json ...]
  jobatlas discover "Company" ["Title"] [--website URL] [--json]   resolve one company to its boards
  jobatlas discover-batch [--budget N] [--workers N]              probe queued candidates
  jobatlas poll [--all] [--limit N] [--ats greenhouse,lever] [--detail-budget N]
  jobatlas watch                                                  poll continuously; alerts within minutes
  jobatlas rescore                                                re-run the filter on open jobs
  jobatlas status [--json]
  jobatlas profile check [--file F | --profile NAME]              validate profile.json (or an extra profile's file)
  jobatlas alerts test [--profile NAME]                           send a test message
  jobatlas alerts catchup [--profile NAME] --hours N [--dry-run]  send what alerts would have sent over the last N hours
  jobatlas export                                                 rewrite export/latest*.json
  jobatlas top [--profile NAME] [--n 20] [--hours 72] [--fresh-days N] [--exclude-seen] [--json]
  jobatlas duel run [--limit N] [--job ID] [--force]                judge jobs that fit two profiles (a model call each)
  jobatlas duel show [--n 20] [--json]                            recent head-to-head verdicts
  jobatlas duel prompt JOB_ID                                     what the model reads for one job

--profile picks one of config.json "profiles" (default: the main profile, profile.json).
  jobatlas serve [--port 8804]
  jobatlas check "title" [--location L] [--text-file F]           score an ad-hoc posting (debug)
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading

from . import config, db, profile


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


def cmd_init(a):
    """Create the data dir and copy in the example profile and employer list (existing files are kept)."""
    import shutil
    config.ensure_dirs()
    for src, dst in (("profile.example.json", config.PROFILE_FILE), ("employers.example.json", os.path.join(config.DATA, "employers.json"))):
        if os.path.exists(dst) and not a.force:
            print("kept   %s" % dst)
        else:
            shutil.copyfile(os.path.join(config.EXAMPLES, src), dst)
            print("wrote  %s" % dst)
    print("Edit the profile, then: jobatlas seed")


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
    client = Client(config.cfg(), log=logging.getLogger("jobatlas.discover").info)
    print(json.dumps(discover.batch(client, a.budget, a.workers), indent=1))


def cmd_poll(a):
    from . import poll
    st = poll.run(limit=a.limit, ats=a.ats.split(",") if a.ats else None, all_boards=a.all, workers=a.workers,
                  detail_budget=a.detail_budget)
    print(json.dumps(st, indent=1, default=str))


def cmd_watch(a):
    import signal
    from . import watch
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        watch.run(stop)
    except KeyboardInterrupt:
        pass


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
    profs = [s.prof for s in profile.seekers()]
    changed = 0
    for r in conn.execute("SELECT id, title, location, workplace, prefilter FROM jobs WHERE closed_at IS NULL").fetchall():
        pf = score.prefilter_all(dict(r), profs)
        if pf != r["prefilter"]:
            conn.execute("UPDATE jobs SET prefilter=?, tier=CASE WHEN ?='pass' THEN tier ELSE NULL END WHERE id=?", (pf, pf, r["id"]))
            if pf != "pass":    # out for everyone: drop the extra profiles' old scores and routing too
                conn.execute("DELETE FROM profile_scores WHERE job_id=?", (r["id"],))
                conn.execute("UPDATE jobs SET routed_to=NULL WHERE id=?", (r["id"],))
            changed += 1
            if changed % 200 == 0:
                conn.commit()
    conn.commit()
    print(json.dumps({"prefilter_changed": changed}))
    print(json.dumps(poll.score_jobs(conn), indent=1))
    print(json.dumps(export.write(conn)))


def cmd_status(a):
    from . import status
    s = status.collect(db.connect())
    print(json.dumps(s, indent=1) if a.json else status.render(s))


def cmd_profile_check(a):
    if a.profile and a.profile != config.MAIN:
        e = (config.cfg().get("profiles") or {}).get(a.profile)
        if e is None:
            sys.exit("no profile named %r in config.json profiles" % a.profile)
        path = profile.extra_path(e)
    else:
        path = a.file or config.PROFILE_FILE
    if not os.path.exists(path):
        sys.exit("no profile at %s (the example is used): run `jobatlas init`" % path)
    problems = profile.check_file(path)
    for p in problems:
        print("  " + p)
    print("%s: %s" % (path, "%d problem(s)" % len(problems) if problems else "ok"))
    if problems:
        sys.exit(1)


def seeker_or_exit(name):
    try:
        return profile.seeker(name)
    except ValueError as e:
        sys.exit(str(e))


def cmd_alerts_test(a):
    from . import alerts
    cfg = seeker_or_exit(a.profile).alerts
    ok, info = alerts.send(a.message or "JobAtlas test alert: alerts reach you here.", cfg)
    print("%s via %s: %s" % ("sent" if ok else "FAILED", cfg.get("channel") or "discord_webhook", info))
    if not ok:
        sys.exit(1)


def cmd_alerts_catchup(a):
    from . import alerts
    s = seeker_or_exit(a.profile)
    try:
        out = alerts.catchup(db.connect(), s.name, s.alerts, a.hours, dry_run=a.dry_run)
    except ValueError as e:
        sys.exit(str(e))
    print(json.dumps(out))
    if out["failed"]:
        sys.exit(1)


def cmd_export(a):
    from . import export
    print(json.dumps(export.write(db.connect())))


def cmd_top(a):
    from . import export
    conn = db.connect()
    seeker_or_exit(a.profile)
    rows = export.query(conn, hours=a.hours, min_tier=a.min_tier, include_seen=not a.exclude_seen, limit=a.n * 5,
                        fresh_days=a.fresh_days, profile_name=a.profile)
    out = export.rows_out(conn, rows[: a.n], a.profile)
    if a.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return
    for i, r in enumerate(out, 1):
        print("%2d. [%s %d] %s | %s | %s | %s | %s" % (i, r["tier"], r["score"], r["company"], r["title"], r["location"][:40],
                                                      r["pay"][:40] or "pay not listed", r["url"]))
        print("      " + "; ".join(r["reasons"][:4]) + ((" | flags: " + "; ".join(r["flags"][:3])) if r["flags"] else ""))
        if r.get("duel"):
            print("      duel: " + r["duel"]["note"])


def cmd_duel_run(a):
    from . import duel
    print(json.dumps(duel.run(db.connect(), limit=a.limit, ids=a.job or None, force=a.force)))


def cmd_duel_show(a):
    """Verdicts, newest first, with where the tier rule had the job and where it is routed now."""
    from . import duel
    conn = db.connect()
    pr = duel.pair()
    if not pr:
        sys.exit("no partner profile: the duel needs config.json profiles")
    lab = duel.labels()
    rows = conn.execute("SELECT d.*, j.company, j.title, j.tier m_tier, ps.tier o_tier, j.routed_to FROM duels d JOIN jobs j ON j.id=d.job_id"
                        " LEFT JOIN profile_scores ps ON ps.job_id=d.job_id AND ps.profile=d.other WHERE d.other=?"
                        " ORDER BY d.created DESC LIMIT ?", (pr[1].name, a.n)).fetchall()
    if a.json:
        print(json.dumps([dict(r) for r in rows], indent=1, ensure_ascii=False, default=str))
        return
    print(json.dumps(duel.summary(conn)))
    for r in rows:
        if r["status"] != "ok":
            print("#%d %s | %s: %s" % (r["job_id"], r["company"], r["title"], r["status"]))
            continue
        print("#%d %s | %s  [tiers %s/%s]" % (r["job_id"], r["company"], r["title"], r["m_tier"], r["o_tier"]))
        print("    %s %.2f %s| %s %.2f %s-> %s (%s); was %s, now %s" % (
            lab.get(config.MAIN), r["p_main"], ("gates %s " % json.loads(r["gates_main"])) if json.loads(r["gates_main"] or "[]") else "",
            lab.get(r["other"]), r["p_other"], ("gates %s " % json.loads(r["gates_other"])) if json.loads(r["gates_other"] or "[]") else "",
            lab.get(r["winner"], r["winner"]), r["why"], lab.get(r["tier_route"], r["tier_route"]),
            lab.get(r["routed_to"] or config.MAIN, r["routed_to"] or config.MAIN)))
        print("    %s (%s): %s" % (lab.get(config.MAIN), r["resume_main"], r["reason_main"]))
        print("    %s (%s): %s" % (lab.get(r["other"]), r["resume_other"], r["reason_other"]))


def cmd_duel_prompt(a):
    """Print what the model would read for one job (no call)."""
    from . import duel
    conn = db.connect()
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (a.job_id,)).fetchone()
    pr = duel.pair()
    if not r or not pr:
        sys.exit("no such job" if not r else "no partner profile")
    try:
        cm, co = duel.materials(pr[0].name), duel.materials(pr[1].name)
    except duel.DuelError as e:
        sys.exit(str(e))
    order = [cm, co] if r["id"] % 2 == 0 else [co, cm]
    print(duel.build_prompt(r, order))


def cmd_serve(a):
    from . import server
    server.serve(port=a.port)


def cmd_check(a):
    from . import score
    desc = open(a.text_file, encoding="utf8").read() if a.text_file else ""
    print(json.dumps(score.assess({"title": a.title, "location": a.location or "", "description": desc, "workplace": a.workplace or ""},
                                  seeker_or_exit(a.profile).prof), indent=1))


def main(argv=None):
    p = argparse.ArgumentParser(prog="jobatlas", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="create the data dir with the example profile and employer list")
    s.add_argument("--force", action="store_true", help="overwrite existing files")
    s.set_defaults(fn=cmd_init)
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
    s = sub.add_parser("watch", help="poll continuously and alert within minutes (run as a service)")
    s.set_defaults(fn=cmd_watch)
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
    s = sub.add_parser("profile", help="profile tools")
    c = s.add_subparsers(dest="profile_cmd", required=True).add_parser("check", help="validate profile.json")
    c.add_argument("--file", help="default: <data>/profile.json")
    c.add_argument("--profile", help="an extra profile from config.json profiles")
    c.set_defaults(fn=cmd_profile_check)
    s = sub.add_parser("alerts", help="alert tools")
    asub = s.add_subparsers(dest="alerts_cmd", required=True)
    c = asub.add_parser("test", help="send a test message (even with alerts off)")
    c.add_argument("--profile", help="use this profile's alert settings (default: main)")
    c.add_argument("--message", help="text to send instead of the stock test line")
    c.set_defaults(fn=cmd_alerts_test)
    c = asub.add_parser("catchup", help="send what alerts would have sent over the last N hours (e.g. right after turning them on)")
    c.add_argument("--profile", help="default: main")
    c.add_argument("--hours", type=int, required=True)
    c.add_argument("--dry-run", action="store_true", help="only count")
    c.set_defaults(fn=cmd_alerts_catchup)
    s = sub.add_parser("duel", help="head-to-head: who of two profiles should get a job that fits both")
    dsub = s.add_subparsers(dest="duel_cmd", required=True)
    c = dsub.add_parser("run", help="judge contested jobs (model calls), then route by the verdicts if duel.enabled")
    c.add_argument("--limit", type=int, help="model calls this run (default duel.per_run)")
    c.add_argument("--job", type=int, action="append", help="only this job id (repeatable); ignores duel.days")
    c.add_argument("--force", action="store_true", help="judge again even if the inputs haven't changed")
    c.set_defaults(fn=cmd_duel_run)
    c = dsub.add_parser("show", help="recent verdicts")
    c.add_argument("--n", type=int, default=20)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_duel_show)
    c = dsub.add_parser("prompt", help="print the model's input for one job")
    c.add_argument("job_id", type=int)
    c.set_defaults(fn=cmd_duel_prompt)
    s = sub.add_parser("export")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("top")
    s.add_argument("--profile", help="an extra profile from config.json profiles (default: main)")
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--hours", type=int, default=72)
    s.add_argument("--fresh-days", type=int)
    s.add_argument("--min-tier", default="fair")
    s.add_argument("--exclude-seen", "--exclude-shown", action="store_true", help="drop jobs in the ledger")
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
    s.add_argument("--profile")
    s.set_defaults(fn=cmd_check)
    a = p.parse_args(argv)
    setup_logging("api" if a.cmd == "serve" else "duel" if a.cmd == "duel" else "jobatlas",
                  quiet=a.cmd in ("init", "status", "profile", "alerts", "top", "check", "export") or (a.cmd == "duel" and a.duel_cmd != "run"))
    try:
        dispatch(a)
    except profile.ProfileError as e:
        sys.exit(str(e))


def dispatch(a):
    if a.cmd in ("poll", "discover-batch", "details", "watch"):
        from .runlock import Busy, RunLock
        try:
            # a poll that finds another job running just skips (the next hourly run catches up);
            # the rest wait up to an hour. A running `watch` holds the lock for good.
            with RunLock(wait=0 if a.cmd == "poll" else 3600):
                a.fn(a)
        except Busy as e:
            logging.getLogger("jobatlas").info("%s skipped: %s", a.cmd, e)
            print("skipped: %s" % e)
        return
    a.fn(a)


if __name__ == "__main__":
    main()
