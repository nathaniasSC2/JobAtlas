"""The job seeker's profile: fields, location, pay and hard rules, from one JSON file.

Read from config.PROFILE_FILE (<data>/profile.json or $JOBATLAS_PROFILE). If it
is missing, examples/profile.example.json is used and `jobatlas status` says so.
score.py holds the generic parsing (pay, years, degree, remote/state wording);
everything that is a personal choice lives in the profile.
"""
import hashlib
import json
import os
import re

from . import config

EXAMPLE = os.path.join(config.EXAMPLES, "profile.example.json")

RULE_VALUES = ("skip", "flag", "ignore")
DEFAULT_RULES = {
    "weekend_required": "skip",   # required weekend shifts / weekend on-call
    "drivers_licence": "skip",    # a driver's licence or own vehicle required
    "clearance": "skip",          # security clearance / public trust
    "degree_required": "skip",    # a degree with no "or equivalent experience"
    "no_benefits": "skip",        # the posting says there are no benefits
    "quota": "skip",              # quota sales titles, quota/OTE pay
    "engineering_titles": "skip",  # "... Engineer" unless it matches engineering_ok
    "internships": "skip",        # internships, student jobs, applicant pools
}

US_STATES = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
             "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
             "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine",
             "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
             "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
             "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
             "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee",
             "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
             "WI": "wisconsin", "WY": "wyoming"}


def _rx(x, flags=re.I):
    """A regex string, or a list of terms/regexes OR'd together -> compiled pattern (None if empty)."""
    if not x:
        return None
    if isinstance(x, str):
        return re.compile(x, flags)
    return re.compile("|".join(t if "\\" in t or "(" in t else re.escape(t) for t in x), flags)


def _places(names):
    """City names -> one word-bounded regex."""
    names = [n for n in (names or []) if n]
    return re.compile(r"\b(" + "|".join(re.escape(n).replace(r"\ ", r"[\s-]") for n in names) + r")\b", re.I) if names else None


def home_only_regex(state_name):
    """'must reside in <state>', '<state> residents only', 'remote within <state>' ...
    For Washington, Washington D.C. is excluded."""
    st = re.escape(state_name).replace(r"\ ", r"\s+")
    st = st + (r"(?!,?\s*d\.?c)" if state_name.lower() == "washington" else "")
    return re.compile(
        r"((?:must|required to|need to|should|will need to)\s+(?:currently\s+)?(?:be\s+)?(?:live|living|reside|residing|located|based|a resident)\s+"
        r"(?:in|within|of)\s+(?:the\s+)?(?:state of\s+)?" + st + r"(?:\s+state)?"
        r"|" + st + r"(?:\s+state)?\s+residents?\s+only|open\s+(?:only\s+)?to\s+" + st + r"\s+(?:state\s+)?residents"
        r"|remote\s+(?:within|in|from)\s+(?:the\s+state\s+of\s+)?" + st + r"(?:\s+state)?"
        r"|(?:telework|work remotely|work from home)\s+(?:from\s+)?(?:anywhere\s+)?(?:within|in)\s+(?:the\s+state\s+of\s+)?" + st +
        r"|(?:residents?|located)\s+(?:of|in)\s+" + st + r"\s+state\s+only"
        r"|only\s+(?:hiring|considering|accepting)\s+(?:applicants|candidates)?\s*(?:from|in|who (?:live|reside) in)\s+" + st + ")", re.I)


class Profile:
    def __init__(self, d, src=""):
        self.raw = d
        self.src = src
        self.hash = hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]
        self.fields = []        # ordered: first matching field wins
        self._f = {}
        for name, f in (d.get("fields") or {}).items():
            if name.startswith("_"):
                continue
            e = {"name": name, "label": f.get("label", name), "titles": _rx(f.get("titles")),
                 "exclude": _rx(f.get("exclude")), "exclude_unless": _rx(f.get("exclude_unless")),
                 "credible": int(f.get("credible_years", 2)), "bonus": int(f.get("bonus", 0))}
            self.fields.append(e)
            self._f[name] = e
        self.search_terms = list(d.get("search_terms") or ["support", "help desk", "IT", "implementation", "remote"])

        loc = d.get("location") or {}
        self.home_state = (loc.get("home_state") or "").upper()
        self.home_state_name = US_STATES.get(self.home_state, "")
        self.local = _places(loc.get("local"))
        self.nearby = _places(loc.get("nearby"))
        self.remote_ok = bool(loc.get("remote", True))
        self.local_bonus = int(loc.get("local_bonus", 8))
        self.home_remote_bonus = int(loc.get("home_state_remote_bonus", 15))
        self.home_only = home_only_regex(self.home_state_name) if self.home_state_name else None

        pay = d.get("pay") or {}
        self.min_year, self.min_hour = int(pay.get("minimum_year", 0)), float(pay.get("minimum_hour", 0))
        self.floor_year, self.floor_hour = int(pay.get("floor_year", self.min_year)), float(pay.get("floor_hour", self.min_hour))

        self.rules = dict(DEFAULT_RULES)
        for k, v in (d.get("rules") or {}).items():
            if k in DEFAULT_RULES and v in RULE_VALUES:
                self.rules[k] = v
        self.engineering_ok = _rx(d.get("engineering_ok"))
        self.exclude_titles = _rx(d.get("exclude_titles"))
        self.quota_fields = set(d.get("quota_fields") or [])
        self.stretch_titles = [(_rx(s["titles"]), s.get("reason", "stretch title")) for s in d.get("stretch_titles") or [] if s.get("titles")]
        ind = d.get("industry_skip") or {}
        self.industry_regex = _rx(ind.get("regex"))
        self.industry_fields = set(ind.get("fields") or [])
        self.employer_bonus = [(_rx(b["regex"]), set(b.get("fields") or []), int(b.get("points", 3)), b.get("reason", "employer bonus"))
                               for b in d.get("employer_bonus") or [] if b.get("regex")]
        # employers whose postings are always skipped (mass posters, fake listings); the reason shows in the skip
        self.blocked_employers = [(_rx(b["regex"]), b.get("reason", "blocked employer"))
                                  for b in d.get("blocked_employers") or [] if b.get("regex")]
        self.keywords = list(dict.fromkeys(d.get("keywords") or []))
        lead = d.get("leadership") or {}
        self.leadership_stretch_years = int(lead.get("stretch_min_years", 8))
        self.senior_stretch_below = int(lead.get("senior_stretch_below_years", 5))

    def field(self, name):
        return self._f.get(name)

    def credible(self, field):
        return (self._f.get(field) or {}).get("credible", 2)

    def label(self, field):
        return (self._f.get(field) or {}).get("label", field)

    def bonus(self, field):
        return (self._f.get(field) or {}).get("bonus", 0)

    def rule(self, name):
        return self.rules.get(name, "skip")


_P = None


def load(force=False, path=None):
    global _P
    if _P is not None and not force and path is None:
        return _P
    d, src = None, ""
    for p in ([path] if path else [config.PROFILE_FILE, EXAMPLE]):
        try:
            with open(p, encoding="utf8") as f:
                d, src = json.load(f), p
            break
        except (OSError, ValueError):
            continue
    prof = Profile(d or {}, src)
    if path is None:
        _P = prof
    return prof
