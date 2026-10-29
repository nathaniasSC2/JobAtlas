"""Greenhouse Job Board API (public, documented): boards-api.greenhouse.io/v1/boards/<slug>/jobs

The list is fetched without content (small, cheap to poll); a job's detail
(`?questions=true&pay_transparency=true`) gives the description, pay ranges and
the application form's custom questions, and is fetched only for jobs that
pass the title/location prefilter.
"""
from .base import Result, from_response, html_text, iso_date, new_job, pay_range_text

ATS = "greenhouse"
PARTIAL = False
API = "https://boards-api.greenhouse.io/v1/boards/"
STANDARD = {"first_name", "last_name", "preferred_name", "email", "phone", "resume", "resume_text", "cover_letter",
            "cover_letter_text", "location"}


def public_url(token):
    return "https://job-boards.greenhouse.io/" + token


def fetch(client, token, etag=None, last_modified=None):
    r = client.get(API + token + "/jobs", etag=etag, last_modified=last_modified)
    bad = from_response(r)
    if bad:
        return bad
    d = r.json()
    if not isinstance(d, dict) or "jobs" not in d:
        return Result("none", error="no jobs key")
    jobs, name = [], ""
    for j in d["jobs"]:
        name = name or j.get("company_name") or ""
        meta = {m.get("name"): m.get("value") for m in (j.get("metadata") or []) if isinstance(m, dict)}
        jobs.append(new_job(j.get("id"), j.get("title"), j.get("absolute_url"),
                            location=(j.get("location") or {}).get("name", ""),
                            posted_at=iso_date(j.get("first_published") or j.get("updated_at")),
                            employment_type=str(meta.get("Employment Type") or ""),
                            company=j.get("company_name") or "", raw=j, needs_detail=True))
    return Result("ok", jobs, name=name, url=public_url(token), etag=r.header("ETag"), last_modified=r.header("Last-Modified"))


def questions_of(d):
    out = []
    for q in (d.get("questions") or []) + (d.get("location_questions") or []):
        fields = q.get("fields") or []
        names = {f.get("name") for f in fields}
        if names & STANDARD and not any(str(n).startswith("question_") for n in names):
            continue
        opts = [v.get("label") for f in fields for v in (f.get("values") or []) if v.get("label")]
        out.append({"label": (q.get("label") or "").strip(), "required": bool(q.get("required")),
                    "type": (fields[0].get("type") if fields else ""), "options": opts[:12],
                    "description": html_text(q.get("description") or "")[:300]})
    return out


def detail(client, token, job):
    r = client.get(API + token + "/jobs/" + job["ext_id"] + "?questions=true&pay_transparency=true")
    if not r.ok:
        return {"_detail_error": "HTTP %s %s" % (r.status, r.error[:60])}
    d = r.json() or {}
    pays = []
    for p in d.get("pay_input_ranges") or []:
        lo, hi = p.get("min_cents"), p.get("max_cents")
        pays.append((p.get("title") or "") + ": " + pay_range_text(lo / 100 if lo else None, hi / 100 if hi else None,
                                                                   p.get("currency_type") or "USD"))
    offices = ", ".join(o.get("name", "") for o in (d.get("offices") or []) if o.get("name"))
    return {"description": html_text(d.get("content") or ""), "questions": questions_of(d),
            "pay_text": "; ".join(pays), "department": ", ".join(x.get("name", "") for x in (d.get("departments") or [])),
            "location": job.get("location") or offices,
            "raw_detail": {k: d.get(k) for k in ("pay_input_ranges", "offices", "departments", "first_published",
                                                 "location_questions", "application_deadline")}}
