"""Discovery: company name (+ website) -> its ATS boards, validated, cached.

Two routes, the ones that work best in practice:
  1. the company's own website: homepage + careers page(s) -> ATS links/embeds
     (Greenhouse, Lever, Ashby, Workday, Rippling, Paylocity, PageUp, ...);
  2. slug guesses on the probe-able ATSs, accepted only when the board shows
     it is this company's (its own name, or job text naming the company, or an
     exact long slug for boards with no jobs to check).
Bot walls and 429s are "can't tell", never cached as "no board".
"""
import re
import urllib.parse

from . import adapters, boards as boards_mod, config, db, names, registry
from .net import is_bot_wall

CAREER_LINK = re.compile(r"career|jobs|join[-_ ]?(?:us|our|the|team)|work[-_ ]?(?:with|for|at)[-_ ]?us|open[-_ ]?(?:positions|roles)|"
                         r"opportunit|hiring|employment|we.?re hiring", re.I)
LEVEL = set("senior sr junior jr lead principal staff head chief director manager associate intern i ii iii iv 1 2 3 remote us usa "
            "hybrid onsite contract temporary full time part fulltime parttime the a an of and for to in".split())


def page_links(client, url):
    r = client.get(url, accept="text/html,application/xhtml+xml,*/*")
    if r.status == 0 and "robots" in r.error:
        return None, "", []
    if is_bot_wall(r):
        return "walled", "", []
    if not r.ok or not r.body:
        return None, "", []
    s = r.text
    links = []
    for href, inner in re.findall(r"<a\b[^>]*?href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", s, flags=re.S | re.I):
        links.append((urllib.parse.urljoin(r.url or url, href.strip()), re.sub(r"<[^>]+>", " ", inner).strip()))
    for src in re.findall(r"<iframe\b[^>]*?src=[\"']([^\"']+)[\"']", s, flags=re.I):
        links.append((urllib.parse.urljoin(r.url or url, src), "iframe"))
    return s, r.url or url, links


def careers_boards(client, website):
    """website -> (boards, pages read, walled?)"""
    if not website:
        return [], [], False
    w = website if website.startswith("http") else "https://" + website
    parts = urllib.parse.urlsplit(w)
    root = "%s://%s" % (parts.scheme or "https", parts.netloc)
    domain = parts.netloc.lower().removeprefix("www.")
    found, read, walled = [], [], False
    pages = []
    if parts.path and parts.path not in ("", "/"):
        pages.append(w)  # given a careers URL directly
    s, final, links = page_links(client, root)
    if s == "walled":
        walled = True
    elif s:
        read.append(final)
        found += boards_mod.boards_in(s)
        for u, t in links:
            host = urllib.parse.urlsplit(u).netloc.lower()
            if boards_mod.ATS_HOSTS.search(host):
                found += boards_mod.boards_in(u)
            elif (CAREER_LINK.search(urllib.parse.urlsplit(u).path) or CAREER_LINK.search(t)) and domain in host and u not in pages:
                pages.append(u)
    for guess in ("/careers", "/jobs", "/careers/", "/company/careers", "/about/careers", "/join-us"):
        if len(pages) >= 3:
            break
        if root + guess not in pages and root + guess.rstrip("/") not in pages:
            pages.append(root + guess)
    pages = list(dict.fromkeys(p.split("#")[0] for p in pages))
    for p in pages[:4]:
        if found and len(read) >= 2:
            break
        s2, f2, links2 = page_links(client, p)
        if s2 == "walled":
            walled = True
            continue
        if not s2:
            continue
        read.append(f2)
        found += boards_mod.boards_in(s2)
        nxt = []
        for u, t in links2:
            host = urllib.parse.urlsplit(u).netloc.lower()
            if boards_mod.ATS_HOSTS.search(host):
                found += boards_mod.boards_in(u)
            elif re.search(r"open (?:positions|roles)|view (?:all )?(?:jobs|openings|positions)|see (?:all )?(?:jobs|openings)|current openings|job openings|search jobs",
                           t, re.I) and u not in pages and u not in nxt:
                nxt.append(u)
        for u in nxt[:1]:  # one hop further: "see open positions"
            s3, f3, links3 = page_links(client, u)
            if s3 and s3 != "walled":
                read.append(f3)
                found += boards_mod.boards_in(s3) + boards_mod.boards_in(" ".join(x for x, _ in links3))
    uniq, seen = [], set()
    for b in found:
        k = (b["ats"], b["token"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(b)
    return uniq, read, walled


def _tokens(t):
    return [w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if w not in LEVEL]


def title_score(want, got):
    a, b = set(_tokens(want)), set(_tokens(got))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b) + (0.25 if (want or "").lower().strip() == (got or "").lower().strip() else 0)


def validate(company, ats, token, res, client=None, exact_slug=False):
    """Does this live board belong to `company`? -> (ok, how)"""
    if res.name:
        same = names.same_company(company, res.name)
        if same:
            return True, "board name '%s'" % res.name
        if same is False:
            return False, "board belongs to '%s'" % res.name
    texts = " ".join((j.get("description") or "")[:3000] + " " + (j.get("company") or "") for j in res.jobs[:8])
    if texts.strip() and names.mentions(company, texts):
        return True, "job text names the company"
    if ats == "rippling" and res.jobs and client is not None:
        d = adapters.get("rippling").detail(client, token, res.jobs[0])
        if d.get("company") and names.same_company(company, d["company"]):
            return True, "Rippling companyName '%s'" % d["company"]
        if d.get("company"):
            return False, "board belongs to '%s'" % d["company"]
    if ats == "bamboohr" and client is not None:
        r = client.get("https://%s.bamboohr.com/careers" % token, accept="text/html,*/*")
        t = re.search(r"<title>(.*?)</title>", r.text or "", re.S)
        if t and names.mentions(company, t.group(1)):
            return True, "careers page title"
    if exact_slug and not res.jobs:
        return True, "exact slug, empty board (unverified)"
    return False, "can't confirm the board is this company's"


def probe_slug(conn, client, company, ats, slug, cfg):
    cached = registry.probe_cached(conn, ats, slug, cfg["discover"]["miss_days"])
    if cached is not None and cached["status"] == "none":
        return None
    a = adapters.get(ats)
    res = a.fetch(client, slug) if ats != "workday" else a.fetch(client, slug, terms=[""])
    if res.status in ("blocked", "error"):
        return {"status": res.status, "error": res.error}
    if res.status == "none" or (res.status == "ok" and not res.jobs and not res.name and ats in ("bamboohr", "breezy", "recruitee", "teamtailor", "smartrecruiters")):
        registry.save_probe(conn, ats, slug, "none")
        conn.commit()  # never hold the write lock across network calls (8 discovery threads share the DB)
        return None
    registry.save_probe(conn, ats, slug, "live", res.name, len(res.jobs))
    conn.commit()
    compact = names.norm_company(company).replace(" ", "")
    exact = slug.lower().replace("-", "") == compact and len(compact) >= 6
    ok, how = validate(company, ats, slug, res, client, exact_slug=exact)
    return {"status": "live", "ok": ok, "how": how, "result": res}


def discover(conn, client, company, website="", title="", source="discover", cfg=None, probe=True, max_slugs=3, sources=""):
    cfg = cfg or config.cfg()
    out = {"company": company, "boards": [], "walled": False, "pages": [], "rejected": [], "blocked": []}
    found = []
    if website:
        bl, read, walled = careers_boards(client, website)
        out["pages"], out["walled"] = read, walled
        for b in bl:
            if adapters.get(b["ats"]) is None:
                continue
            res = adapters.get(b["ats"]).fetch(client, b["token"]) if b["ats"] != "workday" else \
                adapters.get("workday").fetch(client, b["token"], terms=[title or ""])
            if res.status in ("ok", "not_modified"):
                found.append((b["ats"], b["token"], res, "careers page"))
            elif res.status == "blocked":
                out["blocked"].append("%s:%s %s" % (b["ats"], b["token"], res.error))
    if probe and not found:
        slugs = names.slug_variants(company)[:max_slugs]
        probe_ats = list(cfg["discover"]["probe_ats"])
        if "workable" in (sources or "") and "workable" not in probe_ats:
            probe_ats.append("workable")  # Workable rate-limits hard: only probe it for names seen on Workable
        for ats in probe_ats:
            if ats == "workable" and client.host_dead("https://apply.workable.com/"):
                continue
            for s in (slugs if ats in ("greenhouse", "ashby", "lever") else slugs[:2]):
                p = probe_slug(conn, client, company, ats, s, cfg)
                if not p:
                    continue
                if p["status"] != "live":
                    out["blocked"].append("%s:%s %s" % (ats, s, p.get("error", "")))
                    break
                if p["ok"]:
                    found.append((ats, s, p["result"], "slug probe (%s)" % p["how"]))
                    break
                out["rejected"].append("%s:%s %s" % (ats, s, p["how"]))
    for ats, token, res, how in found:
        bid, created = registry.add_board(conn, company, ats, token, source + ":" + ("careers" if how == "careers page" else "probe"),
                                          board_name=res.name, careers_url=res.url or "",
                                          notes="unverified" if "unverified" in how else "")
        entry = {"ats": ats, "token": token, "url": res.url or adapters.get(ats).public_url(token), "jobs": len(res.jobs),
                 "how": how, "board_id": bid, "new": created}
        if title and res.jobs:
            best = max(res.jobs, key=lambda j: title_score(title, j.get("title")))
            sc = title_score(title, best.get("title"))
            if sc >= 0.5:
                entry["match"] = {"title": best.get("title"), "url": best.get("url"), "apply_url": best.get("apply_url"),
                                  "location": best.get("location"), "similarity": round(sc, 2)}
        out["boards"].append(entry)
    conn.execute("INSERT INTO candidates(norm, company, sources, website, priority, first_seen, probed_at, result) VALUES (?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(norm) DO UPDATE SET probed_at=excluded.probed_at, result=excluded.result, "
                 "website=COALESCE(NULLIF(candidates.website,''), excluded.website)",
                 (names.norm_company(company), company, db.jdump([source]), website or "", 5, db.now(), db.now(),
                  ("found:%d" % len(found)) if found else ("blocked" if (out["blocked"] or out["walled"]) and not out["rejected"] else "none")))
    conn.commit()
    return out
