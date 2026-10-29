"""Employer feeds that aren't JSON APIs: PageUp (universities: RSS), Paylocity
(board page carries its jobs as JSON), Jobvite (XML feed), NEOGOV (government
RSS, robots-gated), CivicPlus (city/port RSS) and ApplicantPro (JSON).

NEOGOV (governmentjobs.com / schooljobs.com: state, county and city agencies and
community colleges) answers robots.txt with `User-agent: * Disallow: /` (only
named search engines are allowed), so it isn't crawled unless you add the host
to http.robots_override. ctcLink (PeopleSoft) disallows everything and isn't supported.
"""
import json
import re

from .base import Result, from_response, html_text, iso_date, new_job, rss_items, workplace_of


class PageUp:
    """token = "<instance id>" or "<instance id>/<lang>", e.g. "793"."""
    ATS, PARTIAL = "pageup", False

    @staticmethod
    def _base(token):
        inst, _, lang = token.partition("/")
        return "https://careers.pageuppeople.com/%s/cw/%s" % (inst, lang or "en-us")

    @staticmethod
    def public_url(token):
        return PageUp._base(token) + "/listing/"

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get(PageUp._base(token) + "/rss", accept="application/rss+xml,*/*", etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        if "<rss" not in r.text[:400]:
            return Result("none", error="not RSS")
        jobs = []
        for it in rss_items(r.text):
            link = it.get("link") or it.get("guid") or ""
            m = re.search(r"/job/(\d+)", link)
            # PageUp puts a short summary in <description> and the full ad in <job:description>
            full = it.get("x:description") or it.get("description") or ""
            loc = it.get("x:location", "")
            jobs.append(new_job(m.group(1) if m else (it.get("x:refNo") or link), it.get("title"), link,
                                apply_url=it.get("x:applyLink", ""), location=loc, workplace=workplace_of(loc),
                                department=it.get("x:category", ""), employment_type=it.get("x:workType", ""),
                                posted_at=iso_date(it.get("pubDate") or it.get("updated")),
                                description=html_text(full),
                                raw={k: v for k, v in it.items() if k not in ("description", "x:description")}))
        return Result("ok", jobs, url=PageUp.public_url(token), etag=r.header("ETag"), last_modified=r.header("Last-Modified"))

    @staticmethod
    def detail(client, token, job):
        return {}


class Paylocity:
    """token = company GUID from recruiting.paylocity.com/Recruiting/Jobs/All/<guid>."""
    ATS, PARTIAL = "paylocity", False

    @staticmethod
    def public_url(token):
        return "https://recruiting.paylocity.com/Recruiting/Jobs/All/%s" % token

    @staticmethod
    def _feed(client, token):
        """v2 JSON feed (works for some tenants when the board page doesn't)."""
        r = client.get("https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/%s" % token)
        d = r.json() if r.ok else None
        if not isinstance(d, dict) or not d.get("jobs"):
            return None
        jobs = []
        for j in d["jobs"]:
            loc = j.get("jobLocation") or {}
            where = ", ".join(x for x in [loc.get("city"), loc.get("state")] if x) if isinstance(loc, dict) else ""
            remote = bool(j.get("isRemote"))
            jid = j.get("jobId")
            jobs.append(new_job(jid, j.get("title"), "https://recruiting.paylocity.com/Recruiting/Jobs/Details/%s" % jid,
                                apply_url=j.get("applyUrl") or "", location=where + (" (Remote)" if remote else ""),
                                workplace="remote" if remote else workplace_of(where), department=j.get("hiringDepartment") or "",
                                posted_at=iso_date(j.get("publishedDate") or j.get("createdUtc")),
                                description=html_text(j.get("description") or ""), company=j.get("companyName") or "",
                                raw={k: v for k, v in j.items() if k != "description"}))
        return Result("ok", jobs, name=(d.get("displayName") or "").strip(" ,"), url=Paylocity.public_url(token))

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get(Paylocity.public_url(token), accept="text/html,*/*")
        bad = from_response(r)
        if bad:
            return bad
        if "JobNotFound" in (r.url or "") or "pageData" not in r.text:
            return Paylocity._feed(client, token) or Result("none", error="no Paylocity board or feed")
        s = r.text
        m = re.search(r"window\.pageData\s*=\s*(\{.*?\});\s*</script>", s, re.S)
        rows = []
        if m:
            try:
                rows = json.loads(m.group(1)).get("Jobs") or []
            except ValueError:
                rows = []
        if not rows:  # fallback: the resolver's regex over embedded job objects
            for o in re.finditer(r"\{\"JobId\":(\d+),\"JobTitle\":\"((?:[^\"\\]|\\.)*)\",\"LocationName\":\"((?:[^\"\\]|\\.)*)\"[^{}]*?\"PublishedDate\":\"([^\"]*)\"", s):
                rows.append({"JobId": int(o.group(1)), "JobTitle": json.loads('"' + o.group(2) + '"'),
                             "LocationName": json.loads('"' + o.group(3) + '"'), "PublishedDate": o.group(4)})
        if not rows and "Jobs/All" not in r.url and "pageData" not in s:
            return Result("none", error="not a Paylocity board")
        name = ""
        t = re.search(r"<title>\s*(.*?)\s*</title>", s, re.S)
        if t:
            name = re.sub(r"\s*[-|]\s*Current Openings.*$|^Current Openings\s*[-|]\s*", "", html_text(t.group(1)), flags=re.I).strip()
        jobs = []
        for j in rows:
            loc = j.get("LocationName") or ""
            jl = j.get("JobLocation") or {}
            if isinstance(jl, dict):
                loc = ", ".join(x for x in [jl.get("City"), jl.get("State")] if x) or loc
            jobs.append(new_job(j.get("JobId"), j.get("JobTitle"), "https://recruiting.paylocity.com/Recruiting/Jobs/Details/%s" % j.get("JobId"),
                                location=loc + (" (Remote)" if j.get("IsRemote") else ""),
                                workplace="remote" if j.get("IsRemote") else workplace_of(loc),
                                department=j.get("HiringDepartment") or "", posted_at=iso_date(j.get("PublishedDate")),
                                raw=j, needs_detail=True))
        return Result("ok", jobs, name=name, url=Paylocity.public_url(token))

    @staticmethod
    def detail(client, token, job):
        r = client.get(job["url"], accept="text/html,*/*")
        if not r.ok:
            return {"_detail_error": "HTTP %s" % r.status}
        m = re.search(r'<div class="job-preview-details"[^>]*>(.*?)<div class="job-preview-footer', r.text, re.S) or \
            re.search(r'<div[^>]+class="[^"]*job-listing-header[^"]*".*?(<div.*)</body>', r.text, re.S)
        text = html_text(m.group(1) if m else r.text)
        if "Job Not Found" in r.text[:3000]:
            return {"_closed": True}
        return {"description": text[:20000]}


class Jobvite:
    """token = company slug in jobs.jobvite.com/<slug>; reads the public XML feed."""
    ATS, PARTIAL = "jobvite", False

    @staticmethod
    def public_url(token):
        return "https://jobs.jobvite.com/%s/jobs" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get("https://jobs.jobvite.com/%s/feed" % token, accept="application/rss+xml,application/xml,*/*")
        bad = from_response(r)
        if bad:
            return bad
        items = rss_items(r.text)
        if not items and "<rss" not in r.text[:400]:
            return Result("none", error="no feed")
        jobs = []
        for it in items:
            link = it.get("link") or ""
            m = re.search(r"/job/([A-Za-z0-9]+)", link)
            jobs.append(new_job(m.group(1) if m else link, it.get("title"), link, location=it.get("location", ""),
                                posted_at=iso_date(it.get("pubDate")), description=html_text(it.get("description", "")),
                                raw={k: v for k, v in it.items() if k != "description"}))
        return Result("ok", jobs, url=Jobvite.public_url(token))

    @staticmethod
    def detail(client, token, job):
        return {}


class Neogov:
    """NEOGOV agency feed: www.governmentjobs.com/SearchEngine/JobsFeed?agency=<slug>.

    The feed works (full text, salary, close date), BUT governmentjobs.com's
    robots.txt is `User-agent: * / Disallow: /` (only named search engines such
    as Googlebot and IndeedJobBot are allowed), so the polite client blocks it.
    It runs only if "www.governmentjobs.com" is in http.robots_override
    (config.json). An unknown slug returns 200 with zero items.
    Supplemental questionnaire: /careers/<slug>/jobs/newprint/<id> (same gate).
    """
    ATS, PARTIAL = "neogov", False
    # token "<slug>" = governmentjobs.com; "schooljobs:<slug>" = schooljobs.com (community colleges).
    # schooljobs.com's robots.txt is also "User-agent: * / Disallow: /".

    @staticmethod
    def _host(token):
        return ("www.schooljobs.com", token.split(":", 1)[1]) if token.startswith("schooljobs:") else ("www.governmentjobs.com", token)

    @staticmethod
    def public_url(token):
        host, slug = Neogov._host(token)
        return "https://%s/careers/%s" % (host, slug)

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        host, slug = Neogov._host(token)
        r = client.get("https://%s/SearchEngine/JobsFeed?agency=%s" % (host, slug), accept="application/rss+xml,application/xml,*/*",
                       etag=etag, last_modified=last_modified, timeout=90)
        bad = from_response(r)
        if bad:
            if "robots" in bad.error:
                bad.error = "robots.txt disallows all but named search engines (see http.robots_override)"
            return bad
        items = rss_items(r.text)
        if not items:
            return Result("none" if "<rss" in r.text[:500] else "error", error="feed has no items")
        jobs = []
        for it in items:
            link = it.get("link") or it.get("guid") or ""
            m = re.search(r"/jobs/(\d+)", link)

            def pick(*keys):
                for k, v in it.items():
                    if any(key in k.lower() for key in keys) and v:
                        return v
                return ""
            loc = pick("location", "city")
            sal = pick("salary", "pay")
            jobs.append(new_job(m.group(1) if m else link, it.get("title"), link, location=loc,
                                workplace=workplace_of(it.get("title", ""), loc, it.get("description", "")[:3000]) if re.search(r"telework|remote", it.get("description", ""), re.I) else "onsite",
                                department=pick("department", "category"), employment_type=pick("jobtype", "type"),
                                posted_at=iso_date(it.get("pubDate") or pick("posted", "open")), pay_text=sal,
                                description=html_text(it.get("description") or ""),
                                raw={k: v for k, v in it.items() if k != "description"}))
        return Result("ok", jobs, url=Neogov.public_url(token), etag=r.header("ETag"), last_modified=r.header("Last-Modified"))

    @staticmethod
    def detail(client, token, job):
        host, slug = Neogov._host(token)
        r = client.get("https://%s/careers/%s/jobs/newprint/%s" % (host, slug, job["ext_id"]), accept="text/html,*/*")
        if not r.ok:
            return {}
        text = html_text(r.text)
        i = text.lower().find("supplemental questions")
        qs = []
        if i >= 0:
            for ln in text[i:i + 6000].split("\n")[1:]:
                m = re.match(r"\s*(\d{1,2})\.\s+(.{8,400})", ln)
                if m:
                    qs.append({"label": m.group(2).strip(), "required": True, "type": "", "options": [], "description": ""})
        return {"questions": qs[:40]} if qs else {}


class CivicPlus:
    """CivicPlus city and port sites: token = host; RSSFeed.aspx?ModID=66 = Jobs."""
    ATS, PARTIAL = "civicplus", False

    @staticmethod
    def public_url(token):
        return "https://%s/Jobs.aspx" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get("https://%s/RSSFeed.aspx?ModID=66&CID=All-0" % token, accept="application/rss+xml,*/*",
                       etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        if "<rss" not in r.text[:600]:
            return Result("none", error="not RSS")
        jobs = []
        for it in rss_items(r.text):
            link = it.get("link") or ""
            m = re.search(r"JobID=(\d+)", link, re.I)
            desc = html_text(it.get("description") or "")
            jobs.append(new_job(m.group(1) if m else link, it.get("title"), link, location=token,
                                workplace="remote" if re.search(r"telework|remote", desc, re.I) else "onsite",
                                posted_at=iso_date(it.get("pubDate")), description=desc,
                                raw={k: v for k, v in it.items() if k != "description"}))
        return Result("ok", jobs, url=CivicPlus.public_url(token), etag=r.header("ETag"))

    @staticmethod
    def detail(client, token, job):
        return {}


class ApplicantPro:
    """ApplicantPro: token = "<subdomain>|<domain id>", list = https://<sub>.applicantpro.com/core/jobs/<id>?getParams={}"""
    ATS, PARTIAL = "applicantpro", False

    @staticmethod
    def public_url(token):
        return "https://%s.applicantpro.com/jobs/" % token.split("|")[0]

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        sub, _, did = token.partition("|")
        r = client.get("https://%s.applicantpro.com/core/jobs/%s?getParams=%%7B%%7D" % (sub, did))
        bad = from_response(r)
        if bad:
            return bad
        d = r.json() or {}
        if not d.get("success") and not (d.get("data") or {}).get("jobs") is not None:
            return Result("none", error="not an ApplicantPro board")
        jobs = []
        for j in (d.get("data") or {}).get("jobs") or []:
            jid = j.get("id") or j.get("jobId")
            loc = j.get("jobLocation") or ", ".join(x for x in [j.get("city"), j.get("abbreviation") or j.get("state")] if x)
            jobs.append(new_job(jid, j.get("title") or j.get("jobTitle"), j.get("jobUrl") or "https://%s.applicantpro.com/jobs/%s.html" % (sub, jid),
                                location=loc, workplace=(j.get("workplaceType") or "").lower(), employment_type=j.get("employmentType") or "",
                                posted_at=iso_date(j.get("startDate") or j.get("postingDate")), pay_text=j.get("payInfo") or "",
                                raw=j))
        return Result("ok", jobs, url=ApplicantPro.public_url(token))

    @staticmethod
    def detail(client, token, job):
        return {}
