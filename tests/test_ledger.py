import json
import os
import tempfile
import unittest

from jobatlas import ledger


class LedgerTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"company": "Acme, Inc.", "title": "Support Specialist (Remote)", "feed": "digest", "date": "2026-10-20",
                                "url": "https://boards.greenhouse.io/acme/jobs/123?gh_src=x"}) + "\n")
            f.write("not json\n")
            f.write(json.dumps({"company": "Globex", "title": "IT Technician", "url": "https://jobs.example.com/apply?JobOpeningId=7&utm=a"}) + "\n")
        self.L = ledger.Ledger(self.path)

    def tearDown(self):
        os.remove(self.path)

    def test_url_match_ignores_tracking_params(self):
        e, how = self.L.match("Someone", "Other", ["https://www.boards.greenhouse.io/acme/jobs/123/"])
        self.assertEqual((e["feed"], how), ("digest", "url"))

    def test_job_id_params_are_kept(self):
        self.assertEqual(self.L.match("x", "y", ["https://jobs.example.com/apply?JobOpeningId=7"])[1], "url")
        self.assertIsNone(self.L.match("x", "y", ["https://jobs.example.com/apply?JobOpeningId=8"])[0])

    def test_company_and_title_words(self):
        e, how = self.L.match("ACME Inc", "Support Specialist", [])
        self.assertEqual(how, "company+title")
        self.assertEqual(self.L.entries, 2)


if __name__ == "__main__":
    unittest.main()
