"""Smaller ATSs with public JSON/RSS: Recruitee, Teamtailor, BambooHR, Breezy,
SmartRecruiters and Pinpoint.

SmartRecruiters' API host and Pinpoint's company subdomains disallow all
crawlers in robots.txt, so those two adapters exist but the polite client
returns "blocked" for them (left in so a robots change is picked up).
"""
import re

from .base import Result, from_response, html_text, iso_date, new_job, pay_range_text, rss_items, workplace_of


class Recruitee:
    ATS, PARTIAL = "recruitee", False

    @staticmethod
    def public_url(token):
        return "https://%s.recruitee.com/" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get("https://%s.recruitee.com/api/offers/" % token, etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        d = r.json()
        if not isinstance(d, dict) or "offers" not in d:
            return Result("none", error="no offers key")
        jobs, name = [], ""
        for j in d["offers"]:
            name = name or j.get("company_name") or ""
            sal = j.get("salary") or {}
            pay = pay_range_text(sal.get("min"), sal.get("max"), sal.get("currency") or "USD", sal.get("period") or "") if isinstance(sal, dict) and (sal.get("min") or sal.get("max")) else ""
            loc = j.get("location") or ", ".join(x for x in [j.get("city"), j.get("state_name") or j.get("state_code"), j.get("country")] if x)
            wp = "remote" if j.get("remote") else ("hybrid" if j.get("hybrid") else ("onsite" if j.get("on_site") else ""))
            jobs.append(new_job(j.get("id"), j.get("title"), j.get("careers_url"), apply_url=j.get("careers_apply_url") or "",
                                location=loc, workplace=wp, department=j.get("department") or "",
                                employment_type=j.get("employment_type_code") or "",
                                posted_at=iso_date(j.get("published_at") or j.get("created_at")),
                                description=html_text((j.get("description") or "") + "\n" + (j.get("requirements") or "")),
                                pay_text=pay, raw={k: v for k, v in j.items() if k not in ("description", "requirements", "translations")}))
        return Result("ok", jobs, name=name, url=Recruitee.public_url(token), etag=r.header("ETag"))

    @staticmethod
    def detail(client, token, job):
        return {}


class Teamtailor:
    ATS, PARTIAL = "teamtailor", False

    @staticmethod
    def public_url(token):
        return "https://%s.teamtailor.com/jobs" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        host = token if "." in token else token + ".teamtailor.com"
        r = client.get("https://%s/jobs.rss" % host, accept="application/rss+xml,application/xml,*/*", etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        if "<rss" not in r.text[:500] and "<feed" not in r.text[:500]:
            return Result("none", error="not an RSS feed")
        title = re.search(r"<title>(.*?)</title>", r.text[:3000], re.S)
        jobs = []
        for it in rss_items(r.text):
            link = it.get("link") or it.get("guid") or ""
            m = re.search(r"/jobs/(\d+)", link)
            loc = " / ".join(v for k, v in it.items() if k.startswith("x:locations.")) or it.get("x:locations", "")
            rem = it.get("remoteStatus") or it.get("x:remoteStatus") or ""
            jobs.append(new_job(m.group(1) if m else link, it.get("title"), link, location=loc,
                                workplace={"fully": "remote", "temporary": "remote", "hybrid": "hybrid", "none": "onsite"}.get(rem.lower(), workplace_of(rem)),
                                department=it.get("x:department", ""), posted_at=iso_date(it.get("pubDate")),
                                description=html_text(it.get("description") or ""), raw={k: v for k, v in it.items() if k != "description"}))
        name = re.sub(r"\s*[-–|].*$", "", (title.group(1).strip() if title else ""))
        return Result("ok", jobs, name=name, url="https://%s/jobs" % host, etag=r.header("ETag"), last_modified=r.header("Last-Modified"))

    @staticmethod
    def detail(client, token, job):
        return {}


class BambooHR:
    ATS, PARTIAL = "bamboohr", False

    @staticmethod
    def public_url(token):
        return "https://%s.bamboohr.com/careers" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get("https://%s.bamboohr.com/careers/list" % token, etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        d = r.json()
        if not isinstance(d, dict) or "result" not in d:
            return Result("none", error="not a BambooHR board")
        jobs = []
        for j in d["result"]:
            loc = j.get("location") or {}
            ats_loc = j.get("atsLocation") or {}
            where = ", ".join(x for x in [loc.get("city") or ats_loc.get("city"), loc.get("state") or ats_loc.get("state"),
                                          ats_loc.get("country")] if x)
            lt = str(j.get("locationType") or "")
            wp = "remote" if (j.get("isRemote") or lt == "1") else ("hybrid" if lt == "2" else "")
            jobs.append(new_job(j.get("id"), j.get("jobOpeningName"), "https://%s.bamboohr.com/careers/%s" % (token, j.get("id")),
                                location=where + (" (Remote)" if wp == "remote" else ""), workplace=wp,
                                department=j.get("departmentLabel") or "", employment_type=j.get("employmentStatusLabel") or "",
                                raw=j, needs_detail=True))
        return Result("ok", jobs, url=BambooHR.public_url(token), last_modified=r.header("Last-Modified"))

    @staticmethod
    def detail(client, token, job):
        r = client.get("https://%s.bamboohr.com/careers/%s/detail" % (token, job["ext_id"]))
        if not r.ok:
            return {"_detail_error": "HTTP %s" % r.status}
        d = ((r.json() or {}).get("result") or {}).get("jobOpening") or {}
        return {"description": html_text(d.get("description") or ""), "pay_text": d.get("compensation") or "",
                "posted_at": iso_date(d.get("datePosted")),
                "raw_detail": {k: d.get(k) for k in ("compensation", "datePosted", "minimumExperience", "locationType")}}


class Breezy:
    ATS, PARTIAL = "breezy", False

    @staticmethod
    def public_url(token):
        return "https://%s.breezy.hr/" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get("https://%s.breezy.hr/json" % token, etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        d = r.json()
        if not isinstance(d, list):
            return Result("none", error="not a list")
        jobs, name = [], ""
        for j in d:
            co = j.get("company") or {}
            name = name or (co.get("name") if isinstance(co, dict) else "") or ""
            loc = j.get("location") or {}
            locs = j.get("locations") or ([loc] if loc else [])
            where = " / ".join((l.get("name") or "") for l in locs if isinstance(l, dict))
            remote = any(isinstance(l, dict) and l.get("is_remote") for l in locs)
            sal = j.get("salary") or ""
            jobs.append(new_job(j.get("id"), j.get("name"), j.get("url"), location=where + (" (Remote)" if remote and "remote" not in where.lower() else ""),
                                workplace="remote" if remote else "", department=j.get("department") or "",
                                employment_type=((j.get("type") or {}).get("name") or "") if isinstance(j.get("type"), dict) else "",
                                posted_at=iso_date(j.get("published_date")), pay_text=sal if isinstance(sal, str) else "",
                                raw=j, needs_detail=True))
        return Result("ok", jobs, name=name, url=Breezy.public_url(token), etag=r.header("ETag"))

    @staticmethod
    def detail(client, token, job):
        if not job.get("url"):
            return {}
        r = client.get(job["url"], accept="text/html,*/*")
        if not r.ok:
            return {"_detail_error": "HTTP %s" % r.status}
        m = re.search(r'<div class="description"[^>]*>(.*?)</div>\s*(?:<div class="apply|<ul class="meta|</section)', r.text, re.S)
        return {"description": html_text(m.group(1) if m else r.text)[:20000]}


class SmartRecruiters:
    ATS, PARTIAL = "smartrecruiters", False

    @staticmethod
    def public_url(token):
        return "https://jobs.smartrecruiters.com/%s" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        jobs, name, offset = [], "", 0
        while True:
            r = client.get("https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100&offset=%d" % (token, offset),
                           etag=etag if offset == 0 else None)
            bad = from_response(r)
            if bad:
                return bad if offset == 0 else Result("ok", jobs, name=name, url=SmartRecruiters.public_url(token))
            d = r.json() or {}
            content = d.get("content") or []
            if offset == 0 and not content:
                return Result("none", error="empty board")
            for j in content:
                name = name or ((j.get("company") or {}).get("name") or "")
                l = j.get("location") or {}
                where = ", ".join(x for x in [l.get("city"), l.get("region"), l.get("country")] if x)
                jobs.append(new_job(j.get("id"), j.get("name"), "https://jobs.smartrecruiters.com/%s/%s" % (token, j.get("id")),
                                    location=where + (" (Remote)" if l.get("remote") else ""),
                                    workplace="remote" if l.get("remote") else ("hybrid" if l.get("hybrid") else ""),
                                    department=((j.get("department") or {}).get("label") or ""),
                                    posted_at=iso_date(j.get("releasedDate")), raw=j, needs_detail=True))
            offset += len(content)
            if offset >= int(d.get("totalFound") or 0) or not content or offset >= 1000:
                break
        return Result("ok", jobs, name=name, url=SmartRecruiters.public_url(token))

    @staticmethod
    def detail(client, token, job):
        r = client.get("https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % (token, job["ext_id"]))
        if not r.ok:
            return {"_detail_error": "HTTP %s" % r.status}
        d = r.json() or {}
        secs = ((d.get("jobAd") or {}).get("sections") or {})
        text = "\n\n".join(html_text((s or {}).get("text") or "") for s in secs.values() if isinstance(s, dict))
        return {"description": text, "apply_url": d.get("applyUrl") or ""}


class Pinpoint:
    ATS, PARTIAL = "pinpoint", False

    @staticmethod
    def public_url(token):
        return "https://%s.pinpointhq.com/" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get("https://%s.pinpointhq.com/postings.json" % token, etag=etag, last_modified=last_modified)
        bad = from_response(r)
        if bad:
            return bad
        d = r.json()
        if not isinstance(d, dict) or "data" not in d:
            return Result("none", error="no data key")
        jobs = []
        for j in d["data"]:
            loc = j.get("location") or {}
            jobs.append(new_job(j.get("id"), j.get("title"), j.get("url"),
                                location=loc.get("name", "") if isinstance(loc, dict) else str(loc),
                                workplace=(j.get("workplace_type") or "").lower().replace("on_site", "onsite"),
                                description=html_text((j.get("description") or "") + "\n" + (j.get("key_responsibilities") or "") +
                                                      "\n" + (j.get("skills_knowledge_expertise") or "") + "\n" + (j.get("benefits") or "")),
                                pay_text=pay_range_text(j.get("compensation_minimum"), j.get("compensation_maximum"),
                                                        j.get("compensation_currency") or "USD", j.get("compensation_frequency") or "")
                                if j.get("compensation_visible") else "",
                                raw={k: v for k, v in j.items() if k not in ("description", "benefits", "key_responsibilities",
                                                                             "skills_knowledge_expertise")}))
        return Result("ok", jobs, url=Pinpoint.public_url(token), etag=r.header("ETag"))

    @staticmethod
    def detail(client, token, job):
        return {}
