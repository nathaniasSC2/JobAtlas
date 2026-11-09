"""Head-to-head (duel.py): the rule, reading the model's answer, which jobs are contested, routing by verdict,
the fallback when the model fails, and alerts waiting for a verdict. The model is a small script that answers
from a table keyed by posting title, so nothing leaves the machine."""
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from jobatlas import alerts, config, db, duel, export, poll, profile, score  # noqa: E402

MAIN = profile.load(path=profile.EXAMPLE)
PARTNER = profile.load(path=os.path.join(config.EXAMPLES, "profile.partner.example.json"))
DESC = ("This is a fully remote position open to candidates anywhere in the United States. "
        "Requirements: 2+ years of experience with client onboarding. Bachelor's degree or equivalent experience. "
        "Monday through Friday schedule. Salary: $70,000 - $82,000 per year. Benefits include medical, dental and vision. "
        "You will guide new customers through setup, run kickoff calls, keep projects on schedule and document what you learn. "
        "We are a small, friendly team that values clear writing and calm problem solving.")

FAKE_MODEL = textwrap.dedent('''
    import json, re, sys
    prompt = sys.stdin.read()
    title = re.search(r"^Title: (.*)$", prompt, re.M).group(1)
    answers = json.load(open(sys.argv[1]))
    if "--json-schema" in sys.argv:
        json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
    a = answers.get(title) or answers["*"]
    if a == "FAIL":
        sys.stderr.write("rate limited")
        sys.exit(1)
    v = {"summary": "test"}
    for label, (p, gates) in a.items():
        v[label] = {"p_screen": p, "gates_failed": gates, "resume": "General" if label == "Alex" else "Ops", "reason": "because"}
    print(json.dumps({"type": "result", "is_error": False, "structured_output": v, "total_cost_usd": 0.01,
                      "usage": {"input_tokens": 1000, "output_tokens": 50}, "modelUsage": {"fake-model": {}}}))
''')


class DecideTest(unittest.TestCase):
    def test_the_rule(self):
        self.assertEqual(duel.decide(0.30, 0.30, [], []), ("other", "tie"))
        self.assertEqual(duel.decide(0.39, 0.30, [], []), ("other", "tie"))           # a lead under the margin isn't enough
        self.assertEqual(duel.decide(0.40, 0.30, [], []), ("main", "chance"))         # the margin itself is
        self.assertEqual(duel.decide(0.20, 0.30, [], []), ("other", "chance"))
        self.assertEqual(duel.decide(0.10, 0.90, [], ["degree required"]), ("main", "gate"))   # a gate beats the odds
        self.assertEqual(duel.decide(0.90, 0.10, ["SQL required"], []), ("other", "gate"))
        self.assertEqual(duel.decide(0.20, 0.05, ["a"], ["b"]), ("main", "chance"))   # both gated: back to the odds
        self.assertEqual(duel.decide(0.30, 0.35, [], [], favor_other=False), ("main", "tie"))
        self.assertEqual(duel.decide(0.30, 0.41, [], [], favor_other=False), ("other", "chance"))
        self.assertEqual(duel.decide(0.45, 0.30, [], [], margin=0.2), ("other", "tie"))
        # ratio: long shots where one chance is a multiple of the other aren't near ties
        self.assertEqual(duel.decide(0.09, 0.03, ["a"], ["b"]), ("other", "tie"))
        self.assertEqual(duel.decide(0.09, 0.03, ["a"], ["b"], ratio=2), ("main", "chance"))
        self.assertEqual(duel.decide(0.04, 0.03, [], [], ratio=2), ("other", "tie"))
        self.assertEqual(duel.decide(0.30, 0.30, [], [], ratio=2), ("other", "tie"))
        self.assertEqual(duel.decide(0.0, 0.0, [], [], ratio=2), ("other", "tie"))


class ParseTest(unittest.TestCase):
    V = {"Sam": {"p_screen": 0.4}, "summary": "x"}

    def test_claude_envelope(self):
        env = {"type": "result", "is_error": False, "structured_output": self.V, "total_cost_usd": 0.02,
               "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}, "modelUsage": {"claude-sonnet-5": {}}}
        v, meta = duel.parse_output(json.dumps(env))
        self.assertEqual(v, self.V)
        self.assertEqual((meta["tokens_in"], meta["tokens_out"], meta["model"]), (15, 3, "claude-sonnet-5"))
        env = {"type": "result", "is_error": False, "result": "Here you go:\n```json\n%s\n```" % json.dumps(self.V)}
        self.assertEqual(duel.parse_output(json.dumps(env))[0], self.V)

    def test_bare_json_and_errors(self):
        self.assertEqual(duel.parse_output(json.dumps(self.V))[0], self.V)
        self.assertEqual(duel.parse_output("sure! " + json.dumps(self.V))[0], self.V)
        with self.assertRaises(duel.DuelError):
            duel.parse_output(json.dumps({"type": "result", "is_error": True, "result": "Credit balance is too low"}))
        with self.assertRaises(duel.DuelError):
            duel.parse_output("no idea")

    def test_read_side_checks_the_answer(self):
        c = {"label": "Sam", "resumes": [("Ops", ""), ("Support", "")]}
        self.assertEqual(duel.read_side({"Sam": {"p_screen": "0.4", "gates_failed": "SQL", "resume": "Ops", "reason": "r"}}, c),
                         {"p": 0.4, "gates": ["SQL"], "resume": "Ops", "reason": "r"})
        self.assertEqual(duel.read_side({"Sam": {"p_screen": 0.4, "resume": "CV"}}, c)["resume"], "")
        for bad in ({}, {"Sam": {"p_screen": 1.5}}, {"Sam": {"p_screen": "high"}}):
            with self.assertRaises(duel.DuelError):
                duel.read_side(bad, c)


class DuelRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.conn = db.connect(os.path.join(t, "t.sqlite"))
        for name, text in (("sam-ops.txt", "Sam\nsam@example.com\nOps lead, 3 years"), ("sam-support.txt", "Sam support resume"),
                           ("alex.txt", "Alex\n(555) 010-2030\nClient onboarding coordinator, 4 years"), ("alex-notes.md", "BA, drives")):
            with open(os.path.join(t, name), "w", encoding="utf8") as f:
                f.write(text)
        self.script = os.path.join(t, "fake_model.py")
        with open(self.script, "w", encoding="utf8") as f:
            f.write(FAKE_MODEL)
        self.answers_path = os.path.join(t, "answers.json")
        self.answer({"*": {"Sam": [0.6, []], "Alex": [0.3, []]}})
        self.main_ledger = os.path.join(t, "main-seen.jsonl")
        self.cfg = config._merge(config.DEFAULTS, {"duel": {
            "enabled": True, "command": [sys.executable, self.script, self.answers_path], "model": "fake", "hold_minutes": 30,
            "candidates": {"main": {"label": "Sam", "resumes": {"Ops": os.path.join(t, "sam-ops.txt"), "Support": os.path.join(t, "sam-support.txt")},
                                    "facts": ["No degree", "Remote only"]},
                           "partner": {"label": "Alex", "resumes": {"General": os.path.join(t, "alex.txt")},
                                       "facts_file": os.path.join(t, "alex-notes.md")}}}})
        self.partner_alerts = config._merge(config.DEFAULTS["alerts"], {"enabled": True, "label": "New jobs for Alex", "style": "card",
                                                                         "min_tier": "good"})
        self.seekers = [profile.Seeker(config.MAIN, MAIN, alerts=self.cfg["alerts"], ledger=self.main_ledger),
                        profile.Seeker("partner", PARTNER, claim_tier="good", alerts=self.partner_alerts)]
        for p in (mock.patch.object(profile, "seekers", side_effect=lambda: self.seekers),
                  mock.patch.object(config, "cfg", side_effect=lambda: self.cfg)):
            p.start()
            self.addCleanup(p.stop)
        self.board = self.conn.execute("INSERT INTO companies(company, norm, ats, token) VALUES ('Acme', 'acme', 'greenhouse', 'acme')").lastrowid

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def answer(self, table):
        with open(self.answers_path, "w", encoding="utf8") as f:
            json.dump(table, f)

    def add(self, title, desc=DESC, company="Acme Software"):
        n = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        pf = score.prefilter_all({"title": title, "location": "Remote - US"}, [s.prof for s in self.seekers])
        jid = self.conn.execute(
            "INSERT INTO jobs(board_id, ats, token, ext_id, company, title, url, location, first_seen, prefilter, baseline, description)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (self.board, "greenhouse", "acme", "j%d" % n, company, title, "https://x/%d" % n, "Remote - US", db.now(), pf, desc)).lastrowid
        self.conn.commit()
        return jid

    def route(self, jid):
        return self.conn.execute("SELECT routed_to FROM jobs WHERE id=?", (jid,)).fetchone()[0]

    def verdict(self, jid):
        return self.conn.execute("SELECT * FROM duels WHERE job_id=?", (jid,)).fetchone()

    def test_contested_jobs_follow_the_verdict(self):
        jid = self.add("Onboarding Specialist")          # strong for both: the tier rule gives it to the partner
        only = self.add("Payroll Specialist")             # the partner's alone: never judged
        poll.score_jobs(self.conn)
        self.assertEqual(self.route(jid), "partner")
        self.assertEqual([r["id"] for r in duel.contested(self.conn, "partner")], [jid])
        out = duel.run(self.conn)
        self.assertEqual((out["judged"], out["rerouted"], out["failed"]), (1, 1, 0))
        v = self.verdict(jid)
        self.assertEqual((v["winner"], v["why"], v["p_main"], v["p_other"], v["tier_route"]), ("main", "chance", 0.6, 0.3, "partner"))
        self.assertEqual((v["resume_main"], v["resume_other"], v["cost_usd"]), ("Ops", "General", 0.01))
        self.assertIsNone(self.route(jid))
        self.assertEqual(self.route(only), "partner")
        poll.score_jobs(self.conn)                       # a rescore keeps the verdict's routing
        self.assertIsNone(self.route(jid))
        self.assertEqual(duel.run(self.conn)["judged"], 0)     # nothing changed: no new call
        row = export.rows_out(self.conn, export.query(self.conn), config.MAIN)[0]
        self.assertEqual(row["duel"]["note"], "Better for you than for Alex: Sam 0.60 vs Alex 0.30; send the Ops resume")

    def test_a_near_tie_goes_to_the_favoured_side(self):
        self.answer({"*": {"Sam": [0.38, []], "Alex": [0.30, []]}})
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        duel.run(self.conn)
        self.assertEqual(self.route(jid), "partner")
        self.assertEqual(self.verdict(jid)["why"], "tie")
        row = export.rows_out(self.conn, export.query(self.conn, profile_name="partner"), "partner")[0]
        self.assertEqual(row["duel"]["note"], "Near tie, yours by the tie-break: Alex 0.30 vs Sam 0.38")

    def test_a_gate_one_side_fails_decides_it(self):
        self.answer({"*": {"Sam": [0.7, ["bachelor's degree required"]], "Alex": [0.2, []]}})
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        duel.run(self.conn)
        self.assertEqual((self.route(jid), self.verdict(jid)["why"]), ("partner", "gate"))
        row = export.rows_out(self.conn, export.query(self.conn, profile_name="partner"), "partner")[0]
        self.assertEqual(row["duel"]["note"], "Better for you than for Sam: bachelor's degree required")

    def test_recording_only_while_disabled(self):
        self.cfg["duel"]["enabled"] = False
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        self.assertEqual(duel.run(self.conn)["judged"], 1)
        self.assertEqual(self.verdict(jid)["winner"], "main")
        self.assertEqual(self.route(jid), "partner")       # the tier rule still decides
        poll.score_jobs(self.conn)
        self.assertEqual(self.route(jid), "partner")
        self.assertNotIn("duel", export.rows_out(self.conn, export.query(self.conn, profile_name="partner"), "partner")[0])

    def test_a_failed_call_leaves_the_tier_rule_and_is_retried_later(self):
        self.answer({"*": "FAIL"})
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        self.assertEqual(duel.pending(self.conn), {jid})
        out = duel.run(self.conn)
        self.assertEqual((out["judged"], out["failed"]), (0, 1))
        self.assertTrue(self.verdict(jid)["status"].startswith("failed: exit 1: rate limited"), self.verdict(jid)["status"])
        self.assertEqual(self.route(jid), "partner")
        self.assertEqual(duel.pending(self.conn), set())     # alerts stop waiting
        self.answer({"*": {"Sam": [0.6, []], "Alex": [0.3, []]}})
        self.assertEqual(duel.run(self.conn)["judged"], 0)  # not before duel.retry_minutes
        self.cfg["duel"]["retry_minutes"] = 0
        self.assertEqual(duel.run(self.conn)["judged"], 1)
        self.assertIsNone(self.route(jid))

    def test_a_model_that_is_down_stops_the_run(self):
        self.answer({"*": "FAIL"})
        for t in ("Onboarding Specialist", "Customer Onboarding Specialist", "Client Onboarding Specialist"):
            self.add(t)
        poll.score_jobs(self.conn)
        self.assertEqual(duel.run(self.conn)["failed"], 2)

    def test_a_changed_posting_drops_the_verdict_until_judged_again(self):
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        duel.run(self.conn)
        self.assertIsNone(self.route(jid))
        self.conn.execute("UPDATE jobs SET description=? WHERE id=?", (DESC + " Travel 10% of the time.", jid))
        poll.score_jobs(self.conn)
        self.assertEqual(self.route(jid), "partner")        # back to the tier rule
        self.answer({"*": {"Sam": [0.2, []], "Alex": [0.5, []]}})
        self.assertEqual(duel.run(self.conn)["judged"], 1)
        self.assertEqual(self.verdict(jid)["winner"], "partner")

    def test_a_new_resume_means_a_new_judgment(self):
        self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        duel.run(self.conn)
        self.assertEqual(duel.run(self.conn)["judged"], 0)
        path = os.path.join(self.tmp.name, "alex.txt")
        with open(path, "a", encoding="utf8") as f:
            f.write("\nNow also runs payroll")
        later = time.time() + 5
        os.utime(path, (later, later))
        self.assertEqual(duel.run(self.conn)["judged"], 1)

    FORM = [{"label": "Are you legally authorized to work in the US?", "type": "multi_value_single_select", "required": True,
             "options": ["Yes", "No"]},
            {"label": "How many years have you run customer onboarding?", "type": "multi_value_single_select", "required": True,
             "options": ["0-1", "2-4", "5+"]},
            {"label": "Why Acme?", "type": "textarea", "required": False, "description": "A few sentences."},
            {"label": "How did you hear about us?", "type": "multi_value_single_select", "options": ["LinkedIn", "Discord"]},
            {"label": "Gender", "type": "multi_value_single_select", "options": ["Woman", "Man"], "section": "Voluntary Self-Identification"}]

    def set_form(self, jid, questions):
        self.conn.execute("UPDATE jobs SET questions=? WHERE id=?", (json.dumps(questions), jid))
        self.conn.commit()

    def test_the_form_questions_are_part_of_the_posting(self):
        jid = self.add("Onboarding Specialist")
        self.set_form(jid, self.FORM)
        r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        p = duel.build_prompt(r, [duel.materials("main"), duel.materials("partner")])
        posting = p[p.index("=== THE POSTING ==="):p.index("=== CANDIDATE")]
        self.assertIn("Application form: the employer's questions for everyone who applies", posting)
        self.assertIn("\"required\" means the applicant must answer it, not that the answer must be yes", posting)
        self.assertIn("- Are you legally authorized to work in the US? [required; yes/no]\n"
                      "- How many years have you run customer onboarding? [required; pick one: 0-1 / 2-4 / 5+]\n"
                      "- Why Acme? [optional; long answer] Note: A few sentences.\n", posting)
        for left_out in ("Discord", "Gender"):
            self.assertNotIn(left_out, p)
        self.set_form(jid, [])
        r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        self.assertNotIn("Application form", duel.build_prompt(r, [duel.materials("main"), duel.materials("partner")]))

    def test_a_form_read_after_the_verdict_asks_for_a_new_one(self):
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        duel.run(self.conn)
        self.assertIsNone(self.route(jid))                  # Sam 0.6 vs Alex 0.3
        self.set_form(jid, self.FORM[3:])                   # only where-did-you-hear and the EEO survey: nothing new
        self.assertEqual(duel.run(self.conn)["judged"], 0)
        self.set_form(jid, self.FORM)
        poll.score_jobs(self.conn)
        self.assertIsNone(self.route(jid))                  # the verdict routes the job until the new one
        self.answer({"*": {"Sam": [0.2, []], "Alex": [0.5, []]}})
        out = duel.run(self.conn)
        self.assertEqual((out["judged"], out["rerouted"]), (1, 1))
        self.assertEqual((self.route(jid), self.verdict(jid)["winner"]), ("partner", "partner"))
        self.assertEqual(duel.run(self.conn)["judged"], 0)

    def test_contact_details_stay_out_of_the_prompt(self):
        jid = self.add("Onboarding Specialist")
        r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        p = duel.build_prompt(r, [duel.materials("main"), duel.materials("partner")])
        self.assertNotIn("sam@example.com", p)
        self.assertNotIn("010-2030", p)
        self.assertIn("Client onboarding coordinator, 4 years", p)
        self.assertIn("- No degree", p)
        self.assertIn("BA, drives", p)

    def test_jobs_someone_has_seen_are_left_alone(self):
        seen = self.add("Onboarding Specialist")
        sent = self.add("Customer Onboarding Specialist")
        with open(self.main_ledger, "w", encoding="utf8") as f:
            f.write(json.dumps({"company": "Acme Software", "title": "Onboarding Specialist", "url": "https://x/0"}) + "\n")
        poll.score_jobs(self.conn)
        self.conn.execute("INSERT INTO alerts(job_id, profile, created, mode, text) VALUES (?, 'partner', ?, 'sent', 'x')", (sent, db.now()))
        self.conn.commit()
        self.assertEqual(duel.contested(self.conn, "partner"), [])
        self.assertEqual(duel.run(self.conn)["judged"], 0)

    def test_a_verdict_never_moves_a_job_that_was_sent_meanwhile(self):
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        r, ih = duel.todo(self.conn, *self.seekers, duel.materials("main"), duel.materials("partner"))[0]
        duel.store(self.conn, r, "partner", ih, "fake", result=duel.judge(r, duel.materials("main"), duel.materials("partner")))
        self.conn.execute("INSERT INTO alerts(job_id, profile, created, mode, text) VALUES (?, 'partner', ?, 'sent', 'x')", (jid, db.now()))
        self.conn.commit()
        self.assertEqual(duel.reconcile(self.conn, *self.seekers), 0)
        poll.score_jobs(self.conn)
        self.assertEqual(self.route(jid), "partner")

    def test_alerts_wait_for_the_verdict_then_reach_the_winner(self):
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        with mock.patch.object(alerts, "send", return_value=(True, "ok")) as send:
            out = alerts.process(self.conn, db.now())
            self.assertEqual(out["by_profile"]["partner"]["candidates"], 0)    # held for the verdict
            self.answer({"*": {"Sam": [0.2, []], "Alex": [0.25, []]}})
            duel.run(self.conn)
            out = alerts.process(self.conn, db.now())
        self.assertEqual(out["by_profile"]["partner"]["sent"], 1)
        msg = send.call_args[0][0]
        self.assertIn("⚖️ Better for you than for Sam: Alex 0.25 vs Sam 0.20", msg)
        self.assertEqual(self.route(jid), "partner")

    def test_the_hold_ends_after_hold_minutes(self):
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        self.assertEqual(duel.pending(self.conn), {jid})
        self.conn.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00+00:00' WHERE id=?", (jid,))
        self.assertEqual(duel.pending(self.conn), set())

    def test_per_run_cap_and_newest_first(self):
        ids = [self.add(t) for t in ("Onboarding Specialist", "Customer Onboarding Specialist", "Client Onboarding Specialist")]
        poll.score_jobs(self.conn)
        self.assertEqual(len(duel.contested(self.conn, "partner")), 3)
        self.cfg["duel"]["per_run"] = 2
        out = duel.run(self.conn)
        self.assertEqual((out["judged"], out["left"]), (2, 1))
        self.assertIsNone(self.verdict(ids[0]))         # the oldest waits
        self.cfg["duel"]["per_day"] = 2
        self.assertEqual(duel.run(self.conn)["judged"], 0)

    def test_a_new_margin_re_decides_without_new_calls(self):
        self.answer({"*": {"Sam": [0.45, []], "Alex": [0.30, []]}})
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        duel.run(self.conn)
        self.assertIsNone(self.route(jid))
        self.cfg["duel"]["margin"] = 0.2
        out = duel.run(self.conn)
        self.assertEqual((out["judged"], out["redecided"], out["rerouted"]), (0, 1, 1))
        self.assertEqual((self.route(jid), self.verdict(jid)["why"]), ("partner", "tie"))


if __name__ == "__main__":
    unittest.main()
