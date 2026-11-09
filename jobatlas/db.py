"""SQLite store at <data>/atlas.sqlite (WAL)."""
import datetime
import json
import sqlite3
import threading

from . import config

ALERTS_TABLE = """CREATE TABLE IF NOT EXISTS alerts (
    job_id INTEGER NOT NULL,
    profile TEXT NOT NULL DEFAULT 'main',
    created TEXT,
    mode TEXT,                      -- disabled | sent | failed: ... | duplicate | backlog (never sent: alerts.mark_backlog)
    text TEXT,
    PRIMARY KEY (job_id, profile)
);"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL,
    norm TEXT NOT NULL,
    ats TEXT NOT NULL,
    token TEXT NOT NULL,            -- slug / tenant|wdN|site / guid / feed id
    careers_url TEXT,               -- public board URL
    board_name TEXT,                -- the board's own company name, when the API says
    discovered_from TEXT,           -- seed source, probe, careers-page, curated, ...
    first_seen TEXT,
    last_ok TEXT,
    last_polled TEXT,
    next_due TEXT,
    failures INTEGER DEFAULT 0,
    last_error TEXT,
    active INTEGER DEFAULT 1,
    etag TEXT,
    last_modified TEXT,
    jobs_open INTEGER DEFAULT 0,
    new_7d INTEGER DEFAULT 0,
    notes TEXT,
    UNIQUE(ats, token)
);
CREATE INDEX IF NOT EXISTS companies_norm ON companies(norm);
CREATE INDEX IF NOT EXISTS companies_due ON companies(active, next_due);

CREATE TABLE IF NOT EXISTS candidates (
    norm TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    sources TEXT,                   -- json list of where the name came from
    website TEXT,
    priority INTEGER DEFAULT 5,     -- lower = probe sooner
    first_seen TEXT,
    probed_at TEXT,
    result TEXT                     -- found:<n> | none | blocked | error
);

CREATE TABLE IF NOT EXISTS probes (
    ats TEXT NOT NULL,
    token TEXT NOT NULL,
    status TEXT NOT NULL,           -- live | none | blocked | error
    board_name TEXT,
    jobs INTEGER,
    checked_at TEXT,
    PRIMARY KEY (ats, token)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    board_id INTEGER,
    ats TEXT NOT NULL,
    token TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    company TEXT,
    title TEXT,
    url TEXT,
    apply_url TEXT,
    location TEXT,
    workplace TEXT,                 -- remote | hybrid | onsite | ''
    department TEXT,
    employment_type TEXT,
    posted_at TEXT,
    first_seen TEXT,
    last_seen TEXT,
    closed_at TEXT,
    description TEXT,
    pay_text TEXT,
    questions TEXT,                 -- json list of the form's custom questions
    raw TEXT,                       -- json of the list item (+ detail)
    detail_state TEXT,              -- '' | fetched | skipped | failed
    prefilter TEXT,                 -- pass | off-target | non-us | ...
    field TEXT,
    score INTEGER,
    tier TEXT,                      -- strong | good | fair | long | skip
    reasons TEXT,                   -- json list
    flags TEXT,                     -- json list
    facts TEXT,                     -- json dict (pay, location_class, weekend, ...)
    scored_at TEXT,
    baseline INTEGER DEFAULT 0,     -- first seen on the board's first (baseline) poll
    seen TEXT,                      -- "<feed> <date> (<how>)" when the optional ledger already has it
    routed_to TEXT,                 -- the extra profile (config "profiles") this job goes to; NULL = the main profile's
    UNIQUE(ats, token, ext_id)
);
CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS jobs_open ON jobs(closed_at);
CREATE INDEX IF NOT EXISTS jobs_board ON jobs(board_id);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT,
    started TEXT,
    finished TEXT,
    boards INTEGER,
    ok INTEGER,
    failed INTEGER,
    not_modified INTEGER,
    jobs_seen INTEGER,
    new_jobs INTEGER,
    closed INTEGER,
    new_matches INTEGER,
    notes TEXT
);

""" + ALERTS_TABLE + """

-- the extra profiles' scores (config "profiles"); the main profile's are the jobs.* columns.
-- Only jobs that pass that profile's title/location prefilter get a row.
CREATE TABLE IF NOT EXISTS profile_scores (
    job_id INTEGER NOT NULL,
    profile TEXT NOT NULL,
    field TEXT,
    score INTEGER,
    tier TEXT,
    reasons TEXT,
    flags TEXT,
    facts TEXT,
    scored_at TEXT,
    seen TEXT,
    PRIMARY KEY (job_id, profile)
);
CREATE INDEX IF NOT EXISTS profile_scores_tier ON profile_scores(profile, tier);

-- head-to-head verdicts (jobatlas/duel.py): who of the main profile and `other` should get a contested job
CREATE TABLE IF NOT EXISTS duels (
    job_id INTEGER NOT NULL,
    other TEXT NOT NULL,                -- the extra profile; the other side is the main profile
    created TEXT,
    status TEXT,                        -- ok | failed: <why>
    model TEXT,
    input_hash TEXT,                    -- posting + resumes + facts + model + prompt version: re-judged when it changes
    posting_hash TEXT,                  -- the posting alone: a verdict routes the job only while this matches
    tier_route TEXT,                    -- where the job was routed when it was judged
    p_main REAL,                        -- chance of a phone screen, 0-1
    p_other REAL,
    gates_main TEXT,                    -- json lists of hard requirements each one fails
    gates_other TEXT,
    resume_main TEXT,                   -- the resume each one should send
    resume_other TEXT,
    reason_main TEXT,
    reason_other TEXT,
    summary TEXT,
    winner TEXT,                        -- profile name
    why TEXT,                           -- gate | chance | tie
    applied TEXT,                       -- when this verdict last moved the job
    cost_usd REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    secs REAL,
    raw TEXT,
    PRIMARY KEY (job_id, other)
);
"""

_local = threading.local()


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def connect(path=None):
    path = path or config.DB_PATH
    c = sqlite3.connect(path, timeout=60, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=60000")
    c.executescript(SCHEMA)
    migrate(c)
    return c


def migrate(c):
    """Bring a database made by an older version up to SCHEMA (safe to run from several processes)."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    for col, decl in (("baseline", "INTEGER DEFAULT 0"), ("seen", "TEXT"), ("routed_to", "TEXT")):
        if col not in cols:
            try:
                c.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (col, decl))
            except sqlite3.OperationalError:
                pass        # another process added it first
    if "profile" not in {r[1] for r in c.execute("PRAGMA table_info(alerts)")}:
        # alerts were keyed by job alone before profiles: rebuild with (job_id, profile)
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        try:
            if "profile" not in {r[1] for r in c.execute("PRAGMA table_info(alerts)")}:
                c.execute("ALTER TABLE alerts RENAME TO alerts_v1")
                c.execute(ALERTS_TABLE)
                c.execute("INSERT INTO alerts(job_id, profile, created, mode, text) SELECT job_id, ?, created, mode, text FROM alerts_v1",
                          (config.MAIN,))
                c.execute("DROP TABLE alerts_v1")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    c.commit()


SCORE_COLS = ("field", "score", "tier", "reasons", "flags", "facts", "scored_at", "seen")


def scored(conn, profile=None):
    """-> (sql, args): the jobs as one profile sees them, to use as `SELECT ... FROM <sql>`.

    Every job goes to one profile (poll.score_jobs sets routed_to). The main profile sees
    the jobs.* columns of jobs not routed elsewhere; an extra profile sees the jobs routed
    to it, with its own scores from profile_scores."""
    if not profile or profile == config.MAIN:
        return "(SELECT * FROM jobs WHERE routed_to IS NULL OR routed_to='')", []
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
    sel = ", ".join(("ps.%s" if c in SCORE_COLS else "j.%s") % c for c in cols)
    return ("(SELECT %s FROM jobs j JOIN profile_scores ps ON ps.job_id=j.id AND ps.profile=? WHERE j.routed_to=?)" % sel,
            [profile, profile])


def jdump(x):
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def jload(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default
