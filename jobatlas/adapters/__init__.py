"""ATS adapter registry: name -> module/class with fetch/detail/public_url/PARTIAL."""
from . import ashby, greenhouse, lever, rippling, workable, workday
from .enterprise import ADP, Oracle
from .feeds import ApplicantPro, CivicPlus, Jobvite, Neogov, PageUp, Paylocity
from .small import BambooHR, Breezy, Pinpoint, Recruitee, SmartRecruiters, Teamtailor

ADAPTERS = {a.ATS: a for a in (greenhouse, lever, ashby, workable, rippling, workday, Recruitee, Teamtailor, BambooHR,
                                Breezy, SmartRecruiters, Pinpoint, PageUp, Paylocity, Jobvite, Neogov, CivicPlus,
                                ApplicantPro, ADP, Oracle)}


def get(ats):
    return ADAPTERS.get(ats)
