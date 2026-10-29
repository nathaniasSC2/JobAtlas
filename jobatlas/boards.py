"""Find ATS boards referenced in URLs or page HTML."""
import html as htmlmod
import re
import urllib.parse

PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_app\?(?:[^\"'\s]*&)?for=([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?!j/|api/)([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"workable\.com/api/v\d/(?:widget/)?accounts/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"//([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("bamboohr", re.compile(r"//([a-z0-9-]+)\.bamboohr\.com", re.I)),
    ("pinpoint", re.compile(r"//([a-z0-9-]+)\.pinpointhq\.com", re.I)),
    ("breezy", re.compile(r"//([a-z0-9-]+)\.breezy\.hr", re.I)),
    ("teamtailor", re.compile(r"//([a-z0-9-]+)\.teamtailor\.com", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/([A-Za-z0-9_-]+)", re.I)),
    ("rippling", re.compile(r"ripplingcdn\.com/ats/embeds/.{0,400}?data-job-board-id=[\"']([A-Za-z0-9_-]+)", re.I | re.S)),
    ("rippling", re.compile(r"data-job-board-id=[\"']([A-Za-z0-9_-]+)[\"'].{0,400}?ripplingcdn\.com/ats/embeds", re.I | re.S)),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/recruiting/jobs/(?:All|List)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)),
    ("pageup", re.compile(r"careers\.pageuppeople\.com/(\d+)/", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)", re.I)),
]
WORKDAY = re.compile(r"//([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)
NOT_TOKENS = {"j", "jobs", "job", "embed", "api", "search", "companies", "company", "job-boards", "boards", "s", "e", "www",
              "v0", "v1", "v2", "careers", "career", "en", "en-us", "apply", "static", "assets", "app", "login", "signin",
              "internal", "help", "support", "blog", "privacy", "terms", "legal", "about", "status", "go", "my", "admin",
              "candidate", "candidates", "cdn", "images", "img", "media", "widget", "widgets", "posting", "postings"}
ATS_HOSTS = re.compile(r"(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|recruitee\.com|bamboohr\.com|"
                       r"pinpointhq\.com|breezy\.hr|rippling\.com|myworkdayjobs\.com|icims\.com|jobvite\.com|teamtailor\.com|"
                       r"applytojob\.com|paylocity\.com|ultipro\.com|dayforcehcm\.com|taleo\.net|successfactors|oraclecloud\.com|"
                       r"adp\.com|pageuppeople\.com|governmentjobs\.com|schooljobs\.com|ctclink\.us)", re.I)


def boards_in(text):
    """Every ATS board referenced in a page or URL -> [{"ats","token"}] (unique, in order)."""
    out, seen = [], set()

    def add(ats, tok):
        tok = (tok or "").strip(".").strip()
        k = (ats, tok.lower())
        if tok and tok.lower() not in NOT_TOKENS and k not in seen and len(tok) <= 80:
            seen.add(k)
            out.append({"ats": ats, "token": tok})
    t = htmlmod.unescape(text or "")
    for ats, rx in PATTERNS:
        for m in rx.finditer(t):
            tok = urllib.parse.unquote(m.group(1))
            if ats == "lever" and ".eu.lever.co" in m.group(0):
                tok = "eu:" + tok
            if ats == "ashby" and tok.lower().startswith("api"):
                continue
            add(ats, tok)
    for m in WORKDAY.finditer(t):
        tenant, wd, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if site.lower() in ("wday", "en-us", "job", "jobs", "login", "userhome"):
            continue
        add("workday", "%s|%s|%s" % (tenant, wd, site))
    return out
