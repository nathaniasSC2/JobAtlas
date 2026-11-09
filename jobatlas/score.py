"""Rule-based filter and score: would this posting get the candidate in front of a person?

The generic part lives here: reading pay, years of experience, degree wording,
remote/on-site wording and state hiring limits out of the posting TEXT (not
only its location tag). The personal part (fields and their title patterns,
home location, pay floor, which hard rules apply) comes from the profile
(profile.py, examples/profile.example.json).

Hard skips (verdict "skip"), each switchable to "flag" or "ignore" in the profile:
  * not remote-US and not local (on-site/hybrid elsewhere, non-US remote). The ATS's own country
    (Lever, Workday, Ashby, Oracle) counts, and so do workplace tags like "No Telework";
  * remote limited to states that exclude the home state, or "must reside in <other state>",
    "excludes residents of ...", or a location tag whose only remote part is one other state;
  * the application form's required questions: "must live near <other place>", on-site/in-office
    days, a state list without the home state, work authorization only for another country; and,
    through the rules below, a clearance, a degree, a driver's licence, weekend availability or a
    required_gaps item asked as a yes/no question (these come from the form, not the posting);
  * a posting older than freshness.skip_days (365), except a local one; older than freshness.stale_days
    (120) it is at most fair. A job that appeared later on a board we were already watching counts from then;
  * pay clearly under the minimum; under the floor only with health insurance;
    a stated "no benefits";
  * required weekend shifts or weekend on-call (unclear wording -> flag);
  * quota sales titles, quota/OTE language in quota-sensitive fields;
  * security clearance; a hard degree requirement with no "or equivalent";
  * a driver's licence or own vehicle; internships and applicant pools;
  * fields at unrelated industries (industry_skip); titles outside the profile's fields;
  * the profile's required_gaps (things the person doesn't have) on the posting's required lines.
Strong positive: remote open only to residents of the home state (a small applicant pool).

assess(job) -> {"score", "tier", "verdict", "field", "reasons", "flags", "facts"}
tier: strong >= 85, good >= 72, fair >= 58, long below, skip on any hard skip.
"""
import datetime
import json
import re

from . import profile as profile_mod

TIERS = ["strong", "good", "fair", "long", "skip"]
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
YEARS_RE = re.compile(
    r"(?:(?:minimum|min\.?|at least|over|more than)\s+(?:of\s+)?)?"
    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:\+|plus)?\s*"
    r"(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?"
    r"(?:\(\d{1,2}\)\s*)?(?:years?|yrs?)\b", re.I)
PREFERRED = re.compile(r"\b(prefer|preferred|nice to have|nice-to-have|bonus|a plus|is a plus|ideally|ideal candidate|desired|desirable)\b", re.I)
NOT_EXPERIENCE = re.compile(r"(founded|in business|for over \d+ years|years? (?:old|of age)|anniversary|since \d{4}|"
                            r"\bfor \d+ years\b.*\b(company|we|our)\b|pto|vacation|vesting|tenure|service award|contract length|"
                            r"\d+\s*years? (?:in|at) (?:business|operation)|trusted for|record retention|warranty)", re.I)
EXPERIENCE_CTX = re.compile(r"experience|background|track record|working|history of|proven|minimum|at least|"
                            r"delivering|supporting|managing|administering|producing|leading|running|coordinating|"
                            r"in a (?:similar|related|comparable)|of (?:hands-on|professional|relevant|progressive)", re.I)
DEGREE = re.compile(r"\b(bachelor'?s?|b\.?a\.?/b\.?s\.?|b\.?s\.?/b\.?a\.?|\bba\b|\bbs\b|b\.s\.|b\.a\.|undergraduate degree|"
                    r"college degree|university degree|four[- ]year degree|4[- ]year degree|degree in|degree required|"
                    r"master'?s degree)", re.I)
EQUIV = re.compile(r"(or equivalent|equivalent (?:work |practical |professional )?experience|or relevant experience|"
                   r"in lieu of|or (?:a )?combination of|or comparable experience|or equivalent combination|"
                   r"degree not required|no degree required|not required|may substitute|substitut)", re.I)
CERT_REQ = re.compile(r"\b(PMP|CAPM|CompTIA|A\+|Network\+|Security\+|CCNA|CCNP|CISSP|CISM|ITIL|MCSA|MCSE|RHCE|RHCSA|LFCE|"
                      r"Salesforce (?:Certified|Administrator certification)|AWS Certified)\b")
CLEARANCE = re.compile(r"(security clearance|secret clearance|top secret|ts/sci|public trust|polygraph|"
                       r"clearance (?:is )?required|must (?:be able to )?obtain (?:a |an )?(?:secret|security|dod))", re.I)
LICENCE = re.compile(r"(valid driver'?s? licen[cs]e|driver'?s licen[cs]e (?:is )?required|must (?:have|possess|hold) (?:a )?(?:valid )?"
                     r"(?:state )?driver|reliable (?:personal )?transportation (?:is )?required|own (?:reliable )?vehicle|"
                     r"must be able to drive|insurable driving record|cdl\b)", re.I)
REMOTE_STRONG = re.compile(r"(fully remote|100% remote|this (?:is a |role is |position is |job is )(?:fully |100% )?remote|"
                           r"remote (?:role|position|job|opportunity)\b|work from anywhere|can be (?:performed|done|based|worked) remotely|"
                           r"open to (?:fully )?remote|remote within the (?:us|united states)|remote,? us\b|"
                           r"(?:remote|telework)[- ]eligible|eligible for (?:full[- ]time )?(?:remote|telework)|"
                           r"(?:may|can) (?:work|be located) (?:remotely|anywhere)|fully remote|remote work (?:is )?(?:available|allowed|possible))",
                           re.I)
REMOTE_TEXT = re.compile(r"(fully remote|100% remote|remote[- ]first|remote[- ]friendly role|this (?:is a |role is |position is )"
                         r"(?:fully |100% )?remote|remote (?:role|position|job|opportunity|work(?:er)?)\b|work from anywhere|"
                         r"work from home|work-from-home|remote within the (?:us|united states)|remote,? us\b|"
                         r"telework|telecommut|distributed team|work remotely)", re.I)
REMOTE_NEG = re.compile(r"(?:not (?:be )?(?:eligible|available|approved) for (?:full[- ]time |any )?(?:remote|telework)(?: work)?|(?:remote|telework)(?: work)? (?:is|are) not (?:available|an option|permitted|offered|eligible)|no (?:remote|telework)(?: work)?(?: options?)?\b|not a remote (?:role|position|job)|is not remote|cannot be (?:performed|done|worked) remotely|not (?:a )?(?:remote|telework)[- ]eligible)", re.I)
# remote wording that is only a perk of an office job ("work from anywhere for up to 4 weeks per year")
REMOTE_LIMITED = re.compile(r"(?:work|working) (?:from anywhere|remotely|from home) (?:for )?up to (?:\d+|one|two|three|four) (?:days?|weeks?)|"
                            r"occasional(?:ly)? (?:remote|telework)(?: work)?|option (?:for|of|to) (?:occasional |some )?(?:remote|telework)|"
                            r"flexibility to work (?:from anywhere|remotely) for", re.I)
ONSITE_TEXT = re.compile(r"(on-?site (?:role|position|requirement|\d|in)|in[- ]office (?:\d|role|position|requirement|on a hybrid)|"
                         r"\d\s*(?:days?|x) (?:a|per) week (?:in|at) (?:the|our) office|hybrid (?:role|position|schedule|work)|"
                         r"(?:predominantly|primarily|mostly) (?:on-?site|in[- ]person|in[- ]office|in the office)|"
                         r"(?:work|be|working) (?:in|from) (?:the|our|an?) (?:\w+ )?office (?:at least )?(?:\d|one|two|three|four|five) days|"
                         r"(?:\d|one|two|three|four|five) days (?:a|per|each) week (?:in|at|from) (?:the|our|an?|one of our) (?:\w+ )?(?:office|hub|campus)|"
                         r"#(?:li-)?(?:hybrid|onsite)\b|"
                         r"must (?:be able to )?commute|commut(?:e|ing) distance|relocat(?:e|ion) (?:is )?required|"
                         r"report(?:s|ing)? (?:in person )?to (?:our|the) office|this (?:role|position) is (?:based )?(?:in|on-?site))", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|head|director|vp|vice president|chief|expert|manager ii+)\b", re.I)
ENTRY = re.compile(r"(entry[- ]level|0[-–]2 years|0[-–]1 years?|1[-–]2 years|1\+ years?|recent grad|early[- ]career|"
                   r"\bjunior\b|\bassociate\b|tier 1\b|tier i\b|level 1\b|\bl1\b|no experience)", re.I)


SALES_TITLE = re.compile(r"\b(account executive|sales (?:representative|rep|development|executive|manager|director|engineer|"
                         r"specialist|associate|consultant|lead|agent)|business development|\bsdr\b|\bbdr\b|\bae\b|inside sales|"
                         r"outside sales|territory (?:manager|rep)|closer|appointment setter|growth (?:rep|executive)|"
                         r"partnerships? (?:manager|executive)|channel (?:sales|manager)|retail sales|sales\b|client partner|"
                         r"account director|partner manager|account manager, (?:sales|enterprise))", re.I)
INTERN = re.compile(r"\b(intern|internship|co-op|apprentice(?:ship)?|fellowship|volunteer|unpaid|praktikum|werkstudent\w*)\b|^student\b|"
                    r"\bstudent (?:worker|assistant|employee|technician|position|hourly|staff|help)\b|work[- ]study|"
                    r"vacancy pool|faculty pool|\bpool\s*-\s*\d{4}|talent (?:community|network|pool)|general application|"
                    r"expression of interest|future opportunities|open application|join our team\b(?!.*\b(?:as|-)\b)", re.I)

QUOTA = re.compile(r"\b(quota|on-target earnings|OTE|commission|renewal targets?|upsell targets?|expansion targets?|"
                   r"revenue targets?|book of business)\b", re.I)
QUOTA_ANY = re.compile(r"\b(on-target earnings|OTE range|OTE:|OTE of|\bOTE\b(?= ?(?:range|\$|is|of|:))|quota[- ]carrying|carry(?:ing)? a quota|"
                       r"commission[- ]based|base \+ commission|base plus commission)\b", re.I)
# Recruiting platforms with their own ATS boards that repost other employers' jobs (not direct postings)
AGGREGATORS = re.compile(r"^\s*(jobgether|jobot|crossover|toptal|mercor|braintrust|turing|outlier|dataannotation|"
                         r"remotehunter|hirequest|lensa|jooble|talentify|recruiting from scratch|hire with jarvis)\b", re.I)
LEADERSHIP = re.compile(r"\b(?:head of|director|vp|vice president|chief|principal|senior manager|sr\.? manager|group manager|"
                        r"engineering manager|support manager|it manager|security manager|team lead|supervisor)\b|"
                        r"\bmanager\s*(?:,|of\b|-\s)|&[\w ]{0,30}\bmanager\b", re.I)
NO_QUOTA = re.compile(r"\b(non-quota|no quota|not quota|without (?:a )?quota|no sales quota|non-commissioned|not commission)\b", re.I)

PAY_CTX = re.compile(r"salary|pay\b|compensation|wage|range|base|per year|annual|/yr|/year|per hour|hourly|/hr|/hour|"
                     r"an hour|\busd\b|per month|/mo\b|monthly|\$", re.I)
PAY_YEAR = re.compile(r"\$\s?(\d{2,3})(?:,(\d{3})(?:\.\d{2})?|\s?[kK]\b)(?!\s*(?:/|per\s)\s*(?:mo|month))")
PAY_HOUR = re.compile(r"\$\s?(\d{1,3}(?:\.\d{1,2})?)\s*(?:(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:\.\d{1,2})?))?\s*(?:/\s*h(?:ou)?r|per hour|an hour|hourly|/hour)", re.I)
PAY_MONTH = re.compile(r"\$\s?(\d{1,2}(?:[.,]\d{1,3})?)\s*([kK])?\s*(?:(?:-|–|—|to)\s*\$?\s?(\d{1,2}(?:[.,]\d{1,3})?)\s*([kK])?)?\s*(?:/\s*mo(?:nth)?|per month|a month|monthly)", re.I)
HEALTH = re.compile(r"health (?:insurance|benefits|coverage|plan|care)|medical(?:,| and| &) dental|medical (?:insurance|benefits|coverage|plan)|"
                    r"health, dental|dental(?:,| and| &) vision|benefits? (?:package )?(?:include|including)[^.]{0,60}(?:medical|health)|"
                    r"comprehensive benefits|full benefits|benefits eligible|eligible for (?:full )?benefits|"
                    r"\bhsa\b|\bfsa\b|employer[- ]paid (?:medical|health)", re.I)
NO_BENEFITS = re.compile(r"(no benefits|not eligible for (?:company |any )?benefits|benefits are not (?:offered|provided|available)|"
                         r"does not (?:offer|include|provide) (?:any )?benefits|without benefits|benefits: none|"
                         r"no (?:health|medical) (?:insurance|benefits))", re.I)
CONTRACT = re.compile(r"\b(1099|independent contractor|contract(?:or)? (?:role|position)|freelance|per diem|temporary|temp\b|"
                      r"\d+[- ]month contract|contract[- ]to[- ]hire)\b", re.I)

WEEKEND_REQ = re.compile(
    r"(?:must|required to|need to|expected to|will(?: be)?|able to|willing(?:ness)? to|requires? (?:you to )?)\s+(?:be able to |be available to |be willing to )?"
    r"(?:work\s+)?(?:on\s+)?(?:some\s+|rotating\s+|alternating\s+|occasional\s+)?weekends?\b(?!\s+(?:as needed|if needed|occasionally|on occasion))"
    r"|weekends?\s+(?:shifts?|availability|work|coverage|rotation|hours)\s+(?:is |are |will be )?(?:required|mandatory|expected|a must)"
    r"|(?:availability|available)\s+(?:to work\s+)?(?:on\s+)?(?:evenings?(?:,| and| &|/)\s*)?(?:nights?(?:,| and| &|/)\s*)?weekends?\s+(?:is |are )?(?:required|a must|necessary)"
    r"|(?:rotating|alternating|every other)\s+weekends?"
    r"|(?:one|1|two|2)\s+weekend\s+days?"
    r"|(?:tuesday|wednesday|thursday|tues|wed|thurs?)\s*(?:-|–|through|thru|to)\s*(?:saturday|sunday|sat|sun)\b"
    r"|(?:sunday|saturday|sun|sat)\s*(?:-|–|through|thru|to)\s*(?:wednesday|thursday|friday|wed|thu|thurs|fri)\b"
    r"|weekend\s+(?:shift|schedule)\s+(?:required|position)"
    r"|(?:shifts?|schedule)\s+(?:include|includes|including|will include)\s+(?:some\s+)?weekends?"
    r"|weekend\s+on-?call\s+rotations?|on-?call\s+rotations?\s+(?:that\s+)?(?:includes?|including|covering)\s+weekends?", re.I)
WEEKEND_SOFT = re.compile(r"(weekends?\s+(?:as needed|if needed|occasionally|on occasion|may be required|might be required)|"
                          r"occasional(?:ly)?\s+(?:evening(?:s)?\s+(?:and|or|&)\s+)?weekends?|some\s+(?:evenings?\s+(?:and|or|&)\s+)?weekends?|"
                          r"evenings?\s+(?:and|or|&|/)\s+weekends?|nights?\s+(?:and|or|&|/)\s+weekends?|on[- ]call|24/7|24x7|"
                          r"weekends?\s+(?:and|or|&)\s+holidays?|flexible schedule(?: including| that includes)? weekends?|"
                          r"saturday|sunday)", re.I)
WEEKDAYS = re.compile(r"monday\s*(?:-|–|through|thru|to)\s*friday|\bm-f\b|mon\s*(?:-|–)\s*fri|weekdays only|business hours|"
                      r"no weekends", re.I)

# ---------------------------------------------------------------- places
STATES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
          "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
          "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
          "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
          "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
          "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
          "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
          "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
          "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC"}
ABBR = set(STATES.values())
STATE_NAME_RX = re.compile(r"\b(" + "|".join(sorted((re.escape(s) for s in STATES), key=len, reverse=True)) + r")\b", re.I)
STATE_ABBR_RX = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(ABBR)) + r")(?![A-Za-z])")
DC = re.compile(r"washington,?\s*d\.?\s*c\.?|washington dc|district of columbia", re.I)
US_MARK = re.compile(r"(united states|\bu\.?s\.?a?\b|\bus\b|\bus-|\bamericas\b|north america|nationwide|anywhere in the us|"
                     r"usa\b|\(us\)|- us\b|us remote|remote us|\bus only)", re.I)
NON_US = re.compile(r"\b(canada|canadian|toronto|vancouver, bc|montreal|ontario|quebec|alberta|british columbia|"
                    r"united kingdom|\buk\b|england|london|scotland|ireland|dublin|europe|european|\beu\b|emea|apac|asia|"
                    r"latam|latin america|south america|mexico|brazil|argentina|colombia|chile|peru|costa rica|"
                    r"germany|berlin|munich|münchen|hamburg|frankfurt|cologne|köln|stuttgart|düsseldorf|leipzig|vienna|wien|zurich|zürich|"
                    r"geneva|m/w/d|w/m/d|france|paris|spain|madrid|barcelona|portugal|lisbon|italy|netherlands|amsterdam|"
                    r"belgium|switzerland|austria|poland|warsaw|krakow|romania|bulgaria|serbia|croatia|greece|turkey|"
                    r"sweden|stockholm|norway|denmark|finland|estonia|lithuania|latvia|ukraine|czech|prague|hungary|"
                    r"india|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|noida|gurgaon|pakistan|bangladesh|"
                    r"philippines|manila|vietnam|thailand|malaysia|indonesia|singapore|japan|tokyo|korea|seoul|china|"
                    r"shanghai|beijing|hong kong|taiwan|australia|sydney|melbourne|brisbane|perth|new south wales|nsw|queensland|"
                    r"tasmania|new zealand|auckland|israel|tel aviv|"
                    r"egypt|nigeria|kenya|south africa|uae|dubai|saudi|emirates|middle east|mena|dach|nordics?|benelux|anz|"
                    r"japan(?:ese)?|apj|uk&i|cee|turkey|türkiye|serbia|morocco|tunisia|ghana|rwanda|uganda|"
                    r"guatemala|honduras|el salvador|dominican|jamaica|bogota|bogotá|medellin|medellín|santo domingo|buenos aires|sao paulo|"
                    r"são paulo|lima|santiago|montevideo|quito|panama|cdmx|guadalajara|monterrey|manila|cebu|cairo|lagos|"
                    r"nairobi|karachi|lahore|kyiv|lviv|bucharest|sofia|belgrade|zagreb|tallinn|vilnius|riga|"
                    r"russia|russian federation|moscow|saint petersburg|st\.? petersburg|belarus|minsk|kazakhstan|almaty|astana|"
                    r"uzbekistan|tashkent|kyrgyzstan|azerbaijan|baku|armenia|yerevan|tbilisi|cyprus|limassol|nicosia|malta|"
                    r"luxembourg|iceland|slovenia|ljubljana|slovakia|bratislava|bosnia|sarajevo|montenegro|north macedonia|skopje|"
                    r"albania|tirana|moldova|chisinau|copenhagen|aarhus|odense|porto|coimbra|nepal|kathmandu|sri lanka|colombo|"
                    r"myanmar|yangon|cambodia|phnom penh|laos|mongolia|jakarta|surabaya|bandung|kuala lumpur|taipei|kaohsiung|"
                    r"ho chi minh|hanoi|bangkok|karnataka|telangana|maharashtra|tamil nadu|kerala|"
                    r"ethiopia|addis ababa|tanzania|zambia|zimbabwe|senegal|cameroon|algeria|amman|beirut|qatar|"
                    r"doha|kuwait|bahrain|oman|riyadh|jeddah|martinique|guadeloupe|barbados|bahamas|nicaragua|"
                    r"paraguay|bolivia|ecuador|venezuela|uruguay|johannesburg|cape town|durban|pretoria|kigali)\b", re.I)
# ISO 3166 alpha-3 codes in Workday-style location tags ("PHL Remote", "BRA Remote"); case-sensitive
ISO3_REMOTE = re.compile(r"(?<![A-Za-z])(PHL|IDN|JAM|BRA|VNM|IND|ZAF|MEX|COL|ARG|CHL|PER|CRI|GTM|HND|SLV|DOM|KEN|NGA|EGY|MAR|TUR|"
                         r"POL|ROU|BGR|SRB|HRV|GRC|PRT|ESP|FRA|DEU|ITA|NLD|BEL|CHE|AUT|SWE|NOR|DNK|FIN|EST|LTU|LVA|UKR|CZE|HUN|"
                         r"IRL|GBR|CAN|AUS|NZL|JPN|KOR|CHN|TWN|HKG|SGP|MYS|THA|PAK|BGD|LKA|NPL|ARE|SAU|ISR)(?=[\s-]+Remote\b)")
US_COUNTRY = re.compile(r"^\s*(?:us|usa|u\.s\.a?\.?|united states(?: of america)?)\s*$", re.I)
STATE_LIMIT_CTX = re.compile(r"(following states|these states|eligible states|states? (?:listed|below)|approved states|"
                             r"(?:hire|hiring|employ|employment|recruit\w*) (?:in|from|within) (?:the following|these|select|certain|only)|"
                             r"(?:reside|residing|located|live|living|based) in (?:one of )?(?:the following|these|select|approved|certain|"
                             r"any of the following)|currently (?:hire|hiring|employ) in|"
                             r"(?:registered|able) to (?:hire|employ|do business) in|states where we (?:currently )?(?:hire|operate|employ)|"
                             r"must (?:live|reside|be located|be based) in|candidates (?:in|from|located in)|"
                             r"open to (?:candidates|applicants|residents) (?:in|of)|only (?:open|available) (?:to|in))", re.I)
STATE_EXCLUDE_CTX = re.compile(r"(unable to (?:hire|employ|consider)|not (?:able|eligible|currently able) to (?:hire|employ)|"
                               r"cannot (?:hire|employ|consider)|can't (?:hire|employ)|do not (?:hire|employ)|not hiring (?:in|from)|"
                               r"excluding|except(?: for)?|with the exception of|not (?:open|available) (?:to|in)|"
                               r"excludes?\b[^.]{0,40}?\b(?:residing|living|located|based|residents)\b|"
                               r"(?:residents|candidates) of [^.]{0,80} (?:are not|will not be) (?:eligible|considered))", re.I)


def lines_of(desc):
    parts = re.split(r"(?:\n+|(?<=[.;!?])\s+(?=[A-Z0-9])|\s+-\s+(?=[A-Z0-9]))", desc or "")
    return [p.strip() for p in parts if p and p.strip()]


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def pay_of(desc, extra=""):
    """Top of the stated pay -> ("year"|"hour", value, line) or (None, None, "").

    Takes the TOP of the figures on pay-context lines, so a role is never
    skipped on the low end of its range. Monthly pay is annualised.
    """
    years, hours, best_line = [], [], ""
    text = (extra or "") + "\n" + (desc or "")
    for ln in lines_of(text):
        if not PAY_CTX.search(ln) or "$" not in ln:
            continue
        if re.search(r"(401\s*\(?k\)?|stipend|bonus of|sign[- ]on|referral|reimburse|allowance|per diem|tuition|"
                     r"revenue|funding|raised|valuation|arr\b|budget|\bmillion|\bbillion|customers? (?:spend|save))", ln, re.I) \
                and not re.search(r"salary|base pay|pay range|hourly|compensation range|wage", ln, re.I):
            continue
        for m in PAY_MONTH.finditer(ln):
            for g, k in ((m.group(1), m.group(2)), (m.group(3), m.group(4))):
                v = _num(g)
                if v:
                    v = v * 1000 if k or v < 100 else v
                    if 800 <= v <= 40000:
                        years.append(v * 12)
                        best_line = best_line or ln[:200]
        ln2 = PAY_MONTH.sub(" ", ln)
        for m in PAY_HOUR.finditer(ln2):
            for g in (m.group(1), m.group(2)):
                v = _num(g)
                if v and 7 <= v <= 300:
                    hours.append(v)
                    best_line = best_line or ln[:200]
        ln3 = PAY_HOUR.sub(" ", ln2)
        for m in PAY_YEAR.finditer(ln3):
            v = int(m.group(1)) * 1000 if not m.group(2) else int(m.group(1) + m.group(2))
            if 15000 <= v <= 600000:
                years.append(v)
                best_line = best_line or ln[:200]
    if years:
        return "year", max(years), best_line
    if hours:
        return "hour", max(hours), best_line
    return None, None, ""


def years_asked(desc):
    req, pref = [], []
    for ln in lines_of(desc):
        if NOT_EXPERIENCE.search(ln) or not EXPERIENCE_CTX.search(ln):
            continue
        for m in YEARS_RE.finditer(ln):
            n = m.group(1).lower()
            n = WORDNUM.get(n) if not n.isdigit() else int(n)
            if not n or n > 20:
                continue
            (pref if PREFERRED.search(ln) else req).append((n, ln[:160]))
    return req, pref


def degree_of(desc):
    status, line = "none", ""
    for ln in lines_of(desc):
        if not DEGREE.search(ln):
            continue
        if EQUIV.search(ln):
            return "equivalent_ok", ln[:160]
        if PREFERRED.search(ln):
            if status == "none":
                status, line = "preferred", ln[:160]
            continue
        if re.search(r"high school|ged\b", ln, re.I) and not re.search(r"bachelor|degree in", ln, re.I):
            continue
        status, line = "required", ln[:160]
    return status, line


REQ_HEAD = re.compile(r"^\W*(?:requirements?|(?:minimum |basic |required )?qualifications|required (?:skills|experience)|"
                      r"what you(?:'ll)? (?:need|bring)|what we(?:'re| are) looking for|must[- ]haves?|who you are|about you|"
                      r"you (?:have|bring|are)\b|skills (?:and|&) experience|experience (?:and|&) skills)", re.I)
PREF_HEAD = re.compile(r"^\W*(?:preferred|nice[- ]to[- ]haves?|bonus|desired|pluses|extra credit|"
                       r"it(?:'s| is| would be) (?:a plus|great|nice) if)", re.I)
OTHER_HEAD = re.compile(r"^\W*(?:benefits|perks|what we offer|compensation|pay|salary|about (?:us|the (?:company|team|role))|"
                        r"responsibilities|what you(?:'ll)? do|the role|location)\b", re.I)
REQ_WORD = re.compile(r"\b(requir\w*|must|need to|minimum|mandatory|essential)\b", re.I)


def required_gaps(desc, prof):
    """The profile's required_gaps that the posting REQUIRES -> [(reason, effect, line)].

    A line counts when it sits under a requirements heading or says required/must itself;
    preferred lines and nice-to-have sections never count."""
    if not prof.required_gaps or not desc:
        return []
    out, done, section = [], set(), None
    for ln in lines_of(desc):
        if PREF_HEAD.search(ln):
            section = "pref"
        elif REQ_HEAD.search(ln):
            section = "req"
        elif len(ln) < 60 and OTHER_HEAD.search(ln):
            section = None
        if PREFERRED.search(ln) or (section != "req" and not REQ_WORD.search(ln)):
            continue
        for rx, why, effect in prof.required_gaps:
            if why not in done and rx.search(ln):
                done.add(why)
                out.append((why, effect, ln[:160]))
    return out


def field_of(title, prof):
    """First profile field whose title pattern matches (and whose exclude pattern doesn't)."""
    t = (title or "").lower()
    for f in prof.fields:
        if f["titles"] is None or not f["titles"].search(t):
            continue
        if f["exclude"] is not None and f["exclude"].search(t) and not (f["exclude_unless"] is not None and f["exclude_unless"].search(t)):
            continue
        return f["name"]
    return None


def states_in(s):
    found = set()
    for m in STATE_NAME_RX.finditer(s):
        name = m.group(1).lower()
        if name == "washington" and DC.search(s[max(0, m.start() - 2):m.end() + 6]):
            found.add("DC")
            continue
        found.add(STATES[name])
    for m in STATE_ABBR_RX.finditer(s):
        ab = m.group(1)
        # all-caps abbreviation only, and for the ones that are also English words, only in a list or address context
        if ab in ("IN", "OR", "ME", "OK", "HI", "DE", "CO", "AL", "LA", "PA", "MA", "ID", "MO", "OH") and not re.search(r"[,(/;]\s*$|\band\s*$|^\s*$", s[max(0, m.start() - 3):m.start()]) \
                and not re.search(r"^\s*[,)/;]|^\s*and\b|^\s*$", s[m.end():m.end() + 5]):
            continue
        found.add(ab)
    return found


def state_restriction(desc, home):
    """-> (kind, states, line) kind: 'excludes_home' | 'includes_home' | 'home_excluded_explicit' | None."""
    text = desc or ""
    if not home:
        return None, [], ""
    for ln in lines_of(text):
        if len(ln) > 1200:
            ln = ln[:1200]
        ex = STATE_EXCLUDE_CTX.search(ln)
        lim = STATE_LIMIT_CTX.search(ln)
        if not ex and not lim:
            continue
        # a list often continues on the next lines ("...the following states:\n- AZ\n- CO")
        start = text.find(ln)
        window = text[start:start + 700] if start >= 0 else ln
        sts = states_in(window if lim and len(states_in(ln)) < 2 else ln)
        sts.discard("DC") if "DC" in sts and len(sts) > 1 else None
        if not sts:
            continue
        # "...the following states? DE, ND, VT, WV - Note that we are not able to employ in these states." (Vida)
        after = text[start + len(ln):start + len(ln) + 200] if lim and start >= 0 else ""
        later_ex = lim and STATE_EXCLUDE_CTX.search(after) and re.search(r"\b(?:these|those|the above|listed) states\b", after, re.I)
        if (ex and ex.start() < (lim.start() if lim else 10 ** 6)) or later_ex:
            if home in sts:
                return "home_excluded_explicit", sorted(sts), ln[:200]
            continue
        if lim:
            if home in sts:
                return "includes_home", sorted(sts), ln[:200]
            if len(sts) >= 1 and not re.search(r"\b(including|such as|e\.g\.|for example|headquarter|office|hq)\b", ln, re.I):
                return "excludes_home", sorted(sts), ln[:200]
    return None, [], ""


def norm_workplace(wp):
    """An ATS workplace tag -> remote | hybrid | onsite | ''. Workday's remoteType says "No Telework" for
    on-site jobs and "Occasional Telework" for mostly on-site ones (UW, 2026-11-08), so a bare "telework"
    is not enough."""
    w = (wp or "").lower()
    if re.search(r"\b(?:no|not|non)[- ]?(?:telework|remote|telecommut\w*)", w):
        return "onsite"
    if re.search(r"occasional(?:ly)? (?:telework|remote)|\d\s*days? (?:remote|telework)|(?:remote|telework) \d\s*days?", w):
        return "hybrid"
    if re.search(r"remote or hybrid|hybrid or remote", w):
        return "remote"
    if "hybrid" in w:
        return "hybrid"
    if "remote" in w or "telework" in w or "telecommut" in w or "anywhere" in w:
        return "remote"
    if re.search(r"on.?site|in.?office|office|in.?person", w):
        return "onsite"
    return ""


_STATE_TAG = {}


def _state_tag(prof):
    """'Remote - North Carolina' / 'Remote, NC' style tags naming the home state."""
    if prof.home_state not in _STATE_TAG:
        name = re.escape(prof.home_state_name).replace(r"\ ", r"\s+")
        if prof.home_state_name == "washington":
            name += r"(?! ?,? ?d\.?c)"
        _STATE_TAG[prof.home_state] = re.compile(r"(?<![a-z])(%s|\b%s\b)" % (name, prof.home_state), re.I) if prof.home_state_name else None
    return _STATE_TAG[prof.home_state]


REMOTE_CLASSES = ("remote_us", "remote_ambiguous", "home_state_remote")


def _raw(job):
    raw = job.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except ValueError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def countries_of(job):
    """The countries the ATS itself gives for the job ([] if none): Lever `country` (ISO 2), Recruitee's
    country and locations, Workday's detail country, Ashby's postal address, Oracle's PrimaryLocationCountry."""
    raw = _raw(job)
    out = []
    for c in (raw.get("country"), raw.get("country_code"), raw.get("PrimaryLocationCountry")):
        if isinstance(c, str) and c.strip():
            out.append(c.strip())
    for l in raw.get("locations") if isinstance(raw.get("locations"), list) else []:
        if isinstance(l, dict):
            out += [str(l[k]).strip() for k in ("country", "country_code") if l.get(k)]
    det = raw.get("_detail")
    if isinstance(det, dict) and isinstance(det.get("country"), dict) and det["country"].get("descriptor"):
        out.append(str(det["country"]["descriptor"]).strip())
    addr = raw.get("address")
    if isinstance(addr, dict) and isinstance(addr.get("postalAddress"), dict) and addr["postalAddress"].get("addressCountry"):
        out.append(str(addr["postalAddress"]["addressCountry"]).strip())
    return out


def foreign_country(job, loc, near=False):
    """The ATS's country when it is not the US and the location tag names nothing in the US -> str or ''.
    Any US country among them ("US | EU") means it isn't foreign; so does a Workday "6 Locations" tag,
    whose detail country is only the first of them."""
    ccs = countries_of(job)
    if not ccs or near or US_MARK.search(loc) or states_in(loc) or re.match(r"^\s*\d+ locations\s*$", loc, re.I):
        return ""
    if any(US_COUNTRY.match(c) or re.search(r"\b(?:us|usa|united states)\b", c, re.I) for c in ccs):
        return ""
    return ccs[0]


def location_class(job, desc, prof=None):
    """-> (cls, detail) cls in: local, nearby, home_state_remote, remote_us, remote_ambiguous,
    remote_non_us, onsite_other, unknown."""
    prof = prof or profile_mod.load()
    loc = str(job.get("location") or "")
    title = job.get("title") or ""
    wp = norm_workplace(job.get("workplace"))
    negated = bool(REMOTE_NEG.search(desc or ""))
    desc = REMOTE_LIMITED.sub(" ", REMOTE_NEG.sub(" ", desc or ""))
    head = (desc or "")[:4000]
    tag_remote = wp == "remote" or bool(re.search(r"\bremote\b|anywhere|work from home|virtual|telework|distributed", loc + " " + title, re.I))
    if wp in ("onsite", "hybrid") and not re.search(r"\bremote\b|anywhere|work from home", loc + " " + title, re.I):
        tag_remote = False      # "No Telework" / "Occasional Telework": the tag's own word isn't a remote tag
    city_tagged = bool(loc.strip()) and not tag_remote
    # a posting tagged with a city/office is remote only when its text says so plainly,
    # and on-site/hybrid wording anywhere in it wins (Lyft puts "3 days per week in the office" at the end)
    text_remote = bool((REMOTE_STRONG if city_tagged else REMOTE_TEXT).search(desc or ""))
    text_onsite = ONSITE_TEXT.search(desc if city_tagged or not loc.strip() else head) or negated
    hybrid = wp == "hybrid" or bool(re.search(r"\bhybrid\b", loc + " " + title, re.I))
    onsite_tag = wp == "onsite" or (city_tagged and bool(re.search(r"\boffice\b|\bhq\b|headquarters|on-?site|in[- ]person", loc, re.I)))
    local = prof.local.search(loc) if prof.local is not None else None
    nearby = prof.nearby.search(loc) if prof.nearby is not None else None
    nt = NON_US.search(title)
    if nt and not US_MARK.search(title) and not re.search(r"\b(us|usa|united states|americas|north america)\b", loc, re.I):
        return "remote_non_us" if tag_remote or not loc.strip() else "onsite_other", "title names %s" % nt.group(0)
    fc = foreign_country(job, loc, near=bool(local or nearby))
    if fc:
        return ("remote_non_us" if tag_remote or not loc.strip() else "onsite_other"), "outside the US (the board says %s)" % fc
    tag = _state_tag(prof)
    home_only = (prof.home_only.search(desc or "") if prof.home_only is not None else None) or (
        tag is not None and re.search(r"\bremote\b", loc, re.I) and tag.search(loc)
        and not re.search(r"\b(us|usa|united states)\b", loc, re.I) and len(states_in(loc)) <= 1)
    if local and not tag_remote:
        return "local", "local: " + loc[:80]
    remote = tag_remote or (text_remote and not onsite_tag and not hybrid and not text_onsite)
    if hybrid and not tag_remote:
        remote = False
    if remote:
        if home_only:
            return "home_state_remote", (home_only.group(0) if hasattr(home_only, "group") else loc)[:120]
        non_us = NON_US.search(loc) or ISO3_REMOTE.search(loc)
        us = US_MARK.search(loc) or (states_in(loc) and not re.search(r"europe|emea|apac|latam|asia|canada", loc, re.I))
        if city_tagged and not us and not non_us and not local and not nearby:
            return "remote_ambiguous", "remote per text, but tagged only '%s' (no US marker)" % loc[:60]
        if non_us and not us:
            return "remote_non_us", "remote outside the US (%s)" % non_us.group(0)
        if not loc.strip() or re.fullmatch(r"\s*(remote|anywhere|virtual|work from home)\s*", loc, re.I):
            # tag says only "Remote": read the text for a country
            nd = NON_US.search(head[:1500])
            if nd and not US_MARK.search(head[:1500]):
                return "remote_ambiguous", "remote; text mentions %s, no US" % nd.group(0)
            return "remote_us", "remote (country not tagged)"
        return "remote_us", "remote: " + loc[:80]
    # not remote
    if local:
        return "local", "local: " + loc[:80]
    if nearby:
        return "nearby", nearby.group(0)
    if not loc.strip() and not wp:
        if text_remote and not text_onsite:
            return "remote_us", "remote (from text)"
        return "unknown", "no location"
    return "onsite_other", ("hybrid " if hybrid else "on-site ") + loc[:80]


US_WIDE = re.compile(r"\b(us|usa|u\.s\.|united states|anywhere|nationwide|americas|north america)\b", re.I)


# Workday's compact remote tags: country, state code, "Remote" ("USA PA Remote", "US - Remote, CO", "US, NC, Remote")
WD_STATE_TAG = re.compile(r"^\s*(?:US|USA)\s*[-,]?\s*(?:Remote\s*[-,]?\s*)?([A-Z]{2})\s*(?:[-,]?\s*Remote)?\s*$")
TZ_PAREN = re.compile(r"\([^()]*\b(?:ET|PT|EST|CST|MST|PST|EDT|CDT|PDT|time ?zones?)\b[^()]*\)", re.I)


def remote_tied_states(loc):
    """States a location tag ties its remote parts to -> set. "Durham, North Carolina; Herndon, Virginia; Remote -
    Virginia" -> {"VA"} (remote only for Virginians; the others are offices); "USA PA Remote / USA WI Remote /
    USA TX Remote" -> {"PA", "WI", "TX"}. Empty when any remote part is US-wide ("Remote", "Remote - US", and
    Ashby's "Remote (USA) (California)", whose state is just the office's) or names several states or a
    preference. Remote parts abroad ("Remote - Ontario") are left out."""
    segs = [x.strip() for x in re.split(r"\s*[;|/\n]\s*", loc or "") if x.strip()]
    rsegs = [TZ_PAREN.sub(" ", x) for x in segs if re.search(r"\bremote\b|telework|work from home", x, re.I)]
    tied = set()
    for x in rsegs:
        if re.search(r"prefer", x, re.I):
            return set()
        wd = WD_STATE_TAG.match(x)
        if wd:
            sts = {wd.group(1)} if wd.group(1) in ABBR else set()
        elif US_WIDE.search(x):
            return set()
        else:
            sts = states_in(x)
        if not sts:
            if NON_US.search(x) or ISO3_REMOTE.search(x):
                continue
            return set()
        if len(sts) > 1:
            return set()
        tied |= sts
    return tied


def questions_of(job):
    q = job.get("questions")
    if isinstance(q, str):
        try:
            q = json.loads(q) if q else []
        except ValueError:
            q = []
    return [x for x in (q or []) if isinstance(x, dict) and (x.get("label") or "").strip()]


# an application-form question that ties the job to a place: "be located near Alpharetta, GA, Dallas, TX, or Temple, TX",
# "within commuting distance of", "work on-site 3 days per week", "relocate to New York"
FORM_PLACE = re.compile(
    r"(?:located|based|live|living|reside|residing)\s+(?:near|close to|nearby|within (?:a )?(?:reasonable )?(?:commut\w*|driving) (?:distance|proximity))"
    r"|(?:based|located|live) within (?:the )?proximity|commut(?:able|ing) (?:distance|proximity)|within \d+ miles"
    r"|relocat\w* to\b|\b(?:on-?site|in[- ]office|in[- ]person)\b(?! interview)|\b(?:\d|one|two|three|four|five) days? (?:a|per|each) week (?:in|at|from|on-?site)"
    r"|\bhybrid (?:schedule|position|role|model|work|requirement)|work (?:daily|\d days)", re.I)
FORM_SOFT = re.compile(r"\b(optional|not required|if applicable|occasional(?:ly)?|periodic(?:ally)?|quarterly|annual(?:ly)?|travel\w*|"
                       r"visits?|offsites?|team (?:meetings|events|gatherings)|as needed|once a (?:month|quarter|year)|interviews?|"
                       r"training|experience|have you|describe|tell us|previously|if so|country)\b", re.I)
# an answer that lets a remote applicant say "none of these" (the question is informational)
FORM_INFO_OPTION = re.compile(r"^\s*(?:none(?: of (?:the|these) (?:above|locations|options))?|other|n/?a|not applicable)\s*\.?\s*$", re.I)
FORM_LIST_REF = re.compile(r"\b(?:the following|listed|below|one of these)\b", re.I)
QUESTION_CUT = 280      # adapters keep a question's description to 300 characters and its options to 12


# a question that asks the applicant yes or no: a sentence that starts "Do you / Are you / Have you ..."
# ("This role requires weekend work. Are you available?"), not "How / Which / What ...?"
ASKS = re.compile(r"(?:^|[.?!:]\s+|\)\s+)\W*(?:do|does|are|have|has|can|could|will|would|is|did|were)\b", re.I)
YES_NO = re.compile(r"^\s*(?:yes|no)\b", re.I)
AUTH = re.compile(r"\b(?:authori[sz](?:ed|ation)|right to work|eligib\w* to (?:work|be employed)|legally (?:able|permitted|entitled|allowed) "
                  r"to work|work permit|permission to work|citizen(?:ship)?|permanent resident|residency (?:status|permit))\b", re.I)
FORM_US = re.compile(r"(?i:united states|\bu\.\s?s\.(?:\s?a\.?)?|\bamerica\b|\bstateside\b)|\bUSA?\b")
AUTH_SOFT = re.compile(r"\bif (?:you are|you're|you live|located|based|applying)\b|\b(?:which|what) countr|\bother than\b|"
                       r"\b(?:cuba|iran|syria|north korea|dprk|sanction\w*|embargo\w*|export control)", re.I)   # export-control screens
SOFT_ASK = re.compile(r"\b(?:ever|previously|in the past|if (?:so|yes|applicable)|optional|not required|preferred|a plus|bonus|"
                      r"nice to have|or equivalent|equivalent (?:experience|combination)|or (?:relevant )?experience|in lieu|"
                      r"any (?:type|kind|form|level) of)\b", re.I)
FORM_DEGREE = re.compile(r"\b(?:bachelor'?s?|undergraduate degree|college degree|university degree|four[- ]year degree|"
                         r"4[- ]year degree|master'?s degree|associate'?s? degree|an? (?:college |university |\w+ )?degree\b|degree in\b)", re.I)
NOT_DEGREE = re.compile(r"\b(?:first|second|third)[- ]degree\b|\brelatives?\b|enrolled|currently (?:pursuing|attending|a student)|"
                        r"\bstudent\b|graduat\w+ (?:in|by|between)|high school|\bged\b|diploma", re.I)
FORM_LICENCE = re.compile(r"driver[’']?s?[’']? licen[cs]e|driving licen[cs]e|reliable (?:personal )?(?:transportation|vehicle)|"
                          r"own (?:a |reliable )?(?:car|vehicle)|insurable driving|\bcdl\b|able to drive", re.I)
FORM_WEEKEND = re.compile(r"\b(?:available|availability|able|willing) to work (?:on )?(?:(?:some |the )?weekends?|saturdays?|sundays?|"
                          r"evenings?(?:,)? (?:and|&|or) weekends?|nights?(?:,)? (?:and|&|or) weekends?)|\bweekend (?:shifts?|availability|"
                          r"work|coverage|rotation)\b", re.I)
FORM_WEEKEND_SOFT = re.compile(r"\b(?:occasional(?:ly)?|as needed|if needed|on occasion|rarely|sometimes|from time to time)\b", re.I)


def _cut(q, opts, qdesc):
    """A question's list may have been cut off by its adapter (Lever/Greenhouse keep 12 options and 300 characters)."""
    if "truncated" in q:
        return bool(q["truncated"])
    return len(opts) >= 12 or len(qdesc) >= QUESTION_CUT


def form_gates(job, prof, lc):
    """The application form's own questions -> [(effect, why)]: effect skip (a required question), flag, or
    "rule:<name>" (the profile's rule decides: clearance, degree_required, drivers_licence, weekend_required).

    For jobs that read as remote, the location questions: a form asking a remote applicant to live near an office
    elsewhere, to work on-site, or to live in a list of states without the home state. Not a gate: a question naming
    the home state or a local/nearby place (in its label, options or description), soft ones (optional, occasional
    or periodic visits, training, the country), and one with a "None"/"Other" answer. A list that may be cut off
    (12 options, a 300-character description) without the home state in view is only a flag.

    For every job, required yes/no questions that rule someone out: work authorization only for another country
    ("Are you legally authorized to work in Canada?", with no US answer and no US question), a security clearance,
    a degree (unless the question or the posting takes equivalent experience), a driver's licence or car, weekend
    availability. Questions about the past ("have you ever held a clearance") or marked optional don't count."""
    tag = _state_tag(prof)

    def names_home(s):
        return bool((prof.local is not None and prof.local.search(s)) or (prof.nearby is not None and prof.nearby.search(s))
                    or (tag is not None and tag.search(s)))

    out = []
    qs = questions_of(job)
    us_auth = any(AUTH.search(q["label"] + " " + str(q.get("description") or "")) and
                  FORM_US.search(" ".join([q["label"], str(q.get("description") or "")] + [str(o) for o in q.get("options") or []]))
                  for q in qs)
    deg_desc = None
    for q in qs:
        label = " ".join(q["label"].split())
        opts = [str(o) for o in (q.get("options") or [])]
        qdesc = " ".join(str(q.get("description") or "").split())
        req = bool(q.get("required", True))
        short = label if len(label) <= 140 else label[:137] + "..."
        yes_no = q.get("type") in ("boolean", "knockout") or bool(opts and all(YES_NO.match(o) for o in opts)) or \
            (not opts and q.get("type") in (None, "", "input_text"))
        asks = req and yes_no and ASKS.search(label) and not SOFT_ASK.search(label)
        if asks:
            whole = label + " " + qdesc
            place = NON_US.search(label)
            if AUTH.search(whole) and place and not FORM_US.search(" | ".join([whole] + opts)) and not AUTH_SOFT.search(label):
                out.append(("flag" if us_auth else "skip", "the application form asks for work authorization in %s ('%s')"
                            % (place.group(0).title() if place.group(0).islower() else place.group(0), short)))
                continue
            if CLEARANCE.search(label):
                out.append(("rule:clearance", "the application form asks for a security clearance ('%s')" % short))
                continue
            if FORM_DEGREE.search(label) and not EQUIV.search(whole) and not NOT_DEGREE.search(label):
                if deg_desc is None:
                    deg_desc = degree_of(job.get("description") or "")[0]
                if deg_desc in ("equivalent_ok", "preferred"):
                    out.append(("flag", "the application form asks about a degree; the posting takes %s ('%s')" % (
                        "equivalent experience" if deg_desc == "equivalent_ok" else "it as preferred", short)))
                else:
                    out.append(("rule:degree_required", "the application form asks for a degree ('%s')" % short))
                continue
            if FORM_LICENCE.search(label):
                out.append(("rule:drivers_licence", "the application form asks for a driver's licence or car ('%s')" % short))
                continue
            if FORM_WEEKEND.search(label) and not FORM_WEEKEND_SOFT.search(label):
                out.append(("rule:weekend_required", "the application form asks for weekend availability ('%s')" % short))
                continue
        if lc not in REMOTE_CLASSES + ("unknown",):
            continue
        kind, sts, _ = state_restriction(label + "\n" + qdesc, prof.home_state)
        if kind == "includes_home":
            continue
        if kind in ("excludes_home", "home_excluded_explicit"):
            cut = ("truncated" not in q or q["truncated"]) and len(qdesc) >= QUESTION_CUT and not states_in(label)
            # "live in the Pacific Time Zone or Hawaii-Aleutian Time Zone" is time zones, not a state list (OpenTable)
            zones = kind == "excludes_home" and re.search(r"time ?zones?", label + " " + qdesc, re.I)
            # "we prefer candidates located in either Nashville, TN or Memphis, TN" (Matter Health) is a preference
            pref = kind == "excludes_home" and PREFERRED.search(label + " " + qdesc)
            out.append(("skip" if req and not cut and not zones and not pref else "flag", "the application form limits it to %s ('%s')" % (
                ", ".join(sts[:8]) + ("..." if len(sts) > 8 else ""), short)))
            continue
        if not FORM_PLACE.search(label) or FORM_SOFT.search(label):
            continue
        if names_home(" | ".join([label, qdesc] + opts)):
            continue
        informational = any(FORM_INFO_OPTION.match(o) for o in opts)
        cut = FORM_LIST_REF.search(label) and _cut(q, opts, qdesc)
        out.append(("skip" if req and not informational and not cut else "flag", "the application form asks: '%s'" % short))
    return out


def form_gaps(job, prof):
    """The profile's required_gaps asked as a required yes/no question on the form ("Are you a CPA?",
    "Are you fluent in Spanish?") -> [(reason, effect, label)], like required_gaps() for the description."""
    if not prof.required_gaps:
        return []
    out, done = [], set()
    for q in questions_of(job):
        label = " ".join(q["label"].split())
        if not q.get("required", True) or not ASKS.search(label) or SOFT_ASK.search(label) or PREFERRED.search(label):
            continue
        for rx, why, effect in prof.required_gaps:
            if why not in done and rx.search(label):
                done.add(why)
                out.append((why + " (application form)", effect, label[:160]))
    return out


# form questions that say nothing about the job: where you heard of it (its options name LinkedIn, Discord,
# YouTube...), the voluntary EEO survey, consent boxes, and the built-in contact fields
FORM_NOISE = re.compile(r"how did you (?:hear|find|learn)|what brought you|where did you (?:hear|find|see|learn)|\bsource\b|refer(?:red|ral)|"
                        r"gender|\brace\b|ethnic|veteran|disabilit|transgender|sexual orientation|pronoun|hispanic|latin[ox]|"
                        r"\bconsent|privacy|\bagree\b|acknowledge|^(?:first |last |full |legal |preferred )?name$|^e-?mail|^phone|"
                        r"linkedin|github|twitter|portfolio|website|^resume|^cv$|cover letter|^address|^city|^location$", re.I)
FORM_NOISE_SECTION = re.compile(r"demographic|voluntary|self[- ]identif|eeo|equal (?:employment|opportunity)|diversity", re.I)


def form_text(job):
    """The employer's own questions on the application form as text (labels, descriptions, options), for the
    keyword match: "Do you have experience with Docker and Linux?" says as much about the job as its posting."""
    out = []
    for q in questions_of(job):
        label = " ".join(q["label"].split())
        if FORM_NOISE.search(label) or FORM_NOISE_SECTION.search(str(q.get("section") or "")):
            continue
        out.append(" ".join([label, str(q.get("description") or "")] + [str(o) for o in q.get("options") or []]))
    return "\n".join(out)


# a form question that asks for the applicant's own number, with a sample figure ("desired salary? (i.e. $50000)")
PAY_ASKED = re.compile(r"\b(?:desired|expect\w*|your (?:current|minimum|target|ideal|desired)|current (?:salary|pay|compensation|rate)|"
                       r"i\.e\.|e\.g\.|for example|format)", re.I)
PAY_STATED = re.compile(r"\b(?:set rate|pays|this (?:role|position|job) (?:is|offers|has)|range for this|does this (?:meet|work)|"
                        r"are you (?:ok|okay|comfortable|fine) with)\b", re.I)


def form_pay_text(job):
    """Pay stated in the form's required questions ("a set rate of $18 an hour. Does this meet ...?"), not the
    applicant's expectations with a sample figure in them ("desired base salary? (i.e. $50000)")."""
    return "\n".join("%s %s" % (q["label"], " / ".join(q.get("options") or [])) for q in questions_of(job)
                     if q.get("required", True) and "$" in q["label"] + " ".join(q.get("options") or [])
                     and (PAY_STATED.search(q["label"]) or not PAY_ASKED.search(q["label"])))


def _date(s):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def job_age(job, today=None):
    """Days since the job went up -> (days or None, how). The board's posted date, except that a job which
    appeared on a board we were already polling (not its baseline poll) counts from when it appeared, if that's
    later: boards re-list old postings (Lever keeps createdAt). Workday/Oracle are searched, not read in full,
    so their first-seen date isn't proof of anything."""
    today = today or datetime.date.today()
    posted = _date(job.get("posted_at"))
    if posted is None:
        return None, ""
    age = (today - posted).days
    seen = _date(job.get("first_seen"))
    if seen and job.get("baseline") == 0 and job.get("ats") not in ("workday", "oracle") and (today - seen).days < age - 14:
        return max(0, (today - seen).days), "relisted"
    return age, "posted"


def assess(job, prof=None, company=""):
    prof = prof or profile_mod.load()
    title = job.get("title") or ""
    desc = job.get("description") or ""
    company = company or job.get("company") or ""
    reasons, flags, facts = [], [], {}
    skip = []
    home, home_name = prof.home_state, prof.home_state_name.title()

    def rule(name, msg):
        """A profile-switchable hard rule: skip (default), flag, or ignore."""
        v = prof.rule(name)
        if v == "skip":
            skip.append(msg)
        elif v == "flag":
            flags.append(msg)

    field = field_of(title, prof)
    tl = title.lower()

    # ---- title / field
    if re.search(r"internal (?:transfer |applicants? |candidates? )?only|promotional only|"
                 r"current (?:state |county |city )?employees only", title, re.I) or \
            re.search(r"\binternal\b", str(job.get("employment_type") or ""), re.I):
        skip.append("internal-only posting (not open to outside applicants)")
    ex = prof.exclude_titles.search(title) if prof.exclude_titles is not None else None
    if ex:
        skip.append("excluded title (%s)" % ex.group(0))
    if INTERN.search(title):
        rule("internships", "internship/student/volunteer/applicant-pool posting")
    if SALES_TITLE.search(title) and not re.search(r"sales (?:support|operations? support)|support", tl):
        rule("quota", "quota sales title")
    if re.search(r"\bengineer|\beng\b", tl) and not (prof.engineering_ok is not None and prof.engineering_ok.search(title)):
        rule("engineering_titles", "engineering title")
    tstates = {st for st in states_in(title) if st != home} if re.search(r"[-,(|]\s*[A-Z][a-z]+", title) else set()
    tstates = {st for st in tstates if re.search(r"(?i)[-,(|/]\s*(?:%s)\b" % "|".join(
        [re.escape(k) for k, v in STATES.items() if v == st] + [st]), title)}
    if tstates:
        skip.append("title ties the role to %s" % ", ".join(sorted(tstates)))
    if AGGREGATORS.search(company):
        skip.append("recruiting platform reposting another employer's job (%s)" % company)
    for rx, why in prof.blocked_employers:
        if rx.search(company):
            skip.append(why)
    if not field:
        skip.append("title outside the profile's fields")
        field = prof.fields[0]["name"] if prof.fields else "other"
    facts["field"] = field
    lead = LEADERSHIP.search(title)
    aimed = prof.leadership_ok is not None and bool(prof.leadership_ok.search(title))
    if aimed:
        lead = None     # a level this profile is aiming for
    credible = prof.credible(field)
    label = prof.label(field)

    # ---- location (tag AND text)
    lc, ldetail = location_class(job, desc, prof)
    facts["location_class"] = lc
    if lc == "remote_non_us":
        skip.append(ldetail)
    elif lc == "onsite_other":
        skip.append("not remote: " + ldetail)
    elif lc == "remote_ambiguous":
        flags.append(ldetail)
    elif lc == "unknown":
        flags.append("location not stated")
    elif lc == "nearby":
        flags.append("nearby (%s): check the commute" % ldetail)
    if lc in REMOTE_CLASSES and not prof.remote_ok:
        skip.append("remote role; this profile wants local on-site work")
    if lc in REMOTE_CLASSES and home:
        kind, sts, line = state_restriction(desc, home)
        facts["state_limit"] = kind
        if kind == "excludes_home":
            skip.append("remote only in %s (not %s)" % (", ".join(sts[:8]) + ("..." if len(sts) > 8 else ""), home))
        elif kind == "home_excluded_explicit":
            skip.append("says it can't hire in %s" % home_name)
        elif kind == "includes_home":
            reasons.append("%s is on its list of hiring states" % home)
    if lc == "remote_us" and home:
        loc_s = job.get("location") or ""
        tied = remote_tied_states(loc_s)
        segs = [x.strip() for x in re.split(r"\s*[;|/\n]\s*", loc_s) if x.strip()]
        seg_states = [states_in(x) for x in segs]
        if tied and home not in tied:
            skip.append("remote tied to %s (location tag '%s')" % (", ".join(sorted(tied)), loc_s[:60]))
        elif not tied and not US_WIDE.search(loc_s) and segs and all(len(t) == 1 for t in seg_states) \
                and home not in set().union(*seg_states):
            # a remote job tagged only with offices in other states ("Dallas, TX / Alpharetta, GA")
            if len(segs) == 1:
                flags.append("remote tagged to %s only: check %s residents can apply" % (next(iter(seg_states[0])), home))
            else:
                flags.append("remote, but tagged only to %s: may have to live near one" % loc_s[:60])
    for effect, why in form_gates(job, prof, lc):
        if effect.startswith("rule:"):
            rule(effect[5:], why)
        else:
            (skip if effect == "skip" else flags).append(why)
    if lc == "home_state_remote":
        reasons.append("%s-residents-only remote: small applicant pool" % home_name)
    elif lc == "local":
        reasons.append("local (on-site is fine)")

    # ---- pay
    pk, ptop, pline = pay_of(desc, (job.get("pay_text") or "") + "\n" + form_pay_text(job))
    health = bool(HEALTH.search(desc))
    no_benefits = NO_BENEFITS.search(desc)
    contract = CONTRACT.search(title + "\n" + (job.get("employment_type") or "") + "\n" + desc[:3000])
    facts.update({"pay_kind": pk, "pay_top": ptop, "pay_text": job.get("pay_text") or pline, "health": health,
                  "contract": bool(contract)})
    under_min = low = False
    if pk == "year":
        under_min, low = ptop < prof.min_year, ptop < prof.floor_year
    elif pk == "hour":
        under_min, low = ptop < prof.min_hour, ptop < prof.floor_hour
    ptxt = ("$%dk/yr" % round(ptop / 1000)) if pk == "year" else ("$%.2f/hr" % ptop if pk == "hour" else "")
    if under_min:
        skip.append("pay tops out at %s (under $%dk / $%.2fhr)" % (ptxt, prof.min_year // 1000, prof.min_hour))
    elif low and not health:
        skip.append("pay tops out at %s, under the $%dk floor, and no health insurance stated" % (ptxt, prof.floor_year // 1000))
    elif low:
        flags.append("pay tops out at %s, under the floor; has health insurance" % ptxt)
    if no_benefits:
        rule("no_benefits", "no benefits ('%s')" % no_benefits.group(0))
    elif not health and contract:
        flags.append("contract role (%s): health benefits unlikely" % contract.group(0))
    elif not health and desc:
        flags.append("health benefits not stated")

    # ---- weekend shifts
    wk = WEEKEND_REQ.search(desc)
    soft = WEEKEND_SOFT.search(desc)
    if wk and not re.search(r"no weekends?|never (?:work )?weekends|weekends? off|free weekends", desc, re.I):
        rule("weekend_required", "required weekend work: '%s'" % wk.group(0).strip()[:60])
        facts["weekend"] = "required"
    elif soft:
        ctx = desc[max(0, soft.start() - 60):soft.end() + 40].replace("\n", " ")
        if re.search(r"saturday|sunday", soft.group(0), re.I) and not re.search(r"shift|schedule|work|hours|availab", ctx, re.I):
            facts["weekend"] = "none_stated"
        else:
            if prof.rule("weekend_required") != "ignore":
                flags.append("weekend work unclear: '...%s...'" % ctx.strip()[:90])
            facts["weekend"] = "unclear"
    elif WEEKDAYS.search(desc):
        facts["weekend"] = "weekdays"
        reasons.append("weekday schedule")
    else:
        facts["weekend"] = "none_stated"

    # ---- other hard rules
    if CLEARANCE.search(desc) or CLEARANCE.search(title):
        rule("clearance", "security clearance / public trust")
    lic = LICENCE.search(desc)
    if lic and not re.search(r"(preferred|a plus|nice to have)", desc[lic.start():lic.end() + 40], re.I):
        rule("drivers_licence", "needs a driver's licence or vehicle ('%s')" % lic.group(0)[:50])
    if prof.industry_regex is not None and field in prof.industry_fields:
        ind = prof.industry_regex.search(company + " | " + title)
        if ind:
            skip.append("%s at an unrelated industry (%s)" % (label.lower(), ind.group(0)))
    if field in prof.quota_fields or re.search(r"account manag", tl):
        if not NO_QUOTA.search(desc):
            q = next((m for m in QUOTA.finditer(desc) if "applicable" not in desc[max(0, m.start() - 60):m.end() + 40].lower()), None)
            if q:
                rule("quota", "quota/commission language ('%s')" % q.group(0))
    qa = QUOTA_ANY.search(desc + "\n" + (job.get("pay_text") or ""))
    if qa and not NO_QUOTA.search(desc) and not any(s.startswith("quota") for s in skip):
        rule("quota", "quota/OTE pay ('%s')" % qa.group(0))
    deg, deg_line = degree_of(desc)
    facts["degree"] = deg
    if deg == "required":
        rule("degree_required", "degree required with no equivalency")
    certs = sorted({m.group(1) for ln in lines_of(desc) if not PREFERRED.search(ln) and re.search(r"requir|must", ln, re.I)
                    for m in CERT_REQ.finditer(ln)})
    gap_long = []
    gaps = required_gaps(desc, prof)
    gaps += [g for g in form_gaps(job, prof) if g[0].replace(" (application form)", "") not in {w for w, _, _ in gaps}]
    for why, effect, _ in gaps:
        (skip if effect == "skip" else flags if effect == "flag" else gap_long).append(why)

    # ---- freshness: a posting up for a year is not a fresh match
    age, age_how = job_age(job)
    if age is not None:
        facts["age_days"] = age
        if age_how == "relisted":
            facts["relisted"] = True
        if prof.skip_days and age > prof.skip_days and lc != "local":     # a local always-open posting stays (at most fair)
            skip.append("posted %d days ago: a stale posting" % age)

    # ---- years / verdict
    req, pref = years_asked(desc)
    years = max([n for n, _ in req]) if req else None
    facts["years"] = years
    # in a title the profile aims for, "lead"/"manager" are the target level; only senior/staff/principal stretch it
    senior = bool(re.search(r"\b(senior|sr\.?|staff|principal)\b", title, re.I) if aimed else SENIOR.search(title))
    entry = bool(ENTRY.search(desc) or ENTRY.search(title))
    if skip:
        verdict = "skip"
    elif not desc:
        verdict = "fair"
        flags.append("description not read yet")
    elif certs:
        verdict = "long"
        reasons.append("requires " + ", ".join(certs))
    elif gap_long:
        verdict = "long"
        reasons.extend(gap_long)
    elif years is None:
        verdict = "strong"
        reasons.append("no years bar stated")
    elif years <= credible:
        verdict = "strong"
        reasons.append("asks %d+ yrs; %d credible in %s" % (years, credible, label.lower()))
    elif years <= credible + 1:
        verdict = "fair"
        reasons.append("asks %d+ yrs vs %d credible in %s (one-year stretch)" % (years, credible, label.lower()))
    else:
        verdict = "long"
        reasons.append("asks %d+ yrs vs %d credible in %s" % (years, credible, label.lower()))
    for rx, why in prof.stretch_titles:
        m = rx.search(title)
        if m and verdict == "strong":
            verdict = "fair"
            reasons.append("%s ('%s')" % (why, m.group(0)))
    yearly = ptop if pk == "year" else ptop * 2080 if pk == "hour" else None
    if prof.stretch_above_year and yearly and yearly > prof.stretch_above_year and verdict == "strong":
        verdict = "fair"
        reasons.append("pay tops out at %s: the band suggests a more senior role" % ptxt)
    if lead and verdict in ("strong", "fair"):
        # leadership titles: a stretch in fields with many credible years, a long shot elsewhere
        verdict = "fair" if credible >= prof.leadership_stretch_years else "long"
        reasons.append("leadership title '%s' (%s)" % (lead.group(0), "stretch" if credible >= prof.leadership_stretch_years else "long shot"))
    elif verdict == "strong" and senior and credible < prof.senior_stretch_below:
        verdict = "fair"
        reasons.append("senior/lead title (stretch)")

    # ---- score
    score = 50 + {"strong": 25, "fair": 10, "long": -15, "skip": -40}[verdict]
    score += prof.bonus(field)
    if lc == "home_state_remote":
        score += prof.home_remote_bonus
    elif lc == "local":
        score += prof.local_bonus
    elif lc == "remote_us":
        score += 3
    if pk and not low:
        score += 4 if ((pk == "year" and ptop < 70000) or (pk == "hour" and ptop < 34)) else 6
        reasons.append("pay up to %s" % ptxt)
        if prof.target_year and yearly >= prof.target_year:
            score += prof.target_bonus
            reasons.append("meets the $%dk pay target" % (prof.target_year // 1000))
    if health:
        score += 2
        reasons.append("health insurance stated")
    if facts.get("weekend") == "weekdays":
        score += 2
    kw_text = desc + "\n" + form_text(job)      # the employer's own form questions count as part of the posting
    hooks = [h for h in prof.keywords if re.search(r"(?<![A-Za-z0-9])" + re.escape(h) + r"(?![A-Za-z0-9])", kw_text, re.I)]
    hooks = list(dict.fromkeys(h.lower() for h in hooks))
    if hooks:
        score += min(10, 2 * len(hooks))
        reasons.append("mentions tools you know: " + ", ".join(hooks[:6]))
    for rx, fields, pts, why in prof.employer_bonus:
        if (not fields or field in fields) and rx.search(company + " " + desc[:2000]):
            score += pts
            reasons.append(why)
    if entry and verdict != "skip":
        score += 4
        reasons.append("entry-level signals")
    if senior and credible < prof.senior_stretch_below:
        score -= 8
    if deg == "equivalent_ok":
        score += 2
    stale = bool(age is not None and prof.stale_days and age > prof.stale_days)
    if age is not None:
        if age <= 3:
            score += 3
        elif stale:
            score -= 10
            flags.append("posted %d days ago: probably stale (or always open), so at most fair" % age)
        elif age > 45:
            score -= 10
            flags.append("posted %d days ago" % age)
        elif age > 21:
            score -= 4
    score -= 2 * len([f for f in flags if not f.startswith("health benefits not stated")])
    score = max(0, min(100, score))
    if verdict == "skip":
        tier = "skip"
        reasons = skip + reasons
    else:
        tier = "strong" if score >= 85 else "good" if score >= 72 else "fair" if score >= 58 else "long"
        if verdict == "long" and tier in ("strong", "good"):
            tier = "fair"
        if verdict == "fair" and tier == "strong":
            tier = "good"
        if lc == "nearby" and tier == "strong":
            tier = "good"
        if stale and tier in ("strong", "good"):
            tier = "fair"
    return {"score": score, "tier": tier, "verdict": verdict, "field": field, "reasons": reasons, "flags": flags,
            "facts": facts, "skips": skip}


def prefilter(job, prof=None):
    """Cheap title/location gate before fetching a job's detail. -> 'pass' or a reason."""
    prof = prof or profile_mod.load()
    title = job.get("title") or ""
    if INTERN.search(title) and prof.rule("internships") == "skip":
        return "intern"
    if SALES_TITLE.search(title) and "support" not in title.lower() and prof.rule("quota") == "skip":
        return "sales"
    if re.search(r"\bengineer|\beng\b", title, re.I) and not (prof.engineering_ok is not None and prof.engineering_ok.search(title)) \
            and prof.rule("engineering_titles") == "skip":
        return "engineering"
    if not field_of(title, prof):
        return "off-target"
    loc = (job.get("location") or "") + " " + title
    wp = norm_workplace(job.get("workplace"))
    remote = wp == "remote" or re.search(r"\bremote\b|anywhere|work from home|virtual|telework|distributed", loc, re.I)
    near = (prof.local is not None and prof.local.search(loc)) or (prof.nearby is not None and prof.nearby.search(loc))
    if (NON_US.search(loc) or ISO3_REMOTE.search(loc)) and not (US_MARK.search(loc) or states_in(job.get("location") or "")) and not near:
        return "non-us"
    if foreign_country(job, job.get("location") or "", near=bool(near)):
        return "non-us"
    if not remote and not near and wp in ("onsite", "hybrid") and (job.get("location") or "").strip():
        return "onsite"
    return "pass"


def prefilter_all(job, profs):
    """The prefilter for several profiles: 'pass' when any of them passes, else the first one's reason."""
    first = None
    for p in profs:
        r = prefilter(job, p)
        if r == "pass":
            return r
        first = first or r
    return first or "off-target"
