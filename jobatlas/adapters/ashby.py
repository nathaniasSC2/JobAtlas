"""Ashby Posting API (public): api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true

Descriptions and compensation are in the list. The application form isn't: the
posting's /application page builds it in the browser from jobs.ashbyhq.com/api/,
which robots.txt closes to crawlers. With a browser configured (forms.browser,
browser.py), form() loads that public page and reads every field off it: label,
type, required, options (a dropdown is opened to list them) and description.
"""
from .base import Result, from_response, iso_date, new_job

FORM_BROWSER = True        # form() needs the browser (poll.read_forms passes it)

# Ashby's own type names -> the question types the other adapters use (alerts.written_questions, score.form_gates)
FIELD_TYPES = {"yes_no": "boolean", "radio": "multi_value_single_select", "select": "multi_value_single_select",
               "checkboxes": "multi_value_multi_select", "checkbox": "boolean", "file": "input_file", "textarea": "textarea",
               "location": "input_text", "autocomplete": "input_text", "text": "input_text", "email": "input_text",
               "phone": "input_text", "number": "input_text", "date": "date"}

# Runs in the rendered /application page: every field entry -> {section, label, description, type, required, options}.
# Read-only: the only interaction is opening a dropdown (and closing it) to list its options.
FORM_SCRIPT = r'''(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = e => (e ? (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const ENTRY = '.ashby-application-form-field-entry, fieldset[class*="_fieldEntry_"]';
  for (let i = 0; i < 48 && !document.querySelector(ENTRY); i++) {
    if (i > 8 && /not found|no longer|isn.t accepting|closed/i.test(txt(document.body).slice(0, 3000))) break;
    await sleep(250);
  }
  const out = {fields: [], title: txt(document.querySelector('h1')), url: location.href};
  if (!document.querySelector(ENTRY)) { out.missing = txt(document.body).slice(0, 400); return out; }
  let section = '';
  const nodes = document.querySelectorAll('.ashby-application-form-section-header-title, ' + ENTRY);
  for (const el of nodes) {
    if (el.classList.contains('ashby-application-form-section-header-title')) { section = txt(el); continue; }
    if (el.parentElement && el.parentElement.closest(ENTRY)) continue;
    const has = c => el.matches('.ashby-application-form-input-' + c) || el.querySelector('.ashby-application-form-input-' + c);
    const t = el.querySelector('.ashby-application-form-question-title, legend');
    let label = txt(t);
    const optText = c => Array.from(el.querySelectorAll('.ashby-application-form-input-' + c + '-option-label')).map(txt).filter(Boolean);
    let type = 'text', options = [];
    if (has('yesno')) { type = 'yes_no'; options = Array.from(el.querySelectorAll('button')).map(txt).filter(Boolean); }
    else if (has('radio-group')) { type = 'radio'; options = optText('radio-group'); }
    else if (has('checkbox-group')) { type = 'checkboxes'; options = optText('checkbox-group'); }
    else if (has('file')) type = 'file';
    else if (has('textarea')) type = 'textarea';
    else if (has('autocomplete') || el.querySelector('[role=combobox]')) type = 'select';
    else if (el.querySelector('input[type=checkbox]')) { type = 'checkbox'; options = Array.from(el.querySelectorAll('label')).map(txt).filter(Boolean); }
    else if (el.querySelector('input[type=date]')) type = 'date';
    else if (el.querySelector('input[type=number]')) type = 'number';
    else if (el.querySelector('input[type=email]')) type = 'email';
    else if (el.querySelector('input[type=tel]')) type = 'phone';
    if (!label) {
      const lab = Array.from(el.querySelectorAll('label, span, p, div')).find(x => txt(x) && !x.querySelector('input, button, label') &&
        !String(x.className).includes('option') && !x.closest('[class*="option"]') && !options.includes(txt(x)));
      label = txt(lab);
    }
    const required = /\*\s*$/.test(label) || !!(t && /required/i.test(String(t.className)));
    label = label.replace(/\s*\*\s*$/, '');
    const desc = txt(el.querySelector('.ashby-application-form-question-description'));
    if (type === 'select') {
      const cb = el.querySelector('input[role=combobox], [role=combobox], input');
      try {
        cb.focus(); cb.click();
        cb.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', code: 'ArrowDown', bubbles: true}));
        await sleep(400);
        const lb = document.querySelector('[role=listbox]');
        options = lb ? Array.from(lb.querySelectorAll('[role=option]')).map(txt).filter(Boolean) : [];
        cb.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape', bubbles: true}));
        cb.blur();
        await sleep(120);
      } catch (e) { options = []; }
      if (!options.length) type = /locat|city|address|where do you (live|reside)/i.test(label + ' ' + desc) ? 'location' : 'autocomplete';
    }
    options = options.filter(o => o !== label);
    out.fields.push({section, label, description: desc, type, required, options});
  }
  return out;
})()'''

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


GONE_TEXT = ("job not found", "page not found", "no longer", "isn't accepting", "isn’t accepting", "not accepting applications",
             "has been closed", "posting has closed", "position has been filled")


def questions_from(form):
    """FORM_SCRIPT's result -> questions in the shared shape. Every field is kept, built-in ones included."""
    out = []
    for f in (form or {}).get("fields") or []:
        label = " ".join(str(f.get("label") or "").split())
        if not label:
            continue
        out.append({"label": label, "required": bool(f.get("required")),
                    "type": FIELD_TYPES.get(f.get("type"), "input_text"), "kind": f.get("type") or "",
                    "options": [str(o) for o in (f.get("options") or [])], "truncated": False,
                    "description": " ".join(str(f.get("description") or "").split()), "section": f.get("section") or ""})
    return out


def form(client, token, job, browser=None):
    """Read the posting's application form in the browser -> {"questions", "form", "state": "read"},
    {"_closed": True} (the posting is gone) or {"_detail_error": ...}."""
    if browser is None:
        return {"_detail_error": "no browser configured", "_no_browser": True}
    url = (job.get("url") or "").split("?")[0].rstrip("/")
    if not url:
        return {"_detail_error": "no posting url"}
    if not url.endswith("/application"):
        url += "/application"
    from ..browser import BrowserError
    try:
        res = browser.read(url, FORM_SCRIPT)
    except BrowserError as e:
        return {"_detail_error": str(e)[:160]}
    if not isinstance(res, dict):
        return {"_detail_error": "unexpected result: %s" % str(res)[:80]}
    if not res.get("fields"):
        gone = (res.get("missing") or "").lower()
        if any(w in gone for w in GONE_TEXT):
            return {"_closed": True}
        return {"_detail_error": "no form on the page: %s" % gone[:80]}
    return {"questions": questions_from(res), "form": res, "state": "read"}
