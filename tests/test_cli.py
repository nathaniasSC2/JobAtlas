import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from jobatlas import cli, config, profile  # noqa: E402


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.object(cli, "setup_logging"):
        cli.main(list(argv))
    return out.getvalue()


def read(path):
    with open(path, encoding="utf8") as f:
        return f.read()


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = os.path.join(self.tmp.name, "data")
        self.patch = mock.patch.multiple(config, DATA=d, LOGS=os.path.join(d, "logs"), EXPORT=os.path.join(d, "export"),
                                         CACHE=os.path.join(d, "cache"), PROFILE_FILE=os.path.join(d, "profile.json"))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_init_copies_examples_once(self):
        out = run("init")
        self.assertEqual(out.count("wrote"), 2)
        self.assertTrue(os.path.exists(os.path.join(config.DATA, "employers.json")))
        with open(config.PROFILE_FILE, "a", encoding="utf8") as f:
            f.write(" ")
        self.assertEqual(run("init").count("kept"), 2)
        self.assertTrue(read(config.PROFILE_FILE).endswith(" "))
        self.assertEqual(run("init", "--force").count("wrote"), 2)

    def test_profile_check(self):
        run("init")
        self.assertIn(": ok", run("profile", "check"))
        with open(config.PROFILE_FILE, "w", encoding="utf8") as f:
            f.write('{"fields": {"x": {"titles": "support("}}, "rules": {"quota": "maybe"}, "colour": 1}')
        with self.assertRaises(SystemExit) as e:
            run("profile", "check")
        self.assertEqual(e.exception.code, 1)
        with self.assertRaises(profile.ProfileError):
            profile.load(path=config.PROFILE_FILE)


class ProfileCheckTest(unittest.TestCase):
    def setUp(self):
        with open(profile.EXAMPLE, encoding="utf8") as f:
            self.d = json.load(f)

    def test_example_is_clean(self):
        self.assertEqual(profile.check(self.d), [])

    def assertProblem(self, needle):
        problems = profile.check(self.d)
        self.assertTrue(any(needle in p for p in problems), problems)

    def test_unknown_keys(self):
        self.d["locaton"] = {}
        self.d["fields"]["it_support"]["credible"] = 3
        self.assertProblem("profile: unknown key 'locaton'")
        self.assertProblem("fields.it_support: unknown key 'credible'")

    def test_rules(self):
        self.d["rules"].update(weekend_required="no", weekends="skip")
        self.assertProblem("rules.weekend_required: 'no' should be skip, flag or ignore")
        self.assertProblem("rules: unknown rule 'weekends'")

    def test_bad_regex_and_values(self):
        self.d["exclude_titles"] = "(manager"
        self.d["blocked_employers"] = [{"regex": "[acme"}]
        self.d["pay"]["minimum_year"] = "40k"
        self.d["location"]["home_state"] = "XX"
        self.d["quota_fields"] = ["sales"]
        for needle in ("exclude_titles: bad regex", "blocked_employers[0].regex: bad regex", "pay.minimum_year: '40k' is not a number",
                       "'XX' is not a US state code", "quota_fields: no field named 'sales'"):
            self.assertProblem(needle)

    def test_bad_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"fields": {},}')
        self.addCleanup(os.unlink, f.name)
        self.assertIn("not valid JSON", profile.check_file(f.name)[0])


if __name__ == "__main__":
    unittest.main()
