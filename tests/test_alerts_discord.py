import unittest
from unittest import mock

from jobatlas import alerts


class DiscordAlertTest(unittest.TestCase):
    def test_links_are_wrapped(self):
        t = alerts.discord_text("JobAtlas Strong (95): X at Y.\nhttps://boards.greenhouse.io/y/jobs/1")
        self.assertIn("<https://boards.greenhouse.io/y/jobs/1>", t)
        self.assertEqual(alerts.discord_text("<https://a.b/c>"), "<https://a.b/c>")

    def test_channel_dispatch(self):
        with mock.patch.object(alerts, "send_discord_dm", return_value=(True, "d")) as d, \
             mock.patch.object(alerts, "send_discord_webhook", return_value=(True, "w")) as w, \
             mock.patch.object(alerts, "send_imessage", return_value=(True, "i")) as i:
            self.assertEqual(alerts.send("m", {"channel": "discord_dm"}), (True, "d"))
            self.assertEqual(alerts.send("m", {"channel": "imessage"}), (True, "i"))
            self.assertEqual(alerts.send("m", {}), (True, "w"))
            d.assert_called_once()
            w.assert_called_once()
            i.assert_called_once()

    def test_missing_secrets_fail_cleanly(self):
        ok, info = alerts.send_discord_dm("m", {"env_file": "/nonexistent"})
        self.assertFalse(ok)
        self.assertIn("JOBATLAS_DISCORD_TOKEN", info)
        ok, info = alerts.send_discord_webhook("m", {"env_file": "/nonexistent"})
        self.assertFalse(ok)
        self.assertIn("JOBATLAS_DISCORD_WEBHOOK", info)
        ok, info = alerts.send_imessage("m", {"env_file": "/nonexistent"})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
