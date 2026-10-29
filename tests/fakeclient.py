"""Replays recorded responses (tests/fixtures/*.json) in place of jobatlas.net.Client."""
import json
import os

from jobatlas.net import Response

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class FakeClient:
    def __init__(self, *names, responses=None):
        self.reqs = []
        for n in names:
            with open(os.path.join(FIX, n + ".json"), encoding="utf8") as f:
                self.reqs += json.load(f)["requests"]
        self.reqs += responses or []
        self.calls = []

    def _find(self, method, url):
        self.calls.append((method, url))
        for r in self.reqs:
            if r["method"] == method and (r["url"] == url or r["url"].split("?")[0] == url.split("?")[0]):
                return Response(r["status"], r.get("headers") or {}, r["body"].encode(), url)
        return Response(404, {}, b"{}", url)

    def get(self, url, **kw):
        return self._find("GET", url)

    def post_json(self, url, payload, **kw):
        return self._find("POST", url)

    def allowed(self, url):
        return True

    def host_dead(self, url):
        return False
