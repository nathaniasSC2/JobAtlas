"""`jobatlas watch`: poll continuously, so a new job is scored and alerted within minutes.

Every watch.tick_seconds it polls the boards that are due (poll.board_done sets each
board's interval), scores what's new and sends alerts. One HTTP client lives for the
whole process, so per-host spacing, robots.txt and 429 back-offs carry across cycles.
Once a day at watch.discover_hour it re-seeds and runs discovery in a thread that
shares that client, so the two never double the rate to a host. Run it as a service
in place of the poll and discovery timers.
"""
import datetime
import logging
import threading

from . import config, db, discover, poll, seed
from .net import Client

log = logging.getLogger("jobatlas.watch")


def due_count(conn):
    return conn.execute("SELECT COUNT(*) FROM companies WHERE active=1 AND (next_due IS NULL OR next_due <= ?)",
                        (poll._iso(poll._utcnow()),)).fetchone()[0]


def nightly(client, cfg):
    try:
        conn = db.connect()
        log.info("seed: %s", seed.run(conn, client, sources=tuple(cfg["watch"]["seed_sources"])))
        discover.batch(client, cfg["discover"]["budget"])
    except Exception:  # noqa: BLE001
        log.exception("nightly discovery failed")


def run(stop=None, cycles=None):
    """Loop until `stop` (a threading.Event) is set, or for `cycles` ticks (tests)."""
    cfg = config.cfg()
    w = cfg["watch"]
    stop = stop or threading.Event()
    client = Client(cfg, log=log.info)
    conn = db.connect()
    poll.ensure_columns(conn)
    disc, disc_day, n = None, None, 0
    log.info("watch: started")
    while not stop.is_set():
        if due_count(conn):
            st = poll.run(conn, client=client, verify_budget=w["verify_budget"], quiet=True)
            if st["new_jobs"] or st["alerts"].get("sent") or st["alerts"].get("failed"):
                log.info("watch: %d boards, %d new jobs, %d new matches, alerts %s",
                         st["boards"], st["new_jobs"], st["new_matches"], st["alerts"])
        today = datetime.datetime.now().astimezone()
        if w.get("discover_hour") is not None and today.hour == int(w["discover_hour"]) and disc_day != today.date() \
                and not (disc and disc.is_alive()):
            disc_day = today.date()
            disc = threading.Thread(target=nightly, args=(client, cfg), name="discover", daemon=True)
            disc.start()
        n += 1
        if cycles and n >= cycles:
            break
        stop.wait(float(w["tick_seconds"]))
    log.info("watch: stopped")
