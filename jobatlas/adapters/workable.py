"""Workable: the public careers widget API, apply.workable.com/api/v1/widget/accounts/<slug>?details=true

One call per board returns every job with its description. (The v3
`accounts/<slug>/jobs` POST works too but needs a second call per job for the
text, and apply.workable.com rate-limits hard: Cloudflare 1015 after a few
dozen quick calls, so the client spaces this host 4 s apart and backs off.)
Workable's application form isn't readable without a browser, so no questions.
"""
from .base import Result, from_response, html_text, iso_date, new_job

ATS = "workable"
PARTIAL = False


def public_url(token):
    return "https://apply.workable.com/%s/" % token


def fetch(client, token, etag=None, last_modified=None):
    r = client.get("https://apply.workable.com/api/v1/widget/accounts/%s?details=true" % token,
                   etag=etag, last_modified=last_modified)
    bad = from_response(r)
    if bad:
        return bad
    d = r.json()
    if not isinstance(d, dict) or "jobs" not in d:
        return Result("none", error="no jobs key")
    jobs = []
    for j in d["jobs"]:
        locs = j.get("locations") or []
        loc = " / ".join(", ".join(x for x in [l.get("city"), l.get("region"), l.get("country")] if x) for l in locs if not l.get("hidden")) \
            or ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        sc = j.get("shortcode") or j.get("code")
        jobs.append(new_job(sc, j.get("title"), j.get("url") or j.get("shortlink") or "https://apply.workable.com/%s/j/%s/" % (token, sc),
                            apply_url=j.get("application_url") or "", location=loc,
                            workplace="remote" if j.get("telecommuting") else "",
                            department=j.get("department") or "", employment_type=j.get("employment_type") or "",
                            posted_at=iso_date(j.get("published_on") or j.get("created_at")),
                            description=html_text(j.get("description") or ""),
                            raw={k: v for k, v in j.items() if k != "description"}))
    return Result("ok", jobs, name=d.get("name") or "", url=public_url(token), etag=r.header("ETag"),
                  last_modified=r.header("Last-Modified"))


def detail(client, token, job):
    return {}
