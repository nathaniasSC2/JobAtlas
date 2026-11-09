"""Lever Postings API (public): api.lever.co/v0/postings/<slug>?mode=json (EU: api.eu.lever.co).

Token "eu:<slug>" means the EU host. Descriptions and salary ranges come in
the list itself, so no detail call is needed. Lever's application questions
aren't in the public API; form() reads them from the posting's public
/apply page (robots.txt allows it); poll.read_forms calls it for every scored job, matches first.
"""
import html as htmlmod
import json
import re

from .base import Result, from_response, iso_date, new_job, pay_range_text

# the form's built-in fields (name, contact, links, resume, EEO survey): not the employer's own questions
STANDARD_FIELDS = re.compile(r"^(?:name|email|phone|org|location|selectedLocation|timezone|resume|comments|urls\[.*\]|eeo\[.*\]|"
                             r"surveysResponses.*|consent.*|h-captcha-response)$")
CARD_TYPES = {"multiple-choice": "multi_value_single_select", "dropdown": "multi_value_single_select",
              "multiple-select": "multi_value_multi_select", "textarea": "textarea", "text": "input_text",
              "file-upload": "input_file"}

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


def form_questions(page):
    """The employer's own questions on a Lever /apply page -> [{"label", "required", "type", "options", "description"}].

    Custom questions sit in "cards": each card's field list is a JSON baseTemplate in a hidden input, with
    the real type, options and required flag. The built-in fields (name, links, resume, EEO) are left out."""
    cards = {}
    for m in re.finditer(r'<input[^>]*name="cards\[([^\]]+)\]\[baseTemplate\]"[^>]*>', page):
        v = re.search(r'value="([^"]*)"', m.group(0))
        if v:
            try:
                cards[m.group(1)] = json.loads(htmlmod.unescape(v.group(1)))
            except ValueError:
                pass
    out = []
    for card in cards.values():
        for f in card.get("fields") or []:
            if not isinstance(f, dict) or not (f.get("text") or "").strip():
                continue
            out.append({"label": " ".join(htmlmod.unescape(f["text"]).split()), "required": bool(f.get("required")),
                        "type": CARD_TYPES.get(f.get("type"), "input_text"),
                        "options": [o.get("text", "") for o in (f.get("options") or []) if isinstance(o, dict)][:12],
                        "description": " ".join(htmlmod.unescape(f.get("description") or "").split())[:300]})
    # other questions outside the cards (rare), by their label
    for m in re.finditer(r'<li class="application-question[^"]*"[^>]*>([\s\S]*?)</li>', page):
        blk = m.group(1)
        names = re.findall(r'name="([^"]+)"', blk)
        if not names or names[0].startswith("cards[") or STANDARD_FIELDS.match(names[0]) or names[0] == "opportunityLocationId":
            continue
        raw = re.sub(r"<[^>]+>", " ", (re.search(r'application-label[^>]*>([\s\S]*?)</div>', blk) or [None, ""])[1])
        label = " ".join(htmlmod.unescape(raw).replace("✱", " ").split())
        if label:
            out.append({"label": label, "required": "✱" in raw, "type": "input_text", "options": [], "description": ""})
    return out


def form(client, token, job):
    """Read the posting's application form -> {"questions": [...]}, {"_closed": True} (page gone) or {"_detail_error": ...}."""
    url = (job.get("url") or "").split("?")[0].rstrip("/")
    if not url:
        return {"_detail_error": "no posting url"}
    r = client.get(url + "/apply", accept="text/html,*/*")
    if r.status in (404, 410):
        return {"_closed": True}
    if not r.ok:
        return {"_detail_error": "HTTP %s %s" % (r.status, (r.error or "")[:60])}
    if "application-form" not in r.text:
        return {"_detail_error": "no application form on the page"}
    return {"questions": form_questions(r.text)}
