"""Lever Postings API (public): api.lever.co/v0/postings/<slug>?mode=json (EU: api.eu.lever.co).

Token "eu:<slug>" means the EU host. Descriptions and salary ranges come in
the list itself, so no detail call is needed. Lever's application questions
aren't in the public API.
"""
from .base import Result, from_response, iso_date, new_job, pay_range_text

ATS = "lever"
PARTIAL = False


def _host(token):
    return ("api.eu.lever.co", token[3:]) if token.startswith("eu:") else ("api.lever.co", token)


def public_url(token):
    host, slug = _host(token)
    return "https://jobs.%slever.co/%s" % ("eu." if host.startswith("api.eu") else "", slug)


def fetch(client, token, etag=None, last_modified=None):
    host, slug = _host(token)
    r = client.get("https://%s/v0/postings/%s?mode=json" % (host, slug), etag=etag, last_modified=last_modified)
    bad = from_response(r)
    if bad:
        return bad
    d = r.json()
    if not isinstance(d, list):
        return Result("none", error="not a list")
    jobs = []
    for j in d:
        cat = j.get("categories") or {}
        sr = j.get("salaryRange") or {}
        desc = "\n\n".join(x for x in [j.get("descriptionPlain") or ""] +
                           ["%s\n%s" % (l.get("text", ""), _strip(l.get("content", ""))) for l in (j.get("lists") or [])] +
                           [j.get("additionalPlain") or ""] if x)
        pay = pay_range_text(sr.get("min"), sr.get("max"), sr.get("currency") or "USD", sr.get("interval") or "") if sr else ""
        jobs.append(new_job(j.get("id"), j.get("text"), j.get("hostedUrl"), apply_url=j.get("applyUrl") or "",
                            location=" / ".join(cat.get("allLocations") or []) or cat.get("location") or "",
                            workplace=(j.get("workplaceType") or "").replace("unspecified", ""),
                            department=" / ".join(x for x in [cat.get("department"), cat.get("team")] if x),
                            employment_type=cat.get("commitment") or "", posted_at=iso_date(j.get("createdAt")),
                            description=desc, pay_text=pay or (j.get("salaryDescriptionPlain") or "")[:300],
                            raw={k: v for k, v in j.items() if k not in ("description", "descriptionPlain", "lists",
                                                                         "additional", "additionalPlain", "descriptionBody",
                                                                         "descriptionBodyPlain", "openingPlain", "opening")}))
    return Result("ok", jobs, url=public_url(token), etag=r.header("ETag"), last_modified=r.header("Last-Modified"))


def _strip(s):
    from .base import html_text
    return html_text(s)


def detail(client, token, job):
    return {}
