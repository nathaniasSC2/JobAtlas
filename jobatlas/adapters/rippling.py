"""Rippling ATS: the board's own JSON (what ats.rippling.com's pages load).

List:   api.rippling.com/platform/api/ats/v1/board/<slug>/jobs
Detail: ats.rippling.com/api/v2/board/<slug>/jobs/<uuid>  (description, pay
        ranges and the form's additional questions)
"""
from .base import Result, from_response, html_text, iso_date, new_job, pay_range_text

ATS = "rippling"
PARTIAL = False


def public_url(token):
    return "https://ats.rippling.com/%s/jobs" % token


def fetch(client, token, etag=None, last_modified=None):
    r = client.get("https://api.rippling.com/platform/api/ats/v1/board/%s/jobs" % token, etag=etag, last_modified=last_modified)
    bad = from_response(r)
    if bad:
        return bad
    d = r.json()
    if not isinstance(d, list):
        return Result("none", error="not a list")
    jobs = []
    for j in d:
        wl = j.get("workLocation") or {}
        loc = wl.get("label", "") if isinstance(wl, dict) else str(wl)
        jid = j.get("uuid") or j.get("id")
        jobs.append(new_job(jid, j.get("name"), j.get("url") or "https://ats.rippling.com/%s/jobs/%s" % (token, jid),
                            location=loc, department=((j.get("department") or {}).get("label") or "") if isinstance(j.get("department"), dict) else "",
                            raw=j, needs_detail=True))
    return Result("ok", jobs, url=public_url(token))


def detail(client, token, job):
    r = client.get("https://ats.rippling.com/api/v2/board/%s/jobs/%s" % (token, job["ext_id"]))
    if not r.ok:
        return {"_detail_error": "HTTP %s %s" % (r.status, r.error[:60])}
    d = r.json() or {}
    desc = d.get("description") or {}
    text = "\n\n".join(html_text(v) for v in (desc.values() if isinstance(desc, dict) else [desc]) if isinstance(v, str))
    pays = [pay_range_text(p.get("rangeStart"), p.get("rangeEnd"), p.get("currency") or "USD", p.get("frequency") or "")
            + (" (%s)" % p["location"] if p.get("location") else "") for p in (d.get("payRangeDetails") or [])]
    qs = []
    for blk in ((d.get("activeJobApplication") or {}).get("additionalQuestions") or []):
        for q in ((blk.get("form") or {}).get("questions") or []):
            opts = [o.get("label") or o.get("value") for o in (q.get("options") or q.get("choices") or []) if isinstance(o, dict)]
            qs.append({"label": (q.get("title") or "").strip(), "required": bool(q.get("isRequired") or q.get("required")),
                       "type": q.get("type") or q.get("questionType") or "", "options": [o for o in opts if o][:12],
                       "description": html_text(q.get("description") or "")[:300]})
    et = d.get("employmentType") or {}
    return {"description": text, "pay_text": "; ".join(p for p in pays if p), "questions": qs,
            "location": " / ".join(d.get("workLocations") or []) or job.get("location", ""),
            "employment_type": et.get("id") or et.get("label") or "" if isinstance(et, dict) else str(et),
            "posted_at": iso_date(d.get("createdOn")), "company": d.get("companyName") or "",
            "raw_detail": {k: d.get(k) for k in ("payRangeDetails", "workLocations", "createdOn", "companyName")}}
