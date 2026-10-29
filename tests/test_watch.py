"""Poll intervals, the watch loop and the alert policy, against a throwaway SQLite file."""
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from jobatlas import alerts, config, db, poll, watch  # noqa: E402
from jobatlas.adapters.base import Result  # noqa: E402

CFGP = dict(config.DEFAULTS["poll"])


class DBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.sqlite"))
        poll.ensure_columns(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def board(self, etag=None):
        c = self.conn.execute("INSERT INTO companies(company, norm, ats, token, etag) VALUES ('Acme', 'acme', 'greenhouse', ?, ?)",
                              ("acme%d" % self.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], etag))
        return self.conn.execute("SELECT * FROM companies WHERE id=?", (c.lastrowid,)).fetchone()

    def job(self, board, prefilter="pass", tier=None, **kw):
        n = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        cols = dict(board_id=board["id"], ats="greenhouse", token=board["token"], ext_id="j%d" % n, company="Acme",
                    title=kw.get("title", "Support Specialist"), url="https://x/%d" % n, location=kw.get("location", "Remote - US"),
                    first_seen=kw.get("first_seen", db.now()), prefilter=prefilter, tier=tier, score=kw.get("score", 0),
                    baseline=0, reasons='["fits"]', facts="{}")
        c = self.conn.execute("INSERT INTO jobs(%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))), list(cols.values()))
        return c.lastrowid

    def interval_minutes(self, board, res):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)
        poll.board_done(self.conn, board, res, CFGP, now, {"ok": 0, "not_modified": 0})
        due = self.conn.execute("SELECT next_due FROM companies WHERE id=?", (board["id"],)).fetchone()[0]
        return (dt.datetime.fromisoformat(due) - now).total_seconds() / 60


class IntervalTest(DBTest):
    def test_cheap_rechecks_are_faster_the_better_the_board_fits(self):
        b = self.board(etag='"abc"')
        self.assertEqual(self.interval_minutes(b, Result("not_modified")), 180)
        self.job(b, prefilter="pass")
        self.assertEqual(self.interval_minutes(b, Result("not_modified")), 15)
        self.job(b, tier="strong", score=90)
        self.assertEqual(self.interval_minutes(b, Result("not_modified")), 5)

    def test_no_conditional_requests_keeps_the_slow_interval(self):
        b = self.board()
        self.job(b, tier="strong", score=90)
        self.assertEqual(self.interval_minutes(b, Result("ok")), 180)
        self.assertEqual(self.interval_minutes(b, Result("ok", etag='"e"')), 5)


class WatchTest(DBTest):
    def test_one_cycle_polls_only_when_something_is_due(self):
        self.board()
        self.conn.commit()
        with mock.patch.object(watch.db, "connect", return_value=self.conn), mock.patch.object(watch, "nightly"), \
                mock.patch.object(watch.poll, "run", return_value={"new_jobs": 0, "alerts": {}}) as run:
            watch.run(cycles=1)
            self.assertEqual(run.call_count, 1)
            self.conn.execute("UPDATE companies SET next_due='2999-01-01T00:00:00+00:00'")
            watch.run(cycles=1)
            self.assertEqual(run.call_count, 1)


class AlertPolicyTest(DBTest):
    def setUp(self):
        super().setUp()
        self.cfg = config._merge(config.DEFAULTS, {"alerts": {"enabled": True}})
        self.b = self.board()

    def process(self, **over):
        self.cfg["alerts"].update(over)
        with mock.patch.object(alerts.config, "cfg", return_value=self.cfg), \
                mock.patch.object(alerts, "send", return_value=(True, "ok")) as send:
            return alerts.process(self.conn, db.now()), send

    def test_defaults_send_every_strong_job_now(self):
        for i in range(7):
            self.job(self.b, tier="strong", score=86 + i, ext=i)
        out, send = self.process()
        self.assertEqual((out["sent"], out["held"]), (5, 2))   # max_per_run bundles 5, the rest next cycle
        out, send = self.process()
        self.assertEqual(out["sent"], 2)
        self.assertIn("(Remote - US)", send.call_args[0][0])

    def test_quiet_hours_and_daily_cap_still_work(self):
        self.job(self.b, tier="strong", score=95)
        h = alerts.local_now().hour
        out, _ = self.process(quiet_start=h, quiet_end=(h + 1) % 24)
        self.assertEqual(out["held"], 1)
        out, _ = self.process(quiet_start=None, daily_cap=1)
        self.assertEqual(out["sent"], 1)
        self.job(self.b, tier="strong", score=96, ext="b")
        out, _ = self.process()
        self.assertEqual((out["sent"], out["held"]), (0, 1))

    def test_failed_send_is_retried_later(self):
        jid = self.job(self.b, tier="strong", score=95)
        with mock.patch.object(alerts.config, "cfg", return_value=self.cfg), mock.patch.object(alerts, "send", return_value=(False, "502")):
            self.assertEqual(alerts.process(self.conn, db.now())["failed"], 1)
        self.assertEqual(self.process()[0]["candidates"], 0)        # not straight away
        self.conn.execute("UPDATE alerts SET created='2000-01-01T00:00:00+00:00' WHERE job_id=?", (jid,))
        self.assertEqual(self.process()[0]["sent"], 1)


class SecretsTest(unittest.TestCase):
    def test_env_keys_rename(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("MY_BOT_TOKEN=abc\nOTHER=1\n")
        self.addCleanup(os.unlink, f.name)
        env = alerts.secrets({"env_file": f.name, "env_keys": {"JOBATLAS_DISCORD_TOKEN": "MY_BOT_TOKEN"}})
        self.assertEqual(env["JOBATLAS_DISCORD_TOKEN"], "abc")


if __name__ == "__main__":
    unittest.main()
