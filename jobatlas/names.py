"""Company-name normalisation and ATS slug guessing."""
import re
import unicodedata

# Legal suffixes dropped, & -> and, so "Acme, Inc." and "ACME Inc" are one company.
COMPANY_DROP = set("""inc incorporated llc l l c ltd limited corp corporation co company plc gmbh sa ag bv pbc pte pty
the llp lp holdings""".split())
SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|company|co|technologies|technology|labs|group|holdings|the|pbc|plc|"
                    r"gmbh|io|hq|software|solutions|systems|services|international|usa|us|ai|app|apps|games|studios?|"
                    r"interactive|entertainment|digital|global|careers?|jobs)\b\.?", re.I)


def ascii_fold(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def norm_company(c):
    c = ascii_fold(c).lower().replace("&", " and ").replace("'", "").replace("’", "")
    c = re.sub(r"\(.*?\)", " ", c)
    words = [w for w in re.findall(r"[a-z0-9]+", c) if w not in COMPANY_DROP]
    return " ".join(words)


def core_words(c):
    """Distinctive words (generic suffixes dropped) used to match a board's name to a company."""
    c = re.sub(r"\(.*?\)", " ", ascii_fold(c).lower().replace("&", " and "))
    ws = [w for w in re.findall(r"[a-z0-9]+", SUFFIX.sub(" ", c)) if w not in COMPANY_DROP]
    return ws or re.findall(r"[a-z0-9]+", c)[:1]


def slug_variants(company, limit=8):
    """Likely board slugs for a company name, most likely first."""
    c = re.sub(r"\(.*?\)", " ", ascii_fold(company).lower().replace("&", "and").replace("'", "").replace("’", ""))
    c = re.sub(r"\.(com|io|ai|co|net|org|app|gg|tv|dev)\b", r" \1", c)
    allw = re.findall(r"[a-z0-9]+", c)
    allw = [w for w in allw if w not in ("the",)]
    core = [w for w in re.findall(r"[a-z0-9]+", SUFFIX.sub(" ", c))]
    base = [w for w in allw if w not in COMPANY_DROP]
    out = []
    for s in ["".join(base), "".join(core), "-".join(base), "-".join(core), "".join(allw),
              core[0] if len(core) > 1 and len(core[0]) >= 4 else "",
              "".join(core) + "inc", "".join(core) + "hq", "".join(core) + "careers", "join" + "".join(core),
              "".join(base) + "-careers"]:
        if s and len(s) > 2 and s not in out:
            out.append(s)
    return out[:limit]


def same_company(want, got):
    """True/False when a board's own name says whose it is; None when unknown."""
    if not got:
        return None
    a, b = norm_company(want), norm_company(got)
    if not a or not b:
        return None
    if a == b:
        return True
    ca, cb = "".join(core_words(want)), "".join(core_words(got))
    if ca and cb and ca == cb:
        return True
    short, long_ = sorted((a.replace(" ", ""), b.replace(" ", "")), key=len)
    return len(short) >= 5 and short in long_ and len(short) / len(long_) >= 0.6


def mentions(company, text):
    ws = [w for w in core_words(company) if len(w) > 2]
    t = (text or "").lower()
    return bool(ws) and all(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ws[:2])
