"""Two games-industry job boards, read the way a person's browser would (2026-11-11, Nathan's call).

Hitmarker  (hitmarker.net/jobs): the jobs list is client-rendered, but the site publishes a job sitemap
           (newest first, with lastmod) and every job page carries schema.org JobPosting JSON-LD.
           fetch() reads the sitemap (one request); detail() reads a job page, and only for jobs that
           passed the title prefilter. The slug gives a provisional title, the page the real title and company.
Work With Indies (workwithindies.com): a Webflow site with an RSS feed of the newest 100 jobs; each job
           page carries JobPosting JSON-LD (apply link, valid-through) and a "closed" banner.

Both are personal-search reads: listing page plus detail pages for new jobs, spaced out, polled twice a day
(poll.ats_interval_hours), robots.txt honoured. Nothing is republished. The aggregators' job pages link on to
the employer's own posting, so the shown ledger dedupes them against the ATS boards by company and title.
"""
import datetime
import json
import re
import xml.etree.ElementTree as ET

from .base import Result, from_response, html_text, iso_date, new_job, pay_range_text, rss_items, workplace_of

COUNTRIES = {
    "US": "United States", "CA": "Canada", "GB": "United Kingdom", "UK": "United Kingdom", "IE": "Ireland", "DE": "Germany",
    "FR": "France", "ES": "Spain", "PT": "Portugal", "IT": "Italy", "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland",
    "AT": "Austria", "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland", "CZ": "Czech Republic",
    "RO": "Romania", "BG": "Bulgaria", "HU": "Hungary", "GR": "Greece", "TR": "Turkey", "UA": "Ukraine", "EE": "Estonia",
    "LT": "Lithuania", "LV": "Latvia", "RS": "Serbia", "HR": "Croatia", "AU": "Australia", "NZ": "New Zealand", "JP": "Japan",
    "KR": "South Korea", "CN": "China", "TW": "Taiwan", "HK": "Hong Kong", "SG": "Singapore", "IN": "India", "MY": "Malaysia",
    "TH": "Thailand", "PH": "Philippines", "ID": "Indonesia", "VN": "Vietnam", "BR": "Brazil", "MX": "Mexico", "AR": "Argentina",
    "CL": "Chile", "CO": "Colombia", "PE": "Peru", "ZA": "South Africa", "IL": "Israel", "AE": "United Arab Emirates",
}
US_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
             "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
             "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}
_LD = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def country_name(c):
    c = (c or "").strip()
    return COUNTRIES.get(c.upper(), c) if len(c) == 2 else c


def jobposting(html):
    """The first schema.org JobPosting object in a page's JSON-LD, or {}."""
    for blob in _LD.findall(html or ""):
        try:
            d = json.loads(blob, strict=False)
        except ValueError:
            try:    # Webflow's CMS leaves stray backslashes ("target=\\_blank") in the description
                d = json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", blob), strict=False)
            except ValueError:
                continue
        for x in d if isinstance(d, list) else [d]:
            if isinstance(x, dict) and x.get("@type") == "JobPosting":
                return x
    return {}


def _list(v):
    return v if isinstance(v, list) else ([v] if v else [])


def ld_fields(d):
    """JobPosting JSON-LD -> the detail dict poll.fetch_details merges into the job row."""
    places = [p.get("address") or {} for p in _list(d.get("jobLocation")) if isinstance(p, dict)]
    req = [country_name(r.get("name")) for r in _list(d.get("applicantLocationRequirements")) if isinstance(r, dict) and r.get("name")]
    remote = str(d.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    parts = []
    for a in places:
        s = ", ".join(x for x in [a.get("addressLocality"), a.get("addressRegion"), country_name(a.get("addressCountry"))] if x)
        if s and s not in parts:
            parts.append(s)
    where = " | ".join(parts)
    if remote:
        # remote: who may apply is the applicant requirement; the posting address is only the employer's base
        us = any(r.upper() in ("US", "USA", "UNITED STATES") for r in req)
        if req and not us:
            shown = ", ".join(req[:4]) + (" +%d more" % (len(req) - 4) if len(req) > 4 else "")
            loc = "Remote, other countries only (%s)" % shown
        else:
            loc = "Remote (%s)" % ", ".join(req[:6]) if req else "Remote"
    else:
        loc = where
    sal = d.get("baseSalary") if isinstance(d.get("baseSalary"), dict) else {}
    val = sal.get("value") if isinstance(sal.get("value"), dict) else {}
    pay = pay_range_text(val.get("minValue"), val.get("maxValue") if val.get("maxValue") != val.get("minValue") else None,
                         sal.get("currency") or "USD", str(val.get("unitText") or "")) if (val.get("minValue") or val.get("maxValue")) else ""
    et = "/".join(str(x).replace("_", " ").title() for x in _list(d.get("employmentType")))
    org = d.get("hiringOrganization") if isinstance(d.get("hiringOrganization"), dict) else {}
    cats = d.get("occupationalCategory")
    out = {"title": html_text(d.get("title") or ""), "company": (org.get("name") or "").strip(), "location": loc,
           "workplace": "remote" if remote else ("onsite" if places else ""), "employment_type": et,
           "posted_at": iso_date(d.get("datePosted")), "pay_text": pay, "description": html_text(d.get("description") or ""),
           "department": html_text(", ".join(_list(cats)) if isinstance(cats, (list, str)) else "")}
    if remote and where:
        out["raw_detail"] = {"employer_base": where}
    nonus = [a.get("addressCountry") for a in places if a.get("addressCountry")]
    if remote and req and not any(r.upper() in ("US", "USA", "UNITED STATES") for r in req):
        out["raw_detail"] = {"country": {"descriptor": next((r for r in req if len(r) > 2), req[0])}, "employer_base": where}
    if not remote and nonus and not any(str(c).upper() in ("US", "USA") for c in nonus):
        out["raw_detail"] = {"country": {"descriptor": country_name(nonus[0])}}
    return {k: v for k, v in out.items() if v}


class Hitmarker:
    """token = "jobs" (one board). Sitemap-driven; PARTIAL because the sitemap is a rolling window."""
    ATS, PARTIAL = "hitmarker", True
    SITEMAP = "https://hitmarker.net/sitemap-jobs.xml/p1"
    DAYS = 7

    @staticmethod
    def public_url(token):
        return "https://hitmarker.net/jobs"

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get(Hitmarker.SITEMAP, accept="application/xml,text/xml,*/*", etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        if "<urlset" not in r.text[:500]:
            return Result("error", error="sitemap is not a urlset (site changed?): " + r.text[:80].replace("\n", " "))
        try:
            root = ET.fromstring(r.body)
        except ET.ParseError as e:
            return Result("error", error="sitemap parse: %s" % e)
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=Hitmarker.DAYS)).date().isoformat()
        jobs = []
        for u in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
            loc = (u.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc") or "").strip()
            m = re.search(r"/jobs/([a-z0-9-]+?)-(\d+)$", loc)
            if not m or (iso_date(u.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")) or "9") < cutoff:
                continue
            title = m.group(1).replace("-", " ").title()
            jobs.append(new_job(m.group(2), title, loc, needs_detail=True, provisional=True))
        if not jobs:
            return Result("error", error="sitemap held no job URLs")
        return Result("ok", jobs, name="Hitmarker", url=Hitmarker.public_url(token), etag=r.header("ETag"),
                      last_modified=r.header("Last-Modified"))

    @staticmethod
    def detail(client, token, job):
        r = client.get(job["url"], accept="text/html,*/*")
        if r.status in (404, 410):
            return {"_closed": True}
        if not r.ok:
            return {"_detail_error": "HTTP %s %s" % (r.status, (r.error or "")[:80])}
        d = jobposting(r.text)
        if not d:
            return {"_detail_error": "no JobPosting JSON-LD on the page"}
        out = ld_fields(d)
        if not out.get("location"):     # no address in the JSON-LD: the page's own location chip
            m = re.search(r'location-label="([^"]+)"', r.text)
            if m:
                out["location"] = html_text(m.group(1))
        return out


# "<Company> is hiring a(n) <Title> to work from <where>"
WWI_TITLE = re.compile(r"^\s*(?P<co>.+?) is hiring (?:an? )?(?P<title>.+?) to work from (?P<where>.+?)\s*$", re.I)
_TZ = re.compile(r"(±|\+/-|time zones?\b|\b[A-Z]{1,4}[ST]?\s*[±+-]\s*\d)", re.I)


class WorkWithIndies:
    """token = "careers". RSS feed (newest 100); PARTIAL because older postings fall off the feed."""
    ATS, PARTIAL = "workwithindies", True
    FEED = "https://www.workwithindies.com/careers/rss.xml"

    @staticmethod
    def public_url(token):
        return "https://www.workwithindies.com/"

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get(WorkWithIndies.FEED, accept="application/rss+xml,application/xml,*/*", etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        items = rss_items(r.text) if "<rss" in r.text[:600] else []
        if not items:
            return Result("error", error="feed is not RSS or is empty (site changed?): " + r.text[:80].replace("\n", " "))
        jobs = []
        for it in items:
            link = it.get("link") or it.get("guid") or ""
            slug = link.rstrip("/").rsplit("/", 1)[-1]
            if not slug:
                continue
            m = WWI_TITLE.match(it.get("title") or "")
            company, title, where = (m.group("co"), m.group("title"), m.group("where")) if m else ("", it.get("title") or slug, "")
            wp, loc = "", where
            if where:
                if re.fullmatch(r"anywhere", where, re.I):
                    wp, loc = "remote", "Remote (Anywhere)"
                elif _TZ.search(where):
                    wp, loc = "remote", "Remote (%s)" % where
                else:
                    wp = workplace_of(where)
            jobs.append(new_job(slug, title, link, company=company.strip(), location=loc, workplace=wp,
                                posted_at=iso_date(it.get("pubDate")), description="",
                                raw={"rss_description": html_text(it.get("description") or "")}, needs_detail=True, provisional=True))
        return Result("ok", jobs, name="Work With Indies", url=WorkWithIndies.public_url(token), etag=r.header("ETag"),
                      last_modified=r.header("Last-Modified"))

    @staticmethod
    def detail(client, token, job):
        r = client.get(job["url"], accept="text/html,*/*")
        if r.status in (404, 410):
            return {"_closed": True}
        if not r.ok:
            return {"_detail_error": "HTTP %s %s" % (r.status, (r.error or "")[:80])}
        d = jobposting(r.text)
        if not d:
            return {"_detail_error": "no JobPosting JSON-LD on the page"}
        out = ld_fields(d)
        # the apply button's target; the JSON-LD contact URL is the same link
        contact = d.get("applicationContact") if isinstance(d.get("applicationContact"), dict) else {}
        m = re.search(r'id="apply-top"\s+href="([^"]+)"', r.text)
        apply = (m.group(1) if m else contact.get("url") or "").replace("&amp;", "&")
        if apply:
            out["apply_url"] = apply
        # Webflow renders the banner always; w-condition-invisible hides it while the job is open
        cl = re.search(r'class="closed-notif([^"]*)"', r.text)
        if cl and "w-condition-invisible" not in cl.group(1):
            out["_closed"] = True
        # "where" in the feed title is the poster's own wording; keep it when the page's address says less
        if "Anywhere" in (job.get("location") or "") or (job.get("location") and (not out.get("location") or out["location"] == "Remote")):
            out.pop("location", None)
        return out
