"""Filter-rule tests against examples/profile.example.json: one case per hard rule and the main positives.
The example job seeker wants support / IT / implementation / community roles, remote in the US or on-site
in Asheville, NC, and doesn't drive."""
import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from jobatlas import profile, score  # noqa: E402

P = profile.load(path=profile.EXAMPLE)
GOOD = ("This is a fully remote position open to candidates anywhere in the United States. "
        "Requirements: 1+ years of experience in technical support. Bachelor's degree or equivalent experience. "
        "Monday through Friday schedule. Salary: $55,000 - $65,000 per year. Benefits include medical, dental and vision.")


def a(title, desc=GOOD, location="Remote - US", workplace="", company="Acme Software", prof=P, **kw):
    j = {"title": title, "description": desc, "location": location, "workplace": workplace, "company": company}
    j.update(kw)
    return score.assess(j, prof, company=company)


def variant(**changes):
    """The example profile with some top-level keys or rules replaced."""
    d = copy.deepcopy(P.raw)
    for k, v in changes.items():
        if k == "rules":
            d["rules"].update(v)
        else:
            d[k] = v
    return profile.Profile(d, "test")


class TestSkips(unittest.TestCase):
    def assertSkip(self, r, needle):
        self.assertEqual(r["tier"], "skip", r)
        self.assertTrue(any(needle.lower() in s.lower() for s in r["skips"]), r["skips"])

    def test_baseline_is_strong(self):
        r = a("Technical Support Specialist")
        self.assertEqual(r["tier"], "strong", r)
        self.assertEqual(r["facts"]["location_class"], "remote_us")

    def test_quota_sales_title(self):
        self.assertSkip(a("Account Executive, SMB"), "sales")

    def test_engineering_title(self):
        self.assertSkip(a("Senior Software Engineer"), "engineering")
        self.assertNotEqual(a("Technical Support Engineer")["tier"], "skip")

    def test_clearance(self):
        self.assertSkip(a("IT Specialist", GOOD + " Must be able to obtain a Secret clearance."), "clearance")

    def test_hard_degree(self):
        d = GOOD.replace("Bachelor's degree or equivalent experience.", "Bachelor's degree in Computer Science required.")
        self.assertSkip(a("Help Desk Analyst", d), "degree")

    def test_pay_under_minimum(self):
        self.assertSkip(a("Support Specialist", GOOD.replace("$55,000 - $65,000 per year", "$16 - $18 per hour")), "pay")

    def test_pay_under_floor_without_health(self):
        d = "Fully remote position in the US. Pay: $20 - $21 per hour. 1+ years of support experience."
        self.assertSkip(a("Support Specialist", d), "floor")

    def test_pay_under_floor_with_health_is_flagged(self):
        d = "Fully remote position in the US. Pay: $20 - $21 per hour. Health insurance provided. 1+ years of support experience."
        r = a("Support Specialist", d)
        self.assertNotEqual(r["tier"], "skip")
        self.assertTrue(any("under the floor" in f for f in r["flags"]))

    def test_monthly_pay_no_benefits(self):
        d = ("Remote. Compensation: $2.5K - $4K/month. This is a contractor role with no benefits. "
             "Must be available to work weekends.")
        r = a("Technical Customer Support", d)
        self.assertSkip(r, "benefits")
        self.assertEqual(r["facts"]["pay_top"], 48000)

    def test_weekend_required(self):
        self.assertSkip(a("Technical Support Specialist", GOOD.replace("Monday through Friday schedule.",
                                                                       "Shifts are Wednesday through Sunday.")), "weekend")
        self.assertSkip(a("Technical Support Specialist", GOOD + " This role includes rotating weekends."), "weekend")

    def test_weekend_unclear_is_flag(self):
        r = a("Technical Support Specialist", GOOD.replace("Monday through Friday schedule.", "Occasional evenings and weekends as needed."))
        self.assertNotEqual(r["tier"], "skip")
        self.assertEqual(r["facts"]["weekend"], "unclear")

    def test_weekend_on_call(self):
        self.assertSkip(a("Support Engineer", GOOD + " Participate in weekday and weekend on-call rotations."), "weekend")

    def test_state_list_excludes_home(self):
        d = GOOD + " We are only able to hire in the following states: Texas, Florida, Georgia, Oregon."
        self.assertSkip(a("Technical Support Specialist", d), "not NC")

    def test_state_list_includes_home(self):
        d = GOOD + " Candidates must reside in one of the following states: CA, OR, NC, ID."
        r = a("Technical Support Specialist", d)
        self.assertNotEqual(r["tier"], "skip")
        self.assertTrue(any("NC is on its list" in x for x in r["reasons"]))

    def test_cannot_hire_in_home_state(self):
        d = GOOD + " We are unable to hire in California, North Carolina, or New York at this time."
        self.assertSkip(a("Technical Support Specialist", d), "north carolina")

    def test_must_reside_other_state(self):
        d = GOOD.replace("anywhere in the United States", "who reside in Texas") + " Candidates must reside in Texas."
        self.assertSkip(a("Technical Support Specialist", d), "not NC")

    def test_onsite_elsewhere(self):
        self.assertSkip(a("IT Support Specialist", "On-site role in our Denver office. 1 year of experience.",
                          location="Denver, CO", workplace="onsite"), "not remote")

    def test_hybrid_elsewhere(self):
        d = "This is a hybrid role, 3 days a week in our Charlotte office. 1+ years of IT support experience."
        self.assertSkip(a("IT Support Specialist", d, location="Charlotte, NC", workplace="hybrid"), "not remote")

    def test_remote_text_overrides_city_tag(self):
        r = a("IT Support Specialist", GOOD, location="Boston, MA")
        self.assertEqual(r["facts"]["location_class"], "remote_us")

    def test_non_us_remote(self):
        self.assertSkip(a("Customer Support Specialist", GOOD, location="Remote - Canada"), "outside the US")

    def test_quota_language_in_quota_field(self):
        self.assertSkip(a("Implementation Specialist", GOOD + " You will carry a renewal quota and earn commission."), "quota")

    def test_industry_skip(self):
        self.assertSkip(a("Implementation Specialist", GOOD, company="First Mortgage Lending"), "unrelated industry")

    def test_drivers_licence(self):
        self.assertSkip(a("IT Technician", GOOD + " A valid driver's license is required."), "licen")

    def test_internship(self):
        self.assertSkip(a("IT Support Intern"), "intern")
        self.assertSkip(a("Student Lab Technician"), "student")

    def test_non_it_technician(self):
        self.assertEqual(a("CNA / Patient Care Technician")["tier"], "skip")
        self.assertEqual(a("Field Technician - Appalachian Corridor")["tier"], "skip")
        self.assertEqual(a("Inventory Control Technician")["tier"], "skip")
        self.assertNotEqual(a("Desktop Support Technician")["tier"], "skip")

    def test_aggregator_repost(self):
        self.assertSkip(a("Technical Support Specialist", company="Jobgether"), "reposting")

    def test_ote_any_field(self):
        self.assertSkip(a("AI Operations Specialist", GOOD + " OTE Range: $150,000 to $200,000."), "OTE")

    def test_excluded_title(self):
        self.assertSkip(a("Product Manager, Onboarding and Growth"), "product manager")
        self.assertSkip(a("Associate Talent Acquisition Partner"), "talent acquisition")

    def test_title_names_region(self):
        self.assertSkip(a("Customer Support Specialist - DACH", location="Remote job"), "dach")

    def test_title_names_other_state(self):
        self.assertEqual(a("Field Support Representative - Virginia")["tier"], "skip")
        self.assertNotEqual(a("Technical Support Specialist - North Carolina")["tier"], "skip")
        self.assertNotEqual(a("Help Desk Technician (Tier 1)")["tier"], "skip")

    def test_state_tied_remote_tag(self):
        self.assertEqual(a("Support Specialist", location="Remote, Idaho")["tier"], "skip")
        self.assertNotEqual(a("Support Specialist", location="Remote, North Carolina")["tier"], "skip")

    def test_office_tag_needs_plain_remote_text(self):
        d = GOOD.replace("This is a fully remote position open to candidates anywhere in the United States. ",
                         "Our team works remotely on Fridays. ")
        self.assertSkip(a("AI Operations Associate", d, location="New York Office / Los Angeles Office"), "not remote")

    def test_city_only_tag_outside_us(self):
        self.assertEqual(a("Implementation Engineer", location="Bogota")["tier"], "skip")
        self.assertEqual(a("IT Administrator", location="Remote - Europe (New York)")["tier"], "skip")

    def test_negated_remote_is_onsite(self):
        job = {"title": "IT Customer Support - Entry", "location": "Raleigh, NC", "workplace": ""}
        cls, _ = score.location_class(job, "Great team. This position is not eligible for remote work. Telework is great elsewhere.", P)
        self.assertNotIn(cls, score.REMOTE_CLASSES)

    def test_talent_community(self):
        self.assertEqual(a("Join our Talent Community!")["tier"], "skip")

    def test_outside_fields(self):
        self.assertSkip(a("Registered Nurse"), "outside the profile's fields")


class TestPositives(unittest.TestCase):
    def test_home_state_only_remote_bonus(self):
        d = ("This position is fully remote; however, you must reside in North Carolina. "
             "Two years of IT customer support experience. $52,000-$68,000 annually. Health insurance.")
        r = a("IT Customer Support - Entry", d, location="Raleigh, NC")
        self.assertEqual(r["facts"]["location_class"], "home_state_remote")
        self.assertEqual(r["tier"], "strong")
        self.assertTrue(any("North Carolina-residents-only" in x for x in r["reasons"]))
        base = a("IT Customer Support - Entry", d.replace("however, you must reside in North Carolina", "open across the US"))
        self.assertGreater(r["score"], base["score"])

    def test_washington_dc_is_not_washington_state(self):
        wa = variant(location={"home_state": "WA", "local": [], "remote": True})
        d = GOOD + " You must reside in Washington, D.C."
        self.assertNotEqual(a("Support Specialist", d, prof=wa)["facts"]["location_class"], "home_state_remote")
        d = GOOD + " You must reside in Washington State."
        self.assertEqual(a("Support Specialist", d, prof=wa)["facts"]["location_class"], "home_state_remote")

    def test_local_onsite_ok(self):
        r = a("IT Support Technician", "On-site in Asheville. 1 year of help desk experience. $50,000 - $60,000. Medical benefits.",
              location="Asheville, NC", workplace="onsite")
        self.assertNotEqual(r["tier"], "skip")
        self.assertEqual(r["facts"]["location_class"], "local")

    def test_nearby_flagged_and_capped(self):
        r = a("IT Support Technician", "On-site. 1 year of help desk experience.", location="Black Mountain, NC", workplace="onsite")
        self.assertNotEqual(r["tier"], "skip")
        self.assertNotEqual(r["tier"], "strong")
        self.assertTrue(any("check the commute" in f for f in r["flags"]))

    def test_leadership_title(self):
        self.assertEqual(a("Head of Trust & Safety")["verdict"], "fair")      # community field: 6 credible years
        self.assertEqual(a("IT & Information Security Manager")["verdict"], "long")
        self.assertEqual(a("Community Manager")["verdict"], "strong")        # a normal title, not leadership

    def test_fair_verdict_caps_tier(self):
        r = a("Head of Trust & Safety")
        self.assertEqual(r["verdict"], "fair")
        self.assertNotEqual(r["tier"], "strong")

    def test_stretch_title(self):
        r = a("Video Producer, Community")
        self.assertEqual(r["verdict"], "fair")
        self.assertTrue(any("no editing portfolio" in x for x in r["reasons"]))

    def test_years_over_reach(self):
        r = a("Technical Support Specialist", GOOD.replace("1+ years", "8+ years"))
        self.assertEqual(r["verdict"], "long")
        self.assertIn(r["tier"], ("fair", "long"))

    def test_years_within_credible(self):
        r = a("Community Manager", GOOD.replace("1+ years of experience in technical support", "5+ years of community experience"))
        self.assertEqual(r["verdict"], "strong")
        self.assertEqual(r["field"], "community")

    def test_keywords_and_employer_bonus(self):
        r = a("Community Specialist", GOOD + " Our stack: Discord, OBS and n8n. We build tools for streaming creators.")
        self.assertTrue(any(x.startswith("mentions tools you know") for x in r["reasons"]))
        self.assertIn("games/streaming employer", r["reasons"])

    def test_prefilter(self):
        self.assertEqual(score.prefilter({"title": "Staff Accountant", "location": "Remote"}, P), "off-target")
        self.assertEqual(score.prefilter({"title": "Help Desk Technician", "location": "London, UK"}, P), "non-us")
        self.assertEqual(score.prefilter({"title": "Help Desk Technician", "location": "Remote (US)"}, P), "pass")
        self.assertEqual(score.prefilter({"title": "Help Desk Technician", "location": "Asheville, NC", "workplace": "onsite"}, P), "pass")
        self.assertEqual(score.prefilter({"title": "Help Desk Technician", "location": "Dallas, TX", "workplace": "onsite"}, P), "onsite")


class TestProfileRules(unittest.TestCase):
    def test_rule_can_be_a_flag(self):
        lenient = variant(rules={"drivers_licence": "flag", "weekend_required": "ignore"})
        d = GOOD + " A valid driver's license is required. This role includes rotating weekends."
        r = a("IT Technician", d, prof=lenient)
        self.assertNotEqual(r["tier"], "skip", r)
        self.assertTrue(any("licence" in f for f in r["flags"]))
        self.assertFalse(any("weekend" in f for f in r["flags"]))

    def test_onsite_only_profile(self):
        local_only = variant(location={"home_state": "NC", "local": ["Asheville"], "remote": False})
        self.assertEqual(a("Technical Support Specialist", prof=local_only)["tier"], "skip")

    def test_no_home_state(self):
        anywhere = variant(location={"remote": True})
        d = GOOD + " We are only able to hire in the following states: Texas, Florida."
        self.assertNotEqual(a("Technical Support Specialist", d, prof=anywhere)["tier"], "skip")


class TestParsers(unittest.TestCase):
    def test_pay_top(self):
        self.assertEqual(score.pay_of("Base salary range: $80,000 - $95,000")[:2], ("year", 95000))
        self.assertEqual(score.pay_of("Hourly rate: $24.50 - $28.00 per hour")[:2], ("hour", 28.0))
        self.assertEqual(score.pay_of("$6,189 - $8,327 per month")[:2], ("year", 8327 * 12))
        self.assertEqual(score.pay_of("We raised $50M in funding")[:2], (None, None))

    def test_years(self):
        req, pref = score.years_asked("Requirements: 3+ years of experience in customer support. 5 years of experience preferred.")
        self.assertEqual([n for n, _ in req], [3])
        self.assertEqual([n for n, _ in pref], [5])

    def test_degree(self):
        self.assertEqual(score.degree_of("Bachelor's degree or equivalent experience.")[0], "equivalent_ok")
        self.assertEqual(score.degree_of("Bachelor's degree required.")[0], "required")
        self.assertEqual(score.degree_of("Bachelor's degree preferred.")[0], "preferred")

    def test_profile_loaded(self):
        self.assertEqual((P.floor_year, P.min_year, P.home_state), (45000, 40000, "NC"))
        self.assertEqual([f["name"] for f in P.fields][:2], ["community", "automation_ai"])


if __name__ == "__main__":
    unittest.main()
