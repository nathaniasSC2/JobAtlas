"""Regression tests for the misreads found on 2026-11-08, each on the real posting (tests/fixtures/misreads.json,
tests/fixtures/lever_apply.html). The example job seeker lives in North Carolina; WA below is the same profile
with Washington as the home state, for the postings whose wording names Washington."""
import copy
import datetime
import json
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from jobatlas import db, poll, profile, score  # noqa: E402
from jobatlas.adapters import enterprise, lever  # noqa: E402
from jobatlas.net import Response  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
with open(os.path.join(FIX, "misreads.json"), encoding="utf8") as f:
    POSTINGS = json.load(f)
with open(os.path.join(FIX, "lever_apply.html"), encoding="utf8") as f:
    PDI_FORM = f.read()

P = profile.load(path=profile.EXAMPLE)
_wa = copy.deepcopy(P.raw)
_wa["location"] = dict(_wa["location"], home_state="WA", local=["Olympia"], nearby=["Lacey", "Tumwater"])
WA = profile.Profile(_wa, "test-wa")
FRESH = "This is a fully remote position open to candidates anywhere in the United States. 1+ years of support experience. " \
        "Salary: $55,000 - $65,000 per year. Benefits include medical, dental and vision."


def posting(name, **changes):
    j = copy.deepcopy(POSTINGS[name])
    j.update(changes)
    return j


def assess(job, prof=P):
    return score.assess(job, prof, company=job.get("company") or "")


def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


class LocationMisreads(unittest.TestCase):
    def assertSkipped(self, r, needle):
        self.assertEqual(r["tier"], "skip", r)
        self.assertTrue(any(needle.lower() in s.lower() for s in r["skips"]), r["skips"])

    def test_xsolla_russia_uses_the_boards_country(self):
        j = posting("xsolla_russia")
        r = assess(j)
        self.assertEqual(r["facts"]["location_class"], "remote_non_us")
        self.assertSkipped(r, "the board says RU")
        self.assertEqual(score.prefilter(dict(j, title="Technical Support Specialist"), P), "non-us")
        # "Russia" alone in the tag is non-US too, without the board's country
        self.assertEqual(score.location_class(dict(j, raw={}), j["description"], P)[0], "remote_non_us")

    def test_rws_nepal_city_list_is_non_us(self):
        j = posting("rws_nepal")
        self.assertEqual(assess(j)["facts"]["location_class"], "remote_non_us")
        self.assertEqual(score.prefilter(j, P), "non-us")

    def test_a_us_tag_beats_a_foreign_board_country(self):
        j = posting("rws_nepal", location="Remote - US")
        self.assertEqual(score.location_class(j, FRESH, P)[0], "remote_us")

    def test_board_countries_that_include_the_us(self):
        j = {"title": "Technical Support Engineer", "location": "Remote", "workplace": "remote"}
        # PostHog's Ashby address, 2026-11-08
        self.assertEqual(score.location_class(dict(j, raw={"address": {"postalAddress": {"addressCountry": "US | EU"}}}), FRESH, P)[0],
                         "remote_us")
        # Recruitee: the first location is abroad, another is in the US
        rec = {"country": "Ukraine", "locations": [{"country": "Ukraine"}, {"country": "United States", "country_code": "US"}]}
        self.assertEqual(score.location_class(dict(j, raw=rec), FRESH, P)[0], "remote_us")
        self.assertEqual(score.location_class(dict(j, raw={"country": "Ukraine", "locations": [{"country": "Ukraine"}]}), FRESH, P)[0],
                         "remote_non_us")

    def test_eplus_remote_only_for_virginia(self):
        r = assess(posting("eplus_remote_virginia"))
        self.assertSkipped(r, "remote tied to VA")
        self.assertSkipped(assess(posting("eplus_remote_virginia"), WA), "remote tied to VA")
        # the same tag with the home state's remote part is fine
        ok = assess(posting("eplus_remote_virginia", location="Durham, North Carolina; Herndon, Virginia; Remote - North Carolina"))
        self.assertFalse(any("tied" in s for s in ok["skips"]), ok["skips"])

    def test_workday_remote_state_lists(self):
        # real tags, once Workday's "6 Locations" was replaced by the detail's list (2026-11-08)
        tied = score.remote_tied_states
        self.assertEqual(tied("USA PA Remote / USA IN Remote / USA IL Remote / USA WI Remote / USA TX Remote"),
                         {"PA", "IN", "IL", "WI", "TX"})                                       # Baker Tilly
        self.assertEqual(tied("US - Remote, CO / US - OR, Lake Oswego"), {"CO"})               # Trimble
        self.assertIn("WA", tied("US, CA, Santa Clara / US, NC, Remote / US, WA, Remote / US, CA, Remote"))    # Nvidia
        self.assertIn("WA", tied("Remote - New York / Remote - Delaware / Remote - Washington / Remote - Tennessee (EST)"))
        self.assertEqual(tied("Remote - Ontario / Remote - Alaska"), {"AK"})                  # the Canadian part doesn't count
        self.assertEqual(tied("US - Remote, OH"), {"OH"})
        self.assertEqual(tied("Remote - Virginia"), {"VA"})
        for us_wide in ("New York, NY, USA / San Francisco, CA, USA / Remote, USA", "Remote - US", "Remote / Atlanta, Georgia",
                        "Remote; Charlotte, NC preferred", "Remote, United States (Texas preferred)", "Atlanta, GA preferred, Remote",
                        # Ashby adds the office's state; SpotMe's time zones aren't Connecticut
                        "San Francisco, CA or Remote (USA) (California)", "Remote in the United States (California)",
                        "Williamsburg, NY / Remote - US (New York)", "Remote (US (ET or CT))", "San Francisco, CA or Remote, US"):
            self.assertEqual(tied(us_wide), set(), us_wide)
        r = assess({"title": "Payroll Support Specialist", "description": FRESH,
                    "location": "USA PA Remote / USA WI Remote / USA TX Remote"}, WA)
        self.assertTrue(any("remote tied to PA, TX, WI" in s for s in r["skips"]), r["skips"])

    def test_modsquad_excludes_washington_residents(self):
        j = posting("modsquad_excludes_wa", raw={})       # the text alone, without the board's country
        self.assertSkipped(assess(j, WA), "can't hire in Washington")
        self.assertFalse(any("hire in" in s for s in assess(j, P)["skips"]))      # NC isn't excluded

    def test_modsquad_foreign_locations(self):
        j = posting("modsquad_excludes_wa")
        self.assertEqual(j["raw"]["_detail"]["country"]["descriptor"], "Brazil")
        # "8 Locations": Workday's country is only the first of them, so it decides nothing
        self.assertNotEqual(assess(j)["facts"]["location_class"], "remote_non_us")
        self.assertEqual(assess(dict(j, location="BRA Remote"))["facts"]["location_class"], "remote_non_us")
        k = posting("modsquad_excludes_wa", raw={}, location="PHL Remote / IDN Remote / JAM Remote")
        self.assertEqual(assess(k)["facts"]["location_class"], "remote_non_us")
        us = posting("modsquad_excludes_wa", raw={}, location="TX Remote / PHL Remote")
        self.assertNotEqual(assess(us)["facts"]["location_class"], "remote_non_us")

    def test_lyft_hybrid_three_days_in_nyc_office(self):
        j = posting("lyft_hybrid_nyc")
        self.assertGreater(j["description"].find("hybrid schedule"), 4000)      # past the first 4000 characters
        self.assertEqual(score.location_class(j, j["description"], P)[0], "onsite_other")
        self.assertEqual(score.location_class(j, j["description"], WA)[0], "onsite_other")

    def test_work_from_anywhere_for_four_weeks_is_not_remote(self):
        d = "Hybrid roles have the flexibility to work from anywhere for up to 4 weeks per year."
        self.assertEqual(score.location_class({"title": "Support Specialist", "location": "Austin, TX"}, d, P)[0], "onsite_other")

    def test_uw_predominantly_onsite_and_occasional_telework(self):
        j = posting("uw_predominantly_onsite")
        self.assertEqual(j["workplace"], "occasional telework")
        self.assertEqual(score.norm_workplace(j["workplace"]), "hybrid")
        self.assertEqual(score.location_class(j, j["description"], WA)[0], "onsite_other")
        # the text alone says so too
        k = dict(j, workplace="")
        self.assertEqual(score.location_class(k, j["description"], WA)[0], "onsite_other")

    def test_workday_telework_tags(self):
        self.assertEqual(score.norm_workplace("No Telework"), "onsite")
        self.assertEqual(score.norm_workplace("Occasional Telework"), "hybrid")
        self.assertEqual(score.norm_workplace("Medical Centers Only - 4 days remote per week"), "hybrid")
        self.assertEqual(score.norm_workplace("Remote or Hybrid"), "remote")
        self.assertEqual(score.norm_workplace("Full Remote"), "remote")
        self.assertEqual(score.norm_workplace("remote (100% telework)"), "remote")


class FormMisreads(unittest.TestCase):
    def test_lever_form_parser_reads_pdis_questions(self):
        qs = lever.form_questions(PDI_FORM)
        labels = [q["label"] for q in qs]
        self.assertEqual(len(qs), 10, labels)
        self.assertTrue(all("@" not in x and "Full name" not in x for x in labels))     # built-in fields left out
        near = next(q for q in qs if "located near" in q["label"])
        self.assertTrue(near["required"])
        self.assertEqual(near["options"], ["Yes", "No"])

    def test_pdi_form_requires_living_near_dallas_alpharetta_or_temple(self):
        j = posting("pdi_form_dallas")
        before = assess(j)
        self.assertNotEqual(before["tier"], "skip", before)
        self.assertTrue(any("tagged only to" in f for f in before["flags"]), before["flags"])
        j["questions"] = lever.form_questions(PDI_FORM)
        r = assess(j)
        self.assertEqual(r["tier"], "skip")
        self.assertTrue(any("located near Alpharetta" in s for s in r["skips"]), r["skips"])
        self.assertTrue(any("$18.00/hr" in s for s in r["skips"]), r["skips"])         # the form's set rate
        self.assertEqual(r["facts"]["pay_top"], 18)

    def test_form_state_list(self):
        q = [{"label": "To be considered for this position, you must reside in one of the following states: AZ, CO, FL, GA, ID, "
                       "IA, KS, MD, MA, MN, MO, NE, NV, NH, NJ, NC, OK, OR, SC, TN, TX, VA. Do you live in one of these states?",
              "required": True, "type": "input_text", "options": []}]
        j = {"title": "Technical Support Specialist", "description": FRESH, "location": "Remote - US", "questions": q}
        self.assertNotEqual(assess(j, P)["tier"], "skip")             # NC is on the list
        r = assess(j, WA)
        self.assertEqual(r["tier"], "skip")
        self.assertTrue(any("application form limits it" in s for s in r["skips"]), r["skips"])

    def test_form_questions_that_dont_gate(self):
        soft = ["Are you open to occasional on-site visits to our Denver office?", "Are you located in the United States?",
                "Do you have experience providing on-site support?", "Where are you located, and would you be interested in "
                "attending one of our offices on a hybrid model? (This is optional for this role)",
                "Are you located near Asheville, NC?"]
        for label in soft:
            j = {"title": "Technical Support Specialist", "description": FRESH, "location": "Remote - US",
                 "questions": [{"label": label, "required": True, "type": "multi_value_single_select", "options": ["Yes", "No"]}]}
            self.assertNotEqual(assess(j)["tier"], "skip", label)

    def gate(self, q, prof=P):
        r = assess({"title": "Technical Support Specialist", "description": FRESH, "location": "Remote - US", "questions": [q]}, prof)
        return r["tier"], [x for x in r["skips"] + r["flags"] if "application form" in x]

    def test_real_greenhouse_questions(self):
        # Tailscale: the country, not a place
        self.assertEqual(self.gate({"label": "Are you located in or willing to relocate to the country for which you are applying?",
                                    "required": True, "options": ["Yes", "No"]})[1], [])
        # DEPT: a "None" answer means a remote applicant may be anywhere
        t, why = self.gate({"label": "Do you live within 50 miles or a 60 minute commute to one of the listed locations?", "required": True,
                            "options": ["Salt Lake City, UT (remote hub)", "Denver, CO (Hybrid)", "New York, NY (remote hub)", "None"]})
        self.assertNotEqual(t, "skip")
        self.assertEqual(len(why), 1)
        # EnergyHub: the state list is in the question's description, and Washington is on it
        energyhub = {"label": "This role requires you to be based in one of the following eligible states prior to your start date. Do you "
                              "currently reside in, or are you willing and able to relocate to, one of the states listed below before your "
                              "start date?", "required": True,
                     "options": ["Yes, I currently reside in one of the listed states",
                                 "Yes, I am willing and able to relocate to one of the listed states prior to my start date",
                                 "No, I am not able to meet this requirement"],
                     "description": "Arizona, California, Colorado, Connecticut, Florida, Georgia, Illinois, Idaho, Maine, Maryland, "
                                    "Massachusetts, Michigan, Minnesota, Missouri, New Jersey, New York, North Carolina, Oregon, Pennsylvania, "
                                    "Tennessee, Texas, Utah, Vermont, Virginia, Washington, Wisconsin"}
        self.assertEqual(self.gate(energyhub, WA)[1], [])
        self.assertEqual(self.gate(energyhub, P)[1], [])            # North Carolina too
        without = dict(energyhub, description=energyhub["description"].replace("Washington, ", ""))
        self.assertEqual(self.gate(without, WA)[0], "skip")
        cut = dict(without, description=(without["description"] + ", " + "Wyoming, " * 3)[:300])     # may go on past 300
        self.assertNotEqual(self.gate(cut, WA)[0], "skip")
        # Mixpanel: 12 options (the adapter keeps 12) and the home state not among them: can't tell
        states = ["No and not willing to relocate", "Arizona", "California", "Colorado", "Connecticut", "Florida", "Georgia", "Idaho",
                  "Illinois", "Indiana", "Massachusetts", "Maryland"]
        t, why = self.gate({"label": "Are you currently residing or willing to relocate to one of the following states?",
                            "required": True, "options": states}, WA)
        self.assertNotEqual(t, "skip")
        self.assertEqual(len(why), 1)
        # Judi Health: a hybrid schedule in offices elsewhere
        self.assertEqual(self.gate({"label": "Are you able to work a hybrid schedule (at least 3 days per week) in our New York City, "
                                             "Denver or Charlotte offices?", "required": True, "options": ["Yes", "No"]})[0], "skip")

    def test_an_optional_gate_is_a_flag(self):
        q = [{"label": "Are you located near Plainfield, IN and able to sit on-site at that location?", "required": False}]
        r = assess({"title": "Technical Support Specialist", "description": FRESH, "location": "Remote - US", "questions": q})
        self.assertNotEqual(r["tier"], "skip")
        self.assertTrue(any("Plainfield" in f for f in r["flags"]), r["flags"])

    def test_a_local_job_ignores_the_forms_on_site_question(self):
        q = [{"label": "This role is on-site 5 days a week in Asheville. Are you able to commute?", "required": True}]
        r = assess({"title": "IT Support Technician", "description": FRESH.replace("fully remote", "on-site"),
                    "location": "Asheville, NC", "workplace": "onsite", "questions": json.dumps(q)})
        self.assertEqual(r["facts"]["location_class"], "local")
        self.assertNotEqual(r["tier"], "skip", r)


class StalePostings(unittest.TestCase):
    def test_double_487_days_is_skipped(self):
        j = posting("double_487_days", baseline=1, first_seen="2026-10-19T16:56:05+00:00")
        r = assess(j)
        self.assertEqual(r["tier"], "skip", r)
        self.assertTrue(any("stale posting" in s for s in r["skips"]), r["skips"])

    def test_older_than_stale_days_is_at_most_fair(self):
        j = {"title": "Technical Support Specialist", "description": FRESH, "location": "Remote - US", "posted_at": days_ago(200)}
        r = assess(j)
        self.assertEqual(r["tier"], "fair", r)
        self.assertTrue(any("probably stale" in f for f in r["flags"]), r["flags"])
        self.assertEqual(assess(dict(j, posted_at=days_ago(10)))["tier"], "strong")

    def test_a_relisted_posting_counts_from_when_it_appeared(self):
        j = {"title": "Technical Support Specialist", "description": FRESH, "location": "Remote - US", "ats": "lever",
             "posted_at": days_ago(300), "first_seen": days_ago(2) + "T10:00:00+00:00", "baseline": 0}
        r = assess(j)
        self.assertEqual(r["tier"], "strong", r)
        self.assertEqual(r["facts"]["age_days"], 2)
        self.assertTrue(r["facts"].get("relisted"))
        # on a board's first (baseline) poll, or on a searched board, the board's date stands
        self.assertEqual(assess(dict(j, baseline=1))["tier"], "fair")
        self.assertEqual(assess(dict(j, ats="workday"))["tier"], "fair")

    def test_a_local_always_open_posting_stays_at_fair(self):
        j = {"title": "IT Support Technician", "description": FRESH.replace("fully remote", "on-site"), "location": "Asheville, NC",
             "workplace": "onsite", "posted_at": days_ago(1955)}
        r = assess(j)
        self.assertEqual((r["facts"]["location_class"], r["tier"]), ("local", "fair"), r)

    def test_freshness_is_a_profile_setting(self):
        d = copy.deepcopy(P.raw)
        d["freshness"] = {"stale_days": 0, "skip_days": 0}
        off = profile.Profile(d, "test")
        r = assess(posting("double_487_days", baseline=1), off)
        self.assertFalse(any("stale" in s for s in r["skips"] + r["flags"]), r)
        self.assertEqual(profile.check(d), [])
        d["freshness"] = {"stale_days": "soon"}
        self.assertTrue(any("freshness.stale_days" in e for e in profile.check(d)))


class FieldMisreads(unittest.TestCase):
    def test_uw_ecg_technician_is_not_it_support(self):
        j = posting("uw_ecg_technician")
        self.assertIsNone(score.field_of(j["title"], P))
        r = assess(j, WA)
        self.assertEqual(r["tier"], "skip")
        self.assertTrue(any("outside the profile's fields" in s for s in r["skips"]), r["skips"])
        self.assertEqual(r["facts"]["location_class"], "onsite_other")      # "No Telework" in Seattle
        for t in ("IT Support Technician - UW Medical Center", "Desktop Support Technician", "Computer Technician (Clinical Systems)"):
            self.assertEqual(score.field_of(t, P), "it_support", t)
        for t in ("EKG Technician", "Telemetry Technician - Cardiac Unit", "Technician, Patient Care"):
            self.assertIsNone(score.field_of(t, P), t)


class FakeOracle:
    def __init__(self, bad_finders=("ByReqNumber",)):
        self.bad, self.calls = bad_finders, []

    def get(self, url, **kw):
        self.calls.append(url)
        if any("finder=%s;" % f in url for f in self.bad):
            return Response(400, {}, b"URL request parameter finder with value ... is not valid.", url)
        body = {"items": [{"Title": "Customer Service Clerk", "WorkplaceType": "",
                           "ExternalDescriptionStr": "<p>Help customers at the front end.</p>"}]}
        return Response(200, {}, json.dumps(body).encode(), url)


class OracleDetails(unittest.TestCase):
    def setUp(self):
        enterprise.Oracle._finder.clear()

    def test_detail_uses_the_byid_finder(self):
        c = FakeOracle()
        d = enterprise.Oracle.detail(c, "eofd.fa.us6.oraclecloud.com|CX_1001", {"ext_id": "748367"})
        self.assertIn("Help customers", d["description"])
        self.assertIn("finder=ById;Id=%22748367%22,siteNumber=CX_1001", c.calls[0])
        enterprise.Oracle.detail(c, "eofd.fa.us6.oraclecloud.com|CX_1001", {"ext_id": "1"})
        self.assertEqual(len(c.calls), 2)           # the working finder is remembered per host

    def test_an_older_tenant_falls_back_to_byreqnumber(self):
        c = FakeOracle(bad_finders=("ById",))
        d = enterprise.Oracle.detail(c, "old.fa.us2.oraclecloud.com|CX_1", {"ext_id": "9"})
        self.assertIn("Help customers", d["description"])
        self.assertEqual(len(c.calls), 2)

    def test_failed_details_stop_after_detail_tries(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO jobs(ats, token, ext_id, title, url, raw, detail_state, prefilter, first_seen) VALUES "
                     "('oracle', 'h|s', '1', 'Customer Service Clerk', 'u', '{}', 'failed', 'pass', ?)", (db.now(),))
        jid = conn.execute("SELECT id FROM jobs").fetchone()[0]
        for i in range(poll.DETAIL_TRIES):
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            state = poll.detail_failed(conn, row, "HTTP 400")
        self.assertEqual(state, "gave_up")
        raw = json.loads(conn.execute("SELECT raw FROM jobs WHERE id=?", (jid,)).fetchone()[0])
        self.assertEqual((raw["_detail_fails"], raw["_detail_error"]), (poll.DETAIL_TRIES, "HTTP 400"))


if __name__ == "__main__":
    unittest.main()
