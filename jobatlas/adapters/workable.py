"""Workable: the public careers widget API, apply.workable.com/api/v1/widget/accounts/<slug>?details=true

One call per board returns every job with its description. (The v3
`accounts/<slug>/jobs` POST works too but needs a second call per job for the
text, and apply.workable.com rate-limits hard: Cloudflare 1015 after a few
dozen quick calls, so the client spaces this host 4 s apart and backs off.)
The application form is public JSON too: apply.workable.com/api/v1/jobs/<shortcode>/form,
sections of fields with label, type, required, options and helper text (form() below).
"""
from .base import Result, from_response, html_text, iso_date, new_job

FIELD_TYPES = {"text": "input_text", "email": "input_text", "phone": "input_text", "number": "input_text", "url": "input_text",
               "paragraph": "textarea", "boolean": "boolean", "file": "input_file", "date": "date", "group": "group",
               "dropdown": "multi_value_single_select", "multiple": "multi_value_multi_select", "checkbox": "boolean"}

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


def _clean(form):
    """The served form minus what isn't the employer's: values Workable pre-fills from the visitor's IP."""
    out = []
    for sec in form if isinstance(form, list) else []:
        if not isinstance(sec, dict):
            continue
        fields = []
        for f in sec.get("fields") or []:
            if isinstance(f, dict):
                f = {k: v for k, v in f.items() if k not in ("value", "prefilledByLocation")}
                fields.append(f)
        out.append(dict(sec, fields=fields))
    return out


def questions_from(form):
    """The served form -> questions in the shared shape, every field (a group, e.g. Education, as one entry
    whose options are its sub-fields)."""
    out = []
    for sec in form:
        for f in sec.get("fields") or []:
            label = " ".join(str(f.get("label") or "").split())
            if not label:
                continue
            t = f.get("type") or "text"
            if t == "group":
                opts = [str(x.get("label") or "") for x in f.get("fields") or [] if isinstance(x, dict)]
            else:
                opts = [str(o.get("value") or o.get("name") or "") if isinstance(o, dict) else str(o) for o in f.get("options") or []]
            qt = FIELD_TYPES.get(t, "input_text")
            if t in ("dropdown", "multiple") and isinstance(f.get("singleOption"), bool):
                # "multiple" with singleOption is a set of radio buttons: one answer
                qt = "multi_value_single_select" if f["singleOption"] else "multi_value_multi_select"
            out.append({"label": label, "required": bool(f.get("required")), "type": qt, "kind": t, "options": opts,
                        "truncated": False, "description": " ".join(html_text(str(f.get("helper") or "")).split()),
                        "section": sec.get("name") or "", "id": f.get("id") or ""})
    return out


def form(client, token, job):
    """The posting's application form -> {"questions", "form", "state": "read"}, {"_closed": True} or {"_detail_error": ...}."""
    sc = job.get("ext_id") or ""
    if not sc:
        return {"_detail_error": "no shortcode"}
    r = client.get("https://apply.workable.com/api/v1/jobs/%s/form" % sc, timeout=15)
    if r.status in (404, 410):
        return {"_closed": True}
    if not r.ok:
        return {"_detail_error": "HTTP %s %s" % (r.status, (r.error or "")[:60])}
    try:
        d = r.json()
    except ValueError:
        return {"_detail_error": "not JSON"}
    if not isinstance(d, list) or not d:
        return {"_detail_error": "no form sections"}
    f = _clean(d)
    return {"questions": questions_from(f), "form": f, "state": "read"}
