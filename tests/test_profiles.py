"""Several profiles scored from one crawl: claim routing, per-profile exports and alerts, the old-database
migration, and the profile keys that came with it (required_gaps, leadership.ok_titles, pay targets).
The second person is examples/profile.partner.example.json: bookkeeping, client success, operations."""
import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from jobatlas import alerts, config, db, export, ledger, poll, profile, score  # noqa: E402

MAIN = profile.load(path=profile.EXAMPLE)
PARTNER_FILE = os.path.join(config.EXAMPLES, "profile.partner.example.json")
PARTNER = profile.load(path=PARTNER_FILE)
DESC = ("This is a fully remote position open to candidates anywhere in the United States. "
        "Requirements: 2+ years of experience with client onboarding. Bachelor's degree or equivalent experience. "
        "Monday through Friday schedule. Salary: $70,000 - $82,000 per year. Benefits include medical, dental and vision.")


def a(title, desc=DESC, prof=PARTNER, location="Remote - US", company="Acme Software"):
    return score.assess({"title": title, "description": desc, "location": location, "company": company}, prof, company=company)


def variant(prof, **changes):
    d = copy.deepcopy(prof.raw)
    d.update(changes)
    return profile.Profile(d, "test")


class ProfileKeysTest(unittest.TestCase):
    def test_partner_example_is_clean(self):
        with open(PARTNER_FILE, encoding="utf8") as f:
            d = json.load(f)
        self.assertEqual(profile.check(d), [])
        d["required_gaps"].append({"regex": "x", "effect": "maybe"})
        d["leadership"]["ok_titles"] = "(manager"
        problems = profile.check(d)
        self.assertTrue(any("effect: 'maybe'" in p for p in problems), problems)
        self.assertTrue(any("leadership.ok_titles: bad regex" in p for p in problems), problems)

    def test_target_pay_bonus(self):
        r = a("Client Success Specialist")
        self.assertEqual(r["tier"], "strong", r)
        self.assertIn("meets the $75k pay target", r["reasons"])
        low = a("Client Success Specialist", DESC.replace("$70,000 - $82,000", "$60,000 - $70,000"))
        self.assertNotIn("meets the $75k pay target", low["reasons"])
        self.assertGreater(r["score"], low["score"])

    def test_a_pay_band_far_above_reads_as_senior(self):
        r = a("Client Success Specialist", DESC.replace("$70,000 - $82,000", "$140,000 - $180,000"))
        self.assertEqual(r["verdict"], "fair")
        self.assertTrue(any("more senior role" in x for x in r["reasons"]), r["reasons"])

    def test_leadership_ok_titles(self):
        self.assertEqual(a("Office Manager")["verdict"], "strong")
        self.assertEqual(a("Client Success Team Lead")["verdict"], "strong")
        self.assertEqual(a("Director of Client Success")["verdict"], "long")
        plain = variant(PARTNER, leadership={"stretch_min_years": 6, "senior_stretch_below_years": 4})
        self.assertEqual(a("Client Success Team Lead", prof=plain)["verdict"], "long")

    def test_required_gaps_read_only_required_lines(self):
        self.assertEqual(a("Payroll Specialist", DESC + "\nRequirements:\n- Fluent in Spanish\n")["tier"], "skip")
        nice = a("Payroll Specialist", DESC + "\nNice to have:\n- Fluent in Spanish\n- CPA\n")
        self.assertEqual(nice["verdict"], "strong", nice)
        self.assertEqual(a("Payroll Specialist", DESC + " A CPA license is required.")["verdict"], "long")
        r = a("Payroll Specialist", DESC + "\nWhat you'll bring:\n- Experience with NetSuite\n")
        self.assertIn("names a system they haven't used (learnable)", r["flags"])
        r = a("Payroll Specialist", "We run our books on NetSuite and Salesforce. " + DESC)
        self.assertFalse(any("system" in f for f in r["flags"]), r["flags"])

    def test_german_postings_are_not_us(self):
        self.assertEqual(a("Praktikum Customer Success (m/w/d)", location="Stuttgart & Remote")["tier"], "skip")
        self.assertEqual(a("Client Success Specialist (m/w/d)", location="Remote")["facts"]["location_class"], "remote_non_us")

    def test_prefilter_passes_when_any_profile_wants_the_title(self):
        j = {"title": "Payroll Specialist", "location": "Remote"}
        self.assertEqual(score.prefilter(j, MAIN), "off-target")
        self.assertEqual(score.prefilter_all(j, [MAIN, PARTNER]), "pass")
        self.assertEqual(score.prefilter_all({"title": "Forklift Operator", "location": "Remote"}, [MAIN, PARTNER]), "off-target")


class SeekersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copyfile(PARTNER_FILE, os.path.join(self.tmp, "profile-partner.json"))
        self.cfg = config._merge(config.DEFAULTS, {
            "alerts": {"enabled": True, "channel": "imessage", "chat_guid": "iMessage;-;+15550001111"},
            "profiles": {"partner": {"file": "profile-partner.json", "claim_tier": "good", "alerts": {"enabled": True}},
                         "broken": {"file": "missing.json"}}})

    def tearDown(self):
        shutil.rmtree(self.tmp)
        profile.EXTRA_ERRORS.clear()

    def test_extra_profiles_come_from_config(self):
        with mock.patch.object(config, "cfg", return_value=self.cfg), mock.patch.object(config, "DATA", self.tmp):
            s = profile.seekers()
            terms = profile.search_terms()
        self.assertEqual([x.name for x in s], ["main", "partner"])
        self.assertEqual(s[1].claims, ("strong", "good"))
        self.assertTrue(s[1].alerts["enabled"])
        # an extra profile never inherits the main profile's recipient
        self.assertEqual((s[1].alerts["channel"], s[1].alerts["chat_guid"]), ("discord_webhook", ""))
        self.assertIn("broken", profile.EXTRA_ERRORS)
        self.assertIn("payroll", terms)
        self.assertEqual(len(terms), len({t.lower() for t in terms}))


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.sqlite"))
        self.cfg = config._merge(config.DEFAULTS, {})
        self.partner_alerts = config._merge(config.DEFAULTS["alerts"], {"enabled": True, "label": "New job for you", "style": "plain"})
        self.seekers = [profile.Seeker(config.MAIN, MAIN, alerts=self.cfg["alerts"]),
                        profile.Seeker("partner", PARTNER, claim_tier="good", alerts=self.partner_alerts)]
        for p in (mock.patch.object(profile, "seekers", side_effect=lambda: self.seekers),
                  mock.patch.object(config, "cfg", return_value=self.cfg)):
            p.start()
            self.addCleanup(p.stop)
        self.board = self.conn.execute("INSERT INTO companies(company, norm, ats, token) VALUES ('Acme', 'acme', 'greenhouse', 'acme')").lastrowid

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, title, desc=DESC):
        n = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        pf = score.prefilter_all({"title": title, "location": "Remote - US"}, [s.prof for s in self.seekers])
        return self.conn.execute(
            "INSERT INTO jobs(board_id, ats, token, ext_id, company, title, url, location, first_seen, prefilter, baseline, description)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (self.board, "greenhouse", "acme", "j%d" % n, "Acme Software", title, "https://x/%d" % n, "Remote - US", db.now(), pf, desc)).lastrowid

    def titles(self, name=None):
        return sorted(r["title"] for r in export.query(self.conn, profile_name=name))

    def test_each_job_goes_to_one_profile(self):
        self.add("Technical Support Specialist")          # main only
        self.add("Payroll Specialist")                    # partner only
        both = self.add("Onboarding Specialist")          # strong for both: the partner claims it
        self.add("Implementation Specialist", DESC + " SQL is required.")   # main strong, partner a long shot: stays main's
        counts = poll.score_jobs(self.conn)
        self.assertEqual(counts["claimed"], 1)
        self.assertEqual(self.conn.execute("SELECT routed_to FROM jobs WHERE id=?", (both,)).fetchone()[0], "partner")
        self.assertEqual(self.titles(), ["Implementation Specialist", "Technical Support Specialist"])
        self.assertEqual(self.titles("partner"), ["Onboarding Specialist", "Payroll Specialist"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM profile_scores").fetchone()[0], 3)   # tech support isn't the partner's

    def test_main_keeps_fields_unless_strong_for_the_other_profile(self):
        self.seekers[1] = profile.Seeker("partner", PARTNER, claim_tier="good", alerts=self.partner_alerts,
                                         main_keeps_fields=["implementation"])
        reach = self.add("Onboarding Specialist", DESC.replace("2+ years", "4+ years"))    # good for the partner: main keeps it
        strong = self.add("Onboarding Coordinator")                                       # strong for the partner: theirs
        poll.score_jobs(self.conn)
        route = dict(self.conn.execute("SELECT id, routed_to FROM jobs").fetchall())
        self.assertEqual((route[reach], route[strong]), (None, "partner"))

    def test_claim_is_released_when_the_job_stops_fitting(self):
        jid = self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        self.conn.execute("UPDATE jobs SET description=? WHERE id=?", (DESC + " Proficiency in SQL is required.", jid))
        poll.score_jobs(self.conn)
        self.assertIsNone(self.conn.execute("SELECT routed_to FROM jobs WHERE id=?", (jid,)).fetchone()[0])
        self.assertEqual(self.titles(), ["Onboarding Specialist"])

    def test_alerts_go_to_each_profile_with_its_own_settings(self):
        tech = self.add("Technical Support Specialist")
        self.add("Payroll Specialist")
        self.add("Onboarding Specialist")
        poll.score_jobs(self.conn)
        with mock.patch.object(alerts, "send", return_value=(True, "ok")) as send:
            out = alerts.process(self.conn, db.now())
        self.assertEqual((out["sent"], out["disabled"]), (2, 1))       # main's alerts are off: recorded only
        self.assertEqual(out["by_profile"]["partner"]["sent"], 2)
        msg, cfg = send.call_args[0]
        self.assertIs(cfg, self.partner_alerts)
        self.assertTrue(msg.startswith("New job for you: 2 new matches"), msg)
        self.assertNotIn("Technical Support", msg)
        modes = {(r[0], r[1]): r[2] for r in self.conn.execute("SELECT job_id, profile, mode FROM alerts")}
        self.assertEqual(modes[(tech, "main")], "disabled")
        self.assertEqual(sorted(m for (j, p), m in modes.items() if p == "partner"), ["sent", "sent"])

    def test_a_job_already_sent_to_someone_stays_theirs(self):
        jid = self.add("Onboarding Specialist")
        self.conn.execute("INSERT INTO alerts(job_id, profile, created, mode, text) VALUES (?, 'main', ?, 'sent', 'x')", (jid, db.now()))
        poll.score_jobs(self.conn)
        self.assertIsNone(self.conn.execute("SELECT routed_to FROM jobs WHERE id=?", (jid,)).fetchone()[0])
        self.assertEqual(self.titles(), ["Onboarding Specialist"])

    def test_without_claim_tier_the_main_profile_picks_first(self):
        self.seekers[1] = profile.Seeker("partner", PARTNER, claim_tier=None, alerts=self.partner_alerts)
        self.add("Onboarding Specialist")
        self.add("Payroll Specialist")
        poll.score_jobs(self.conn)
        self.assertEqual(self.titles(), ["Onboarding Specialist"])
        self.assertEqual(self.titles("partner"), ["Payroll Specialist"])

    def test_export_writes_a_file_per_profile(self):
        self.add("Payroll Specialist")
        self.add("Technical Support Specialist")
        poll.score_jobs(self.conn)
        out = export.write(self.conn, os.path.join(self.tmp.name, "export", "latest.json"))
        self.assertEqual((out["count"], out["profiles"]), (1, {"partner": 1}))
        with open(os.path.join(self.tmp.name, "export", "latest-partner.json"), encoding="utf8") as f:
            d = json.load(f)
        self.assertEqual((d["profile"], d["jobs"][0]["title"]), ("partner", "Payroll Specialist"))

    def test_a_backlog_is_never_posted(self):
        old = self.add("Payroll Specialist")
        new = self.add("Payroll Coordinator")
        poll.score_jobs(self.conn)
        self.assertEqual(alerts.mark_backlog(self.conn, "partner", [old]), 1)
        self.assertEqual(alerts.mark_backlog(self.conn, "partner", [old]), 0)
        with mock.patch.object(alerts, "send", return_value=(True, "ok")) as send:
            out = alerts.process(self.conn, db.now())
        self.assertEqual(out["by_profile"]["partner"]["sent"], 1)
        self.assertIn("Payroll Coordinator", send.call_args[0][0])
        self.assertNotIn("Payroll Specialist", send.call_args[0][0])
        modes = dict(self.conn.execute("SELECT job_id, mode FROM alerts WHERE profile='partner'").fetchall())
        self.assertEqual((modes[old], modes[new]), ("backlog", "sent"))

    def test_backfill_tries_new_jobs_before_failed_ones_and_rescores_them(self):
        jid = self.add("Payroll Specialist", desc="")
        failed = self.add("Payroll Coordinator", desc="")
        self.conn.execute("UPDATE jobs SET detail_state='' WHERE id=?", (jid,))       # as poll.store leaves a new job
        self.conn.execute("UPDATE jobs SET detail_state='failed', first_seen='2099-01-01T00:00:00+00:00' WHERE id=?", (failed,))
        poll.score_jobs(self.conn)
        self.assertIn("description not read yet", self.conn.execute("SELECT flags FROM profile_scores WHERE job_id=?", (jid,)).fetchone()[0])
        got = []
        with mock.patch.object(poll.adapters, "get") as get:
            get.return_value.detail.return_value = {"description": DESC}
            self.assertEqual(poll.backfill_details(self.conn, mock.Mock(), 1, 1, got), 1)
        self.assertEqual(got, [jid])
        poll.score_jobs(self.conn, ids=got)
        self.assertEqual(self.conn.execute("SELECT tier FROM profile_scores WHERE job_id=?", (jid,)).fetchone()[0], "strong")

    def test_verified_pages_carry_over_to_extra_profiles(self):
        jid = self.add("Payroll Specialist")
        poll.score_jobs(self.conn)
        client = mock.Mock()
        client.get.return_value = mock.Mock(status=200, ok=True, text="<html>Payroll Specialist</html>")
        self.assertEqual(poll.verify_urls(self.conn, client)["ok"], 1)
        facts = json.loads(self.conn.execute("SELECT facts FROM profile_scores WHERE job_id=?", (jid,)).fetchone()[0])
        self.assertIn("verified", facts)


class MigrationTest(unittest.TestCase):
    def test_old_alerts_table_is_keyed_by_profile(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.sqlite")
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE alerts (job_id INTEGER PRIMARY KEY, created TEXT, mode TEXT, text TEXT)")
            c.execute("INSERT INTO alerts VALUES (7, '2026-10-01T00:00:00+00:00', 'sent', 'hi')")
            c.commit()
            c.close()
            conn = db.connect(path)
            self.assertEqual([tuple(r) for r in conn.execute("SELECT job_id, profile, mode FROM alerts")], [(7, "main", "sent")])
            conn.execute("INSERT INTO alerts(job_id, profile, mode) VALUES (7, 'partner', 'sent')")
            self.assertIn("routed_to", {r[1] for r in conn.execute("PRAGMA table_info(jobs)")})
            conn.close()
            db.connect(path).close()     # a second connect finds nothing to do


class TextTest(unittest.TestCase):
    ROW = {"facts": '{"pay_top": 82000, "pay_kind": "year"}', "location": "Remote - US", "reasons": '["asks 2+ yrs"]', "score": 90,
           "title": "Payroll Specialist", "company": "Acme", "url": "https://x/1"}

    def test_plain_and_full_styles(self):
        self.assertEqual(alerts.text_for(self.ROW, {"label": "New job for you", "style": "plain"}),
                         "New job for you: Payroll Specialist at Acme (Remote - US), up to $82k\nhttps://x/1")
        self.assertEqual(alerts.text_for(self.ROW), "JobAtlas Strong (90): Payroll Specialist at Acme (Remote - US) $82k. asks 2+ yrs\nhttps://x/1")


class LedgerReloadTest(unittest.TestCase):
    def test_ledger_reloads_when_its_file_changes(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"company": "Acme", "title": "Payroll Specialist", "url": "https://x/1"}) + "\n")
        self.assertTrue(ledger.seen("Acme", "Payroll Specialist", [], path=path))
        self.assertFalse(ledger.seen("Globex", "Office Manager", [], path=path))
        with open(path, "a") as f:
            f.write(json.dumps({"company": "Globex", "title": "Office Manager", "url": "https://x/2"}) + "\n")
        later = time.time() + 5
        os.utime(path, (later, later))
        self.assertTrue(ledger.seen("Globex", "Office Manager", [], path=path))


if __name__ == "__main__":
    unittest.main()
