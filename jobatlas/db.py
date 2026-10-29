"""SQLite store at <data>/atlas.sqlite (WAL)."""
import datetime
import json
import sqlite3
import threading

from . import config

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

CREATE TABLE IF NOT EXISTS alerts (
    job_id INTEGER PRIMARY KEY,
    created TEXT,
    mode TEXT,                      -- disabled | sent | failed
    text TEXT
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
    return c


def jdump(x):
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def jload(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default
