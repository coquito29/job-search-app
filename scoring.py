"""
scoring.py  —  Pure helpers extracted from app.py.

Job scoring, skill extraction, ATS classification, remote detection, and date
parsing. No Flask, no DB, no env — safe to import from anywhere and easy to
unit-test (see tests/test_scoring.py).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List
import re


# ── Skills & no-go terms ──────────────────────────────────────────────────────

KNOWN_SKILLS = [
    "html","css","javascript","python","sql","php","java","react","angular","node.js",
    "typescript","c++","c#","linux","windows","macos","networking","network",
    "cybersecurity","security","firewall","vpn","active directory","azure","aws",
    "helpdesk","help desk","ticketing","jira","zendesk","freshdesk","servicenow",
    "itil","tcp/ip","dns","dhcp","troubleshooting","hardware","software",
    "excel","word","powerpoint","microsoft office","office 365","google workspace",
    "crm","erp","sap","data entry","data analysis","reporting","database",
    "mysql","postgresql","mongodb","tableau","power bi",
    "customer service","customer support","technical support","it support",
    "call center","phone support","live chat","email support","communication",
    "project management","accounting","bookkeeping","quickbooks","invoicing",
    "payroll","scheduling","administrative","virtual assistant",
    "social media","content writing","copywriting","marketing","seo",
    "research","translation","transcription","remote","bilingual",
    "spanish","french","mandarin","voip","go","ruby",
]

NO_GO_TERMS = ["cold calling", "commission only", "door to door"]


# ── ATS triage ────────────────────────────────────────────────────────────────
# Three classes, derived from real application logs:
#   "fast"   = clean Easy-Apply, no account wall, no OTP loop. Submit in <5 min.
#   "walled" = usable but slow — account creation, OTP, multi-step flows.
#   "blocked"= aggregators that Cloudflare-block, force signup, or hide the
#              real employer. Skip entirely.

ATS_FAST = {
    "greenhouse.io":       "Greenhouse",
    "boards.greenhouse.io":"Greenhouse",
    "smartrecruiters.com": "SmartRecruiters",
    "jobs.smartrecruiters.com":"SmartRecruiters",
    "workable.com":        "Workable",
    "jobs.workable.com":   "Workable",
    "lever.co":            "Lever",
    "jobs.lever.co":       "Lever",
    "ashbyhq.com":         "Ashby",
    "jobs.ashbyhq.com":    "Ashby",
    "rippling.com":        "Rippling",
    "ats.rippling.com":    "Rippling",
    "polymer.co":          "Polymer",
    "applytojob.com":      "JazzHR",
    "breezy.hr":           "Breezy HR",
    "recruitee.com":       "Recruitee",
    "bamboohr.com":        "BambooHR",
    "jobvite.com":         "Jobvite",
}
ATS_WALLED = {
    "myworkdayjobs.com":   "Workday",
    "workday.com":         "Workday",
    "myworkdaysite.com":   "Workday",
    "oracle.com":          "Oracle HCM",
    "oraclecloud.com":     "Oracle HCM",
    "taleo.net":           "Taleo",
    "icims.com":           "iCIMS",
    "successfactors.com":  "SuccessFactors",
    "sapsf.com":           "SuccessFactors",
    "ukg.com":             "UKG",
    "ultipro.com":         "UKG/UltiPro",
    "ukgpro.com":          "UKG",
    "dayforce.com":        "Dayforce",
    "dayforcehcm.com":     "Dayforce",
    "adp.com":             "ADP",
    "myjobs.adp.com":      "ADP",
    "paycom.com":          "Paycom",
    "paylocity.com":       "Paylocity",
    "brassring.com":       "BrassRing",
    "kenexa.com":          "BrassRing",
    "applicantstack.com":  "ApplicantStack",
}
ATS_BLOCKED = {
    "jobleads.com": "Aggregator", "lensa.com": "Aggregator",
    "theelitejob.com": "Aggregator", "talent.com": "Aggregator",
    "jobot.com": "Aggregator", "neuvoo.com": "Aggregator",
    "snagajob.com": "Aggregator", "dice.com/jobs": "Aggregator",
    "adzuna.com/details": "Aggregator", "resume-library.com": "Aggregator",
    "clickajobs.com": "Aggregator", "jobs2careers.com": "Aggregator",
    "jobgoal.com": "Aggregator", "jobrapido.com": "Aggregator",
    "joblum.com": "Aggregator", "trabajo.org": "Aggregator",
    "learn4good.com": "Aggregator", "jobsora.com": "Aggregator",
    "bebee.com": "Aggregator",
    "jooble.org":       "Aggregator",
    "dailyremote.com":  "Aggregator",
    "tallo.com":        "Aggregator",
    "himalayas.app":    "Aggregator",
    "apexsystems.com":  "Aggregator",
    "socalnonprofitjobs.org": "Aggregator",
    "remoterocketship.com":   "Aggregator",
}

# Back-compat: callers that just want "is this any direct ATS?" check
ATS_BOOST_DOMAINS = list(ATS_FAST.keys()) + list(ATS_WALLED.keys())

# Domains that require sign-in to view/apply — skip any job URL from these
SIGNIN_WALL_DOMAINS = [
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerbuilder.com", "simplyhired.com", "jobicy.com",
]


def _classify_ats(url):
    """Return ('fast'|'walled'|'blocked'|'unknown', ats_name).
    Longest-match wins so 'jobs.greenhouse.io' beats a generic 'greenhouse.io'.
    ats_name is a human label for the badge ('' if unknown)."""
    if not url:
        return ("unknown", "")
    u = url.lower()
    best = ("unknown", "", 0)
    for cls, table in (("fast", ATS_FAST), ("walled", ATS_WALLED), ("blocked", ATS_BLOCKED)):
        for domain, label in table.items():
            if domain in u and len(domain) > best[2]:
                best = (cls, label, len(domain))
    return (best[0], best[1])


def _url_host_matches(url, domain_list):
    """Case-insensitive 'is this URL from any of these domains?' check."""
    if not url:
        return False
    u = url.lower()
    return any(d in u for d in domain_list)


def _aggregator_denylist():
    """Back-compat shim for the search endpoint."""
    return list(ATS_BLOCKED.keys())


# ── Profile + posted-date parsing ─────────────────────────────────────────────

@dataclass
class UserProfile:
    summary: str
    skills: List[str]
    no_go_terms: List[str]


def _parse_years_required(text):
    text = text.lower()
    patterns = [
        r'(\d+)\+\s*years?\s+(?:of\s+)?(?:experience|exp\b)',
        r'(\d+)\s*[-–]\s*\d+\s+years?\s+(?:of\s+)?(?:experience|exp\b)',
        r'minimum\s+(?:of\s+)?(\d+)\s+years?',
        r'at\s+least\s+(\d+)\s+years?',
        r'(\d+)\s+years?\s+(?:of\s+)?(?:relevant\s+|professional\s+)?(?:experience|exp\b)',
        r'(\d+)\s+years?\s+(?:work(?:ing)?\s+)?experience',
    ]
    found = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            y = int(m.group(1))
            if 1 <= y <= 20:
                found.append(y)
    return min(found) if found else 0


def _days_since_posted(posted):
    if not posted:
        return 999
    now = datetime.utcnow()
    if isinstance(posted, (int, float)):
        try:
            return max(0, (now - datetime.utcfromtimestamp(float(posted))).days)
        except Exception:
            return 999
    s = str(posted).replace("Z", "").replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            return max(0, (now - datetime.strptime(s, fmt)).days)
        except Exception:
            continue
    try:
        return max(0, (now - datetime.fromisoformat(s)).days)
    except Exception:
        return 999


def score_job(job, profile):
    title = (job.get("title", "") or "").lower()
    desc  = (job.get("description", "") or "").lower()
    combo = title + " " + desc
    matched_t = [sk for sk in profile.skills if sk.lower() in title]
    matched_d = [sk for sk in profile.skills if sk.lower() in desc and sk not in matched_t]
    seen_m = set()
    unique = []
    for sk in matched_t + matched_d:
        if sk.lower() not in seen_m:
            seen_m.add(sk.lower()); unique.append(sk)
    skill_score = len(matched_t) * 3 + len(matched_d) * 1
    job_skill_count = sum(1 for sk in KNOWN_SKILLS if sk in desc)
    ratio_bonus = int((len(unique) / max(job_skill_count, 1)) * 22) if job_skill_count else 0
    years_req = _parse_years_required(desc)
    if   years_req == 0: exp_bonus = 14
    elif years_req == 1: exp_bonus = 10
    elif years_req == 2: exp_bonus =  5
    elif years_req == 3: exp_bonus =  0
    else:                exp_bonus = max(-45, -9 * (years_req - 3))
    days_old = job.get("days_old", 999)
    if   days_old <= 1:  fresh_bonus = 12
    elif days_old <= 3:  fresh_bonus =  8
    elif days_old <= 7:  fresh_bonus =  5
    elif days_old <= 14: fresh_bonus =  2
    else:                fresh_bonus =  0
    hire_up = [
        ("entry level",8),("junior",8),("associate",6),
        ("no experience",10),("training provided",10),("will train",10),
        ("we will train",10),("trainee",8),("fresh graduate",8),
        ("recent graduate",8),("open to entry",8),("no degree required",9),
        ("full training",10),("immediate start",5),("urgently hiring",5),
        ("eager to learn",5),("grow with us",4),("mentorship",4),
        ("enthusiastic",3),("self-motivated",3),("motivated",2),
    ]
    hire_down = [
        ("senior",-10),("lead ",-8),("manager",-8),("director",-12),
        ("10+ years",-15),("8+ years",-12),("7+ years",-12),("6+ years",-10),
        ("5+ years",-8),("principal",-10),("architect",-10),("vp ",-15),
        ("cto",-15),("phd required",-12),("doctorate",-12),
        ("proven track record of",-8),("extensive experience",-8),
        ("deep expertise",-8),("expert level",-8),
    ]
    hire_bonus = 0
    hire_labels = []
    _nice = {
        "no experience":"No Exp. Required","training provided":"Training Provided",
        "will train":"Will Train","we will train":"Will Train",
        "no degree required":"No Degree","full training":"Full Training",
        "eager to learn":"Growth Mindset","fresh graduate":"Fresh Grad OK",
        "recent graduate":"Fresh Grad OK","open to entry":"Entry-level Open",
    }
    for kw, pts in hire_up:
        if kw in combo:
            hire_bonus += pts
            if pts >= 8:
                hire_labels.append(_nice.get(kw, kw.title()))
    for kw, pts in hire_down:
        if kw in combo:
            hire_bonus += pts
    for kw in profile.no_go_terms:
        if kw.lower() in combo:
            hire_bonus -= 30
    ats_class, ats_name = _classify_ats(job.get("url", ""))
    ats_bonus = {"fast": 18, "walled": 4, "unknown": 0, "blocked": -50}[ats_class]

    block_bonus = 0
    block_labels = []
    BLOCKERS = [
        ("active security clearance", -40, "Clearance required"),
        ("security clearance",        -35, "Clearance required"),
        ("public trust",              -30, "Public Trust required"),
        ("top secret",                -45, "TS clearance required"),
        ("ts/sci",                    -45, "TS/SCI required"),
        ("us citizen",                -25, "US-citizen-only"),
        ("u.s. citizen",              -25, "US-citizen-only"),
        ("citizenship required",      -30, "Citizenship required"),
        ("must be a us citizen",      -30, "US-citizen-only"),
        ("must be located in",        -10, "State-restricted"),
        ("residents of",              -8,  "State-restricted"),
    ]
    for kw, pts, lbl in BLOCKERS:
        if kw in combo:
            block_bonus += pts
            block_labels.append(lbl)

    # Language-requirement detection: EN + ES profile. Any job that REQUIRES
    # a different language is structurally a no-go.
    NON_ES_LANGUAGES = {
        "french":     "French",
        "italian":    "Italian",
        "portuguese": "Portuguese",
        "german":     "German",
        "mandarin":   "Mandarin",
        "cantonese":  "Cantonese",
        "japanese":   "Japanese",
        "korean":     "Korean",
        "vietnamese": "Vietnamese",
        "hindi":      "Hindi",
        "arabic":     "Arabic",
        "russian":    "Russian",
        "dutch":      "Dutch",
        "polish":     "Polish",
        "tagalog":    "Tagalog",
    }
    for lang_kw, lang_label in NON_ES_LANGUAGES.items():
        patterns = [
            rf"\bbilingual\s+\(?{lang_kw}\b",
            rf"\bfluent\s+(?:in\s+)?{lang_kw}\b",
            rf"\b{lang_kw}[/\s-]+english\b",
            rf"\b{lang_kw}\s*(?:and|&)\s+english\b",
            rf"\b{lang_kw}[-\s]+speaking\b",
            rf"\b(?:speak|speaks|speaking)\s+{lang_kw}\b",
            rf"\b{lang_kw}\s+(?:required|preferred|proficiency|fluency)\b",
            rf"\bmust\s+speak\s+{lang_kw}\b",
            rf"\bnative\s+{lang_kw}\b",
        ]
        if any(re.search(p, combo) for p in patterns):
            block_bonus += -35
            block_labels.append(f"{lang_label} required")
            break

    fit_bonus = 0
    fit_labels = []
    FITS = [
        ("bilingual spanish", 10, "Spanish bilingual"),
        ("english/spanish",   10, "EN/ES bilingual"),
        ("english and spanish", 10, "EN/ES bilingual"),
        ("spanish speaking",  8,  "Spanish bilingual"),
        ("tier 1",            8,  "Tier 1"),
        ("tier i",            8,  "Tier 1"),
        ("level 1",           6,  "Level 1"),
        ("level i ",          6,  "Level 1"),
        ("help desk",         5,  None),
        ("service desk",      5,  None),
        ("soc analyst",       6,  None),
        ("healthcare it",     4,  None),
        ("clinical support",  4,  None),
    ]
    for kw, pts, lbl in FITS:
        if kw in combo:
            fit_bonus += pts
            if lbl: fit_labels.append(lbl)

    total = (skill_score + ratio_bonus + exp_bonus + fresh_bonus
             + hire_bonus + ats_bonus + block_bonus + fit_bonus)
    top   = len(profile.skills) * 3 + 80
    pct   = min(100, max(0, int((total / max(top, 1)) * 100)))

    reasons = []
    if unique:
        reasons.append(f"Skills: {', '.join(unique[:5])}")
    if years_req == 0 and any(k in combo for k in ["entry level","junior","no experience","trainee"]):
        reasons.append("Entry-level")
    elif years_req > 0 and years_req <= 2:
        reasons.append(f"Only {years_req}yr exp needed")
    if hire_labels:
        reasons.append(" + ".join(list(dict.fromkeys(hire_labels))[:2]))
    if fit_labels:
        reasons.append(" + ".join(list(dict.fromkeys(fit_labels))[:2]))
    if days_old <= 7 and days_old < 999:
        reasons.append(f"Fresh ({days_old}d old)")
    if job.get("salary"):
        reasons.append("Salary listed")
    if ats_class == "fast":
        reasons.insert(0, f"{ats_name or 'Easy'} fast apply")
    elif ats_class == "walled":
        reasons.insert(0, f"{ats_name or 'ATS'} (account wall)")
    if block_labels:
        reasons.append("⚠ " + " + ".join(list(dict.fromkeys(block_labels))[:2]))
    if not reasons:
        reasons.append("Keyword match")

    return {
        "match_pct":      pct,
        "match_why":      " · ".join(reasons),
        "matched_skills": unique,
        "hire_signals":   hire_labels,
        "years_req":      years_req,
        "days_old":       days_old,
        "is_ats":         ats_class in ("fast", "walled"),
        "ats_class":      ats_class,
        "ats_name":       ats_name,
        "blockers":       block_labels,
        "fits":           fit_labels,
    }


def is_remote_job(job):
    loc   = (job.get("location", "") or "").lower()
    title = (job.get("title", "") or "").lower()
    desc  = (job.get("description", "") or "").lower()[:400]
    non_remote = [
        "on-site","onsite","in office","in-office","on site",
        "hybrid only","office based","office-based","must be local",
        "required on site","in person","in-person",
    ]
    if any(k in loc for k in non_remote): return False
    if any(k in title for k in ["on-site","onsite","hybrid","in-person"]): return False
    if any(k in desc for k in ["must report to office","required to work in office",
                                "not a remote position","on-site only"]):
        return False
    return True


def _clean_salary(sal):
    if not sal or sal.strip() in ("-", "", "None"):
        return ""
    sal = sal.strip()
    nums = [int(n.replace(",","")) for n in re.findall(r"[\d,]+", sal) if n.replace(",","").isdigit()]
    if not nums:
        return sal
    def fmt(n):
        return f"${n//1000}k" if n >= 1000 else f"${n}"
    if len(nums) >= 2:
        lo, hi = sorted(nums[:2])
        if lo > 0 and hi > lo:
            return f"{fmt(lo)}–{fmt(hi)}"
    return fmt(nums[0]) if nums[0] > 0 else sal


def fmt_date(posted):
    if not posted: return ""
    if isinstance(posted, (int, float)):
        try: return datetime.utcfromtimestamp(float(posted)).strftime("%b %d, %Y")
        except: return str(posted)
    s = str(posted).replace("Z","").replace("T"," ").strip()
    for fmt_str in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try: return datetime.strptime(s, fmt_str).strftime("%b %d, %Y")
        except: continue
    try: return datetime.fromisoformat(s).strftime("%b %d, %Y")
    except: return str(posted)


def extract_skills_from_text(text):
    """Extract known skills from CV/resume text, with fallback word frequency."""
    text_lower = text.lower()
    found = []
    for skill in KNOWN_SKILLS:
        if skill.lower() in text_lower and skill not in found:
            found.append(skill)
    if len(found) < 3:
        words = re.findall(r'\b[A-Za-z][a-z]{2,}\b', text)
        freq = {}
        for w in words:
            freq[w.lower()] = freq.get(w.lower(), 0) + 1
        extras = [w for w, c in sorted(freq.items(), key=lambda x: -x[1])
                  if c >= 2 and w not in found and len(w) > 3][:10]
        found.extend(extras)
    return found
