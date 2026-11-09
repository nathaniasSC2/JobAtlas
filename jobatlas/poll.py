"""Poll due boards, store jobs, detect NEW and CLOSED, fetch details for likely fits, score.

Schedule: each board has next_due. Normal boards every poll.interval_hours (3 h);
boards with >= hot_new_7d new jobs in the last week every hot_interval_hours (1 h).
Boards that answer conditional requests (so a re-check is a cheap 304) are re-checked
every match_interval_minutes if they've had a strong/good job, or fit_interval_minutes
if any job passed the prefilter. `jobatlas watch` polls whatever is due continuously;
`jobatlas poll` does one pass (e.g. from a timer).

NEW = first seen in this run on a board that had been polled before. A board's
first poll is its baseline (jobs get baseline=1) so a newly added board doesn't
flood "new"; baseline jobs still count as new matches when their own posted
date is within 72 h.
"""
import concurrent.futures as cf
import datetime
import logging
import re
import threading
import time

from . import adapters, config, db, ledger, score as score_mod
from .net import Client

log = logging.getLogger("jobatlas.poll")


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def due_boards(conn, limit=None, ats=None, all_boards=False, ids=None):
    q = "SELECT * FROM companies WHERE active=1"
    args = []
    if ids:
        q += " AND id IN (%s)" % ",".join("?" * len(ids))
        args += list(ids)
    elif not all_boards:
        q += " AND (next_due IS NULL OR next_due <= ?)"
        args.append(_iso(_utcnow()))
    if ats:
        q += " AND ats IN (%s)" % ",".join("?" * len(ats))
        args += list(ats)
    q += " ORDER BY next_due, id"
    if limit:
        q += " LIMIT %d" % int(limit)
    return conn.execute(q, args).fetchall()


def ensure_columns(conn):
    db.migrate(conn)


def store(conn, board, res, run_started, stats, partial):
    """Upsert one board's result. Returns ids of jobs needing detail/scoring."""
    now = run_started
    first_poll = not board["last_ok"]
    open_rows = {r["ext_id"]: r for r in conn.execute(
        "SELECT id, ext_id, closed_at, last_seen, detail_state, prefilter, title, location FROM jobs WHERE board_id=?", (board["id"],))}
    seen = set()
    todo = []
    profs = [s.prof for s in score_mod.profile_mod.seekers()]
    for j in res.jobs:
        ext = j["ext_id"]
        if not ext or ext in seen:
            continue
        seen.add(ext)
        row = open_rows.get(ext)
        company = j.get("company") or board["company"]
        if row is None:
            pf = score_mod.prefilter_all(j, profs)
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs(board_id, ats, token, ext_id, company, title, url, apply_url, location, workplace, department,"
                " employment_type, posted_at, first_seen, last_seen, description, pay_text, questions, raw, detail_state, prefilter, baseline)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (board["id"], board["ats"], board["token"], ext, company, j["title"], j["url"], j.get("apply_url") or "",
                 j.get("location") or "", j.get("workplace") or "", j.get("department") or "", j.get("employment_type") or "",
                 j.get("posted_at") or "", now, now, j.get("description") or "", j.get("pay_text") or "",
                 db.jdump(j.get("questions") or []), db.jdump(j.get("raw") or {}), "", pf, 1 if first_poll else 0))
            if cur.rowcount:
                stats["new_jobs"] += 1
                if pf == "pass":
                    todo.append((cur.lastrowid, j.get("needs_detail") and not j.get("description")))
        else:
            sets = "last_seen=?, closed_at=NULL"
            args = [now]
            # Workday lists say "3 Locations"; the detail call stored the real list, so keep it
            if row["title"] != j["title"] or (j.get("location") and row["location"] != j.get("location")
                                              and not MANY_LOCATIONS.match(j["location"])):
                sets += ", title=?, location=?"
                args += [j["title"], j.get("location") or row["location"]]
            if j.get("description") and not j.get("needs_detail"):
                sets += ", description=?, pay_text=?"
                args += [j["description"], j.get("pay_text") or ""]
            if j.get("posted_at"):
                sets += ", posted_at=COALESCE(NULLIF(posted_at,''), ?)"
                args.append(j["posted_at"])
            conn.execute("UPDATE jobs SET %s WHERE id=?" % sets, args + [row["id"]])
            if row["closed_at"]:
                stats["reopened"] += 1
            if row["prefilter"] == "pass" and row["detail_state"] == "" and j.get("needs_detail"):
                todo.append((row["id"], True))      # failed ones are retried by backfill_details, spaced out
    # closures
    if partial:
        cutoff = _iso(_utcnow() - datetime.timedelta(days=5))
        c = conn.execute("UPDATE jobs SET closed_at=? WHERE board_id=? AND closed_at IS NULL AND last_seen < ?", (now, board["id"], cutoff))
    else:
        gone = [r["id"] for e, r in open_rows.items() if e not in seen and not r["closed_at"]]
        for i in range(0, len(gone), 500):
            conn.execute("UPDATE jobs SET closed_at=? WHERE id IN (%s)" % ",".join("?" * len(gone[i:i + 500])), [now] + gone[i:i + 500])
        c = None
    stats["closed"] += (c.rowcount if c is not None else len([1 for e, r in open_rows.items() if e not in seen and not r["closed_at"]]))
    return todo


def fast_minutes(conn, board_id, cfgp):
    """Re-check interval for a board whose re-checks are cheap: shorter the more it has fit the profile."""
    if conn.execute("SELECT 1 FROM jobs j WHERE board_id=? AND (tier IN ('strong','good') OR EXISTS "
                    "(SELECT 1 FROM profile_scores ps WHERE ps.job_id=j.id AND ps.tier IN ('strong','good'))) LIMIT 1",
                    (board_id,)).fetchone():
        return float(cfgp.get("match_interval_minutes") or 1e9)
    if conn.execute("SELECT 1 FROM jobs WHERE board_id=? AND prefilter='pass' LIMIT 1", (board_id,)).fetchone():
        return float(cfgp.get("fit_interval_minutes") or 1e9)
    return 1e9


def board_done(conn, board, res, cfgp, now_dt, stats):
    now = _iso(now_dt)
    st = res.status
    if st in ("ok", "not_modified"):
        if st == "not_modified":
            conn.execute("UPDATE jobs SET last_seen=? WHERE board_id=? AND closed_at IS NULL", (now, board["id"]))
        n_open = conn.execute("SELECT COUNT(*) FROM jobs WHERE board_id=? AND closed_at IS NULL", (board["id"],)).fetchone()[0]
        week = _iso(now_dt - datetime.timedelta(days=7))
        new7 = conn.execute("SELECT COUNT(*) FROM jobs WHERE board_id=? AND first_seen>=? AND baseline=0", (board["id"], week)).fetchone()[0]
        hours = cfgp["hot_interval_hours"] if new7 >= cfgp["hot_new_7d"] else cfgp["interval_hours"]
        if res.etag or res.last_modified or board["etag"] or board["last_modified"]:
            hours = min(hours, fast_minutes(conn, board["id"], cfgp) / 60)
        # per-ATS floor, e.g. NEOGOV feeds are large: poll.ats_interval_hours = {"neogov": 12}
        hours = max(hours, float((cfgp.get("ats_interval_hours") or {}).get(board["ats"], 0)))
        conn.execute("UPDATE companies SET last_polled=?, last_ok=?, failures=0, last_error=NULL, etag=COALESCE(?, etag),"
                     " last_modified=COALESCE(?, last_modified), jobs_open=?, new_7d=?, next_due=?,"
                     " board_name=COALESCE(NULLIF(?,''), board_name) WHERE id=?",
                     (now, now, res.etag, res.last_modified, n_open, new7, _iso(now_dt + datetime.timedelta(hours=hours)),
                      res.name, board["id"]))
        stats["ok"] += 1
        if st == "not_modified":
            stats["not_modified"] += 1
    elif st == "blocked":
        conn.execute("UPDATE companies SET last_polled=?, last_error=?, next_due=? WHERE id=?",
                     (now, res.error[:200], _iso(now_dt + datetime.timedelta(hours=24)), board["id"]))
        stats["blocked"] += 1
    else:
        fails = (board["failures"] or 0) + 1
        limit = 3 if st == "none" else cfgp["max_failures"]
        active = 0 if fails >= limit else 1
        backoff = min(48, cfgp["interval_hours"] * (2 ** (fails - 1)))
        conn.execute("UPDATE companies SET last_polled=?, failures=?, last_error=?, active=?, next_due=? WHERE id=?",
                     (now, fails, ("%s: %s" % (st, res.error))[:200], active, _iso(now_dt + datetime.timedelta(hours=backoff)), board["id"]))
        stats["failed"] += 1
        if not active:
            stats["deactivated"] += 1


MANY_LOCATIONS = re.compile(r"^\s*\d+ locations\s*$", re.I)
DETAIL_TRIES = 5            # failed detail calls before a job is left as detail_state 'gave_up'
DETAIL_RETRY_HOURS = 1      # a failed one is tried again no sooner than this


def detail_failed(conn, row, why):
    """Count a failed detail call in the job's raw (_detail_fails, _detail_failed_at, _detail_error); after
    DETAIL_TRIES it is 'gave_up' and never fetched again (it is still scored on its list data)."""
    raw = db.jload(row["raw"], {}) or {}
    raw["_detail_fails"] = int(raw.get("_detail_fails") or 0) + 1
    raw["_detail_failed_at"] = db.now()
    raw["_detail_error"] = str(why or "empty answer")[:160]
    state = "gave_up" if raw["_detail_fails"] >= DETAIL_TRIES else "failed"
    conn.execute("UPDATE jobs SET detail_state=?, raw=? WHERE id=?", (state, db.jdump(raw), row["id"]))
    conn.commit()
    return state


def fetch_details(conn, client, todo, budget, workers, fetched=None):
    """todo: [(job_id, needs_fetch)] -> number fetched. The ids that got their detail are appended to `fetched`."""
    want = [jid for jid, need in todo if need][:budget]
    if not want:
        return 0
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs WHERE id IN (%s)" % ",".join("?" * len(want)), want)} if len(want) < 900 else \
        {r["id"]: r for i in range(0, len(want), 800) for r in conn.execute(
            "SELECT * FROM jobs WHERE id IN (%s)" % ",".join("?" * len(want[i:i + 800])), want[i:i + 800])}

    def one(jid):
        r = rows[jid]
        a = adapters.get(r["ats"])
        job = {"ext_id": r["ext_id"], "url": r["url"], "title": r["title"], "location": r["location"], "raw": db.jload(r["raw"], {})}
        try:
            return jid, a.detail(client, r["token"], job)
        except Exception as e:  # noqa: BLE001
            return jid, {"_detail_error": "%s: %s" % (type(e).__name__, e)}

    n = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for jid, d in ex.map(one, want):
            if not d or d.get("_detail_error"):
                detail_failed(conn, rows[jid], (d or {}).get("_detail_error"))
                continue
            r = rows[jid]
            raw = db.jload(r["raw"], {})
            if d.get("raw_detail"):
                raw["_detail"] = d["raw_detail"]
            closed = _iso(_utcnow()) if d.get("_closed") else None
            conn.execute("UPDATE jobs SET description=COALESCE(NULLIF(?,''), description), questions=?, pay_text=COALESCE(NULLIF(?,''), pay_text),"
                         " location=COALESCE(NULLIF(?,''), location), workplace=COALESCE(NULLIF(?,''), workplace),"
                         " employment_type=COALESCE(NULLIF(?,''), employment_type), posted_at=COALESCE(NULLIF(posted_at,''), ?),"
                         " department=COALESCE(NULLIF(?,''), department), url=COALESCE(NULLIF(?,''), url),"
                         " apply_url=COALESCE(NULLIF(?,''), apply_url), raw=?, detail_state='fetched', closed_at=COALESCE(?, closed_at)"
                         " WHERE id=?",
                         (d.get("description") or "", db.jdump(d.get("questions") or db.jload(r["questions"], [])), d.get("pay_text") or "",
                          d.get("location") or "", d.get("workplace") or "", d.get("employment_type") or "", d.get("posted_at") or "",
                          d.get("department") or "", d.get("url") or "", d.get("apply_url") or "", db.jdump(raw), closed, jid))
            n += 1
            if fetched is not None:
                fetched.append(jid)
            conn.commit()  # never hold the write lock while waiting on the network
            if n % 100 == 0:
                log.info("details: %d/%d", n, len(want))
    conn.commit()
    return n


DETAIL_ATS = ("greenhouse", "rippling", "workday", "bamboohr", "breezy", "smartrecruiters", "paylocity", "adp", "oracle")
GONE = re.compile(r"(job not found|no longer (?:available|accepting|open)|position has been filled|posting (?:has )?(?:closed|expired)|"
                  r"this job (?:is )?(?:closed|no longer)|page not found|404 not found)", re.I)


def backfill_details(conn, client, budget, workers, fetched=None):
    """Fetch details for open prefilter-passing jobs that never got them (e.g. after an interrupted run, or
    jobs that newly pass after a profile change). Never-tried jobs come before failed ones, so a board
    whose detail calls keep failing can't take every slot; a failed one waits DETAIL_RETRY_HOURS between
    tries and is given up after DETAIL_TRIES. Fetched ids are appended to `fetched`."""
    cutoff = _iso(_utcnow() - datetime.timedelta(hours=DETAIL_RETRY_HOURS))
    rows = conn.execute("SELECT id FROM jobs WHERE closed_at IS NULL AND prefilter='pass' AND detail_state IN ('', 'failed') "
                        "AND (detail_state='' OR COALESCE(json_extract(raw, '$._detail_failed_at'), '') < ?) "
                        "AND (description IS NULL OR description='') AND ats IN (%s) ORDER BY detail_state='failed', first_seen DESC LIMIT ?"
                        % ",".join("?" * len(DETAIL_ATS)), [cutoff] + list(DETAIL_ATS) + [budget]).fetchall()
    return fetch_details(conn, client, [(r["id"], True) for r in rows], budget, workers, fetched)


def verify_urls(conn, client, budget=300):
    """Open the posting page of matches that aren't verified yet; close the ones that are gone.

    Lever's API can list jobs whose pages 404 (2026-10-19), so a match is only
    trusted once its page loads. Hosts whose robots.txt disallows the page
    (Workday's human site) stay unverified rather than being fetched.
    """
    rows = conn.execute("SELECT id, url, ats FROM jobs WHERE closed_at IS NULL AND (tier IN ('strong','good','fair') OR id IN "
                        "(SELECT job_id FROM profile_scores WHERE tier IN ('strong','good','fair'))) "
                        "AND (COALESCE(facts, '') NOT LIKE '%\"verified\"%') ORDER BY score DESC LIMIT ?", (budget,)).fetchall()
    out = {"checked": 0, "gone": 0, "ok": 0, "skipped": 0}
    for r in rows:
        if not r["url"] or r["ats"] in ("workday", "neogov"):
            out["skipped"] += 1
            continue
        resp = client.get(r["url"], accept="text/html,*/*")
        out["checked"] += 1
        row = conn.execute("SELECT facts FROM jobs WHERE id=?", (r["id"],)).fetchone()
        facts = db.jload(row["facts"], {}) or {}
        if resp.status in (404, 410) or (resp.ok and GONE.search(resp.text[:30000]) and len(resp.text) < 60000):
            conn.execute("UPDATE jobs SET closed_at=?, tier='skip' WHERE id=?", (db.now(), r["id"]))
            conn.execute("UPDATE profile_scores SET tier='skip' WHERE job_id=?", (r["id"],))
            out["gone"] += 1
        elif resp.ok:
            facts["verified"] = db.now()
            conn.execute("UPDATE jobs SET facts=? WHERE id=?", (db.jdump(facts), r["id"]))
            conn.execute("UPDATE profile_scores SET facts=json_set(COALESCE(facts, '{}'), '$.verified', ?) WHERE job_id=?",
                         (facts["verified"], r["id"]))
            out["ok"] += 1
        else:
            out["skipped"] += 1
        conn.commit()
    return out


MATCH = ("strong", "good", "fair")
FORM_ATS = ("lever",)       # adapters whose form is read from its own page (adapter.form), for matches only
FORM_TRIES = 3


def read_forms(conn, client, budget=20):
    """Read the application form of open matches on ATSes whose questions aren't in the job data (Lever's
    /apply page): the questions can rule a job out ("must live near Dallas", a set $18/hr rate). Each job's
    form is read once (raw._form_read); a page that is gone closes the job. -> ids whose questions changed."""
    if budget <= 0:
        return []
    rows = conn.execute(
        "SELECT * FROM jobs WHERE closed_at IS NULL AND ats IN (%s) AND (tier IN ('strong','good','fair') OR id IN "
        "(SELECT job_id FROM profile_scores WHERE tier IN ('strong','good','fair'))) AND json_extract(raw, '$._form_read') IS NULL "
        "AND COALESCE(json_extract(raw, '$._form_fails'), 0) < ? ORDER BY first_seen DESC LIMIT ?"
        % ",".join("?" * len(FORM_ATS)), list(FORM_ATS) + [FORM_TRIES, budget]).fetchall()
    changed = []
    for r in rows:
        a = adapters.get(r["ats"])
        raw = db.jload(r["raw"], {}) or {}
        try:
            d = a.form(client, r["token"], {"url": r["url"], "ext_id": r["ext_id"]})
        except Exception as e:  # noqa: BLE001
            d = {"_detail_error": "%s: %s" % (type(e).__name__, e)}
        if d.get("_closed"):
            conn.execute("UPDATE jobs SET closed_at=? WHERE id=?", (db.now(), r["id"]))
        elif d.get("_detail_error"):
            raw["_form_fails"] = int(raw.get("_form_fails") or 0) + 1
            raw["_form_error"] = d["_detail_error"][:160]
            conn.execute("UPDATE jobs SET raw=? WHERE id=?", (db.jdump(raw), r["id"]))
        else:
            raw["_form_read"] = db.now()
            conn.execute("UPDATE jobs SET questions=?, raw=? WHERE id=?", (db.jdump(d.get("questions") or []), db.jdump(raw), r["id"]))
            changed.append(r["id"])
        conn.commit()
    return changed



def score_jobs(conn, ids=None, where=None):
    """Score jobs for the main profile (jobs.*) and each extra profile (profile_scores), then route each
    job to one profile (jobs.routed_to): the first extra profile it fits at its claim_tier or better;
    else the main profile if it is a match there; else the first extra profile it is a match for.
    With the duel on, a job that is a match for both sides and has a verdict for its current text
    goes where the verdict says (duel.py). A job that was already sent to someone stays with them."""
    from . import duel
    main, *extras = score_mod.profile_mod.seekers()
    prof = main.prof
    duel_pair = duel.pair([main, *extras]) if duel.enabled() else None
    duel_with = duel_pair[1].name if duel_pair else None
    if ids is not None:
        rows = []
        ids = list(ids)
        for i in range(0, len(ids), 800):
            rows += conn.execute("SELECT * FROM jobs WHERE id IN (%s)" % ",".join("?" * len(ids[i:i + 800])), ids[i:i + 800]).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs WHERE " + (where or "prefilter='pass' AND closed_at IS NULL")).fetchall()
    now = db.now()
    counts = {}
    for i, r in enumerate(rows):
        if i and i % 200 == 0:
            conn.commit()   # short write transactions: a full rescore must not lock out `watch` or the API
        job = dict(r)
        a = score_mod.assess(job, prof, company=r["company"] or "")
        old = db.jload(r["facts"], {}) or {}
        if old.get("verified"):
            a["facts"]["verified"] = old["verified"]
        urls = [r["url"], r["apply_url"]]
        note = ledger.seen(r["company"], r["title"], urls, path=main.ledger)
        conn.execute("UPDATE jobs SET field=?, score=?, tier=?, reasons=?, flags=?, facts=?, scored_at=?, seen=? WHERE id=?",
                     (a["field"], a["score"], a["tier"], db.jdump(a["reasons"]), db.jdump(a["flags"]), db.jdump(a["facts"]), now,
                      note, r["id"]))
        counts[a["tier"]] = counts.get(a["tier"], 0) + 1
        claim = fallback = duel_side = None
        for s in extras:
            if score_mod.prefilter(job, s.prof) != "pass":
                conn.execute("DELETE FROM profile_scores WHERE job_id=? AND profile=?", (r["id"], s.name))
                continue
            b = score_mod.assess(job, s.prof, company=r["company"] or "")
            if old.get("verified"):
                b["facts"]["verified"] = old["verified"]
            b_seen = ledger.seen(r["company"], r["title"], urls, path=s.ledger)
            conn.execute("INSERT OR REPLACE INTO profile_scores(job_id, profile, field, score, tier, reasons, flags, facts, scored_at, seen)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (r["id"], s.name, b["field"], b["score"], b["tier"], db.jdump(b["reasons"]), db.jdump(b["flags"]),
                          db.jdump(b["facts"]), now, b_seen))
            if s.name == duel_with:
                duel_side = (b["tier"], b_seen)
            c = counts.setdefault(s.name, {})
            c[b["tier"]] = c.get(b["tier"], 0) + 1
            kept = a["tier"] in MATCH and a["field"] in s.main_keeps and b["tier"] != "strong"
            if claim is None and b["tier"] in s.claims and not kept:
                claim = s.name
            if fallback is None and b["tier"] in MATCH:
                fallback = s.name
        if extras or r["routed_to"]:
            route = claim or (None if a["tier"] in MATCH else fallback)
            # once a job has reached someone (an alert, or the main profile's ledger) it stays theirs:
            # they may have applied already
            reached = conn.execute("SELECT profile, mode FROM alerts WHERE job_id=? AND mode IN ('sent','duplicate')", (r["id"],)).fetchall()
            sent = [p for p, m in reached if m == "sent"]
            if duel_side and not reached and not note and not duel_side[1] and duel.contested_tiers(a["tier"], duel_side[0]):
                won = duel.verdict_winner(conn, r["id"], duel_with, job)
                if won:
                    route = None if won == main.name else won
            if (note or main.name in sent) and a["tier"] in MATCH:
                route = None
            elif sent and any(s.name in sent for s in extras):
                route = next(s.name for s in extras if s.name in sent)
            conn.execute("UPDATE jobs SET routed_to=? WHERE id=?", (route, r["id"]))
            if claim and a["tier"] in MATCH:
                counts["claimed"] = counts.get("claimed", 0) + 1     # a match for main that went to someone else
    conn.commit()
    return counts


def run(conn=None, limit=None, ats=None, all_boards=False, ids=None, workers=None, detail_budget=None, quiet=False,
        client=None, verify_budget=300, backfill_budget=None):
    cfg = config.cfg()
    cfgp = cfg["poll"]
    conn = conn or db.connect()
    ensure_columns(conn)
    client = client or Client(cfg, log=log.info)
    started_dt = _utcnow()
    started = _iso(started_dt)
    run_id = conn.execute("INSERT INTO runs(kind, started) VALUES ('poll', ?)", (started,)).lastrowid
    conn.commit()
    boards = due_boards(conn, limit=limit, ats=ats, all_boards=all_boards, ids=ids)
    stats = {"boards": len(boards), "ok": 0, "failed": 0, "blocked": 0, "not_modified": 0, "new_jobs": 0, "closed": 0,
             "reopened": 0, "deactivated": 0, "jobs_seen": 0, "details": 0}
    (log.debug if quiet else log.info)("poll: %d boards due", len(boards))
    todo_all = []
    lock = threading.Lock()
    workers = workers or cfgp["workers"]

    def fetch(b):
        a = adapters.get(b["ats"])
        t0 = time.time()
        try:
            res = a.fetch(client, b["token"], etag=b["etag"], last_modified=b["last_modified"])
        except Exception as e:  # noqa: BLE001
            from .adapters.base import Result
            res = Result("error", error="%s: %s" % (type(e).__name__, e))
        return b, res, time.time() - t0

    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch, b) for b in boards]
        for fut in cf.as_completed(futs):
            b, res, dt = fut.result()
            with lock:
                if res.status == "ok":
                    stats["jobs_seen"] += len(res.jobs)
                    todo_all += store(conn, b, res, started, stats, adapters.get(b["ats"]).PARTIAL)
                board_done(conn, b, res, cfgp, _utcnow(), stats)
                conn.commit()  # short write transactions: discovery and the API share this DB
                done += 1
                if res.status not in ("ok", "not_modified"):
                    log.info("board %s:%s (%s) -> %s %s", b["ats"], b["token"], b["company"], res.status, res.error[:100])
                if done % 50 == 0:
                    if not quiet:
                        log.info("poll: %d/%d boards, %d new jobs so far", done, len(boards), stats["new_jobs"])
    conn.commit()
    budget = detail_budget if detail_budget is not None else cfgp["detail_budget"]
    stats["details"] = fetch_details(conn, client, todo_all, budget, workers)
    left = budget - stats["details"] if backfill_budget is None else min(backfill_budget, budget - stats["details"])
    backfilled = []
    if left > 0:
        stats["details_backfill"] = backfill_details(conn, client, left, workers, backfilled)
    ids = {jid for jid, _ in todo_all} | set(backfilled)     # a job is scored again once its text arrives
    ids |= {r[0] for r in conn.execute("SELECT id FROM jobs WHERE prefilter='pass' AND closed_at IS NULL AND scored_at IS NULL")}
    tiers = score_jobs(conn, ids=sorted(ids))
    stats["scored"] = tiers
    formed = read_forms(conn, client, cfgp.get("form_budget", 20))
    if formed:      # before the alerts: a form question can rule a new match out
        stats["forms"] = len(formed)
        score_jobs(conn, ids=formed)
    stats["verified"] = verify_urls(conn, client, verify_budget)
    new_matches = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE first_seen=? AND tier IN ('strong','good','fair') AND (baseline=0 OR posted_at>=?)",
        (started, (started_dt - datetime.timedelta(hours=72)).date().isoformat())).fetchone()[0]
    stats["new_matches"] = new_matches
    stats["http"] = client.stats
    # alerts (disabled by default) right after scoring, then the export
    from . import alerts, export
    stats["alerts"] = alerts.process(conn, started)
    stats["export"] = export.write(conn)
    conn.execute("UPDATE runs SET finished=?, boards=?, ok=?, failed=?, not_modified=?, jobs_seen=?, new_jobs=?, closed=?, new_matches=?, notes=? WHERE id=?",
                 (db.now(), stats["boards"], stats["ok"], stats["failed"] + stats["blocked"], stats["not_modified"], stats["jobs_seen"],
                  stats["new_jobs"], stats["closed"], new_matches, db.jdump({k: stats[k] for k in ("blocked", "deactivated", "details", "scored", "http", "alerts")}),
                  run_id))
    conn.commit()
    (log.debug if quiet else log.info)("poll done: %s", {k: v for k, v in stats.items() if k != "export"})
    return stats
