#!/usr/bin/env python3
"""Record trimmed real API responses into tests/fixtures/<name>.json (run on the box, rarely).

    python3 tests/record_fixtures.py [name ...]

Each fixture is {"requests": [{"method", "url", "status", "body"}]}; list
responses are trimmed to a few jobs so the files stay small. Tests replay them
through tests/fakeclient.py; no network is used by the tests.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from jobatlas.net import Client  # noqa: E402

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
KEEP = 3


def trim(body, kind):
    if kind == "json":
        d = json.loads(body)
        if isinstance(d, list):
            d = d[:KEEP]
        elif isinstance(d, dict):
            for k in ("jobs", "offers", "result", "content", "data", "jobPostings"):
                if isinstance(d.get(k), list):
                    d[k] = d[k][:KEEP]
        return json.dumps(d)
    if kind == "rss":
        items = re.findall(r"<item>.*?</item>", body, re.S)
        head = body.split("<item>", 1)[0]
        return head + "".join(items[:KEEP]) + "</channel></rss>"
    return body


CASES = {
    "greenhouse": [("GET", "https://boards-api.greenhouse.io/v1/boards/tailscale/jobs", "json"),
                   ("GET", "DETAIL:greenhouse", "json")],
    "lever": [("GET", "https://api.lever.co/v0/postings/truv?mode=json", "json")],
    "ashby": [("GET", "https://api.ashbyhq.com/posting-api/job-board/n8n?includeCompensation=true", "json")],
    "rippling": [("GET", "https://api.rippling.com/platform/api/ats/v1/board/nerdio-careers/jobs", "json"),
                 ("GET", "DETAIL:rippling", "json")],
    "recruitee": [("GET", "https://bunq.recruitee.com/api/offers/", "json")],
    "teamtailor": [("GET", "https://career.teamtailor.com/jobs.rss", "rss")],
    "workday": [("POST", "https://uw.wd5.myworkdayjobs.com/wday/cxs/uw/UWHires/jobs", "json"), ("GET", "DETAIL:workday", "json")],
}


def main(names):
    c = Client(log=print)
    os.makedirs(HERE, exist_ok=True)
    for name in names or CASES:
        reqs = []
        last = None
        for method, url, kind in CASES[name]:
            if url.startswith("DETAIL:"):
                d = json.loads(last)
                if name == "greenhouse":
                    url = "https://boards-api.greenhouse.io/v1/boards/tailscale/jobs/%s?questions=true&pay_transparency=true" % d["jobs"][0]["id"]
                elif name == "rippling":
                    url = "https://ats.rippling.com/api/v2/board/nerdio-careers/jobs/%s" % d[0]["uuid"]
                elif name == "workday":
                    url = "https://uw.wd5.myworkdayjobs.com/wday/cxs/uw/UWHires%s" % d["jobPostings"][0]["externalPath"]
            r = c.post_json(url, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "support"}) if method == "POST" else c.get(url)
            body = trim(r.text, kind) if r.ok else r.text
            last = body
            reqs.append({"method": method, "url": url, "status": r.status, "body": body})
            print(name, method, url, r.status, len(body))
        with open(os.path.join(HERE, name + ".json"), "w", encoding="utf8") as f:
            json.dump({"requests": reqs}, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
