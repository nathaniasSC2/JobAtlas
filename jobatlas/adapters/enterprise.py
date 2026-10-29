"""Enterprise HR suites with public career-site JSON: ADP Workforce Now and Oracle Recruiting
Cloud (HCM).

ADP:    token = cid. GET workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/
        job-requisitions?cid=<cid>&lang=en_US&ccId=19000101_000001&locale=en_US&$top=200 (+ /<itemID> detail)
Oracle: token = "<host>|<siteNumber>". GET <host>/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&finder=findReqs;siteNumber=<site>,limit=25,offset=N,keyword="..."  (keyword search, so PARTIAL;
        the keywords are the profile's search_terms)

Not added: UKG Ready (secure7.saashr.com) and myschooldata.net, whose robots.txt disallows all crawlers.
"""
import re
import urllib.parse

from .. import profile
from .base import Result, from_response, html_text, iso_date, new_job, pay_range_text, workplace_of


class ADP:
    ATS, PARTIAL = "adp", False
    BASE = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    Q = "cid=%s&lang=en_US&ccId=19000101_000001&locale=en_US"

    @staticmethod
    def public_url(token):
        return "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=%s&ccId=19000101_000001&lang=en_US" % token

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None):
        r = client.get(ADP.BASE + "?" + (ADP.Q % token) + "&$top=200")
        bad = from_response(r)
        if bad:
            return bad
        d = r.json()
        if not isinstance(d, dict) or "jobRequisitions" not in d:
            return Result("none", error="not an ADP career center")
        jobs = []
        for j in d["jobRequisitions"]:
            iid = j.get("itemID")
            pg = j.get("payGradeRange") or {}
            lo = (pg.get("minimumRate") or {}).get("amountValue")
            hi = (pg.get("maximumRate") or {}).get("amountValue")
            per = ""
            for f in ((j.get("customFieldGroup") or {}).get("codeFields") or []):
                if (f.get("nameCode") or {}).get("codeValue") == "SalaryType":
                    per = "hour" if f.get("codeValue") == "HR" else "year"
            locs = [", ".join(x for x in [(l.get("address") or {}).get("cityName"), ((l.get("address") or {}).get("countrySubdivisionLevel1") or {}).get("codeValue")] if x)
                    for l in (j.get("requisitionLocations") or [])]
            jobs.append(new_job(iid, j.get("requisitionTitle"), ADP.public_url(token) + "&jobId=" + urllib.parse.quote(str(iid)),
                                location=" / ".join(x for x in locs if x), employment_type=(j.get("workLevelCode") or {}).get("shortName", ""),
                                posted_at=iso_date(j.get("postDate")), pay_text=pay_range_text(lo, hi, "USD", per) if (lo or hi) else "",
                                raw={k: v for k, v in j.items() if k not in ("customFieldGroup",)}, needs_detail=True))
        return Result("ok", jobs, url=ADP.public_url(token))

    @staticmethod
    def detail(client, token, job):
        r = client.get(ADP.BASE + "/" + urllib.parse.quote(job["ext_id"]) + "?" + (ADP.Q % token))
        if not r.ok:
            return {"_detail_error": "HTTP %s" % r.status}
        d = r.json() or {}
        locs = []
        for l in d.get("requisitionLocations") or []:
            a = l.get("address") or {}
            locs.append(", ".join(x for x in [a.get("cityName"), (a.get("countrySubdivisionLevel1") or {}).get("codeValue")] if x))
        desc = html_text(d.get("requisitionDescription") or "")
        return {"description": desc, "location": " / ".join(x for x in locs if x), "workplace": workplace_of(desc[:1500]) if "remote" in desc[:1500].lower() else ""}


class Oracle:
    ATS, PARTIAL = "oracle", True
    PAGES = 2

    @staticmethod
    def _parts(token):
        host, _, site = token.partition("|")
        return host, site

    @staticmethod
    def public_url(token):
        host, site = Oracle._parts(token)
        return "https://%s/hcmUI/CandidateExperience/en/sites/%s/jobs" % (host, site)

    @staticmethod
    def fetch(client, token, etag=None, last_modified=None, terms=None):
        host, site = Oracle._parts(token)
        api = "https://%s/hcmRestApi/resources/latest/recruitingCEJobRequisitions" % host
        jobs, seen, any_ok = [], set(), False
        for term in (terms if terms is not None else profile.load().search_terms):
            for page in range(Oracle.PAGES):
                kw = (',keyword="%s"' % term) if term else ""
                url = "%s?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=%s,limit=25,offset=%d%s" % (
                    api, site, page * 25, urllib.parse.quote(kw, safe='=,";'))
                r = client.get(url)
                if not r.ok:
                    if not any_ok:
                        return from_response(r) or Result("error", error="HTTP %s" % r.status)
                    break
                any_ok = True
                items = (r.json() or {}).get("items") or []
                reqs = (items[0].get("requisitionList") if items else []) or []
                for j in reqs:
                    jid = str(j.get("Id"))
                    if jid in seen:
                        continue
                    seen.add(jid)
                    locs = [j.get("PrimaryLocation") or ""] + [x.get("Name", "") for x in (j.get("secondaryLocations") or [])]
                    jobs.append(new_job(jid, j.get("Title"), "https://%s/hcmUI/CandidateExperience/en/sites/%s/job/%s" % (host, site, jid),
                                        location=" / ".join(x for x in locs if x), workplace=(j.get("WorkplaceType") or "").lower(),
                                        posted_at=iso_date(j.get("PostedDate")), raw=j, needs_detail=True))
                if len(reqs) < 25:
                    break
        return Result("ok", jobs, url=Oracle.public_url(token))

    @staticmethod
    def detail(client, token, job):
        host, site = Oracle._parts(token)
        url = ("https://%s/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
               "&finder=ByReqNumber;Id=%%22%s%%22,siteNumber=%s" % (host, job["ext_id"], site))
        r = client.get(url)
        if not r.ok:
            return {"_detail_error": "HTTP %s" % r.status}
        items = (r.json() or {}).get("items") or []
        if not items:
            return {"_closed": True}
        d = items[0]
        text = "\n\n".join(html_text(d.get(k) or "") for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                                                "ExternalQualificationsStr", "CorporateDescriptionStr") if d.get(k))
        return {"description": text, "workplace": (d.get("WorkplaceType") or "").lower()}
