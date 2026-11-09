"""Fast alerts: message you when a NEW job scores Strong.

alerts.channel picks the transport. Secrets are read at send time from
alerts.env_file (KEY=value lines), never stored in the repo or config.json:

* "discord_webhook": POST to a channel webhook (JOBATLAS_DISCORD_WEBHOOK).
  The simplest setup: Server Settings -> Integrations -> Webhooks.
* "discord_dm": a DM from a bot you own (JOBATLAS_DISCORD_TOKEN, and your user
  id in JOBATLAS_DISCORD_USER or alerts.discord_user_id). REST only
  (POST /users/@me/channels, then /channels/<id>/messages); no gateway session,
  so a bot that is also connected elsewhere is untouched.
* "discord_channel": the same bot posts in a server channel (alerts.discord_channel_id
  or JOBATLAS_DISCORD_CHANNEL); the bot needs Send Messages there.
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
import time
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from . import __version__, config, db, duel, profile

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


# adapters that read the application form's own questions (the others leave `questions` empty);
# Lever's form is read from its /apply page for matches only (poll.read_forms), so only once raw._form_read is set
FORM_ATS = ("greenhouse", "rippling", "neogov")
PAGE_FORM_ATS = ("lever",)
WRITTEN = re.compile(r"\b(why|describe|tell us|explain|share|what (?:excites|interests|draws|motivates|makes|would)|"
                     r"how (?:would|do|did|have) you|walk us|example|cover letter|in your own words|elaborate|summar\w*|"
                     r"motivat\w*|essay|in \d+ (?:words|sentences))\b", re.I)
FACT = re.compile(r"(linkedin|website|portfolio|github|\burl\b|\bname\b|pronoun|phone|e-?mail|address|\bcity\b|\bzip\b|country|"
                  r"located|location|how did you (?:hear|find|learn)|refer|current (?:or most recent )?(?:company|employer|title)|"
                  r"start date|available to start|availab|notice|salary|compensation|\bpay\b|expectation|authori[sz]|sponsor|visa|"
                  r"\bage\b|gender|race|ethnic|veteran|disabilit|hispanic|time ?zone)", re.I)
CHOICE_TYPES = ("knockout", "boolean", "date", "input_hidden", "yes_no_scale_4")


def written_questions(questions):
    """The form's required questions that need a written answer: not names, links, facts, yes/no or picks."""
    out = []
    for q in questions or []:
        if not isinstance(q, dict) or not q.get("required", True):
            continue
        label, t = (q.get("label") or "").strip(), (q.get("type") or "").lower()
        if any(x in t for x in ("select", "radio", "checkbox")) or t in CHOICE_TYPES:
            continue
        if t == "input_file":
            if re.search(r"cover letter", label, re.I):
                out.append("Cover letter")
            continue
        fact, written = bool(FACT.search(label)), bool(WRITTEN.search(label))
        if t in ("textarea", "long_answer"):
            if written or not fact:     # an address or a salary in a big text box is still a fact
                out.append(label)
        elif written and not fact:
            out.append(label)
    return out


def form_line(r):
    """One line on the application form: written answers needed, quick apply, or not read here."""
    page_read = r["ats"] in PAGE_FORM_ATS and "_form_read" in (db.jload(_get(r, "raw"), {}) or {})
    if r["ats"] not in FORM_ATS and not page_read:
        return "❔ Form not read (%s): check the page" % r["ats"].title()
    written = written_questions(db.jload(r["questions"], []))
    if not written:
        return "✅ Quick apply: no written answers"
    shown = "; ".join((w if len(w) <= 90 else w[:87] + "...") for w in written[:2])
    return "✍️ Written answers (%d): %s%s" % (len(written), shown, "; ..." if len(written) > 2 else "")


def _get(r, k):
    try:
        return r[k]
    except (KeyError, IndexError):
        return None


def md(s):
    """Escape Discord markdown in a title or company name."""
    return re.sub(r"([*_~|`>])", r"\\\1", s or "")


def posted_text(r, today=None):
    """"posted 3d ago" from the board's own date, else "found 3d ago" (when JobAtlas first saw it)."""
    day = (r["posted_at"] or "")[:10]
    verb = "posted" if day else "found"
    try:
        days = ((today or dt.date.today()) - dt.date.fromisoformat(day or (r["first_seen"] or "")[:10])).days
    except ValueError:
        return ""
    return "%s %s" % (verb, "today" if days <= 0 else "yesterday" if days == 1 else "%dd ago" % days)


def _col(r, k):
    try:
        return r[k] or ""
    except (KeyError, IndexError):
        return ""


def duel_note(r):
    """The head-to-head line candidates() attached to a row ("" when there is none)."""
    return _col(r, "_duel")


def resume_for(r, cfg):
    """The resume to send: the head-to-head's pick when there is one, else alerts.resume_by_field for the job's
    field. "" when alerts.resume_by_field isn't set (one resume, or no advice wanted)."""
    by_field = cfg.get("resume_by_field") or {}
    if not by_field:
        return ""
    return _col(r, "_resume") or by_field.get(_col(r, "field"), "")


DEFAULT_LABEL = "JobAtlas Strong"


def label_for(r, cfg):
    """alerts.label; with the default label, a job below strong is called by its own tier ("JobAtlas Good")."""
    label = cfg.get("label") or DEFAULT_LABEL
    tier = _col(r, "tier")
    return "JobAtlas " + tier.title() if label == DEFAULT_LABEL and tier and tier != "strong" else label


def text_for(r, cfg=None):
    """One job as a message. alerts.style "plain" drops the score and the scorer's reason; "card" is
    Discord markdown on one line plus the link. alerts.form adds a line about the application form,
    a job with a head-to-head verdict gets its line (alerts.duel_line), and alerts.resume_by_field
    adds which resume to send."""
    cfg = cfg or {}
    facts = db.jload(r["facts"], {}) or {}
    pay = ""
    if facts.get("pay_top"):
        pay = (" $%dk" % round(facts["pay_top"] / 1000)) if facts.get("pay_kind") == "year" else (" $%.0f/hr" % facts["pay_top"])
    where = (" (%s)" % r["location"][:60]) if r["location"] else ""
    label = label_for(r, cfg)
    form = ("\n" + form_line(r)) if cfg.get("form") else ""
    if resume_for(r, cfg):
        form += "\n📄 Send the %s resume" % resume_for(r, cfg)
    if cfg.get("duel_line", True) and duel_note(r):
        form += "\n⚖️ " + duel_note(r)
    style = cfg.get("style")
    if style == "card":
        body = " · ".join(x for x in ("**%s**" % md(r["title"]), md(r["company"]), (r["location"] or "")[:50],
                                      ("up to" + pay) if pay else "", posted_text(r)) if x) + form
    elif style == "plain":
        body = "%s: %s at %s%s%s%s" % (label, r["title"], r["company"], where, (", up to" + pay) if pay else "", form)
    else:
        why = (db.jload(r["reasons"], []) or [""])[0]
        body = "%s (%d): %s at %s%s%s. %s%s" % (label, r["score"], r["title"], r["company"], where, pay, why, form)
    url = r["url"] or ""
    return body[:max(80, 599 - len(url))] + "\n" + url      # the link is never cut off


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


def send_discord_channel(message, cfg):
    env = secrets(cfg)
    token = env.get("JOBATLAS_DISCORD_TOKEN", "")
    chan = str(cfg.get("discord_channel_id") or env.get("JOBATLAS_DISCORD_CHANNEL", "")).strip()
    if not (token and chan):
        return False, "missing JOBATLAS_DISCORD_TOKEN (alerts.env_file) / alerts.discord_channel_id"
    try:
        _post(DISCORD_API + "/channels/%s/messages" % chan, {"content": discord_text(message), "allowed_mentions": {"parse": []}},
              {"Authorization": "Bot " + token})
        return True, "discord channel ok"
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
    if ch == "discord_channel":
        return send_discord_channel(message, cfg)
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


def batch_text(rows, cfg=None, limit=1500):
    cfg = cfg or {}
    style = cfg.get("style")
    if len(rows) == 1 and style != "card":
        return text_for(rows[0], cfg)
    label = cfg.get("label") or "JobAtlas Strong"
    if style == "card":
        lines = ["**%s** (%d)" % (label, len(rows)) if len(rows) > 1 else "**%s**" % label]
        lines += [x for r in rows for x in ("", text_for(r, cfg))]
    elif style == "plain":
        lines = ["%s: %d new matches" % (label, len(rows))]
        lines += [x for r in rows for x in ("", text_for(r, cfg).split(": ", 1)[1])]
    else:
        if label == DEFAULT_LABEL:
            tiers = [_col(r, "tier") or "strong" for r in rows]
            kinds = ", ".join("%d %s" % (tiers.count(t), t) for t in ("strong", "good", "fair") if t in tiers)
            lines = ["JobAtlas: %d strong new matches" % len(rows)] if set(tiers) == {"strong"} else ["JobAtlas: %d new matches (%s)" % (len(rows), kinds)]
        else:
            lines = ["%s: %d new matches" % (label, len(rows))]
        lines += [x for r in rows for x in ("", text_for(r, cfg).replace(label_for(r, cfg) + " ", "", 1))]
    return "\n".join(lines)[:limit]


def fit_batch(rows, cfg):
    """As many of rows (up to alerts.max_per_run) as fit in one message without cutting a link off."""
    limit = 1900 if (cfg.get("channel") or "discord_webhook").startswith("discord") else 1500
    batch = list(rows[: max(1, int(cfg.get("max_per_run") or 5))])
    while len(batch) > 1 and len(batch_text(batch, cfg, limit=10 ** 6)) > limit:
        batch.pop()
    return batch, limit


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


def candidates(conn, name, cfg):
    """-> (rows, dups): jobs for this profile's alerts, best first, one per company + title (dups: the rest).

    Jobs first seen in the last alerts.lookback_hours (24) at alerts.min_tier or better, not in the
    ledger and not alerted yet. A job a board listed on its first (baseline) poll counts only if it was
    posted in the last alerts.baseline_days (3). While alerts are on, jobs only recorded while they
    were off (mode "disabled") count as not sent."""
    min_rank = {"strong": 0, "good": 1, "fair": 2}.get(cfg.get("min_tier", "strong"), 0)
    tiers = [t for t, i in (("strong", 0), ("good", 1), ("fair", 2)) if i <= min_rank]
    utc = dt.datetime.now(dt.timezone.utc)
    since = (utc - dt.timedelta(hours=int(cfg.get("lookback_hours", 24)))).isoformat(timespec="seconds")
    retry = (utc - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    handled = ("(mode NOT LIKE 'failed%%' AND mode<>'disabled') OR (mode LIKE 'failed%%' AND created>?)" if cfg.get("enabled")
               else "mode NOT LIKE 'failed%%' OR created>?")
    src, args = db.scored(conn, name)
    rows = conn.execute(
        "SELECT * FROM %s WHERE first_seen>=? AND tier IN (%s) AND closed_at IS NULL AND (seen IS NULL OR seen='') "
        "AND (baseline=0 OR posted_at>=date('now',?)) "
        "AND id NOT IN (SELECT job_id FROM alerts WHERE profile=? AND (%s)) "
        "AND score>=? ORDER BY score DESC" % (src, ",".join("?" * len(tiers)), handled),
        args + [since] + tiers + ["-%d day" % int(cfg.get("baseline_days", 3)), name, retry, int(cfg.get("min_score") or 0)]).fetchall()
    pat = cfg.get("skip_titles", DEFAULT_SKIP_TITLES)
    rows = [r for r in rows if title_ok(r["title"], pat)]
    # a new job that fits both sides of the duel waits (duel.hold_minutes at most) for its verdict
    held = duel.pending(conn)
    if held:
        rows = [r for r in rows if r["id"] not in held]
    notes = duel.lookup(conn, [r["id"] for r in rows], name)
    rows = [dict(r, _duel=notes[r["id"]]["line"] if r["id"] in notes else "", _resume=(notes.get(r["id"]) or {}).get("resume") or "")
            for r in rows]
    # one line per company + title: the same job posted in several locations is alerted once
    keys, dups = set(), {}
    for r in list(rows):
        k = ((r["company"] or "").lower(), (r["title"] or "").lower())
        if k in keys:
            rows.remove(r)
            dups.setdefault(k, []).append(r)
        keys.add(k)
    return rows, dups


def process_one(conn, name, cfg):
    """Message about Strong jobs first seen in the last day (not already alerted or in the ledger).

    Called right after each poll cycle scores its new jobs. Policy (all optional):
      * only score >= alerts.min_score and titles not matching alerts.skip_titles;
      * quiet hours alerts.quiet_start..quiet_end in alerts.tz: nothing is sent or recorded,
        so overnight finds go out on the first run after quiet hours end;
      * at most alerts.daily_cap sent jobs per local day (0 = no cap), highest scores first,
        and up to alerts.max_per_run jobs bundled into one message (fewer if they don't fit).
    A failed send is retried after 5 minutes. With alerts.enabled false, candidates are
    only recorded (mode "disabled"). `name` is the profile, `cfg` its alert settings.
    """
    now_local = local_now(cfg.get("tz") or "")
    rows, dups = candidates(conn, name, cfg)
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
    if not room:
        out["held"] = len(rows)
        log.info("daily cap reached%s (%d); holding %d alert(s)", who, sent_today, len(rows))
        return out
    batch, limit = fit_batch(rows[:room], cfg)
    ok, info = send(batch_text(batch, cfg, limit), cfg)
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


def mark_backlog(conn, name, ids):
    """Record jobs as handled for this profile's alerts (mode "backlog") without sending them. For the catch-up
    after a profile change: jobs that only now fit were not found in real time, so they shouldn't post to a
    live channel; send them as one digest (an email) instead, if at all. Jobs already in the alerts table for
    this profile are left as they are. -> number of jobs marked."""
    n = 0
    for i, jid in enumerate(ids):
        n += conn.execute("INSERT OR IGNORE INTO alerts(job_id, profile, created, mode, text) VALUES (?,?,?,'backlog','')",
                          (jid, name, db.now())).rowcount
        if i % 200 == 199:
            conn.commit()
    conn.commit()
    return n


def catchup(conn, name, cfg, hours, dry_run=False, pause=1.5):
    """Send everything this profile's alerts would have sent over the last `hours` (e.g. when they are first
    turned on), best first, as many messages as it takes. Quiet hours and the daily cap don't apply.
    Mind the volume: dozens of jobs at once read badly in a chat; `--dry-run` counts first.
    -> {"candidates", "sent", "messages", "failed"}."""
    c = dict(cfg, lookback_hours=hours, baseline_days=max(3, hours // 24), quiet_start=None, quiet_end=None, daily_cap=0)
    out = {"candidates": len(candidates(conn, name, c)[0]), "sent": 0, "messages": 0, "failed": 0}
    if dry_run or not out["candidates"]:
        return out
    if not cfg.get("enabled"):
        raise ValueError("alerts for %s are off: set enabled first" % name)
    while True:
        o = process_one(conn, name, c)
        out["sent"] += o["sent"]
        out["messages"] += 1 if o["sent"] else 0
        if not o["sent"] or not o["held"]:
            out["failed"] += o["failed"]
            return out
        time.sleep(pause)      # Discord allows 5 messages per 5 s in a channel
