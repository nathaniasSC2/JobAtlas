import datetime as dt
import unittest

from jobatlas import alerts


class AlertPolicyTest(unittest.TestCase):
    def test_quiet_hours_wrap_midnight(self):
        f = lambda h: alerts.in_quiet_hours(dt.datetime(2026, 10, 26, h, 30), 22, 8)
        self.assertTrue(f(23) and f(0) and f(7))
        self.assertFalse(f(8) or f(12) or f(21))

    def test_quiet_hours_same_day_window(self):
        self.assertTrue(alerts.in_quiet_hours(dt.datetime(2026, 10, 26, 13), 12, 14))
        self.assertFalse(alerts.in_quiet_hours(dt.datetime(2026, 10, 26, 14), 12, 14))

    def test_local_now(self):
        self.assertEqual(alerts.local_now("America/New_York").tzinfo.key, "America/New_York")
        self.assertIsNotNone(alerts.local_now("").utcoffset())

    def test_title_filter(self):
        p = alerts.DEFAULT_SKIP_TITLES
        for bad in ["Head of Trust & Safety", "Senior Support Engineer", "Sr. Implementation Specialist",
                    "Website Producer", "Marketing Producer", "Director, Support"]:
            self.assertFalse(alerts.title_ok(bad, p), bad)
        for good in ["Technical Support Engineer (L1)", "Implementation Specialist I", "Community Manager",
                     "IT Support Specialist", "Implementation Manager"]:
            self.assertTrue(alerts.title_ok(good, p), good)


if __name__ == "__main__":
    unittest.main()
