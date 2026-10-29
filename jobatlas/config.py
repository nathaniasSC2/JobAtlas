"""Paths and settings. Defaults live here; <data>/config.json overrides them.

Everything is relative to the data directory ($JOBATLAS_DATA, default ./data).
Nothing secret is stored in the repo or in config.json: alert senders read their
tokens from an env file *by reference* at send time (see alerts.py).
"""
import copy
import json
import os

DATA = os.path.abspath(os.environ.get("JOBATLAS_DATA", "data"))
DB_PATH = os.environ.get("JOBATLAS_DB", os.path.join(DATA, "atlas.sqlite"))
LOGS = os.path.join(DATA, "logs")
EXPORT = os.path.join(DATA, "export")
CACHE = os.path.join(DATA, "cache")
CONFIG_FILE = os.environ.get("JOBATLAS_CONFIG", os.path.join(DATA, "config.json"))
# Who you are and what you want: fields, location, pay, hard rules (see examples/profile.example.json).
PROFILE_FILE = os.environ.get("JOBATLAS_PROFILE", os.path.join(DATA, "profile.json"))

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 JobAtlas/0.1"
ROBOTS_TOKEN = "JobAtlas"

DEFAULTS = {
    "poll": {
        "interval_hours": 3.0,        # normal boards
        "hot_interval_hours": 1.0,    # boards that posted >= hot_new_7d new jobs in the last week
        "hot_new_7d": 5,
        "workers": 12,                # threads; per-host spacing still applies
        "max_failures": 6,            # consecutive failures before a board is deactivated
        "detail_budget": 1500,        # max per-job detail fetches per poll run
        "ats_interval_hours": {"neogov": 12},   # minimum hours between polls for these ATSes
    },
    "http": {
        "timeout": 25,
        "user_agent": UA,
        "default_spacing": 0.5,       # seconds between requests to one host
        "host_spacing": {
            "boards-api.greenhouse.io": 0.25,
            "api.lever.co": 0.3,
            "api.ashbyhq.com": 0.3,
            "apply.workable.com": 4.0,     # Cloudflare 1015 after a few dozen quick calls
            "api.rippling.com": 0.6,
            "recruiting.paylocity.com": 1.5,
            "careers.pageuppeople.com": 2.0,
        },
        "workday_spacing": 1.5,       # per tenant host; some tenants wall repeated hits
        "cooldown_429": 120,          # pause a host this long after a 429/1015
        "max_429": 3,                 # drop a host for the rest of the run after this many
        # Hosts whose robots.txt JobAtlas may ignore. EMPTY by default; add a host only for a
        # site you would otherwise check by hand (e.g. a public-sector job feed whose robots.txt
        # admits only named search engines).
        "robots_override": [],
    },
    "discover": {
        "budget": 400,                # company names probed per discovery run
        "miss_days": 21,              # don't re-probe a slug that had no board for this long
        # workable is added per company only when the name was seen on Workable (it rate-limits hard)
        "probe_ats": ["greenhouse", "ashby", "lever", "rippling", "bamboohr", "recruitee", "breezy", "teamtailor"],
    },
    "export": {"hours": 72, "min_tier": "fair"},
    "api": {"host": "127.0.0.1", "port": 8804},
    # Optional "already seen" ledger: a JSONL file of {company, title, url} rows that some other
    # tool (a tracker, a spreadsheet export) writes. Matching rows are marked already_seen.
    "ledger": {"path": ""},
    "alerts": {
        "enabled": False,
        "min_tier": "strong",
        "min_score": 92,
        "max_per_run": 3,
        "daily_cap": 6,
        "quiet_start": 22,
        "quiet_end": 8,
        "tz": "",                       # IANA name for quiet hours and the daily cap; empty = server local time
        "channel": "discord_webhook",   # discord_webhook | discord_dm | imessage
        # Secrets are read at send time from this env file, never stored in config.json:
        #   JOBATLAS_DISCORD_WEBHOOK                       (discord_webhook)
        #   JOBATLAS_DISCORD_TOKEN, JOBATLAS_DISCORD_USER  (discord_dm: a bot token and your user id)
        #   BLUEBUBBLES_SERVER_URL, BLUEBUBBLES_PASSWORD   (imessage, via a BlueBubbles server)
        "env_file": "",
        "chat_guid": "",                # imessage only, e.g. iMessage;-;+1...
    },
}


def _merge(a, b):
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


_CFG = None


def cfg():
    global _CFG
    if _CFG is None:
        over = {}
        try:
            with open(CONFIG_FILE, encoding="utf8") as f:
                over = json.load(f)
        except (OSError, ValueError):
            over = {}
        _CFG = _merge(DEFAULTS, over)
    return _CFG


def ensure_dirs():
    for d in (DATA, LOGS, EXPORT, CACHE):
        os.makedirs(d, exist_ok=True)
