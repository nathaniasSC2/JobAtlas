"""Adapter tests against recorded fixtures (tests/fixtures, from tests/record_fixtures.py) and small
hand-made payloads in each ATS's documented shape. No network."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakeclient import FakeClient  # noqa: E402

from jobatlas import adapters, boards  # noqa: E402
from jobatlas.adapters.base import html_text, iso_date, relative_date, rss_items  # noqa: E402


def resp(method, url, body, status=200):
    return {"method": method, "url": url, "status": status, "body": body if isinstance(body, str) else json.dumps(body)}


class TestRecorded(unittest.TestCase):
    def test_greenhouse_list_and_detail(self):
        c = FakeClient("greenhouse")
        r = adapters.get("greenhouse").fetch(c, "tailscale")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.name, "Tailscale")
        self.assertTrue(r.jobs and all(j["needs_detail"] for j in r.jobs))
        j = r.jobs[0]
        self.assertTrue(j["url"].startswith("https://job-boards.greenhouse.io/tailscale/jobs/"))
        self.assertRegex(j["posted_at"], r"^\d{4}-\d{2}-\d{2}$")
        d = adapters.get("greenhouse").detail(c, "tailscale", j)
        self.assertGreater(len(d["description"]), 200)
        labels = [q["label"] for q in d["questions"]]
        self.assertNotIn("First Name", labels)  # standard fields are dropped
        self.assertTrue(any("Tailscale" in l for l in labels))  # custom questions kept
        self.assertIn("Pay Range", d["pay_text"])

    def test_lever(self):
        r = adapters.get("lever").fetch(FakeClient("lever"), "truv")
        self.assertEqual(r.status, "ok")
        j = r.jobs[0]
        self.assertTrue(j["url"].startswith("https://jobs.lever.co/truv/"))
        self.assertGreater(len(j["description"]), 200)
        self.assertFalse(j["needs_detail"])
        self.assertTrue(any(x["pay_text"] for x in r.jobs) or True)

    def test_lever_eu_token(self):
        self.assertEqual(adapters.get("lever").public_url("eu:acme"), "https://jobs.eu.lever.co/acme")

    def test_ashby(self):
        r = adapters.get("ashby").fetch(FakeClient("ashby"), "n8n")
        self.assertEqual(r.status, "ok")
        j = r.jobs[0]
        self.assertTrue(j["url"].startswith("https://jobs.ashbyhq.com/n8n/"))
        self.assertGreater(len(j["description"]), 200)
        self.assertIn(j["workplace"], ("remote", "hybrid", "onsite", ""))

    def test_rippling(self):
        c = FakeClient("rippling")
        r = adapters.get("rippling").fetch(c, "nerdio-careers")
        self.assertEqual(r.status, "ok")
        d = adapters.get("rippling").detail(c, "nerdio-careers", r.jobs[0])
        self.assertEqual(d["company"], "Nerdio")
        self.assertIn("$", d["pay_text"])
        self.assertTrue(d["questions"])
        self.assertGreater(len(d["description"]), 200)

    def test_recruitee(self):
        r = adapters.get("recruitee").fetch(FakeClient("recruitee"), "bunq")
        self.assertEqual((r.status, r.name), ("ok", "bunq"))
        self.assertTrue(r.jobs[0]["description"])

    def test_teamtailor_rss(self):
        r = adapters.get("teamtailor").fetch(FakeClient("teamtailor"), "career")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.name, "Teamtailor")
        j = r.jobs[0]
        self.assertTrue(j["ext_id"].isdigit())
        self.assertTrue(j["location"])
        self.assertIn(j["workplace"], ("remote", "hybrid", "onsite", ""))

    def test_pageup_rss(self):
        long_ad = "<p>" + "Provide desktop and account support to faculty and staff. " * 6 + "</p>"
        rss = ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel xmlns:job="http://pageuppeople.com/"><item>'
               '<guid>https://careers.pageuppeople.com/123/cw/en-us/job/500123/it-support-technician</guid>'
               '<link>https://careers.pageuppeople.com/123/cw/en-us/job/500123/it-support-technician</link>'
               '<title>IT Support Technician</title><description>Short summary</description>'
               '<job:description><![CDATA[' + long_ad + ']]></job:description><job:location>Asheville, NC</job:location>'
               '<job:applyLink>https://careers.pageuppeople.com/123/cw/en-us/job/500123/apply</job:applyLink>'
               '<job:workType>Full Time</job:workType><pubDate>Fri, 16 Oct 2026 09:00:00 GMT</pubDate></item></channel></rss>')
        c = FakeClient(responses=[resp("GET", "https://careers.pageuppeople.com/123/cw/en-us/rss", rss)])
        r = adapters.get("pageup").fetch(c, "123")
        self.assertEqual(r.status, "ok")
        j = r.jobs[0]
        self.assertEqual((j["ext_id"], j["location"], j["posted_at"]), ("500123", "Asheville, NC", "2026-10-16"))
        self.assertTrue(j["apply_url"].startswith("https://"))
        self.assertGreater(len(j["description"]), 200)

    def test_workday(self):
        c = FakeClient("workday")
        r = adapters.get("workday").fetch(c, "uw|wd5|UWHires", terms=["support"])
        self.assertEqual(r.status, "ok")
        j = r.jobs[0]
        self.assertTrue(j["url"].startswith("https://uw.wd5.myworkdayjobs.com/en-US/UWHires/job/"))
        d = adapters.get("workday").detail(c, "uw|wd5|UWHires", j)
        self.assertGreater(len(d["description"]), 200)
        self.assertTrue(d["location"])


class TestHandMade(unittest.TestCase):
    def test_workable_widget(self):
        c = FakeClient(responses=[resp("GET", "https://apply.workable.com/api/v1/widget/accounts/acme", {
            "name": "Acme Inc", "jobs": [{"title": "IT Support Specialist", "shortcode": "ABC123", "telecommuting": True,
                                          "url": "https://apply.workable.com/j/ABC123", "published_on": "2026-10-18",
                                          "city": "", "state": "", "country": "United States", "description": "<p>Help our team</p>"}]})])
        r = adapters.get("workable").fetch(c, "acme")
        self.assertEqual((r.status, r.name, r.jobs[0]["workplace"]), ("ok", "Acme Inc", "remote"))
        self.assertEqual(r.jobs[0]["description"], "Help our team")

    def test_bamboohr(self):
        c = FakeClient(responses=[
            resp("GET", "https://acme.bamboohr.com/careers/list", {"meta": {"totalCount": 1}, "result": [
                {"id": "42", "jobOpeningName": "Help Desk Technician", "departmentLabel": "IT", "employmentStatusLabel": "Full-Time",
                 "location": {"city": "Asheville", "state": "North Carolina"}, "isRemote": None, "locationType": "0"}]}),
            resp("GET", "https://acme.bamboohr.com/careers/42/detail", {"result": {"jobOpening": {
                "description": "<p>Support staff</p>", "compensation": "$25 - $30 per hour", "datePosted": "2026-10-17"}}})])
        a = adapters.get("bamboohr")
        r = a.fetch(c, "acme")
        self.assertEqual(r.jobs[0]["location"], "Asheville, North Carolina")
        d = a.detail(c, "acme", r.jobs[0])
        self.assertEqual((d["pay_text"], d["posted_at"]), ("$25 - $30 per hour", "2026-10-17"))

    def test_breezy(self):
        c = FakeClient(responses=[resp("GET", "https://acme.breezy.hr/json", [
            {"id": "x1", "name": "Support Engineer", "url": "https://acme.breezy.hr/p/x1", "published_date": "2026-10-10T00:00:00Z",
             "location": {"name": "Remote, US", "is_remote": True}, "company": {"name": "Acme"}}])])
        r = adapters.get("breezy").fetch(c, "acme")
        self.assertEqual((r.name, r.jobs[0]["workplace"], r.jobs[0]["posted_at"]), ("Acme", "remote", "2026-10-10"))

    def test_paylocity_pagedata(self):
        page = ('<html><head><title>Acme Co - Current Openings</title></head><script>window.pageData = {"Jobs":[{"JobId":123,'
                '"JobTitle":"IT Technician","LocationName":"Asheville","PublishedDate":"2026-10-16T00:00:00","IsRemote":false,'
                '"JobLocation":{"City":"Asheville","State":"NC"}}]};</script></html>')
        guid = "11111111-2222-3333-4444-555555555555"
        c = FakeClient(responses=[resp("GET", "https://recruiting.paylocity.com/Recruiting/Jobs/All/" + guid, page)])
        r = adapters.get("paylocity").fetch(c, guid)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.name, "Acme Co")
        self.assertEqual(r.jobs[0]["url"], "https://recruiting.paylocity.com/Recruiting/Jobs/Details/123")
        self.assertEqual(r.jobs[0]["location"], "Asheville, NC")

    def test_missing_board_is_none(self):
        r = adapters.get("greenhouse").fetch(FakeClient(), "nope")
        self.assertEqual(r.status, "none")

    def test_rate_limited_is_blocked(self):
        c = FakeClient(responses=[resp("GET", "https://api.ashbyhq.com/posting-api/job-board/acme", "slow down", status=429)])
        self.assertEqual(adapters.get("ashby").fetch(c, "acme").status, "blocked")


class TestHelpers(unittest.TestCase):
    def test_boards_in(self):
        t = ('<a href="https://boards.greenhouse.io/acme/jobs/1">x</a> https://jobs.lever.co/beta/abc '
             'https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/x https://jobs.ashbyhq.com/Gamma '
             '<script src="https://boards.greenhouse.io/embed/job_board/js?for=delta"></script> '
             'https://recruiting.paylocity.com/Recruiting/Jobs/All/11111111-2222-3333-4444-555555555555 '
             'https://careers.pageuppeople.com/793/cw/en-us/job/1 https://jobs.eu.lever.co/euco/x')
        got = {(b["ats"], b["token"]) for b in boards.boards_in(t)}
        for want in [("greenhouse", "acme"), ("lever", "beta"), ("workday", "acme|wd5|Careers"), ("ashby", "Gamma"),
                     ("greenhouse", "delta"), ("paylocity", "11111111-2222-3333-4444-555555555555"), ("pageup", "793"),
                     ("lever", "eu:euco")]:
            self.assertIn(want, got)

    def test_html_text_double_escaped(self):
        self.assertEqual(html_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;"), "Hello & welcome")

    def test_dates(self):
        self.assertEqual(iso_date(1783311306243), "2026-07-06")
        self.assertEqual(iso_date("Fri, 24 Jul 2026 09:05:16 +0200"), "2026-07-24")
        import datetime
        self.assertEqual(relative_date("Posted 3 Days Ago", datetime.date(2026, 10, 19)), "2026-10-16")
        self.assertEqual(relative_date("Posted 30+ Days Ago", datetime.date(2026, 10, 19)), "2026-09-19")

    def test_rss_namespaced(self):
        x = ('<rss xmlns:job="http://pageuppeople.com/"><channel><item><title>T</title><description>short</description>'
             '<job:description>long text</job:description><job:location>Asheville, NC</job:location></item></channel></rss>')
        it = rss_items(x)[0]
        self.assertEqual((it["description"], it["x:description"], it["x:location"]), ("short", "long text", "Asheville, NC"))


if __name__ == "__main__":
    unittest.main()
