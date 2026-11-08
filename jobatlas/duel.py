"""Head-to-head ("duel"): when a job fits two people in one household, a model judges who'd get the interview.

The tier rules in poll.score_jobs send each job to one person. A job that is a match for both the
main profile and the duel partner (duel.with, default: the first extra profile) is *contested*:
open, a match for both (fair or better, at least one of them good or better), first seen within
duel.days, with its full text read, and not yet sent to or seen by either of them (ledgers and
alerts). For those, `jobatlas duel run` gives a model (duel.command, e.g. `claude -p`) the posting
next to each person's resumes and facts and asks it, as the employer's recruiter, for each one's
chance of a phone screen (0-1), the hard gates each fails, the resume each should send and a
one-line reason, as strict JSON.

The rule (decide): a hard gate that one of them fails and the other passes decides it. Otherwise
duel.favor (default: the partner) gets the job unless the other's chance is higher by at least
duel.margin (0.10), so a tie or near-tie goes to the favoured side.

Verdicts are stored in the duels table with a hash of every input (posting, resumes, facts,
model, prompt), so a job is judged again only when one of them changes.

duel.enabled false: verdicts are only recorded, nothing is routed by them (a dry run; see
`jobatlas duel show`). Enabled:
  * score_jobs routes a contested job by its verdict while the posting text is the one judged;
  * `duel run` re-routes judged jobs that neither person has seen yet;
  * alerts hold a new contested job for up to duel.hold_minutes while it waits for its verdict,
    so it reaches the person the verdict picks; exports and alert texts carry the verdict.
A missing or failed verdict (model down, quota, bad answer) leaves the tier rule in charge.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import re
import subprocess
import time

from . import config, db, ledger, profile

log = logging.getLogger("jobatlas.duel")

PROMPT_VERSION = "3"    # bump when INSTRUCTIONS change: every job is judged again
MATCH = ("strong", "good", "fair")
TOP = ("strong", "good")
CONTACT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b|\b(?:www\.)?(?:linkedin|github)\.com/\S+", re.I)


class DuelError(RuntimeError):
    """A judgment that couldn't be made (the model failed, or the inputs are missing)."""


def settings():
    return config.cfg().get("duel") or {}


def enabled():
    return bool(settings().get("enabled"))


def pair(seekers=None):
    """-> (main Seeker, partner Seeker), or None when there is no partner profile."""
    seekers = seekers if seekers is not None else profile.seekers()
    want = settings().get("with") or ""
    for s in seekers[1:]:
        if not want or s.name == want:
            return seekers[0], s
    return None


def favor_other(other_name):
    f = settings().get("favor") or ""
    return f != config.MAIN and (not f or f == other_name)


def contested_tiers(main_tier, other_tier):
    return main_tier in MATCH and other_tier in MATCH and (main_tier in TOP or other_tier in TOP)


def _get(job, k):
    try:
        return job[k] or ""
    except (KeyError, IndexError):
        return ""


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def sha(*parts):
    return hashlib.sha1("\x1f".join(_norm(p) if isinstance(p, str) else json.dumps(p, sort_keys=True) for p in parts).encode()).hexdigest()[:16]


def posting_hash(job):
    """The posting as the model reads it. A verdict applies only while this matches."""
    return sha(*[_get(job, k) for k in ("company", "title", "location", "workplace", "pay_text", "description")])


# ---------------------------------------------------------------- inputs

_TEXT = {}      # path -> (mtime, size, text)


def _path(p):
    return p if os.path.isabs(p) else os.path.join(config.DATA, p)


def file_text(path):
    """A resume or notes file as text: .pdf through pdftotext (poppler-utils), anything else as UTF-8. Cached by mtime."""
    path = _path(path)
    st = os.stat(path)
    hit = _TEXT.get(path)
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    if path.lower().endswith(".pdf"):
        try:
            out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, timeout=60, check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise DuelError("can't read %s with pdftotext: %s" % (path, e)) from e
        text = out.decode("utf8", "replace")
    else:
        with open(path, encoding="utf8", errors="replace") as f:
            text = f.read()
    text = re.sub(r"[ \t]{3,}", "   ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    _TEXT[path] = (st.st_mtime, st.st_size, text)
    return text


def materials(name):
    """One side of the duel from duel.candidates.<name>: label, resumes [(name, text)], facts, notes, hash."""
    c = (settings().get("candidates") or {}).get(name) or {}
    resumes = []
    for rname, path in (c.get("resumes") or {}).items():
        if rname.startswith("_") or not path:
            continue
        try:
            resumes.append((rname, CONTACT.sub("[contact details removed]", file_text(path))))
        except OSError as e:
            raise DuelError("duel.candidates.%s.resumes.%s: %s" % (name, rname, e)) from e
    if not resumes:
        raise DuelError("duel.candidates.%s.resumes: no resume configured" % name)
    facts = c.get("facts") or []
    facts = [facts] if isinstance(facts, str) else [str(f) for f in facts if f]
    notes = ""
    if c.get("facts_file"):
        try:
            notes = CONTACT.sub("[contact details removed]", file_text(c["facts_file"]))
        except OSError as e:
            raise DuelError("duel.candidates.%s.facts_file: %s" % (name, e)) from e
    label = c.get("label") or name
    return {"name": name, "label": label, "resumes": resumes, "facts": facts, "notes": notes,
            "hash": sha(label, resumes, facts, notes)}


INSTRUCTIONS = """You are the recruiter screening applicants for the job posting below, at {company}. Two people from the same household, {a} and {b}, are both thinking of applying; only one of them will. For each of them, estimate the chance that you (this employer's recruiter, after any ATS keyword screen) would invite them to a first phone screen if they applied today with the resume named.

Judge only from the documents: the posting, each candidate's resume(s), facts and notes. Names, gender, age and other personal traits must not affect the estimate. The notes were written for other purposes and may include the person's preferences, lists of roles or industries they skip ("hard skips"), and rules about which of them should get which jobs: use the notes only for facts (credentials, experience, location and commute, pay needs). Skip lists and preferences are not the employer's requirements, and the routing rules are what this comparison decides, so ignore both.

For each candidate give:
- p_screen: probability 0-1 of a phone screen. Use the scale honestly: about 0.6 or more = close to exactly what the posting describes; about 0.3 = credible, with a gap or two; about 0.1 = a long shot; under 0.05 = screened out by a hard gate.
- gates_failed: hard gates this candidate clearly fails. A hard gate is a stated REQUIREMENT of the employer (not "preferred", "a plus" or "nice to have") that a screener would reject on: a required degree they don't have, with no "or equivalent experience"; a minimum number of years in a specific title or function they clearly lack; a required technical skill, certification or licence they lack; a location, on-site or commute requirement they can't meet given their facts; a required language; pay whose top is clearly below their stated floor. A few words each, naming the requirement. An empty list if none. Soft gaps don't belong here, and neither do the candidate's own preferences (kinds of roles they'd rather avoid, such as quota roles or fields they skip): those are not gates and don't change p_screen either.
- resume: the one of their resumes they should send (one of the names given).
- reason: one line, under 20 words, for the estimate.
Then summary: one line on who is the better bet for this posting and why.

Today is {today}. Answer with JSON only."""


def build_prompt(job, cands, today=None):
    """The model's whole input: instructions, the posting, then each candidate (in the order given)."""
    today = today or dt.date.today().isoformat()
    head = INSTRUCTIONS.format(company=_get(job, "company") or "this employer", a=cands[0]["label"], b=cands[1]["label"], today=today)
    desc = _get(job, "description")
    posting = ["=== THE POSTING ===",
               "Title: " + _get(job, "title"),
               "Company: " + _get(job, "company"),
               "Location: %s%s" % (_get(job, "location") or "not stated", (" (workplace: %s)" % _get(job, "workplace")) if _get(job, "workplace") else ""),
               "Pay: " + (_get(job, "pay_text") or "not stated"),
               "Employment type: " + (_get(job, "employment_type") or "not stated"),
               "Posted: " + (_get(job, "posted_at")[:10] or "not stated"),
               "", "Description:", desc[:16000] + ("\n[... cut]" if len(desc) > 16000 else "")]
    parts = [head, "", "\n".join(posting)]
    for c in cands:
        block = ["", "=== CANDIDATE: %s ===" % c["label"]]
        if c["facts"]:
            block += ["Facts:"] + ["- " + f for f in c["facts"]]
        if c["notes"]:
            block += ["", "Notes about %s:" % c["label"], c["notes"]]
        names = [r for r, _ in c["resumes"]]
        block += ["", "Resumes (%s): %s" % (len(names), ", ".join(names))]
        for rname, text in c["resumes"]:
            block += ["", "--- %s's resume \"%s\" ---" % (c["label"], rname), text]
        parts.append("\n".join(block))
    return "\n".join(parts) + "\n"


def schema(cands):
    """JSON schema for the answer: one object per candidate label plus a summary."""
    def one(c):
        return {"type": "object", "additionalProperties": False,
                "properties": {"p_screen": {"type": "number", "minimum": 0, "maximum": 1},
                               "gates_failed": {"type": "array", "items": {"type": "string"}},
                               "resume": {"type": "string", "enum": [r for r, _ in c["resumes"]]},
                               "reason": {"type": "string"}},
                "required": ["p_screen", "gates_failed", "resume", "reason"]}
    props = {c["label"]: one(c) for c in cands}
    props["summary"] = {"type": "string"}
    return {"type": "object", "additionalProperties": False, "properties": props,
            "required": [c["label"] for c in cands] + ["summary"]}


# ---------------------------------------------------------------- the model

def _json_in(s):
    s = (s or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except ValueError:
        return None


def parse_output(text):
    """-> (verdict dict, meta). Reads `claude -p --output-format json` (its structured_output or its result
    text), or the bare verdict JSON that any other command prints."""
    text = (text or "").strip()
    try:
        env = json.loads(text)
    except ValueError:
        env = None
    if isinstance(env, list):       # --output-format json with --verbose prints every message
        env = next((e for e in reversed(env) if isinstance(e, dict) and e.get("type") == "result"), env)
    meta = {}
    if isinstance(env, dict) and env.get("type") == "result":
        if env.get("is_error"):
            raise DuelError("model error: %s" % str(env.get("result") or env.get("subtype") or env.get("api_error_status"))[:300])
        u = env.get("usage") or {}
        meta = {"cost_usd": env.get("total_cost_usd"), "tokens_out": u.get("output_tokens"),
                "tokens_in": sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
                "model": ",".join(env.get("modelUsage") or {})}
        v = env.get("structured_output")
        if not isinstance(v, dict):
            v = _json_in(env.get("result") or "")
    elif isinstance(env, dict):
        v = env
    else:
        v = _json_in(text)
    if not isinstance(v, dict):
        raise DuelError("no JSON verdict in the answer: %r" % text[:200])
    return v, meta


def call_model(prompt, sch, s=None):
    """Run duel.command with the prompt on stdin (and the schema after duel.schema_flag). -> (verdict, meta)."""
    s = s if s is not None else settings()
    cmd = [str(x) for x in (s.get("command") or [])]
    if not cmd:
        raise DuelError("duel.command is empty")
    if s.get("schema_flag"):
        cmd += [s["schema_flag"], json.dumps(sch, separators=(",", ":"))]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, input=prompt.encode(), capture_output=True, timeout=float(s.get("timeout") or 300))
    except subprocess.TimeoutExpired as e:
        raise DuelError("no answer after %ss" % s.get("timeout")) from e
    except OSError as e:
        raise DuelError("can't run %s: %s" % (cmd[0], e)) from e
    out = p.stdout.decode("utf8", "replace")
    if p.returncode != 0:
        err = (p.stderr.decode("utf8", "replace").strip() or out.strip())[-300:]
        try:
            env = json.loads(out)   # claude reports quota and API errors in its JSON
            if isinstance(env, dict) and env.get("is_error"):
                err = str(env.get("result") or env.get("subtype") or err)[:300]
        except ValueError:
            pass
        raise DuelError("exit %d: %s" % (p.returncode, err))
    v, meta = parse_output(out)
    meta["secs"] = round(time.time() - t0, 1)
    return v, meta


def read_side(v, c):
    """One candidate's part of the answer, checked. -> {p, gates, resume, reason}."""
    d = v.get(c["label"])
    if not isinstance(d, dict):
        raise DuelError("the answer has no entry for %s" % c["label"])
    try:
        p = float(d.get("p_screen"))
    except (TypeError, ValueError) as e:
        raise DuelError("bad p_screen for %s: %r" % (c["label"], d.get("p_screen"))) from e
    if not 0 <= p <= 1:
        raise DuelError("p_screen for %s is outside 0-1: %r" % (c["label"], p))
    gates = d.get("gates_failed") or []
    gates = [gates] if isinstance(gates, str) else gates
    gates = [str(g).strip()[:160] for g in gates if str(g).strip()]
    names = [r for r, _ in c["resumes"]]
    resume = str(d.get("resume") or "")
    if resume not in names:
        resume = names[0] if len(names) == 1 else ""
    return {"p": round(p, 3), "gates": gates, "resume": resume, "reason": str(d.get("reason") or "").strip()[:300]}


def decide(p_main, p_other, gates_main, gates_other, favor_other=True, margin=0.10, ratio=0):
    """-> (winner, why): winner "main" or "other"; why "gate" (a hard gate only one of them fails),
    "chance" (the winner's chance is higher; for the unfavoured side by at least `margin`, or, with
    `ratio` set, at least `ratio` times the favoured side's) or "tie" (the favoured side wins a near tie)."""
    if bool(gates_main) != bool(gates_other):
        return ("other" if gates_main else "main"), "gate"
    fav, unf = ("other", "main") if favor_other else ("main", "other")
    p = {"main": p_main, "other": p_other}
    lead = p[unf] - p[fav]
    if lead >= margin - 1e-9 or (ratio and lead > 1e-9 and p[unf] >= ratio * p[fav] - 1e-9):
        return unf, "chance"
    return fav, ("chance" if p[fav] > p[unf] else "tie")


def rule(other):
    """(favor_other, margin, ratio) from the settings, for decide()."""
    s = settings()
    return favor_other(other), float(s.get("margin", 0.10)), float(s.get("ratio") or 0)


# ---------------------------------------------------------------- which jobs

def contested(conn, other, since=None, ids=None, days=None):
    """Open jobs that are a match for both the main profile and `other`, first seen since `since`
    (default: duel.days ago), with their full text, and not sent to or seen by either of them as far
    as the stored columns know (see unseen() for the live ledger check). Newest first."""
    s = settings()
    if since is None:
        days = int(s.get("days", 21)) if days is None else days
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    q = ("SELECT j.*, ps.tier o_tier, ps.score o_score, ps.field o_field, ps.seen o_seen FROM jobs j "
         "JOIN profile_scores ps ON ps.job_id=j.id AND ps.profile=? "
         "WHERE j.closed_at IS NULL AND j.prefilter='pass' AND j.first_seen>=? "
         "AND j.tier IN ('strong','good','fair') AND ps.tier IN ('strong','good','fair') "
         "AND (j.tier IN ('strong','good') OR ps.tier IN ('strong','good')) "
         "AND COALESCE(j.seen,'')='' AND COALESCE(ps.seen,'')='' AND length(COALESCE(j.description,''))>=? "
         "AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.job_id=j.id AND a.mode IN ('sent','duplicate'))")
    args = [other, since, int(s.get("min_description", 400))]
    if ids:
        q += " AND j.id IN (%s)" % ",".join("?" * len(ids))
        args += list(ids)
    return conn.execute(q + " ORDER BY j.first_seen DESC, j.id DESC", args).fetchall()


def unseen(r, main, other):
    """Neither person's ledger has the job, read as the file is now (the seen columns are from scoring time)."""
    urls = [_get(r, "url"), _get(r, "apply_url")]
    return not any(ledger.seen(_get(r, "company"), _get(r, "title"), urls, path=s.ledger) for s in (main, other))


def pending(conn):
    """Ids of new contested jobs (first seen in the last duel.hold_minutes) that have no verdict for their
    current text yet. Alerts wait for them. Empty unless the duel is enabled."""
    if not enabled():
        return set()
    hold = float(settings().get("hold_minutes") or 0)
    pr = pair()
    if hold <= 0 or not pr:
        return set()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=hold)).isoformat(timespec="seconds")
    rows = contested(conn, pr[1].name, since=since)
    if not rows:
        return set()
    ids = [r["id"] for r in rows]
    done = {(d[0], d[1]) for d in conn.execute("SELECT job_id, posting_hash FROM duels WHERE other=? AND job_id IN (%s)"
                                                % ",".join("?" * len(ids)), [pr[1].name] + ids)}
    return {r["id"] for r in rows if (r["id"], posting_hash(r)) not in done}


def verdict_winner(conn, job_id, other, job):
    """The profile a stored verdict picks for this job, or None (no verdict, a failed one, or the posting changed)."""
    d = conn.execute("SELECT winner, posting_hash FROM duels WHERE job_id=? AND other=? AND status='ok'", (job_id, other)).fetchone()
    return d["winner"] if d and d["posting_hash"] == posting_hash(job) else None


# ---------------------------------------------------------------- judging

def judge(job, cm, co, s=None):
    """Ask the model about one job. cm/co: materials of the main profile and the partner.
    -> (main side, other side, summary, meta). The order they're presented in alternates by job id."""
    order = [cm, co] if int(_get(job, "id") or 0) % 2 == 0 else [co, cm]
    v, meta = call_model(build_prompt(job, order), schema(order), s)
    return read_side(v, cm), read_side(v, co), str(v.get("summary") or "").strip()[:400], meta


def store(conn, job, other, input_hash, model, result=None, error=None):
    """Record a verdict (or a failure) for one job. -> the row's winner (None on failure)."""
    tier_route = _get(job, "routed_to") or config.MAIN
    base = (job["id"], other, db.now(), model, input_hash, posting_hash(job), tier_route)
    if error is not None:
        conn.execute("INSERT OR REPLACE INTO duels(job_id, other, created, model, input_hash, posting_hash, tier_route, status)"
                     " VALUES (?,?,?,?,?,?,?,?)", base + ("failed: " + str(error)[:300],))
        conn.commit()
        return None
    m, o, summary, meta = result
    key, why = decide(m["p"], o["p"], m["gates"], o["gates"], *rule(other))
    winner = config.MAIN if key == "main" else other
    conn.execute("INSERT OR REPLACE INTO duels(job_id, other, created, model, input_hash, posting_hash, tier_route, status, p_main, p_other,"
                 " gates_main, gates_other, resume_main, resume_other, reason_main, reason_other, summary, winner, why, cost_usd,"
                 " tokens_in, tokens_out, secs, raw) VALUES (?,?,?,?,?,?,?,'ok',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 base + (m["p"], o["p"], db.jdump(m["gates"]), db.jdump(o["gates"]), m["resume"], o["resume"], m["reason"], o["reason"],
                         summary, winner, why, meta.get("cost_usd"), meta.get("tokens_in"), meta.get("tokens_out"), meta.get("secs"),
                         db.jdump({"main": m, "other": o, "summary": summary, "model_used": meta.get("model")})))
    conn.commit()
    return winner


def calls_since(conn, since):
    return conn.execute("SELECT COALESCE(SUM(ok + failed), 0) FROM runs WHERE kind='duel' AND started>=?", (since,)).fetchone()[0]


def redecide(conn, other):
    """Apply the current rule (duel.margin, duel.ratio, duel.favor) to every stored verdict, so tuning it
    needs no new model calls. -> number of verdicts whose winner or reason changed."""
    r = rule(other)
    n = 0
    for d in conn.execute("SELECT job_id, p_main, p_other, gates_main, gates_other, winner, why FROM duels WHERE other=? AND status='ok'",
                          (other,)).fetchall():
        key, why = decide(d["p_main"], d["p_other"], db.jload(d["gates_main"], []), db.jload(d["gates_other"], []), *r)
        w = config.MAIN if key == "main" else other
        if (w, why) != (d["winner"], d["why"]):
            conn.execute("UPDATE duels SET winner=?, why=? WHERE job_id=? AND other=?", (w, why, d["job_id"], other))
            n += 1
    conn.commit()
    return n


def reconcile(conn, main, other, ids=None):
    """Route judged, contested, still-unseen jobs the way their verdict says. Each move is its own short
    transaction that first re-checks no alert has gone out for the job. -> number of jobs moved."""
    rows = contested(conn, other.name, ids=ids, since="")
    if not rows:
        return 0
    verdicts = {d["job_id"]: d for d in conn.execute("SELECT job_id, winner, posting_hash FROM duels WHERE other=? AND status='ok'",
                                                     (other.name,))}
    moved = 0
    for r in rows:
        d = verdicts.get(r["id"])
        if not d or d["posting_hash"] != posting_hash(r):
            continue
        want = None if d["winner"] == config.MAIN else other.name
        if (r["routed_to"] or None) == want or not unseen(r, main, other):
            continue
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM alerts WHERE job_id=? AND mode IN ('sent','duplicate')", (r["id"],)).fetchone():
                conn.execute("ROLLBACK")
                continue
            conn.execute("UPDATE jobs SET routed_to=? WHERE id=?", (want, r["id"]))
            conn.execute("UPDATE duels SET applied=? WHERE job_id=? AND other=?", (db.now(), r["id"], other.name))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        moved += 1
        log.info("duel: job %d %s / %s -> %s", r["id"], r["company"], r["title"], d["winner"])
    return moved


def todo(conn, main, other, cm, co, ids=None, force=False):
    """Contested, unseen jobs that need a (new) verdict: [(row, input_hash)], newest first."""
    s = settings()
    base = sha(PROMPT_VERSION, s.get("model") or "", cm["hash"], co["hash"])
    have = {d["job_id"]: d for d in conn.execute("SELECT job_id, status, input_hash, created FROM duels WHERE other=?", (other.name,))}
    retry = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=float(s.get("retry_minutes", 60)))).isoformat(timespec="seconds")
    out = []
    for r in contested(conn, other.name, ids=ids, since="" if ids else None):
        ih = sha(base, posting_hash(r))
        d = have.get(r["id"])
        if d and not force and d["input_hash"] == ih and (d["status"] == "ok" or d["created"] > retry):
            continue        # current, or failed recently
        if unseen(r, main, other):
            out.append((r, ih))
    return out


def run(conn, limit=None, ids=None, force=False):
    """Judge contested jobs that need it (up to duel.per_run, and duel.per_day a day), then, when the duel is
    enabled, route judged jobs by their verdicts. -> counts. A failed call is recorded and leaves the tier
    rule in charge of that job; it is retried after duel.retry_minutes."""
    s = settings()
    out = {"enabled": enabled(), "todo": 0, "judged": 0, "failed": 0, "rerouted": 0, "left": 0, "cost_usd": 0.0}
    pr = pair()
    if not pr:
        out["error"] = "no partner profile (config profiles / duel.with)"
        return out
    main, other = pr
    started = db.now()
    out["redecided"] = redecide(conn, other.name)
    try:
        cm, co = materials(main.name), materials(other.name)
        if cm["label"] == co["label"]:
            raise DuelError("duel.candidates: both sides are labelled %r" % cm["label"])
    except DuelError as e:
        log.error("duel: %s", e)
        out["error"] = str(e)
        cm = None
    if cm:
        work = todo(conn, main, other, cm, co, ids=ids, force=force)
        out["todo"] = len(work)
        local_midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        room = max(0, int(s.get("per_day", 60)) - calls_since(conn, local_midnight.astimezone(dt.timezone.utc).isoformat(timespec="seconds")))
        n = min(len(work), room, int(limit if limit is not None else s.get("per_run", 6)))
        model = s.get("model") or ""
        for r, ih in work[:n]:
            try:
                res = judge(r, cm, co, s)
            except DuelError as e:
                log.warning("duel: job %d (%s / %s) failed: %s", r["id"], r["company"], r["title"], e)
                store(conn, r, other.name, ih, model, error=e)
                out["failed"] += 1
                if out["failed"] >= 2 and not out["judged"]:
                    break       # the model is down or out of quota: stop for this run
                continue
            w = store(conn, r, other.name, ih, model, result=res)
            out["judged"] += 1
            out["cost_usd"] += float(res[3].get("cost_usd") or 0)
            log.info("duel: job %d %s / %s: %s %.2f vs %s %.2f -> %s", r["id"], r["company"], r["title"], cm["label"], res[0]["p"],
                     co["label"], res[1]["p"], w)
        out["left"] = len(work) - out["judged"] - out["failed"]
    if enabled():
        out["rerouted"] = reconcile(conn, main, other, ids=ids)
    out["cost_usd"] = round(out["cost_usd"], 4)
    if out["judged"] or out["failed"] or out["rerouted"] or out["redecided"]:
        conn.execute("INSERT INTO runs(kind, started, finished, ok, failed, notes) VALUES ('duel', ?, ?, ?, ?, ?)",
                     (started, db.now(), out["judged"], out["failed"], db.jdump(out)))
        conn.commit()
    return out


# ---------------------------------------------------------------- showing it

def labels():
    pr = pair()
    cands = settings().get("candidates") or {}
    if not pr:
        return {}
    return {s.name: (cands.get(s.name) or {}).get("label") or s.name for s in pr}


def view(d, viewer, lab=None):
    """A verdict as one person sees it: {winner, you, them, p_you, p_them, gates_you, gates_them, resume,
    reason, their_reason, why, note}. `viewer` is a profile name; `lab` maps profile names to labels."""
    other = d["other"]
    lab = lab or labels()
    me_main = viewer == config.MAIN
    you, them = (config.MAIN, other) if me_main else (other, config.MAIN)
    side = lambda name: "main" if name == config.MAIN else "other"   # noqa: E731
    p = {"main": d["p_main"], "other": d["p_other"]}
    g = {"main": db.jload(d["gates_main"], []) or [], "other": db.jload(d["gates_other"], []) or []}
    reason = {"main": d["reason_main"] or "", "other": d["reason_other"] or ""}
    resume = {"main": d["resume_main"] or "", "other": d["resume_other"] or ""}
    L = {"main": lab.get(config.MAIN, config.MAIN), "other": lab.get(other, other)}
    y, t = side(you), side(them)
    score = "%s %.2f vs %s %.2f" % (L[y], p[y], L[t], p[t])
    if d["winner"] == you:
        if d["why"] == "gate":
            note = "Better for you than for %s: %s" % (L[t], "; ".join(g[t][:2]))
        elif d["why"] == "tie":
            note = "Near tie, yours by the tie-break: %s" % score
        else:
            note = "Better for you than for %s: %s" % (L[t], score)
    elif d["why"] == "gate":
        note = "Better for %s: you'd fail %s" % (L[t], "; ".join(g[y][:2]))
    else:
        note = "Better for %s: %s" % (L[t], score)
    n_resumes = len(((settings().get("candidates") or {}).get(you) or {}).get("resumes") or {})
    if resume[y] and n_resumes > 1:
        note += "; send the %s resume" % resume[y]
    return {"winner": lab.get(d["winner"], d["winner"]), "you": L[y], "them": L[t], "p_you": p[y], "p_them": p[t],
            "gates_you": g[y], "gates_them": g[t], "resume": resume[y], "reason": reason[y], "their_reason": reason[t],
            "why": d["why"], "summary": d["summary"] or "", "model": d["model"] or "", "judged": d["created"], "note": note}


def lookup(conn, ids, viewer):
    """{job_id: view} for the jobs among `ids` that have a verdict, as `viewer` sees them. Empty unless enabled."""
    ids = [i for i in ids if i is not None]
    if not ids or not enabled():
        return {}
    pr = pair()
    if not pr:
        return {}
    lab = labels()
    out = {}
    for i in range(0, len(ids), 800):
        chunk = ids[i:i + 800]
        for d in conn.execute("SELECT * FROM duels WHERE other=? AND status='ok' AND job_id IN (%s)" % ",".join("?" * len(chunk)),
                              [pr[1].name] + chunk):
            out[d["job_id"]] = view(d, viewer or config.MAIN, lab)
    return out


def summary(conn):
    """Numbers for `jobatlas status` and `duel show`."""
    pr = pair()
    if not pr:
        return None
    other = pr[1].name
    by = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT CASE WHEN status='ok' THEN 'ok' ELSE 'failed' END, COALESCE(winner, ''), COUNT(*) FROM duels WHERE other=? GROUP BY 1, 2", (other,))}
    midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    cost = conn.execute("SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM duels WHERE other=? AND created>=? AND status='ok'",
                        (other, midnight.isoformat(timespec="seconds"))).fetchone()
    return {"enabled": enabled(), "main": config.MAIN, "other": other, "margin": rule(other)[1], "ratio": rule(other)[2],
            "favor": other if favor_other(other) else config.MAIN,
            "verdicts": {w or "?": n for (st, w), n in by.items() if st == "ok"}, "failed": sum(n for (st, _), n in by.items() if st == "failed"),
            "contested_now": len(contested(conn, other)), "calls_today": calls_since(conn, midnight.isoformat(timespec="seconds")),
            "cost_today_usd": round(cost[0], 4)}
