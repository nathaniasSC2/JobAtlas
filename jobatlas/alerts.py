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
"""
import datetime as dt
import json
import logging
import re
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from . import config, db

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


def text_for(r):
    facts = db.jload(r["facts"], {}) or {}
    pay = ""
    if facts.get("pay_top"):
        pay = (" $%dk" % round(facts["pay_top"] / 1000)) if facts.get("pay_kind") == "year" else (" $%.0f/hr" % facts["pay_top"])
    why = (db.jload(r["reasons"], []) or [""])[0]
    return ("JobAtlas Strong (%d): %s at %s%s. %s\n%s" % (r["score"], r["title"], r["company"], pay, why, r["url"]))[:600]


DISCORD_API = "https://discord.com/api/v10"
URL_RE = re.compile(r"(?<!<)(https?://\S+)")


def discord_text(message):
    """Wrap bare links in <...> so Discord doesn't unfurl a preview card per job."""
    return URL_RE.sub(r"<\1>", message)[:2000]


UA = "DiscordBot (https://github.com/nathaniasSC2, 1.0)"


def _post(url, payload, headers=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json", "User-Agent": UA}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def send_discord_dm(message, cfg):
    env = _env(cfg.get("env_file") or "")
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
    hook = _env(cfg.get("env_file") or "").get("JOBATLAS_DISCORD_WEBHOOK", "")
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
    env = _env(cfg.get("env_file") or "")
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


def title_ok(title, pattern):
    return not (pattern and re.search(pattern, title or "", re.I))


def batch_text(rows):
    if len(rows) == 1:
        return text_for(rows[0])
    lines = ["JobAtlas: %d strong new matches" % len(rows)]
    for r in rows:
        lines.append("")
        lines.append(text_for(r).replace("JobAtlas Strong ", "", 1))
    return "\n".join(lines)[:1500]


def process(conn, run_started):
    """Message about Strong jobs first seen in the last day (not already alerted or in the ledger).

    Policy:
      * only score >= alerts.min_score and titles not matching alerts.skip_titles
        (leadership/senior stretches stay in the export, not on your phone);
      * quiet hours alerts.quiet_start..quiet_end (America/Los_Angeles): nothing is sent or
        recorded, so overnight finds go out on the first run after quiet hours end;
      * at most alerts.daily_cap texted jobs per local day, highest scores first, and up to
        alerts.max_per_run jobs batched into ONE text per run.
    With alerts.enabled false, candidates are only recorded (mode "disabled").
    """
    cfg = config.cfg()["alerts"]
    tz = ZoneInfo(cfg.get("tz", "America/Los_Angeles"))
    now_local = dt.datetime.now(tz)
    min_rank = {"strong": 0, "good": 1, "fair": 2}.get(cfg.get("min_tier", "strong"), 0)
    tiers = [t for t, i in (("strong", 0), ("good", 1), ("fair", 2)) if i <= min_rank]
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=int(cfg.get("lookback_hours", 24)))).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM jobs WHERE first_seen>=? AND tier IN (%s) AND closed_at IS NULL AND (seen IS NULL OR seen='') "
        "AND (baseline=0 OR posted_at>=date('now','-3 day')) AND id NOT IN (SELECT job_id FROM alerts) "
        "AND score>=? ORDER BY score DESC" % ",".join("?" * len(tiers)),
        [since] + tiers + [int(cfg.get("min_score", 92))]).fetchall()
    pat = cfg.get("skip_titles", DEFAULT_SKIP_TITLES)
    rows = [r for r in rows if title_ok(r["title"], pat)]
    out = {"candidates": len(rows), "sent": 0, "disabled": 0, "failed": 0, "held": 0}
    if not rows:
        return out
    if not cfg.get("enabled"):
        for r in rows:
            msg = text_for(r)
            conn.execute("INSERT OR IGNORE INTO alerts(job_id, created, mode, text) VALUES (?,?,?,?)", (r["id"], db.now(), "disabled", msg))
            log.info("alerts disabled; would text: %s", msg.replace("\n", " | "))
            out["disabled"] += 1
        conn.commit()
        return out
    if in_quiet_hours(now_local, int(cfg.get("quiet_start", 22)), int(cfg.get("quiet_end", 8))):
        out["held"] = len(rows)
        log.info("quiet hours: holding %d alert(s) until %02d:00", len(rows), int(cfg.get("quiet_end", 8)))
        return out
    midnight_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    sent_today = conn.execute("SELECT count(*) FROM alerts WHERE mode='sent' AND created>=?", (midnight_utc,)).fetchone()[0]
    room = max(0, int(cfg.get("daily_cap", 6)) - sent_today)
    batch = rows[: min(room, max(0, int(cfg.get("max_per_run", 3))))]
    if not batch:
        out["held"] = len(rows)
        log.info("daily cap reached (%d); holding %d alert(s)", sent_today, len(rows))
        return out
    ok, info = send(batch_text(batch), cfg)
    for r in batch:
        conn.execute("INSERT OR IGNORE INTO alerts(job_id, created, mode, text) VALUES (?,?,?,?)",
                     (r["id"], db.now(), "sent" if ok else "failed: " + info, text_for(r)))
    out["sent" if ok else "failed"] = len(batch)
    out["held"] = len(rows) - len(batch)
    conn.commit()
    return out
