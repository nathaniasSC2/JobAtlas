"""Workday CXS (what every myworkdayjobs.com career site calls):

List:   POST https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
        {"appliedFacets":{}, "limit":20, "offset":N, "searchText":"..."}
Detail: GET  .../wday/cxs/<tenant>/<site><externalPath>

Token is "tenant|wdN|site". Big tenants list thousands of jobs, so the poll
searches the profiles' search_terms (a few pages each) instead of paging the whole
board: PARTIAL = True, and a job only counts as closed after it has been
missing for several days (poll.py). robots.txt on these hosts disallows the
human site paths (/External/...) but not /wday/cxs/.
"""
import json
import os
import re

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
    queries = [({}, t, PAGES) for t in (terms if terms is not None else profile.search_terms())]
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


# The application questions (questionnaireId in the detail) are only served to a signed-in candidate:
# Workday's apply flow starts with Create Account/Sign In on most tenants, and on the tenants that take
# guests the questions come after "My Information", i.e. only once an application has been started.
# JobAtlas does neither. form() loads the public apply page in the browser, reads which wall it shows and
# the flow's steps, and stores that; the tenant's answer is reused for its other jobs (poll.read_forms).
FORM_PER_BOARD = True
FORM_BROWSER = True
FORM_SCRIPT = r'''(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = e => (e ? (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const main = () => document.querySelector('main') || document.body;
  for (let i = 0; i < 48; i++) {
    const t = txt(main());
    if (/doesn.t exist|no longer|not available/i.test(t) || (/step \d+ of \d+/i.test(t) && document.querySelector('main input'))) break;
    await sleep(250);
  }
  await sleep(400);
  const fields = Array.from(document.querySelectorAll('main [data-automation-id^="formField"], main [data-automation-id="multiSelectContainer"]'))
    .filter(f => !f.parentElement.closest('[data-automation-id^="formField"]')).map(f => {
      const l = (txt(f.querySelector('label, legend')) || txt(f)).slice(0, 200);
      return {label: l.replace(/\s*\*.*$/, ''), required: /\*/.test(l) || !!f.querySelector('[aria-required="true"], [required]'),
              id: f.getAttribute('data-automation-id') || ''};
    }).filter(f => f.label).slice(0, 60);
  return {url: location.href, title: document.title, text: txt(main()).slice(0, 1500),
          headings: Array.from(document.querySelectorAll('main h1, main h2, main h3')).map(txt).filter(Boolean).slice(0, 8),
          password: !!document.querySelector('main input[type=password]'),
          buttons: Array.from(document.querySelectorAll('main button')).map(txt).filter(Boolean).slice(0, 12), fields};
})()'''
GONE = re.compile(r"doesn.t exist|no longer (?:available|accepting|open)|job (?:posting )?is not available|position has been filled", re.I)
STEP_NAMES = ("Create Account/Sign In", "My Information", "My Experience", "Application Questions", "Voluntary Disclosures",
              "Self Identify", "Take Assessment", "Review")


def steps_of(text):
    """'current step 1 of 6 Create Account/Sign In step 2 of 6 My Information ...' -> step names."""
    out = []
    for m in re.finditer(r"step (\d+) of (\d+)\s+", text or "", re.I):
        rest = text[m.end():]
        nxt = re.search(r"\s+(?:current )?step \d+ of \d+", rest, re.I)
        name = rest[:nxt.start()] if nxt else rest
        known = next((s for s in STEP_NAMES if name.lower().startswith(s.lower())), None)
        out.append(known or name[:40].strip())
    return out


def wall_of(page):
    """FORM_SCRIPT's result -> (state, form). state: signin (an account first), guest (questions after the
    first steps), gone (the posting no longer exists), or '' (couldn't tell)."""
    text = page.get("text") or ""
    steps = steps_of(text)
    form = {"steps": steps, "headings": page.get("headings") or [], "fields": page.get("fields") or [], "url": page.get("url") or ""}
    if GONE.search(text) and not steps:
        return "gone", form
    if page.get("password") or any(re.search(r"create account|sign in", s, re.I) for s in steps[:1]) or \
            any(re.match(r"create account|sign in", h, re.I) for h in page.get("headings") or []):
        return "signin", form
    if steps:
        return "guest", form
    return "", form


def form(client, token, job, browser=None):
    """-> {"state": "signin"|"guest", "form": {...}, "questions": [...]} (the questions themselves can't be read:
    see above), {"_closed": True} or {"_detail_error": ...}."""
    if browser is None:
        return {"_detail_error": "no browser configured", "_no_browser": True}
    url = (job.get("url") or "").split("?")[0].rstrip("/")
    if not url:
        return {"_detail_error": "no posting url"}
    from ..browser import BrowserError
    try:
        page = browser.read(url + "/apply/applyManually", FORM_SCRIPT)
    except BrowserError as e:
        return {"_detail_error": str(e)[:160]}
    if not isinstance(page, dict):
        return {"_detail_error": "unexpected result: %s" % str(page)[:80]}
    state, f = wall_of(page)
    if state == "gone":
        return {"_closed": True}
    if not state:
        return {"_detail_error": "couldn't tell the apply page's first step: %s" % (page.get("text") or "")[:80]}
    f["questionnaire"] = bool(((job.get("raw") or {}).get("_detail") or {}).get("questionnaireId"))
    return {"state": state, "form": f}
