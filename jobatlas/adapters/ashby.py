"""Ashby Posting API (public): api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true

Descriptions and compensation are in the list. Ashby's application form lives
behind jobs.ashbyhq.com/api/, which robots.txt disallows, so no questions.
"""
from .base import Result, from_response, iso_date, new_job

ATS = "ashby"
PARTIAL = False


def public_url(token):
    return "https://jobs.ashbyhq.com/" + token


def fetch(client, token, etag=None, last_modified=None):
    r = client.get("https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true" % token,
                   etag=etag, last_modified=last_modified)
    bad = from_response(r)
    if bad:
        return bad
    d = r.json()
    if not isinstance(d, dict) or "jobs" not in d:
        return Result("none", error="no jobs key")
    jobs = []
    for j in d["jobs"]:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [x.get("location", "") for x in (j.get("secondaryLocations") or [])]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        comp = j.get("compensation") or {}
        pay = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or ""
        wp = (j.get("workplaceType") or "").lower()
        if not wp and j.get("isRemote"):
            wp = "remote"
        jobs.append(new_job(j.get("id"), j.get("title"), j.get("jobUrl"), apply_url=j.get("applyUrl") or "",
                            location=" / ".join(x for x in locs if x) + (" (%s)" % addr.get("addressRegion") if addr.get("addressRegion") and addr.get("addressRegion") not in " ".join(locs) else ""),
                            workplace={"onsite": "onsite", "hybrid": "hybrid", "remote": "remote"}.get(wp, wp),
                            department=" / ".join(x for x in [j.get("department"), j.get("team")] if x),
                            employment_type=j.get("employmentType") or "", posted_at=iso_date(j.get("publishedAt")),
                            description=j.get("descriptionPlain") or "", pay_text=pay,
                            raw={k: v for k, v in j.items() if k not in ("descriptionHtml", "descriptionPlain")}))
    return Result("ok", jobs, url=public_url(token), etag=r.header("ETag"), last_modified=r.header("Last-Modified"))


def detail(client, token, job):
    return {}
