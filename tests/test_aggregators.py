"""Hitmarker (sitemap + JSON-LD) and Work With Indies (RSS + JSON-LD) adapters, hand-made payloads in the sites' real shapes."""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakeclient import FakeClient  # noqa: E402

from jobatlas import adapters  # noqa: E402


def resp(url, body, status=200):
    return {"method": "GET", "url": url, "status": status, "body": body}


def page(ld, extra=""):
    return '<html><script type="application/ld+json">%s</script>%s</html>' % (json.dumps(ld), extra)


class TestHitmarker(unittest.TestCase):
    def test_sitemap_and_detail(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = (now - datetime.timedelta(days=40)).isoformat()
        sm = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              '<url><loc>https://hitmarker.net/jobs/ravenwake-games-community-manager-5566959</loc><lastmod>%s</lastmod></url>'
              '<url><loc>https://hitmarker.net/jobs/old-co-old-job-1</loc><lastmod>%s</lastmod></url></urlset>' % (now.isoformat(), old))
        ld = {"@type": "JobPosting", "title": "Community Manager", "description": "<p>Run the Discord.</p>", "datePosted": "2026-11-10T23:18:11+00:00",
              "employmentType": ["FULL_TIME"], "hiringOrganization": {"name": "Ravenwake Games"}, "jobLocationType": "TELECOMMUTE",
              "applicantLocationRequirements": {"@type": "Country", "name": "US"},
              "jobLocation": [{"address": {"addressRegion": "British Columbia", "addressCountry": "CA"}}],
              "baseSalary": {"currency": "USD", "value": {"minValue": 75000, "maxValue": 105000, "unitText": "YEAR"}},
              "occupationalCategory": ["Community Management", "Social Media"]}
        c = FakeClient(responses=[resp("https://hitmarker.net/sitemap-jobs.xml/p1", sm),
                                  resp("https://hitmarker.net/jobs/ravenwake-games-community-manager-5566959", page(ld))])
        a = adapters.get("hitmarker")
        r = a.fetch(c, "jobs")
        self.assertEqual(r.status, "ok")
        self.assertEqual([j["ext_id"] for j in r.jobs], ["5566959"])
        self.assertEqual(r.jobs[0]["title"], "Ravenwake Games Community Manager")
        d = a.detail(c, "jobs", r.jobs[0])
        self.assertEqual((d["title"], d["company"], d["location"], d["workplace"]), ("Community Manager", "Ravenwake Games", "Remote (United States)", "remote"))
        self.assertEqual(d["pay_text"], "$75,000 - $105,000/yr")
        self.assertIn("Discord", d["description"])

    def test_onsite_foreign_country_and_gone(self):
        ld = {"@type": "JobPosting", "title": "Producer", "description": "x", "hiringOrganization": {"name": "Riot"},
              "jobLocation": [{"address": {"addressLocality": "Shanghai", "addressCountry": "CN"}}]}
        c = FakeClient(responses=[resp("https://hitmarker.net/jobs/a-1", page(ld)), resp("https://hitmarker.net/jobs/b-2", "", 404)])
        a = adapters.get("hitmarker")
        d = a.detail(c, "jobs", {"url": "https://hitmarker.net/jobs/a-1"})
        self.assertEqual(d["location"], "Shanghai, China")
        self.assertEqual(d["raw_detail"]["country"]["descriptor"], "China")
        self.assertTrue(a.detail(c, "jobs", {"url": "https://hitmarker.net/jobs/b-2"})["_closed"])

    def test_remote_outside_us_only(self):
        from jobatlas.adapters.aggregators import ld_fields
        d = ld_fields({"title": "x", "jobLocationType": "TELECOMMUTE", "jobLocation": [{"address": {"addressCountry": "LT"}}],
                       "applicantLocationRequirements": [{"name": n} for n in ("AX", "Germany", "France")]})
        self.assertTrue(d["location"].startswith("Remote, other countries only"))
        self.assertEqual(d["raw_detail"]["country"]["descriptor"], "Germany")

    def test_changed_site_is_an_error_not_silence(self):
        c = FakeClient(responses=[resp("https://hitmarker.net/sitemap-jobs.xml/p1", "<html>blocked</html>")])
        self.assertEqual(adapters.get("hitmarker").fetch(c, "jobs").status, "error")
        a = adapters.get("hitmarker")
        c = FakeClient(responses=[resp("https://hitmarker.net/jobs/a-1", "<html>no json</html>")])
        self.assertIn("_detail_error", a.detail(c, "jobs", {"url": "https://hitmarker.net/jobs/a-1"}))


class TestWorkWithIndies(unittest.TestCase):
    RSS = ('<?xml version="1.0"?><rss version="2.0"><channel><item>'
           '<title>Fellow Traveller is hiring a Community Manager to work from AECT ± 4 hours</title>'
           '<link>https://www.workwithindies.com/careers/fellow-traveller-community-manager</link><guid>x</guid>'
           '<description>Join us. Tags: [Marketing] [Full Time]</description><pubDate>Mon, 09 Nov 2026 00:00:00 GMT</pubDate></item>'
           '<item><title>Wooster Games is hiring a Social Media Specialist to work from Anywhere</title>'
           '<link>https://www.workwithindies.com/careers/wooster-games-social-media-specialist</link><guid>y</guid>'
           '<description>d</description><pubDate>Tue, 10 Nov 2026 00:00:00 GMT</pubDate></item></channel></rss>')

    def test_feed(self):
        c = FakeClient(responses=[resp("https://www.workwithindies.com/careers/rss.xml", self.RSS)])
        r = adapters.get("workwithindies").fetch(c, "careers")
        self.assertEqual(r.status, "ok")
        j = r.jobs[0]
        self.assertEqual((j["company"], j["title"], j["workplace"]), ("Fellow Traveller", "Community Manager", "remote"))
        self.assertEqual(r.jobs[1]["location"], "Remote (Anywhere)")

    def test_detail_open_and_closed(self):
        ld = {"@type": "JobPosting", "title": "Social Media Specialist", "description": "<p>Post things</p>", "datePosted": "2026-11-10",
              "hiringOrganization": {"name": "Wooster Games"}, "jobLocationType": "TELECOMMUTE",
              "jobLocation": {"address": {"addressCountry": "GB"}}, "applicationContact": {"url": "https://x.example/apply"}}
        openp = page(ld, '<div class="closed-notif w-condition-invisible">closed</div><a id="apply-top" href="https://x.example/apply?a=1&amp;b=2">Apply</a>')
        closedp = page(ld, '<div class="closed-notif">This position has been closed.</div>')
        openp = openp.replace('Post things', 'Post\nthings')
        c = FakeClient(responses=[resp("https://www.workwithindies.com/careers/a", openp), resp("https://www.workwithindies.com/careers/b", closedp)])
        a = adapters.get("workwithindies")
        d = a.detail(c, "careers", {"url": "https://www.workwithindies.com/careers/a", "location": "Remote (Anywhere)"})
        self.assertEqual(d["apply_url"], "https://x.example/apply?a=1&b=2")
        self.assertEqual(d["company"], "Wooster Games")
        self.assertNotIn("location", d)          # the poster's "Anywhere" is kept
        self.assertNotIn("_closed", d)
        self.assertTrue(a.detail(c, "careers", {"url": "https://www.workwithindies.com/careers/b"})["_closed"])


class TestJsonLd(unittest.TestCase):
    def test_webflow_stray_backslash_and_raw_newline(self):
        from jobatlas.adapters.aggregators import jobposting
        html = '<script type="application/ld+json">{"@type": "JobPosting", "title": "A", "description": "x target=\\_blank\ny"}</script>'
        self.assertEqual(jobposting(html)["title"], "A")


if __name__ == "__main__":
    unittest.main()
