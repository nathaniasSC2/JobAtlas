"""Workday CXS (what every myworkdayjobs.com career site calls):

List:   POST https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
        {"appliedFacets":{}, "limit":20, "offset":N, "searchText":"..."}
Detail: GET  .../wday/cxs/<tenant>/<site><externalPath>

Token is "tenant|wdN|site". Big tenants list thousands of jobs, so the poll
searches the profile's search_terms (a few pages each) instead of paging the whole
board: PARTIAL = True, and a job only counts as closed after it has been
missing for several days (poll.py). robots.txt on these hosts disallows the
human site paths (/External/...) but not /wday/cxs/.
"""
import json
import os

from .. import profile
from .base import Result, html_text, iso_date, new_job, relative_date

ATS = "workday"
PARTIAL = True
PAGES = 2
LIMIT = 20
FACET_PAGES = 5
# Optional per-tenant facet queries (e.g. a university's remoteType=remote or IT jobFamily ids).
_FACETS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "workday_facets.json")
try:
    with open(_FACETS_FILE, encoding="utf8") as _f:
        FACETS = {k: v for k, v in json.load(_f).items() if not k.startswith("_")}
except (OSError, ValueError):
    FACETS = {}


def parts(token):
    tenant, wd, site = token.split("|")
    return tenant, wd, site, "https://%s.%s.myworkdayjobs.com" % (tenant, wd)


def public_url(token):
    tenant, wd, site, base = parts(token)
    return "%s/%s" % (base, site)


def fetch(client, token, etag=None, last_modified=None, terms=None):
    try:
        tenant, wd, site, base = parts(token)
    except ValueError:
        return Result("error", error="bad workday token")
    api = "%s/wday/cxs/%s/%s/jobs" % (base, tenant, site)
    seen, jobs, total = set(), [], None
    queries = [({}, t, PAGES) for t in (terms if terms is not None else profile.load().search_terms)]
    if terms is None:
        queries = [(q.get("appliedFacets") or {}, q.get("searchText") or "", FACET_PAGES) for q in FACETS.get(token, [])] + queries
    for facets, term, pages in queries:
        for page in range(pages):
            r = client.post_json(api, {"appliedFacets": facets, "limit": LIMIT, "offset": page * LIMIT, "searchText": term})
            if not r.ok and facets and r.status in (400, 422, 500):
                break  # a stale facet id; the plain searches still run
            if not r.ok:
                if not jobs and total is None:
                    if r.status in (404, 422, 400) or (r.status == 500 and "not found" in r.text.lower()):
                        return Result("none", error="HTTP %d" % r.status)
                    if r.status in (401, 403, 429) or r.error:
                        return Result("blocked" if r.status in (401, 403, 429) or "robots" in r.error or "rate" in r.error else "error",
                                      error=("HTTP %d " % r.status) + r.error[:80])
                    return Result("error", error="HTTP %d" % r.status)
                break
            d = r.json() or {}
            if total is None:
                total = d.get("total")
            posts = d.get("jobPostings") or []
            for j in posts:
                path = j.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                bullets = j.get("bulletFields") or []
                jobs.append(new_job(path.rsplit("_", 1)[-1] if "_" in path else path, j.get("title"),
                                    "%s/en-US/%s%s" % (base, site, path), location=j.get("locationsText") or "",
                                    workplace=(j.get("remoteType") or "").lower(),
                                    posted_at=relative_date(j.get("postedOn") or ""),
                                    raw=dict(j, _term=term, _bullets=bullets), needs_detail=True))
            if len(posts) < LIMIT:
                break
    return Result("ok", jobs, url=public_url(token), error="" if total is None else "total=%s" % total)


def detail(client, token, job):
    tenant, wd, site, base = parts(token)
    path = (job.get("raw") or {}).get("externalPath") or job["url"].split("/" + site, 1)[-1]
    r = client.get("%s/wday/cxs/%s/%s%s" % (base, tenant, site, path))
    if not r.ok:
        return {"_detail_error": "HTTP %s %s" % (r.status, r.error[:60])}
    d = r.json() or {}
    info = d.get("jobPostingInfo") or {}
    locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    out = {"description": html_text(info.get("jobDescription") or ""),
           "location": " / ".join(x for x in locs if x) or job.get("location", ""),
           "employment_type": info.get("timeType") or "",
           "company": ((d.get("hiringOrganization") or {}).get("name") or ""),
           "raw_detail": {k: info.get(k) for k in ("jobReqId", "postedOn", "startDate", "timeType", "remoteType", "country",
                                                   "externalUrl", "questionnaireId", "canApply")}}
    if info.get("remoteType"):
        out["workplace"] = str(info["remoteType"]).lower()
    if info.get("startDate"):
        out["posted_at"] = iso_date(info.get("startDate"))
    if info.get("externalUrl"):
        out["url"] = info["externalUrl"]
    if info.get("canApply") is False:
        out["_closed"] = True
    return out
