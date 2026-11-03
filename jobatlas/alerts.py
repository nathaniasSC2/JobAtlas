"""Fast alerts: message you when a NEW job scores Strong.

alerts.channel picks the transport. Secrets are read at send time from
alerts.env_file (KEY=value lines), never stored in the repo or config.json:

* "discord_webhook": POST to a channel webhook (JOBATLAS_DISCORD_WEBHOOK).
  The simplest setup: Server Settings -> Integrations -> Webhooks.
* "discord_dm": a DM from a bot you own (JOBATLAS_DISCORD_TOKEN, and your user
  id in JOBATLAS_DISCORD_USER or alerts.discord_user_id). REST only
  (POST /users/@me/channels, then /channels/<id>/messages); no gateway session,
  so a bot that is also connected elsewhere is untouched.
* "imessage": a BlueBubbles server's REST API,
  `POST <server>/api/v1/message/text?password=<pw>` with {chatGuid, message, method}
  (BLUEBUBBLES_SERVER_URL or alerts.server_url, BLUEBUBBLES_PASSWORD, alerts.chat_guid).

With alerts.enabled false nothing is sent; would-be messages are recorded in
the alerts table (mode "disabled") so the policy can be checked first.

Each extra profile (config "profiles") has its own alert settings, over the
defaults (so off until its own "enabled": true), and its own record in the
alerts table. It is never sent the main profile's settings or recipient.
"""
import datetime as dt
import json
import logging
import re
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from . import __version__, config, db, profile

log = logging.getLogger("jobatlas.alerts")


def _env(path):
    out = {}
    try:
        with open(path, encoding="utf8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def secrets(cfg):
    """alerts.env_file as a dict, plus alerts.env_keys renames (our name -> the name used in that file)."""
    env = _env(cfg.get("env_file") or "")
    for ours, theirs in (cfg.get("env_keys") or {}).items():
        if theirs in env:
            env[ours] = env[theirs]
    return env


def text_for(r, cfg=None):
    """One job as a message. alerts.style "plain" drops the score and the scorer's reason."""
    cfg = cfg or {}
    facts = db.jload(r["facts"], {}) or {}
    pay = ""
    if facts.get("pay_top"):
        pay = (" $%dk" % round(facts["pay_top"] / 1000)) if facts.get("pay_kind") == "year" else (" $%.0f/hr" % facts["pay_top"])
    where = (" (%s)" % r["location"][:60]) if r["location"] else ""
    label = cfg.get("label") or "JobAtlas Strong"
    if cfg.get("style") == "plain":
        return ("%s: %s at %s%s%s\n%s" % (label, r["title"], r["company"], where, (", up to" + pay) if pay else "", r["url"]))[:600]
    why = (db.jload(r["reasons"], []) or [""])[0]
    return ("%s (%d): %s at %s%s%s. %s\n%s" % (label, r["score"], r["title"], r["company"], where, pay, why, r["url"]))[:600]


DISCORD_API = "https://discord.com/api/v10"
URL_RE = re.compile(r"(?<!<)(https?://\S+)")


def discord_text(message):
    """Wrap bare links in <...> so Discord doesn't unfurl a preview card per job."""
    return URL_RE.sub(r"<\1>", message)[:2000]


UA = "DiscordBot (https://github.com/nathaniasSC2/JobAtlas, %s)" % __version__


def _post(url, payload, headers=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json", "User-Agent": UA}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def send_discord_dm(message, cfg):
    env = secrets(cfg)
    token = env.get("JOBATLAS_DISCORD_TOKEN", "")
    user = str(cfg.get("discord_user_id") or env.get("JOBATLAS_DISCORD_USER", "")).split(",")[0].strip()
    if not (token and user):
        return False, "missing JOBATLAS_DISCORD_TOKEN / JOBATLAS_DISCORD_USER (alerts.env_file)"
    try:
        auth = {"Authorization": "Bot " + token}
        dm = _post(DISCORD_API + "/users/@me/channels", {"recipient_id": user}, auth)
        _post(DISCORD_API + "/channels/%s/messages" % dm["id"], {"content": discord_text(message), "allowed_mentions": {"parse": []}}, auth)
        return True, "discord DM ok"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def send_discord_webhook(message, cfg):
    hook = secrets(cfg).get("JOBATLAS_DISCORD_WEBHOOK", "")
    if not hook.startswith("https://"):
        return False, "missing JOBATLAS_DISCORD_WEBHOOK (alerts.env_file)"
    try:
        _post(hook + ("&" if "?" in hook else "?") + "wait=true", {"content": discord_text(message), "allowed_mentions": {"parse": []}})
        return True, "discord webhook ok"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def send(message, cfg):
    ch = cfg.get("channel") or "discord_webhook"
    if ch == "discord_dm":
        return send_discord_dm(message, cfg)
    if ch == "imessage":
        return send_imessage(message, cfg)
    return send_discord_webhook(message, cfg)


def send_imessage(message, cfg):
    env = secrets(cfg)
    # alerts.server_url (config.json) overrides the env file's URL; the password still comes from the env file.
    server = (cfg.get("server_url") or env.get("BLUEBUBBLES_SERVER_URL", "")).rstrip("/")
    pw, guid = env.get("BLUEBUBBLES_PASSWORD", ""), cfg.get("chat_guid") or ""
    if not (server and pw and guid):
        return False, "missing BlueBubbles server/password (env file) or alerts.chat_guid (config.json)"
    body = json.dumps({"chatGuid": guid, "tempGuid": str(uuid.uuid4()), "message": message, "method": "apple-script"}).encode()
    req = urllib.request.Request("%s/api/v1/message/text?password=%s" % (server, urllib.parse.quote(pw)), data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300, "HTTP %d" % r.status
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


DEFAULT_SKIP_TITLES = r"\b(head of|director|vice president|vp|chief|principal|staff|senior|sr\.?|marketing|website)\b"


def in_quiet_hours(now_local, start_h, end_h):
    """True when now_local's hour falls in [start_h, end_h) wrapping midnight (e.g. 22 -> 8)."""
    h = now_local.hour
    return (start_h <= h or h < end_h) if start_h > end_h else (start_h <= h < end_h)


def local_now(tz_name=""):
    """Now in alerts.tz (an IANA name), or in the server's local time when it is empty."""
    return dt.datetime.now(ZoneInfo(tz_name)) if tz_name else dt.datetime.now().astimezone()


def title_ok(title, pattern):
    return not (pattern and re.search(pattern, title or "", re.I))


def batch_text(rows, cfg=None):
    cfg = cfg or {}
    if len(rows) == 1:
        return text_for(rows[0], cfg)
    label = cfg.get("label") or "JobAtlas Strong"
    if cfg.get("style") == "plain":
        lines = ["%s: %d new matches" % (label, len(rows))]
        lines += [x for r in rows for x in ("", text_for(r, cfg).split(": ", 1)[1])]
    else:
        lines = ["JobAtlas: %d strong new matches" % len(rows)] if label == "JobAtlas Strong" else ["%s: %d new matches" % (label, len(rows))]
        lines += [x for r in rows for x in ("", text_for(r, cfg).replace(label + " ", "", 1))]
    return "\n".join(lines)[:1500]


def process(conn, run_started):
    """Alerts for every profile (main first); see process_one. -> totals, plus by_profile when there are extras."""
    keys = ("candidates", "sent", "disabled", "failed", "held")
    out = dict.fromkeys(keys, 0)
    seekers = profile.seekers()
    for s in seekers:
        o = process_one(conn, s.name, s.alerts)
        for k in keys:
            out[k] += o.get(k, 0)
        if len(seekers) > 1:
            out.setdefault("by_profile", {})[s.name] = o
    return out


def process_one(conn, name, cfg):
    """Message about Strong jobs first seen in the last day (not already alerted or in the ledger).

    Called right after each poll cycle scores its new jobs. Policy (all optional):
      * only score >= alerts.min_score and titles not matching alerts.skip_titles;
      * quiet hours alerts.quiet_start..quiet_end in alerts.tz: nothing is sent or recorded,
        so overnight finds go out on the first run after quiet hours end;
      * at most alerts.daily_cap sent jobs per local day (0 = no cap), highest scores first,
        and up to alerts.max_per_run jobs bundled into one message.
    A failed send is retried after 5 minutes. With alerts.enabled false, candidates are
    only recorded (mode "disabled"). `name` is the profile, `cfg` its alert settings.
    """
    now_local = local_now(cfg.get("tz") or "")
    min_rank = {"strong": 0, "good": 1, "fair": 2}.get(cfg.get("min_tier", "strong"), 0)
    tiers = [t for t, i in (("strong", 0), ("good", 1), ("fair", 2)) if i <= min_rank]
    utc = dt.datetime.now(dt.timezone.utc)
    since = (utc - dt.timedelta(hours=int(cfg.get("lookback_hours", 24)))).isoformat(timespec="seconds")
    retry = (utc - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    src, args = db.scored(conn, name)
    rows = conn.execute(
        "SELECT * FROM %s WHERE first_seen>=? AND tier IN (%s) AND closed_at IS NULL AND (seen IS NULL OR seen='') "
        "AND (baseline=0 OR posted_at>=date('now','-3 day')) "
        "AND id NOT IN (SELECT job_id FROM alerts WHERE profile=? AND (mode NOT LIKE 'failed%%' OR created>?)) "
        "AND score>=? ORDER BY score DESC" % (src, ",".join("?" * len(tiers))),
        args + [since] + tiers + [name, retry, int(cfg.get("min_score") or 0)]).fetchall()
    pat = cfg.get("skip_titles", DEFAULT_SKIP_TITLES)
    rows = [r for r in rows if title_ok(r["title"], pat)]
    # one line per company + title: the same job posted in several locations is alerted once
    keys, dups = set(), {}
    for r in list(rows):
        k = ((r["company"] or "").lower(), (r["title"] or "").lower())
        if k in keys:
            rows.remove(r)
            dups.setdefault(k, []).append(r)
        keys.add(k)
    out = {"candidates": len(rows), "sent": 0, "disabled": 0, "failed": 0, "held": 0}
    if not rows:
        return out
    who = "" if name == config.MAIN else " (%s)" % name
    if not cfg.get("enabled"):
        for r in rows:
            msg = text_for(r, cfg)
            conn.execute("INSERT OR IGNORE INTO alerts(job_id, profile, created, mode, text) VALUES (?,?,?,?,?)",
                         (r["id"], name, db.now(), "disabled", msg))
            log.info("alerts%s disabled; would text: %s", who, msg.replace("\n", " | "))
            out["disabled"] += 1
        conn.commit()
        return out
    qs, qe = cfg.get("quiet_start"), cfg.get("quiet_end")
    if qs is not None and qe is not None and in_quiet_hours(now_local, int(qs), int(qe)):
        out["held"] = len(rows)
        log.info("quiet hours%s: holding %d alert(s) until %02d:00", who, len(rows), int(qe))
        return out
    midnight_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    sent_today = conn.execute("SELECT count(*) FROM alerts WHERE profile=? AND mode='sent' AND created>=?", (name, midnight_utc)).fetchone()[0]
    cap = int(cfg.get("daily_cap") or 0)
    room = max(0, cap - sent_today) if cap else len(rows)
    batch = rows[: min(room, max(1, int(cfg.get("max_per_run") or 5)))]
    if not batch:
        out["held"] = len(rows)
        log.info("daily cap reached%s (%d); holding %d alert(s)", who, sent_today, len(rows))
        return out
    ok, info = send(batch_text(batch, cfg), cfg)
    for r in batch:
        conn.execute("INSERT OR REPLACE INTO alerts(job_id, profile, created, mode, text) VALUES (?,?,?,?,?)",
                     (r["id"], name, db.now(), "sent" if ok else "failed: " + info, text_for(r, cfg)))
        for d in dups.get(((r["company"] or "").lower(), (r["title"] or "").lower()), []) if ok else []:
            conn.execute("INSERT OR REPLACE INTO alerts(job_id, profile, created, mode, text) VALUES (?,?,?,?,?)",
                         (d["id"], name, db.now(), "duplicate", text_for(d, cfg)))
    out["sent" if ok else "failed"] = len(batch)
    out["held"] = len(rows) - len(batch)
    conn.commit()
    return out
