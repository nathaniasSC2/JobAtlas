import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from jobatlas import cli, config  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
