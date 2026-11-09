"""Application forms read on their own: Workable's form JSON, Ashby's and Workday's pages in a browser
(tests/fixtures/forms.json holds what those boards really served on 2026-11-08), the gates their questions
feed, and poll.read_forms (matches first, a time budget, Workday's wall once per board)."""
import copy
import json
import os
import sqlite3
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from jobatlas import alerts, browser as browser_mod, db, duel, poll, profile, score  # noqa: E402
from jobatlas.adapters import ashby, workable, workday  # noqa: E402
from jobatlas.net import Response  # noqa: E402

with open(os.path.join(HERE, "fixtures", "forms.json"), encoding="utf8") as f:
    FORMS = json.load(f)

P = profile.load(path=profile.EXAMPLE)
_wa = copy.deepcopy(P.raw)
_wa["location"] = dict(_wa["location"], home_state="WA", local=["Olympia"], nearby=["Lacey", "Tumwater"])
WA = profile.Profile(_wa, "test-wa")
_lenient = copy.deepcopy(P.raw)
_lenient["rules"] = dict(_lenient.get("rules") or {}, degree_required="ignore", drivers_licence="ignore", weekend_required="flag")
_lenient["required_gaps"] = [{"regex": "\\b(?:cpa|cma)\\b", "reason": "requires a CPA/CMA licence", "effect": "long"},
                             {"regex": "fluen(?:t|cy) in spanish|bilingual", "reason": "asks for Spanish", "effect": "flag"}]
LENIENT = profile.Profile(_lenient, "test-lenient")       # a degree, no car worries, weekends only flagged
FRESH = "This is a fully remote position open to candidates anywhere in the United States. 1+ years of support experience. " \
        "Salary: $55,000 - $65,000 per year. Benefits include medical, dental and vision."


def job_with(questions, **kw):
    j = {"title": "Technical Support Specialist", "description": FRESH, "location": "Remote - US", "workplace": "remote",
         "questions": questions}
    j.update(kw)
    return j


def assess(job, prof=P):
    return score.assess(job, prof, company=job.get("company") or "")


def yes_no(label, required=True, **kw):
    return dict({"label": label, "required": required, "type": "boolean", "options": ["Yes", "No"], "truncated": False}, **kw)


class FakeBrowser:
    """Hands back a recorded page result for each URL (as camofox's evaluate would)."""

    def __init__(self, pages=None, error=None):
        self.pages, self.error, self.urls = pages or {}, error, []

    def read(self, url, script, timeout=None):
        self.urls.append(url)
        if self.error:
            raise browser_mod.BrowserError(self.error)
        for k, v in self.pages.items():
            if url.startswith(k):
                return copy.deepcopy(v)
        raise browser_mod.BrowserError("HTTP 500 no such page")


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append(url)
        for k, (status, body) in self.routes.items():
            if url.startswith(k):
                return Response(status, {}, json.dumps(body).encode() if not isinstance(body, bytes) else body, url)
        return Response(404, {}, b"Not Found", url)

    def allowed(self, url):
        return True


# ----------------------------------------------------------------------------------- Workable
class WorkableForms(unittest.TestCase):
    def read(self, name):
        fx = FORMS[name]
        c = FakeHTTP({"https://apply.workable.com/api/v1/jobs/%s/form" % fx["ext_id"]: (200, fx["body"])})
        return workable.form(c, "better-impact", {"ext_id": fx["ext_id"]}), c

    def test_every_field_with_type_required_and_options(self):
        d, c = self.read("workable_better_impact")
        self.assertEqual(c.calls, ["https://apply.workable.com/api/v1/jobs/6A71681550/form"])
        self.assertEqual(d["state"], "read")
        labels = [q["label"] for q in d["questions"]]
        self.assertEqual(labels[:4], ["First name", "Last name", "Email", "Resume"])     # built-in fields kept too
        country = next(q for q in d["questions"] if q["label"].startswith("In which country"))
        self.assertEqual((country["required"], country["type"], country["options"]),
                         (True, "multi_value_single_select", ["United States", "Canada", "Neither"]))
        hours = next(q for q in d["questions"] if "Pacific or Mountain" in q["label"])
        self.assertEqual(hours["type"], "boolean")
        self.assertTrue(all(q["truncated"] is False for q in d["questions"]))
        # "multiple" fields are radio buttons with singleOption true, checkboxes with it false
        kinds = [(q["label"].split()[:3], q["type"]) for q in d["questions"] if q["kind"] == "multiple"]
        self.assertEqual(kinds, [(["Which", "best", "describes"], "multi_value_single_select"),
                                 (["What", "is", "your"], "multi_value_single_select"),
                                 (["What", "is", "your"], "multi_value_multi_select")])

    def test_the_visitors_location_prefill_is_not_kept(self):
        d, _ = self.read("workable_cnc")
        fields = [f for sec in d["form"] for f in sec["fields"]]
        addr = next(f for f in fields if f["id"] == "address")
        self.assertNotIn("value", addr)
        self.assertNotIn("prefilledByLocation", addr)
        edu = next(q for q in d["questions"] if q["label"] == "Education")
        self.assertEqual(edu["type"], "group")
        self.assertIn("School", edu["options"])

    def test_gone_and_failed(self):
        self.assertEqual(workable.form(FakeHTTP({}), "x", {"ext_id": "ABC"}), {"_closed": True})
        d = workable.form(FakeHTTP({"https://apply.workable.com/": (429, b"slow down")}), "x", {"ext_id": "ABC"})
        self.assertIn("HTTP 429", d["_detail_error"])

    def test_a_us_or_canada_question_is_not_a_gate(self):
        d, _ = self.read("workable_better_impact")
        r = assess(job_with(d["questions"], title="Member Implementation Advisor"), WA)
        self.assertEqual([x for x in r["skips"] + r["flags"] if "application form" in x], [])

    def test_salary_question_without_a_figure(self):
        d, _ = self.read("workable_cnc")
        self.assertEqual(score.form_pay_text(job_with(d["questions"])), "")


# ----------------------------------------------------------------------------------- Ashby
class AshbyForms(unittest.TestCase):
    def read(self, name):
        fx = FORMS[name]
        b = FakeBrowser({fx["job"]["url"] + "/application": fx["page"]})
        return ashby.form(None, "x", fx["job"], browser=b), b

    def test_1password_every_field(self):
        d, b = self.read("ashby_1password")
        self.assertEqual(b.urls, ["https://jobs.ashbyhq.com/1password/443f9ef6-13a7-4edb-ace5-16be7e630041/application"])
        q = {x["label"]: x for x in d["questions"]}
        self.assertEqual(len(d["questions"]), 35)
        self.assertEqual(q["Size of company you have worked for most recently"]["options"],
                         [">100", "101-999", "1000-2499", "2500-4999", "5000+"])
        self.assertTrue(q["Size of company you have worked for most recently"]["required"])
        brought = q["What brought you to this job posting"]
        self.assertEqual(brought["type"], "multi_value_single_select")
        self.assertIn("Agency", brought["options"])                     # the dropdown's options, opened to list them
        self.assertEqual(q["Do you have experience in cybersecurity SaaS?"]["type"], "boolean")
        self.assertFalse(q["Pronouns"]["required"])
        self.assertEqual(q["Current Location"]["kind"], "location")
        self.assertEqual(d["form"]["title"], "Developer, Device Trust Reliability")

    def test_a_sample_salary_isnt_the_jobs_pay(self):
        d, _ = self.read("ashby_1password")
        j = job_with(d["questions"])
        self.assertEqual(score.form_pay_text(j), "")                     # "(i.e. $50000, £50000)" is an example
        r = assess(j)
        self.assertNotEqual(r["facts"]["pay_top"], 50000)
        self.assertFalse(any("pay" in s for s in r["skips"]), r["skips"])

    def test_headway_us_authorization_and_sponsorship_dont_gate(self):
        d, _ = self.read("ashby_headway")
        labels = [x["label"] for x in d["questions"]]
        self.assertIn("Are you authorized to work in the United States for any employer?", labels)
        r = assess(job_with(d["questions"]), WA)
        self.assertEqual([x for x in r["skips"] + r["flags"] if "application form" in x], [])

    def test_closed_posting_and_no_browser(self):
        d, _ = self.read("ashby_closed")
        self.assertEqual(d, {"_closed": True})
        d = ashby.form(None, "x", FORMS["ashby_1password"]["job"])
        self.assertTrue(d["_no_browser"])
        d = ashby.form(None, "x", FORMS["ashby_1password"]["job"], browser=FakeBrowser(error="robots.txt disallows"))
        self.assertEqual(d["_detail_error"], "robots.txt disallows")

    def test_written_answers_line(self):
        d, _ = self.read("ashby_1password")
        self.assertEqual(alerts.written_questions(d["questions"]), ["Why 1Password?"])


# ----------------------------------------------------------------------------------- Workday
class WorkdayWalls(unittest.TestCase):
    def test_account_first(self):
        st, f = workday.wall_of(FORMS["workday_harris"])
        self.assertEqual(st, "signin")
        self.assertEqual(f["steps"], ["Create Account/Sign In", "My Information", "My Experience", "Application Questions",
                                      "Voluntary Disclosures", "Review"])

    def test_guest_flow_asks_its_questions_after_the_first_steps(self):
        st, f = workday.wall_of(FORMS["workday_openlane"])
        self.assertEqual(st, "guest")
        self.assertEqual(f["steps"][:3], ["My Information", "My Experience", "Application Questions"])
        self.assertIn("How Did You Hear About Us?", [x["label"] for x in f["fields"]])

    def test_form_reads_the_wall_and_closes_gone_postings(self):
        url = "https://harriscomputer.wd3.myworkdayjobs.com/1/job/Utah-United-States/Billing-Specialist_R0047255"
        b = FakeBrowser({url + "/apply/applyManually": FORMS["workday_harris"]})
        d = workday.form(None, "harriscomputer|wd3|1", {"url": url, "raw": {"_detail": {"questionnaireId": "abc"}}}, browser=b)
        self.assertEqual((d["state"], d["form"]["questionnaire"]), ("signin", True))
        self.assertNotIn("questions", d)            # nothing is invented: the questions stay unread
        gone = "https://cdk.wd1.myworkdayjobs.com/CDK/job/Cincinnati-OH-USA/Associate-Technical-Support-Analyst---Drive-Service_JR9387-1"
        d = workday.form(None, "cdk|wd1|CDK", {"url": gone}, browser=FakeBrowser({gone: FORMS["workday_gone"]}))
        self.assertEqual(d, {"_closed": True})


# ----------------------------------------------------------------------------------- gates
class FormGates(unittest.TestCase):
    def why(self, q, prof=P, **kw):
        r = assess(job_with([q], **kw), prof)
        return r["tier"], [x for x in r["skips"] + r["flags"] if "application form" in x], r

    def test_work_authorization_only_abroad(self):
        t, why, _ = self.why(yes_no("Are you legally authorized to work in Canada?"))
        self.assertEqual(t, "skip")
        self.assertIn("work authorization in Canada", why[0])
        # a US answer, a US question elsewhere on the form, a conditional, or optional: not a gate
        self.assertEqual(self.why({"label": "Which country are you authorized to work in?", "required": True,
                                   "type": "multi_value_single_select", "options": ["United States", "Canada"]})[1], [])
        self.assertNotEqual(self.why(yes_no("If you are located in Canada, are you authorized to work there?"))[0], "skip")
        self.assertNotEqual(self.why(yes_no("Are you authorized to work in the UK?", required=False))[0], "skip")
        both = [yes_no("Are you authorized to work in the United States?"), yes_no("Are you authorized to work in Canada?")]
        r = assess(job_with(both))
        self.assertNotEqual(r["tier"], "skip")
        self.assertTrue(any("work authorization in Canada" in f for f in r["flags"]), r["flags"])
        self.assertEqual(self.why(yes_no("Will you now or in the future require sponsorship for employment visa status?"))[1], [])

    def test_clearance(self):
        t, why, _ = self.why(yes_no("Do you currently hold an active Secret or Top Secret security clearance?"))
        self.assertEqual(t, "skip")
        self.assertIn("security clearance", why[0])
        self.assertNotEqual(self.why(yes_no("Have you ever held a security clearance?"))[0], "skip")

    def test_degree_follows_the_profile_rule_and_the_posting(self):
        q = yes_no("Do you have a Bachelor's degree?")
        self.assertEqual(self.why(q)[0], "skip")                         # degree_required: skip
        self.assertNotEqual(self.why(q, LENIENT)[0], "skip")             # degree_required: ignore
        self.assertNotEqual(self.why(yes_no("Do you have a bachelor's degree or equivalent experience?"))[0], "skip")
        t, why, _ = self.why(q, description=FRESH + "\nBachelor's degree or equivalent experience.")
        self.assertNotEqual(t, "skip")
        self.assertIn("takes equivalent experience", why[0])
        self.assertEqual(self.why({"label": "What is your highest level of education?", "required": True,
                                   "type": "multi_value_single_select", "options": ["High school", "Bachelor's", "Master's"]})[1], [])

    def test_licence_and_weekends(self):
        self.assertEqual(self.why(yes_no("Do you have a valid driver's license and reliable transportation?"))[0], "skip")
        self.assertNotEqual(self.why(yes_no("Do you have a valid driver's license?"), LENIENT)[0], "skip")
        self.assertEqual(self.why(yes_no("Are you available to work weekends?"))[0], "skip")
        t, why, r = self.why(yes_no("Are you available to work weekends?"), LENIENT)
        self.assertNotEqual(t, "skip")
        self.assertTrue(why)
        self.assertNotEqual(self.why(yes_no("Are you able to work occasional weekends as needed?"))[0], "skip")

    def test_the_profiles_required_gaps(self):
        r = assess(job_with([yes_no("Are you fluent in Spanish?")]), LENIENT)
        self.assertIn("asks for Spanish (application form)", r["flags"])
        r = assess(job_with([yes_no("Are you a CPA?")]), LENIENT)
        self.assertEqual(r["tier"], "long")
        self.assertIn("requires a CPA/CMA licence (application form)", r["reasons"])
        self.assertNotEqual(assess(job_with([yes_no("Are you a CPA? (preferred)")]), LENIENT)["tier"], "long")

    def test_form_text_counts_for_keywords(self):
        P2 = profile.Profile(dict(copy.deepcopy(P.raw), keywords=["Docker", "Discord"]), "kw")
        base = assess(job_with([]), P2)
        q = [yes_no("Do you have experience running Docker containers on Linux?"),
             {"label": "How did you hear about us?", "required": True, "type": "multi_value_single_select",
              "options": ["LinkedIn", "Discord", "YouTube"]}]
        r = assess(job_with(q), P2)
        self.assertIn("mentions tools you know: docker", r["reasons"])
        self.assertEqual(r["score"], base["score"] + 2)                  # Docker counts, the Discord answer doesn't

    def test_real_questions_that_are_not_gates(self):
        # read off real forms on 2026-11-08: each looked like a gate to a first draft
        cases = [
            # NICE: "first-degree relatives" is not a degree
            yes_no("Do you have any first-degree relatives (spouse, parent, child, sibling) that are currently employed by NICE or "
                   "any of its subsidiaries?"),
            # Hasbro: being a student is not a degree requirement
            yes_no("Are you currently enrolled in an accredited undergraduate or graduate degree program?"),
            # Elastic: an export-control screen, not work authorization abroad
            yes_no("Is your most recent country of citizenship one of the following countries: Cuba, Iran, North Korea (DPRK), or Syria?"),
            # Ignite Digital: asks whether you have any clearance at all (informational)
            yes_no("Do you have any type of Government security clearance?"),
            # Qualtrics / Gigs: fact-collecting, not yes/no
            {"label": "What month/year did you graduate with your bachelor's degree?", "required": True, "type": "input_text", "options": []},
            {"label": "Which statement best describes your hands-on involvement with API integration design?", "required": True,
             "type": "multi_value_single_select", "options": ["Led it", "Supported it", "None"]},
        ]
        for q in cases:
            self.assertEqual(self.why(q, WA)[1], [], q["label"])

    def test_vida_lists_the_states_it_cant_employ_in(self):
        q = {"label": "Do you reside in either of the following states? DE, ND, VT, WV - Note that we are not able to employ in these "
                      "states.", "required": True, "type": "multi_value_single_select", "options": ["Yes", "No"], "description": ""}
        self.assertEqual(self.why(q, WA)[1], [])                       # Washington isn't on it
        _de = copy.deepcopy(WA.raw)
        _de["location"] = dict(_de["location"], home_state="DE", local=["Dover"], nearby=[])
        self.assertEqual(self.why(q, profile.Profile(_de, "de"))[0], "skip")

    def test_instacart_excludes_washington(self):
        q = yes_no("This is a remote role that may be performed from any U.S. State except California, Connecticut, New Jersey, New York, "
                   "and Washington. If hired, would you be able to perform this role primarily from an eligible state by your start date?")
        t, why, _ = self.why(q, WA)
        self.assertEqual(t, "skip")
        self.assertIn("limits it to", why[0])
        self.assertNotEqual(self.why(q, P)[0], "skip")                  # North Carolina is fine

    def test_matter_health_prefers_tennessee(self):
        q = {"label": "Please confirm the city and state where you currently reside", "required": True, "type": "input_text",
             "options": [], "truncated": False, "description": "Please note, we prefer candidates located in either Nashville, TN or Memphis, TN."}
        t, why, _ = self.why(q, WA, location="Nashville | Remote / Memphis, TN")
        self.assertNotEqual(t, "skip")

    def test_opentable_time_zones_are_not_a_state_list(self):
        tz = {"label": "Applicants must live in either the Pacific Time Zone or Hawaii-Aleutian Time Zone to align with the team and market. "
                       "Seattle or Hawaii residence strongly preferred. Domestic travel will be required throughout West Coast and Hawaii. "
                       "Do you live in either of these Time Zones? If so, which one?", "required": True, "type": "input_text", "options": []}
        t, why, _ = self.why(tz, WA)
        self.assertNotEqual(t, "skip")
        car = {"label": "Do you have a valid driver’s license and transportation, with residency in Hawaii, Pacific, or Mountain Time Zones "
                        "to align with the territory and team?", "required": True, "type": "input_text", "options": []}
        self.assertEqual(self.why(car, WA)[0], "skip")                  # a curly apostrophe is still a driver's licence
        self.assertNotEqual(self.why(car, LENIENT)[0], "skip")


# ----------------------------------------------------------------------------------- read_forms
def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db.migrate(conn)
    return conn


def add(conn, ats, url, tier="good", board=1, ext="1", token="t", first_seen=None):
    cur = conn.execute("INSERT INTO jobs(board_id, ats, token, ext_id, title, url, raw, questions, detail_state, prefilter, tier, first_seen) "
                       "VALUES (?,?,?,?,?,?,'{}','[]','', 'pass', ?, ?)", (board, ats, token, ext, "Support", url, tier, first_seen or db.now()))
    return cur.lastrowid


def raw(conn, jid):
    return json.loads(conn.execute("SELECT raw FROM jobs WHERE id=?", (jid,)).fetchone()[0])


class ReadForms(unittest.TestCase):
    A1 = FORMS["ashby_1password"]

    def setUp(self):
        self.conn = mem_db()

    def test_reads_store_questions_form_and_state(self):
        jid = add(self.conn, "ashby", self.A1["job"]["url"])
        b = FakeBrowser({self.A1["job"]["url"]: self.A1["page"]})
        st = {}
        self.assertEqual(poll.read_forms(self.conn, None, 10, browser=b, stats=st), [jid])
        r = raw(self.conn, jid)
        self.assertEqual(r["_form_state"], "read")
        self.assertEqual(len(r["_form"]["fields"]), 35)
        qs = json.loads(self.conn.execute("SELECT questions FROM jobs WHERE id=?", (jid,)).fetchone()[0])
        self.assertEqual(len(qs), 35)
        self.assertEqual(st, {"ashby_read": 1})
        self.assertEqual(poll.read_forms(self.conn, None, 10, browser=b), [])         # read once

    def test_no_browser_leaves_browser_boards_alone(self):
        jid = add(self.conn, "ashby", self.A1["job"]["url"])
        self.assertEqual(poll.read_forms(self.conn, None, 10, browser=None), [])
        self.assertEqual(raw(self.conn, jid), {})

    def test_matches_first_and_this_runs_jobs_only(self):
        b = FakeBrowser({"https://jobs.ashbyhq.com/": self.A1["page"]})
        long_ = add(self.conn, "ashby", "https://jobs.ashbyhq.com/a/1", tier="long", ext="1")
        good = add(self.conn, "ashby", "https://jobs.ashbyhq.com/a/2", tier="good", ext="2")
        self.assertEqual(poll.read_forms(self.conn, None, 10, browser=b, matches_only=True), [good])
        other = add(self.conn, "ashby", "https://jobs.ashbyhq.com/a/3", tier="strong", ext="3")
        self.assertEqual(poll.read_forms(self.conn, None, 10, browser=b, ids=[long_]), [long_])
        self.assertEqual(poll.read_forms(self.conn, None, 10, browser=b), [other])

    def test_out_of_time_waits_for_a_later_run(self):
        class Slow(FakeBrowser):
            def read(self, url, script, timeout=None):
                time.sleep(0.05)
                return super().read(url, script)
        b = Slow({"https://jobs.ashbyhq.com/": self.A1["page"]})
        for i in range(3):
            add(self.conn, "ashby", "https://jobs.ashbyhq.com/a/%d" % i, ext=str(i))
        st = {}
        got = poll.read_forms(self.conn, None, 10, seconds=0.01, browser=b, stats=st)
        self.assertEqual((len(got), st["out_of_time"]), (1, 2))
        self.assertEqual(len(poll.read_forms(self.conn, None, 10, browser=b)), 2)

    def test_workday_wall_is_checked_once_per_board(self):
        url = "https://harriscomputer.wd3.myworkdayjobs.com/1/job/Utah-United-States/Billing-Specialist_R0047255"
        b = FakeBrowser({url: FORMS["workday_harris"]})
        a = add(self.conn, "workday", url, board=7, ext="R1", first_seen="2026-11-08T10:00:00+00:00")
        c = add(self.conn, "workday", url + "-2", board=7, ext="R2", first_seen="2026-11-08T09:00:00+00:00")
        self.assertEqual(poll.read_forms(self.conn, None, 10, browser=b), [a, c])
        self.assertEqual(len(b.urls), 1)
        self.assertEqual(raw(self.conn, c)["_form_state"], "signin")
        self.assertEqual(raw(self.conn, c)["_form"]["from_job"], a)

    def test_failures_retry_later_then_stop_and_robots_is_final(self):
        jid = add(self.conn, "ashby", self.A1["job"]["url"])
        bad = FakeBrowser(error="HTTP 502 bad gateway")
        poll.read_forms(self.conn, None, 10, browser=bad)
        self.assertEqual(poll.read_forms(self.conn, None, 10, browser=bad), [])      # not again so soon
        self.assertEqual(raw(self.conn, jid)["_form_fails"], 1)
        r = raw(self.conn, jid)
        r["_form_failed_at"] = "2000-01-01T00:00:00+00:00"
        self.conn.execute("UPDATE jobs SET raw=? WHERE id=?", (json.dumps(r), jid))
        poll.read_forms(self.conn, None, 10, browser=FakeBrowser(error="robots.txt disallows"))
        r = raw(self.conn, jid)
        self.assertEqual((r["_form_state"], "_form_error" in r), ("robots", False))

    def test_a_gone_posting_closes_the_job(self):
        jid = add(self.conn, "ashby", FORMS["ashby_closed"]["job"]["url"])
        poll.read_forms(self.conn, None, 10, browser=FakeBrowser({FORMS["ashby_closed"]["job"]["url"]: FORMS["ashby_closed"]["page"]}))
        self.assertIsNotNone(self.conn.execute("SELECT closed_at FROM jobs WHERE id=?", (jid,)).fetchone()[0])


# ----------------------------------------------------------------------------------- the alert's form line
class FormLine(unittest.TestCase):
    def line(self, ats, raw_, questions=()):
        return alerts.form_line({"ats": ats, "raw": json.dumps(raw_), "questions": json.dumps(list(questions))})

    def test_states(self):
        self.assertEqual(self.line("ashby", {}), "❔ Form not read yet (Ashby): check the page")
        self.assertEqual(self.line("ashby", {"_form_read": "x", "_form_state": "read"}, [yes_no("Are you 18?")]),
                         "✅ Quick apply: no written answers")
        self.assertIn("asks for an account", self.line("workday", {"_form_read": "x", "_form_state": "signin"}))
        self.assertIn("only after you start", self.line("workday", {"_form_read": "x", "_form_state": "guest"}))
        self.assertIn("robots.txt", self.line("workday", {"_form_read": "x", "_form_state": "robots"}))
        self.assertEqual(self.line("smartrecruiters", {}), "❔ Form not read (Smartrecruiters): check the page")


# ----------------------------------------------------------------------------------- the browser client
class BrowserClient(unittest.TestCase):
    def test_off_without_a_url(self):
        self.assertIsNone(browser_mod.from_config({}))
        self.assertIsNone(browser_mod.from_config({"browser": {}}))
        self.assertIsNotNone(browser_mod.from_config({"browser": {"url": "http://127.0.0.1:9377"}}))

    def test_key_from_the_environment_and_tab_always_closed(self):
        b = browser_mod.Browser({"url": "http://cf"}, env={"CAMOFOX_ACCESS_KEY": "k"})
        self.assertEqual(b.key(), "k")
        calls = []

        def fake(method, path, body=None, timeout=None):
            calls.append((method, path))
            if path == "/tabs":
                return {"tabId": "t1"}
            if path.endswith("/evaluate"):
                raise browser_mod.BrowserError("HTTP 500 boom")
            return {}
        b._call = fake
        with self.assertRaises(browser_mod.BrowserError):
            b.read("https://jobs.ashbyhq.com/x/1/application", "1")
        self.assertEqual([m for m, _ in calls], ["POST", "POST", "DELETE"])

    def test_robots_is_checked_first(self):
        class NoRobots:
            def allowed(self, url):
                return False
        b = browser_mod.Browser({"url": "http://cf"}, client=NoRobots(), env={})
        b._call = lambda *a, **k: self.fail("no call to the browser")
        with self.assertRaises(browser_mod.BrowserError) as e:
            b.read("https://homedepot.wd5.myworkdayjobs.com/CareerDepot/job/x/apply/applyManually", "1")
        self.assertIn("robots", str(e.exception))


# ----------------------------------------------------------------------------------- the head-to-head prompt
class FormInTheDuelPrompt(unittest.TestCase):
    """What the head-to-head model reads of a real form: the employer's questions, not the standard fields."""

    def test_1password(self):
        fx = FORMS["ashby_1password"]
        d = ashby.form(None, "x", fx["job"], browser=FakeBrowser({fx["job"]["url"] + "/application": fx["page"]}))
        lines = duel.form_lines({"questions": d["questions"]})
        self.assertEqual(len(lines), 11)                    # of 35 fields
        self.assertIn("- Do you have experience in cybersecurity SaaS? [required; yes/no]", lines)
        self.assertIn("- Size of company you have worked for most recently [required; pick one: >100 / 101-999 / 1000-2499 / "
                      "2500-4999 / 5000+]", lines)
        self.assertIn("- Why 1Password? [required; long answer] Note: There are a lot of great companies out there. "
                      "What makes you excited to work at 1Password?", lines)
        text = "\n".join(lines)
        for left_out in ("What brought you", "found this job", "Pronouns", "Gender", "Current Location", "Current job title",
                         "LinkedIn", "Resume", "sign up"):
            self.assertNotIn(left_out, text)

    def test_workable_radio_buttons_and_checkboxes(self):
        fx = FORMS["workable_better_impact"]
        d = workable.form(FakeHTTP({"https://apply.workable.com/": (200, fx["body"])}), "x", {"ext_id": fx["ext_id"]})
        lines = duel.form_lines({"questions": d["questions"]})
        self.assertEqual(lines[0], "- In which country are you currently located and authorized to work? "
                                   "[required; pick one: United States / Canada / Neither]")
        self.assertTrue(lines[2].startswith("- Which best describes your experience with Better Impact software? [required; pick one: "))
        self.assertTrue(lines[4].startswith("- What is your experience with software implementation or customer onboarding? "
                                            "[required; pick any: "))
        self.assertEqual(len(lines), 5)
        cnc = FORMS["workable_cnc"]
        d = workable.form(FakeHTTP({"https://apply.workable.com/": (200, cnc["body"])}), "x", {"ext_id": cnc["ext_id"]})
        self.assertEqual(duel.form_lines({"questions": d["questions"]}),
                         ["- What are your salary expectations for this role? [required; short answer]"])   # no Photo, Education...

    def test_no_form_no_lines(self):
        self.assertEqual(duel.form_lines({"questions": "[]"}), [])
        self.assertEqual(duel.form_hash({"questions": None}), "")
        noise = [{"label": "How did you hear about us?", "options": ["LinkedIn", "Discord"]}, {"label": "Resume", "type": "input_file"}]
        self.assertEqual(duel.form_hash({"questions": noise}), "")

    def test_a_long_form_is_cut(self):
        qs = [{"label": "Question %d about the work %s" % (i, "x" * 200), "required": True, "type": "textarea"} for i in range(60)]
        lines = duel.form_lines({"questions": qs})
        self.assertEqual(lines[-1], "- [... %d more questions cut]" % (60 - len(lines) + 1))
        self.assertLessEqual(sum(len(x) + 1 for x in lines[:-1]), duel.FORM_MAX)


if __name__ == "__main__":
    unittest.main()
