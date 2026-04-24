"""
app.py  —  Remote Job Search Mobile PWA
Sources: Apify · Adzuna · JSearch  (paid/keyed APIs only — free sources removed for signal quality)
AI Cover Letters: Claude Haiku (set ANTHROPIC_API_KEY env var)
"""
import io, json, os, re, socket, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any

import requests as http_req
from flask import Flask, request, jsonify, render_template, send_file

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except ImportError:
    pdf_extract_text = None

try:
    import docx as _docx
except ImportError:
    _docx = None

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

# psycopg2 is only imported when DATABASE_URL is set (Render Postgres);
# local dev falls back to sqlite with no new dependency
try:
    import psycopg2 as _psycopg2
    from psycopg2.extras import RealDictCursor as _RealDictCursor
except ImportError:
    _psycopg2 = None
    _RealDictCursor = None

app = Flask(__name__)

# ── Applications tracker (Postgres on Render, SQLite locally) ────────────────
# If DATABASE_URL is set (Render auto-injects this when you attach a Postgres
# instance), use Postgres so the tracker survives redeploys/restarts.
# Otherwise fall back to a local SQLite file for easy dev.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL) and _psycopg2 is not None

APPLICATIONS_DB = os.environ.get(
    "APPLICATIONS_DB",
    os.path.join(os.path.dirname(__file__), "applications.db"),
)

APP_STATUSES = ["Applied", "Phone Screen", "Interview", "Offer", "Rejected", "Withdrawn", "No Response"]


class _UniformConn:
    """Thin adapter so the rest of the code keeps using
      conn.execute("... ? ...", (params,))
    and gets dict-like rows back, regardless of sqlite/Postgres.
    Postgres-specific translation happens here:
      - ? placeholders → %s
      - RealDictCursor returns dicts (sqlite.Row is already dict-like)
    """
    def __init__(self, raw, is_pg):
        self._raw   = raw
        self._is_pg = is_pg

    def execute(self, sql, params=()):
        if self._is_pg:
            cur = self._raw.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self._raw.execute(sql, params)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        try: self._raw.rollback()
        except Exception: pass

    def close(self):
        try: self._raw.close()
        except Exception: pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None: self.commit()
        else:                self.rollback()
        self.close()
        return False


def _db_conn():
    if USE_POSTGRES:
        raw = _psycopg2.connect(DATABASE_URL, cursor_factory=_RealDictCursor)
        return _UniformConn(raw, is_pg=True)
    raw = sqlite3.connect(APPLICATIONS_DB)
    raw.row_factory = sqlite3.Row
    return _UniformConn(raw, is_pg=False)


def _init_applications_db():
    # SERIAL for PG, AUTOINCREMENT for sqlite — rest of the schema is identical
    id_col = "id SERIAL PRIMARY KEY" if USE_POSTGRES else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    with _db_conn() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS applications (
                {id_col},
                company      TEXT NOT NULL,
                title        TEXT NOT NULL,
                url          TEXT,
                ats          TEXT,
                location     TEXT,
                salary       TEXT,
                source       TEXT,
                status       TEXT NOT NULL DEFAULT 'Applied',
                notes        TEXT,
                applied_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)


_init_applications_db()
print(f"[db] Applications tracker backend: {'Postgres' if USE_POSTGRES else 'SQLite (' + APPLICATIONS_DB + ')'}")

TOP_JOBS_LIMIT = 50
FETCH_LIMIT = 100  # per Apify search — ~$1.20/search at $0.012/job (opt-in from UI)
DEFAULT_TIMERANGE = "7d"
NO_GO_TERMS = ["cold calling", "commission only", "door to door"]

# Domains that require sign-in to view/apply — skip any job URL from these
SIGNIN_WALL_DOMAINS = [
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerbuilder.com", "simplyhired.com", "jobicy.com",
]

# Aggregators that Cloudflare-block or force signup — drop these entirely
AGGREGATOR_DENYLIST = [
    "jobleads.com", "lensa.com", "theelitejob.com", "talent.com",
    "jobot.com", "neuvoo.com", "snagajob.com", "dice.com/jobs",
    "adzuna.com/details", "resume-library.com", "clickajobs.com",
    "jobs2careers.com", "jobgoal.com", "jobrapido.com", "joblum.com",
    "trabajo.org", "learn4good.com", "jobsora.com",
]

# Direct ATS domains — boost these in ranking (fastest, most reliable apply flow)
ATS_BOOST_DOMAINS = [
    "oracle.com",          # Oracle HCM (taleo-successor)
    "taleo.net",           # Taleo
    "myworkdayjobs.com",   # Workday
    "workday.com",         # Workday
    "icims.com",           # iCIMS
    "smartrecruiters.com", # SmartRecruiters
    "greenhouse.io",       # Greenhouse
    "lever.co",            # Lever
    "ashbyhq.com",         # Ashby
    "jobvite.com",         # Jobvite
    "bamboohr.com",        # BambooHR
    "brassring.com",       # BrassRing / Kenexa
    "successfactors.com",  # SAP SuccessFactors
    "adp.com",             # ADP (enterprise ATS)
    "workable.com",        # Workable
    "recruitee.com",       # Recruitee
    "breezy.hr",           # Breezy HR
    "paycom.com",          # Paycom
    "paylocity.com",       # Paylocity
    "ukg.com",             # UKG / Kronos
]


def _url_host_matches(url, domain_list):
    """Case-insensitive 'is this URL from any of these domains?' check."""
    if not url:
        return False
    u = url.lower()
    return any(d in u for d in domain_list)

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

@dataclass
class UserProfile:
    summary: str
    skills: List[str]
    no_go_terms: List[str]


# ── Fetch functions ───────────────────────────────────────────────────────────
# Free sources (Remotive, RemoteOK, Jobicy, Arbeitnow, Himalayas, TheMuse,
# WeWorkRemotely) were removed — they returned thin/stale results that diluted
# rankings. Only paid/keyed APIs remain: Apify, Adzuna, JSearch.


def fetch_adzuna(skills, app_id, app_key, limit=50):
    """Fetch from Adzuna API (free key from developer.adzuna.com) — remote only."""
    if not app_id or not app_key:
        return []
    query = " ".join(skills[:3])
    try:
        r = http_req.get(
            "https://api.adzuna.com/v1/api/jobs/us/search/1",
            params={
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": limit,
                "what": query,
                "what_and": "remote",
                "content-type": "application/json",
            },
            timeout=20,
        )
        r.raise_for_status()
        jobs = r.json().get("results", [])
    except Exception:
        return []
    results = []
    for j in jobs:
        url = j.get("redirect_url", "")
        if not url:
            continue
        title = j.get("title", "")
        desc  = j.get("description", "")
        # Adzuna returns city locations even for remote jobs — keep only if remote signals exist
        combined = (title + " " + desc).lower()
        remote_signals = ("remote" in combined or "work from home" in combined
                          or "wfh" in combined or "telecommut" in combined)
        if not remote_signals:
            continue
        sal = ""
        lo = j.get("salary_min")
        hi = j.get("salary_max")
        if lo and hi:
            sal = f"${int(lo)//1000}k-${int(hi)//1000}k"
        elif lo:
            sal = f"${int(lo)//1000}k+"
        results.append({
            "title": title,
            "company_name": (j.get("company") or {}).get("display_name", ""),
            "location": "Remote",
            "salary": sal,
            "description": desc,
            "url": url,
            "posted": j.get("created", ""),
            "id": url,
            "source": "Adzuna",
        })
    return results


def fetch_jsearch(skills, rapidapi_key, limit=50):
    """Fetch from JSearch via RapidAPI — searches Indeed, LinkedIn, Glassdoor (free key from rapidapi.com)."""
    if not rapidapi_key:
        return []
    query = " ".join(skills[:4]) + " remote"
    try:
        r = http_req.get(
            "https://jsearch.p.rapidapi.com/search",
            headers={
                "X-RapidAPI-Key": rapidapi_key,
                "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
            },
            params={"query": query, "page": "1", "num_pages": "3", "remote_jobs_only": "true"},
            timeout=20,
        )
        r.raise_for_status()
        jobs = r.json().get("data", [])
    except Exception:
        return []
    results = []
    for j in jobs[:limit]:
        url = j.get("job_apply_link", "") or j.get("job_google_link", "")
        if not url:
            continue
        sal = ""
        lo = j.get("job_min_salary")
        hi = j.get("job_max_salary")
        if lo and hi:
            sal = f"${int(lo)//1000}k-${int(hi)//1000}k"
        results.append({
            "title": j.get("job_title", ""),
            "company_name": j.get("employer_name", ""),
            "location": j.get("job_city", "") or j.get("job_country", "Remote"),
            "salary": sal,
            "description": j.get("job_description", ""),
            "url": url,
            "posted": j.get("job_posted_at_datetime_utc", ""),
            "id": url,
            "source": "JSearch",
        })
    return results


# Role titles George is actually targeting — used for Apify titleSearch
# (kept separate from the broader skills list used for match scoring / descriptionSearch)
TARGET_TITLES = [
    "help desk", "helpdesk", "it support", "technical support",
    "desktop support", "application support", "service desk",
    "end user support", "junior developer", "jr developer",
    "web developer", "software developer", "qa", "test engineer",
    "soc analyst", "security analyst", "cybersecurity analyst",
]

# Post-filter: drop listings with any of these words in the title (too senior for entry IT)
SENIOR_TITLE_BLOCKLIST = (
    "senior", " sr.", " sr ", "lead ", "principal", "staff ",
    "manager", "director", "architect", " vp ", " vp,", "head of",
    "chief ", "ii ", "iii ", "iv ",
)


APIFY_TASK_ID = os.environ.get("APIFY_TASK_ID", "SaaKhEMNZxRC5uGk0")


def fetch_apify_jobs(skills, token, limit=300, time_range="7d"):
    """Fetch remote jobs from Apify via the saved Task "IT Career Search — George".

    The task (id APIFY_TASK_ID) holds the curated config: entry-level filter,
    TARGET_TITLES for titleSearch, senior-title exclusions, remote-only, etc.
    We only override `limit` and `timeRange` per call so digest vs manual
    searches can tune cost. Falls back to the actor endpoint if no task id.
    """
    if not token:
        return []
    run_override = {
        "timeRange": time_range,
        "limit": int(max(10, min(limit, 5000))),
    }
    if APIFY_TASK_ID:
        url = (f"https://api.apify.com/v2/actor-tasks/{APIFY_TASK_ID}"
               "/run-sync-get-dataset-items")
    else:
        url = ("https://api.apify.com/v2/acts/fantastic-jobs~career-site-job-listing-api"
               "/run-sync-get-dataset-items")
        run_override.update({
            "includeAi": True,
            "includeLinkedIn": True,
            "aiWorkArrangementFilter": ["Remote OK", "Remote Solely"],
            "remote only (legacy)": True,
            "removeAgency": True,
            "aiEmploymentTypeFilter": ["FULL_TIME", "CONTRACTOR", "INTERN"],
            "aiExperienceLevelFilter": ["Entry Level", "Associate", "Internship"],
            "titleSearch": TARGET_TITLES,
            "descriptionSearch": skills[:20],
        })
    r = http_req.post(url, params={"token": token}, json=run_override, timeout=180)
    r.raise_for_status()
    items = r.json()
    if not isinstance(items, list):
        return []

    def pick(*vals):
        for v in vals:
            if v is None: continue
            if isinstance(v, str) and not v.strip(): continue
            if isinstance(v, list) and not v: continue
            return v
        return None

    results, seen = [], set()
    for item in items:
        if not isinstance(item, dict): continue
        title   = pick(item.get("title"), item.get("job_title"), item.get("position")) or ""
        # Drop senior-level titles that slipped past the experience filter
        title_lc = title.lower()
        if any(bad in title_lc for bad in SENIOR_TITLE_BLOCKLIST):
            continue
        company = pick(item.get("organization"), item.get("organization_name"),
                       item.get("company"), item.get("company_name")) or ""
        loc = pick(item.get("location"), item.get("ai_remote_location"),
                   item.get("locations"), item.get("locations_derived"))
        if isinstance(loc, list): loc = ", ".join(str(x) for x in loc if x)
        loc = loc or "Remote"
        sv = pick(item.get("salary_raw"), item.get("salary"), item.get("ai_salary"))
        sal = ""
        if sv:
            if isinstance(sv, dict):
                cur = sv.get("currency", "")
                v   = sv.get("value")
                if isinstance(v, dict):
                    lo, hi = v.get("minValue") or v.get("value"), v.get("maxValue")
                    sal = f"{lo}-{hi} {cur}".strip("-None ") if lo and hi else str(lo or hi or "")
                else:
                    sal = f"{v} {cur}".strip()
            else:
                sal = str(sv)
        desc    = pick(item.get("description"), item.get("description_text"),
                       item.get("description_html")) or ""
        url_val = pick(item.get("job_url"), item.get("apply_url"),
                       item.get("url"), item.get("jobUrl"))
        if not url_val or url_val in seen: continue
        seen.add(url_val)
        posted = pick(item.get("date_posted"), item.get("posted"),
                      item.get("published_at"), item.get("publication_date"))
        results.append({
            "title": title, "company_name": company, "location": loc,
            "salary": sal, "description": str(desc), "url": url_val,
            "posted": posted, "id": pick(item.get("id"), url_val),
            "source": "Apify",
        })
    return results


# ── Helper functions ──────────────────────────────────────────────────────────

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
    # Direct ATS apply flow is faster & more reliable — boost these URLs
    ats_bonus = 0
    is_ats = _url_host_matches(job.get("url", ""), ATS_BOOST_DOMAINS)
    if is_ats:
        ats_bonus = 12
    total = skill_score + ratio_bonus + exp_bonus + fresh_bonus + hire_bonus + ats_bonus
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
    if days_old <= 7 and days_old < 999:
        reasons.append(f"Fresh ({days_old}d old)")
    if job.get("salary"):
        reasons.append("Salary listed")
    if is_ats:
        reasons.insert(0, "Direct ATS apply")
    if not reasons:
        reasons.append("Keyword match")
    return {
        "match_pct": pct,
        "match_why": " · ".join(reasons),
        "matched_skills": unique,
        "hire_signals": hire_labels,
        "years_req": years_req,
        "days_old": days_old,
        "is_ats": is_ats,
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
    # Fallback: if very few skills found, also grab frequent capitalized words
    if len(found) < 3:
        words = re.findall(r'\b[A-Za-z][a-z]{2,}\b', text)
        freq = {}
        for w in words:
            freq[w.lower()] = freq.get(w.lower(), 0) + 1
        extras = [w for w, c in sorted(freq.items(), key=lambda x: -x[1])
                  if c >= 2 and w not in found and len(w) > 3][:10]
        found.extend(extras)
    return found


# ── CV parsing helpers ────────────────────────────────────────────────────────

def _parse_pdf(file_bytes):
    if pdf_extract_text is None:
        raise RuntimeError("pdfminer.six not installed")
    return pdf_extract_text(io.BytesIO(file_bytes))


def _parse_docx(file_bytes):
    if _docx is None:
        raise RuntimeError("python-docx not installed")
    doc = _docx.Document(io.BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs)


def _parse_txt(file_bytes):
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


# ── AI CV parsing ────────────────────────────────────────────────────────────

def _parse_cv_ai(text, api_key):
    """Use Claude Sonnet to extract a rich structured profile from CV text."""
    client  = _anthropic.Anthropic(api_key=api_key)
    excerpt = text[:5000]
    prompt  = f"""Analyse this CV/resume and extract structured information.

CV TEXT:
{excerpt}

Return ONLY valid JSON (no markdown, no extra text):
{{
  "skills": ["skill1", "skill2", ...],
  "summary": "<2-3 sentence professional summary of this person>",
  "job_titles": ["<most recent job title>", "<second title>"],
  "experience_years": <estimated total years of work experience as integer>,
  "education": "<highest education level, e.g. Bachelor's in Computer Science>",
  "search_terms": ["<job title to search for 1>", "<job title 2>", "<job title 3>"]
}}

skills: all technical tools, languages, frameworks, soft skills, domain expertise (max 35, all lowercase).
search_terms: 3-5 specific job titles this person should search for based on their background."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=900,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    m   = re.search(r'\{.*\}', raw, re.DOTALL)
    result = json.loads(m.group() if m else raw)

    return jsonify({
        "skills":           result.get("skills", []),
        "summary":          result.get("summary", ""),
        "skill_count":      len(result.get("skills", [])),
        "job_titles":       result.get("job_titles", []),
        "experience_years": result.get("experience_years", 0),
        "education":        result.get("education", ""),
        "search_terms":     result.get("search_terms", []),
        "ai_parsed":        True,
    })


# ── Cover letter generation ───────────────────────────────────────────────────

def generate_cover_letter(job, skills, resume_summary=""):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and _anthropic:
        try:
            return _generate_cover_letter_ai(job, skills, api_key, resume_summary)
        except Exception:
            pass
    return _generate_cover_letter_template(job, skills)


def _generate_cover_letter_ai(job, skills, api_key, resume_summary=""):
    client = _anthropic.Anthropic(api_key=api_key)
    desc_excerpt = re.sub(r"<[^>]+>", "", job.get("description", "") or "")[:2000]
    matched = job.get("matched_skills", skills[:10])
    skills_str = ", ".join(matched[:10]) if matched else ", ".join(skills[:10])
    summary_line = f"\nMy Background: {resume_summary}" if resume_summary else ""
    prompt = f"""You are an expert career coach writing a highly personalized, compelling cover letter.

CANDIDATE PROFILE:
Skills: {skills_str}{summary_line}

JOB POSTING:
Title: {job.get('title', 'the position')}
Company: {job.get('company_name', 'the company')}
Description:
{desc_excerpt}

Write a 3-paragraph cover letter that:
1. Opening: Shows genuine enthusiasm for THIS specific role. Reference something concrete from the job description (a tool, responsibility, or company mission).
2. Middle: Connects 2-3 of my exact skills to specific requirements in the job description. Use brief, concrete examples that demonstrate real value. Be specific, not generic.
3. Closing: Confident call to action. Express readiness to contribute immediately.

Start with "Dear Hiring Team," and end with "Sincerely,\\n[Your Name]\\n[Your Email]\\n[Your Phone]"
Rules: No generic filler phrases ("I am excited to apply", "I believe I am a great fit"). Be direct, specific, and professional. Reference actual job requirements by name."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


def _generate_cover_letter_template(job, skills):
    title = job.get("title", "this position")
    company = job.get("company_name", "your company")
    matched = job.get("matched_skills", skills[:8])
    skills_str = ", ".join(matched[:8]) if matched else ", ".join(skills[:8])
    pct = job.get("match_pct", 0)
    return f"""Dear Hiring Team at {company},

I am writing to express my strong interest in the {title} role. Having reviewed the job description, I am confident that my background makes me an excellent fit — with a {pct}% match to your requirements.

My relevant skills include: {skills_str}. These align directly with what you are looking for, and I am eager to bring this expertise to {company}. I thrive in remote environments and excel at collaborating across distributed teams.

I would welcome the opportunity to discuss how I can contribute to your team. Thank you for your time and consideration.

Sincerely,
[Your Name]
[Your Email]
[Your Phone]
[LinkedIn / Portfolio URL]
"""


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config")
def config():
    """Return server-side config so the frontend can auto-fill all API keys."""
    return jsonify({
        "apify_token":  os.environ.get("APIFY_TOKEN", ""),
        "rapidapi_key": os.environ.get("RAPIDAPI_KEY", ""),
        "adzuna_id":    os.environ.get("ADZUNA_APP_ID", ""),
        "adzuna_key":   os.environ.get("ADZUNA_APP_KEY", ""),
        "ai_enabled":   bool(os.environ.get("ANTHROPIC_API_KEY") and _anthropic),
        "adzuna_enabled": bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")),
        "jsearch_enabled": bool(os.environ.get("RAPIDAPI_KEY")),
    })


@app.route("/api/status")
def status():
    """Visual status page — open this in your browser to check all sources."""
    sources = {
        "Apify":     {"free": False, "ok": bool(os.environ.get("APIFY_TOKEN"))},
        "Adzuna":    {"free": False, "ok": bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))},
        "JSearch":   {"free": False, "ok": bool(os.environ.get("RAPIDAPI_KEY"))},
        "Reed":      {"free": False, "ok": bool(os.environ.get("REED_API_KEY"))},
    }

    ai_ok = bool(os.environ.get("ANTHROPIC_API_KEY") and _anthropic)

    rows = ""
    for name, info in sources.items():
        if info["ok"] is True:
            icon, color, label = "✅", "#198754", "Connected"
        elif info["ok"] is False and not info["free"]:
            icon, color, label = "🔑", "#dc3545", "No API key set"
        elif info["ok"] is False:
            icon, color, label = "❌", "#dc3545", "Unreachable"
        else:
            icon, color, label = "❓", "#6c757d", "Unknown"
        rows += f"<tr><td><b>{name}</b></td><td>{'Free' if info['free'] else 'API Key'}</td><td style='color:{color}'>{icon} {label}</td></tr>"

    ai_row = f"<tr><td><b>AI Cover Letters (Claude)</b></td><td>API Key</td><td style='color:{'#198754' if ai_ok else '#dc3545'}'>{'✅ Enabled' if ai_ok else '🔑 No ANTHROPIC_API_KEY set'}</td></tr>"

    html = f"""<!DOCTYPE html>
<html><head><meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Job Search App — Status</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:600px;margin:40px auto;padding:16px;background:#f8f9fa}}
  h1{{color:#0d6efd;font-size:1.4rem}}
  table{{width:100%;border-collapse:collapse;background:white;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
  th{{background:#0d6efd;color:white;padding:10px 14px;text-align:left;font-size:.85rem}}
  td{{padding:10px 14px;border-bottom:1px solid #dee2e6;font-size:.9rem}}
  tr:last-child td{{border:none}}
  .refresh{{margin-top:16px;color:#6c757d;font-size:.8rem;text-align:center}}
</style></head>
<body>
<h1>🔍 Remote Job Search — Source Status</h1>
<table>
  <tr><th>Source</th><th>Type</th><th>Status</th></tr>
  {rows}{ai_row}
</table>
<p class='refresh'>Refreshed live · <a href='/api/status'>Reload</a> · <a href='/'>Back to App</a></p>
</body></html>"""
    return html


@app.route("/api/parse-cv", methods=["POST"])
def parse_cv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    filename = f.filename.lower()
    file_bytes = f.read()

    try:
        if filename.endswith(".pdf"):
            text = _parse_pdf(file_bytes)
        elif filename.endswith(".docx"):
            text = _parse_docx(file_bytes)
        elif filename.endswith(".txt"):
            text = _parse_txt(file_bytes)
        else:
            return jsonify({"error": "Unsupported file type. Use PDF, DOCX, or TXT."}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {str(e)}"}), 500

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and _anthropic:
        try:
            return _parse_cv_ai(text, api_key)
        except Exception:
            pass

    # Fallback: keyword matching
    skills = extract_skills_from_text(text)
    lines  = [ln.strip() for ln in text.splitlines() if ln.strip()]
    summary = " ".join(lines[:5])[:300] if lines else ""
    return jsonify({"skills": skills, "summary": summary, "skill_count": len(skills), "ai_parsed": False})


@app.route("/api/search", methods=["POST"])
def search_jobs():
    data = request.get_json(force=True)
    token        = (data.get("token") or "").strip()
    rapidapi_key = (data.get("rapidapi_key") or os.environ.get("RAPIDAPI_KEY", "")).strip()
    adzuna_id    = (data.get("adzuna_id") or os.environ.get("ADZUNA_APP_ID", "")).strip()
    adzuna_key   = (data.get("adzuna_key") or os.environ.get("ADZUNA_APP_KEY", "")).strip()
    skills       = data.get("skills") or []
    time_range   = data.get("time_range") or DEFAULT_TIMERANGE
    # Default: paid/keyed APIs only (free sources return thin / stale results)
    sources      = data.get("sources") or ["apify", "jsearch", "adzuna"]

    if not skills:
        return jsonify({"error": "At least one skill is required"}), 400

    # Build fetch tasks based on selected sources (paid/keyed APIs only)
    fetch_tasks = {}
    if "apify" in sources and token:
        fetch_tasks["apify"] = lambda: fetch_apify_jobs(skills, token, limit=FETCH_LIMIT, time_range=time_range)
    if "adzuna" in sources and adzuna_id and adzuna_key:
        fetch_tasks["adzuna"] = lambda: fetch_adzuna(skills, adzuna_id, adzuna_key, limit=50)
    if "jsearch" in sources and rapidapi_key:
        fetch_tasks["jsearch"] = lambda: fetch_jsearch(skills, rapidapi_key, limit=50)

    # Run all sources in parallel
    source_results = {src: [] for src in fetch_tasks}
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_src = {executor.submit(fn): src for src, fn in fetch_tasks.items()}
        for future in as_completed(future_to_src):
            src = future_to_src[future]
            try:
                source_results[src] = future.result()
            except Exception:
                source_results[src] = []

    # Location keywords that signal an in-person / on-site job
    ONSITE_SIGNALS = [
        ", al", ", ak", ", az", ", ar", ", ca", ", co", ", ct", ", de", ", fl",
        ", ga", ", hi", ", id", ", il", ", in", ", ia", ", ks", ", ky", ", la",
        ", me", ", md", ", ma", ", mi", ", mn", ", ms", ", mo", ", mt", ", ne",
        ", nv", ", nh", ", nj", ", nm", ", ny", ", nc", ", nd", ", oh", ", ok",
        ", or", ", pa", ", ri", ", sc", ", sd", ", tn", ", tx", ", ut", ", vt",
        ", va", ", wa", ", wv", ", wi", ", wy",
    ]

    # Merge, deduplicate by URL, remove sign-in-wall domains, keep remote only
    seen_urls = set()
    all_jobs = []
    for src, jobs_list in source_results.items():
        for job in jobs_list:
            url = job.get("url", "")
            if not url or url in seen_urls:
                continue
            if _url_host_matches(url, SIGNIN_WALL_DOMAINS):
                continue  # skip Indeed, LinkedIn, Glassdoor etc.
            if _url_host_matches(url, AGGREGATOR_DENYLIST):
                continue  # skip jobleads, lensa, etc. (Cloudflare-blocked)
            # Remote-only filter: skip jobs with clear in-person location signals
            loc_lower = (job.get("location") or "").lower().strip()
            title_lower = (job.get("title") or "").lower()
            # Allow if location is empty, "remote", "worldwide", "anywhere" etc.
            is_remote = (
                not loc_lower
                or "remote" in loc_lower
                or "flexible" in loc_lower
                or "worldwide" in loc_lower
                or "anywhere" in loc_lower
                or "work from home" in loc_lower
                or loc_lower in ("us", "usa", "united states", "global", "international")
            )
            # Also allow if title mentions remote
            if not is_remote and "remote" in title_lower:
                is_remote = True
            # Block if location ends with a US state abbreviation (e.g. "Austin, TX")
            if not is_remote and any(loc_lower.endswith(sig) for sig in ONSITE_SIGNALS):
                continue
            if not is_remote:
                # One last check — if description mentions "remote" prominently keep it
                desc_lower = (job.get("description") or "").lower()[:500]
                if "remote" not in desc_lower and "work from home" not in desc_lower:
                    continue
            seen_urls.add(url)
            all_jobs.append(job)

    # Secondary dedup: same title + company posted on multiple sources
    seen_fps = set()
    deduped  = []
    for job in all_jobs:
        t = re.sub(r'\s+', ' ', (job.get("title") or "").lower().strip())[:60]
        c = re.sub(r'\s+', ' ', (job.get("company_name") or "").lower().strip())[:30]
        if t and c:
            fp = f"{t}|{c}"
            if fp in seen_fps:
                continue
            seen_fps.add(fp)
        deduped.append(job)
    all_jobs = deduped

    total_fetched = len(all_jobs)

    # Source counts (before filtering)
    source_counts = {}
    for job in all_jobs:
        src = job.get("source", "Unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    # Enrich with days_old
    for job in all_jobs:
        job["days_old"] = _days_since_posted(job.get("posted"))

    # Filter remote only
    remote_jobs = [j for j in all_jobs if is_remote_job(j)]
    total_remote = len(remote_jobs)

    # Score and sort
    profile = UserProfile(summary="", skills=skills, no_go_terms=NO_GO_TERMS)
    scored = []
    for job in remote_jobs:
        s = score_job(job, profile)
        job.update(s)
        job["salary_clean"] = _clean_salary(job.get("salary", ""))
        job["date_fmt"] = fmt_date(job.get("posted"))
        scored.append(job)

    scored.sort(key=lambda j: j["match_pct"], reverse=True)
    top = scored[:TOP_JOBS_LIMIT]

    return jsonify({
        "jobs": top,
        "total_fetched": total_fetched,
        "total_remote": total_remote,
        "total_shown": len(top),
        "source_counts": source_counts,
    })


@app.route("/api/cover-letter", methods=["POST"])
def cover_letter():
    data           = request.get_json(force=True)
    job            = data.get("job") or {}
    skills         = data.get("skills") or []
    resume_summary = data.get("resume_summary", "")

    text = generate_cover_letter(job, skills, resume_summary)

    buf = io.BytesIO(text.encode("utf-8"))
    buf.seek(0)
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title", "cover_letter")).strip().replace(" ", "_").lower()
    filename = f"cover_letter_{safe_title}.txt"

    return send_file(
        buf,
        mimetype="text/plain",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/ai-score", methods=["POST"])
def ai_score():
    """Use Claude Sonnet to deeply analyse job fit and return structured feedback."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not _anthropic:
        return jsonify({"error": "AI not available — set ANTHROPIC_API_KEY"}), 503

    data           = request.get_json(force=True)
    job            = data.get("job") or {}
    skills         = data.get("skills") or []
    resume_summary = data.get("resume_summary", "")

    desc       = re.sub(r"<[^>]+>", "", job.get("description", "") or "")[:2500]
    skills_str = ", ".join(skills[:20])
    summary_line = f"\nMy Background: {resume_summary}" if resume_summary else ""

    prompt = f"""Analyse this job posting and honestly assess how well the candidate fits.

CANDIDATE PROFILE:
Skills: {skills_str}{summary_line}

JOB POSTING:
Title: {job.get('title', '')}
Company: {job.get('company_name', '')}
Description:
{desc}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "ai_score": <integer 0-100>,
  "fit_summary": "<2-sentence honest assessment>",
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "gaps": ["<gap 1>", "<gap 2>"],
  "recommendation": "<apply|consider|skip>",
  "tip": "<one specific actionable tip for this exact application>"
}}"""

    try:
        client  = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        m    = re.search(r'\{.*\}', text, re.DOTALL)
        result = json.loads(m.group() if m else text)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Daily Digest ──────────────────────────────────────────────────────────────

@app.route("/api/digest", methods=["POST", "GET"])
def daily_digest():
    """
    Run a job search with the configured skills and email the top 10 results.
    Can be triggered via GET (GitHub Actions cron) or POST (manual/test).
    Requires GMAIL_USER + GMAIL_APP_PASSWORD env vars on Render.
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    gmail_user = os.environ.get("GMAIL_USER", "")   # account that authenticates + sends
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    digest_to  = os.environ.get("DIGEST_TO", gmail_user)  # recipient; defaults to sender
    apify_token = os.environ.get("APIFY_TOKEN", "")
    rapidapi_key = os.environ.get("RAPIDAPI_KEY", "")
    adzuna_id  = os.environ.get("ADZUNA_APP_ID", "")
    adzuna_key = os.environ.get("ADZUNA_APP_KEY", "")

    if not gmail_user or not gmail_pass:
        return jsonify({"error": "GMAIL_USER / GMAIL_APP_PASSWORD not set"}), 500

    # Skills tuned to George's FULL profile — entry IT + dev background (Cholo Tech)
    # Pulls in help-desk, app-support, jr-dev, QA, and SOC-I roles
    skills = [
        # Core IT support (primary target)
        "help desk", "helpdesk", "it support", "technical support",
        "desktop support", "application support", "tier 1", "tier 2",
        "service desk", "end user support", "remote support",
        "troubleshooting", "incident response", "escalation",
        # Entry-level cybersecurity
        "cybersecurity", "security+", "soc analyst", "security analyst",
        # Dev stack (Cholo Tech freelance)
        "javascript", "html", "css", "angular", "node.js", "php",
        "sql", "python", "java",
        # Adjacent entry roles
        "junior developer", "web developer", "qa", "test engineer",
        # Platform / commerce
        "shopify", "seo",
        # Soft differentiators
        "access control", "network", "compliance",
        "bilingual", "spanish"
    ]

    # --- Run all sources in parallel (same logic as /api/search) ---
    # Paid/keyed APIs only — free sources removed
    fetch_tasks = {}
    if apify_token:
        # Cost-capped: $0.012/job * 50 = $0.60/day for the daily digest
        fetch_tasks["apify"] = lambda: fetch_apify_jobs(skills, apify_token, limit=50, time_range="1d")
    if adzuna_id and adzuna_key:
        fetch_tasks["adzuna"] = lambda: fetch_adzuna(skills, adzuna_id, adzuna_key, limit=50)
    if rapidapi_key:
        fetch_tasks["jsearch"] = lambda: fetch_jsearch(skills, rapidapi_key, limit=50)

    source_results = {src: [] for src in fetch_tasks}
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_src = {executor.submit(fn): src for src, fn in fetch_tasks.items()}
        for future in as_completed(future_to_src):
            src = future_to_src[future]
            try:
                source_results[src] = future.result()
            except Exception:
                source_results[src] = []

    # Deduplicate, score, sort
    seen_urls = set()
    seen_fps  = set()
    all_jobs  = []
    for src, jobs_list in source_results.items():
        for job in jobs_list:
            url = job.get("url", "")
            if not url or url in seen_urls:
                continue
            if _url_host_matches(url, SIGNIN_WALL_DOMAINS):
                continue
            if _url_host_matches(url, AGGREGATOR_DENYLIST):
                continue
            seen_urls.add(url)
            t = re.sub(r'\s+', ' ', (job.get("title") or "").lower().strip())[:60]
            c = re.sub(r'\s+', ' ', (job.get("company_name") or "").lower().strip())[:30]
            if t and c:
                fp = f"{t}|{c}"
                if fp in seen_fps:
                    continue
                seen_fps.add(fp)
            all_jobs.append(job)

    # Score jobs by skill match
    scored = []
    for job in all_jobs:
        text_blob = " ".join([
            (job.get("title") or ""),
            (job.get("description") or ""),
            (job.get("company_name") or ""),
        ]).lower()
        hits  = sum(1 for s in skills if s.lower() in text_blob)
        total = max(len(skills), 1)
        pct   = min(100, round((hits / total) * 100))
        job["match_pct"] = pct
        scored.append(job)

    scored.sort(key=lambda j: j["match_pct"], reverse=True)
    top10 = scored[:10]

    if not top10:
        return jsonify({"sent": False, "reason": "No jobs found today"}), 200

    # --- Build HTML email ---
    today = datetime.utcnow().strftime("%B %d, %Y")
    rows_html = ""
    for i, job in enumerate(top10, 1):
        title   = job.get("title", "No title")
        company = job.get("company_name", "Unknown")
        url     = job.get("url", "#")
        pct     = job.get("match_pct", 0)
        loc     = job.get("location") or "Remote"
        bar_color = "#28a745" if pct >= 70 else ("#ffc107" if pct >= 40 else "#6c757d")
        rows_html += f"""
        <tr style="border-bottom:1px solid #e9ecef">
          <td style="padding:14px 8px;font-weight:600;color:#555;width:24px">{i}</td>
          <td style="padding:14px 8px">
            <a href="{url}" style="color:#0d6efd;font-weight:700;font-size:15px;text-decoration:none">{title}</a><br>
            <span style="color:#555;font-size:13px">{company}</span> &nbsp;·&nbsp;
            <span style="color:#888;font-size:12px">{loc}</span>
          </td>
          <td style="padding:14px 8px;text-align:center;white-space:nowrap">
            <span style="background:{bar_color};color:#fff;padding:3px 9px;border-radius:12px;font-size:13px;font-weight:700">{pct}%</span>
          </td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#333">
      <div style="background:#0d6efd;padding:24px 32px;border-radius:8px 8px 0 0">
        <h1 style="color:#fff;margin:0;font-size:22px">🔍 Your Daily Job Digest</h1>
        <p style="color:#cce5ff;margin:6px 0 0">{today} — Top {len(top10)} remote jobs matched to your profile</p>
      </div>
      <div style="border:1px solid #dee2e6;border-top:none;border-radius:0 0 8px 8px;padding:0 16px 16px">
        <table style="width:100%;border-collapse:collapse">
          <thead>
            <tr style="border-bottom:2px solid #dee2e6">
              <th style="padding:10px 8px;text-align:left;color:#888;font-size:12px">#</th>
              <th style="padding:10px 8px;text-align:left;color:#888;font-size:12px">JOB</th>
              <th style="padding:10px 8px;text-align:center;color:#888;font-size:12px">MATCH</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
      <p style="text-align:center;color:#aaa;font-size:12px;margin-top:20px">
        Sent by <a href="https://job-search-app-9pnx.onrender.com" style="color:#0d6efd">Remote Job Search</a> · Click any job to apply directly on the employer site
      </p>
    </body></html>"""

    # --- Send via Gmail SMTP ---
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔍 Daily Job Digest — {today} ({len(top10)} top matches)"
    msg["From"]    = gmail_user
    msg["To"]      = digest_to
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, digest_to, msg.as_string())
    except Exception as e:
        return jsonify({"sent": False, "error": str(e)}), 500

    return jsonify({"sent": True, "jobs_emailed": len(top10), "to": digest_to})


# ── Applications tracker routes ───────────────────────────────────────────────

@app.route("/api/applications", methods=["GET"])
def list_applications():
    status_filter = request.args.get("status")
    with _db_conn() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM applications WHERE status = ? ORDER BY applied_at DESC",
                (status_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM applications ORDER BY applied_at DESC"
            ).fetchall()
    return jsonify({"applications": [dict(r) for r in rows], "count": len(rows)})


@app.route("/api/applications", methods=["POST"])
def create_application():
    data = request.get_json(force=True) or {}
    company = (data.get("company") or "").strip()
    title   = (data.get("title") or "").strip()
    if not company or not title:
        return jsonify({"error": "company and title are required"}), 400
    status = (data.get("status") or "Applied").strip()
    if status not in APP_STATUSES:
        return jsonify({"error": f"status must be one of {APP_STATUSES}"}), 400
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    # RETURNING id works on both sqlite (>=3.35) and Postgres — keeps the
    # adapter backend-agnostic (lastrowid is sqlite-only)
    with _db_conn() as conn:
        cur = conn.execute(
            """INSERT INTO applications
               (company, title, url, ats, location, salary, source, status, notes, applied_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (
                company, title,
                (data.get("url") or "").strip() or None,
                (data.get("ats") or "").strip() or None,
                (data.get("location") or "").strip() or None,
                (data.get("salary") or "").strip() or None,
                (data.get("source") or "").strip() or None,
                status,
                (data.get("notes") or "").strip() or None,
                now, now,
            ),
        )
        row = cur.fetchone()
        # sqlite3.Row supports row["id"], RealDictCursor returns {"id": ...}
        new_id = row["id"] if row is not None else None
    return jsonify({"id": new_id, "ok": True})


@app.route("/api/applications/<int:app_id>", methods=["PATCH"])
def update_application(app_id):
    data = request.get_json(force=True) or {}
    fields = []
    values = []
    for f in ("company", "title", "url", "ats", "location", "salary", "source", "status", "notes"):
        if f in data:
            if f == "status" and data[f] not in APP_STATUSES:
                return jsonify({"error": f"status must be one of {APP_STATUSES}"}), 400
            fields.append(f"{f} = ?")
            values.append((data[f] or "").strip() or None if f != "status" else data[f])
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = ?")
    values.append(datetime.utcnow().isoformat(timespec="seconds") + "Z")
    values.append(app_id)
    with _db_conn() as conn:
        cur = conn.execute(
            f"UPDATE applications SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/applications/<int:app_id>", methods=["DELETE"])
def delete_application(app_id):
    with _db_conn() as conn:
        cur = conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/applications/urls", methods=["GET"])
def applied_urls():
    """Lightweight index of already-applied jobs — used by the frontend
    to dedupe jobs you've already submitted (by URL or company|title).
    Returns:
      { urls: { <url>: {applied_at, status, company, title} },
        fingerprints: { "<co>|<title>": {applied_at, status} } }
    """
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT company, title, url, status, applied_at FROM applications"
        ).fetchall()
    urls = {}
    fingerprints = {}
    for r in rows:
        meta = {
            "applied_at": r["applied_at"],
            "status":     r["status"],
            "company":    r["company"],
            "title":      r["title"],
        }
        if r["url"]:
            urls[r["url"]] = meta
        co = (r["company"] or "").lower().strip()
        ti = (r["title"]   or "").lower().strip()
        if co and ti:
            fingerprints[f"{co}|{ti}"] = {
                "applied_at": r["applied_at"],
                "status":     r["status"],
            }
    return jsonify({"urls": urls, "fingerprints": fingerprints})


@app.route("/api/applications/stats", methods=["GET"])
def application_stats():
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"]
    by_status = {s: 0 for s in APP_STATUSES}
    for r in rows:
        by_status[r["status"]] = r["n"]
    return jsonify({"total": total, "by_status": by_status})


@app.route("/api/gmail/scan", methods=["POST"])
def gmail_scan():
    """Scan Gmail for ATS status updates and forward-bump applications.

    - Reads the last 30 days from INBOX via IMAP (stdlib imaplib).
    - Classifies each email into: Rejected, Offer, Interview, Phone Screen,
      Applied (receipt), or None.
    - Matches to an application by company-name token overlap against the
      sender domain + subject + body snippet.
    - Applies forward-only progression: Applied → Phone Screen → Interview
      → Offer. Rejection can override anything except Offer.
    - Appends a note documenting each auto-change.

    Requires GMAIL_USER + GMAIL_APP_PASSWORD env vars.
    Optional JSON body: { "dry_run": true } previews without writing.
    """
    import imaplib
    import email as _email
    from email.header import decode_header
    from datetime import timedelta

    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_user or not gmail_pass:
        return jsonify({"error": "GMAIL_USER / GMAIL_APP_PASSWORD not set on server"}), 500

    body_json = request.get_json(silent=True) or {}
    dry_run = bool(body_json.get("dry_run"))

    # Load all applications (tracker is small)
    with _db_conn() as conn:
        rows = conn.execute("SELECT * FROM applications").fetchall()
    apps = [dict(r) for r in rows]
    if not apps:
        return jsonify({"scanned": 0, "matched": 0, "updated": 0, "hits": [],
                        "reason": "No applications to match against"})

    # Extract distinguishing tokens from company names (drop filler words)
    _STOP = {"inc", "llc", "corp", "corporation", "company", "co", "ltd",
             "the", "and", "group", "services", "solutions", "technologies",
             "technology", "systems", "global", "international", "holdings"}
    def _tokens(name):
        n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
        return [t for t in n.split() if len(t) >= 3 and t not in _STOP]

    app_tokens = [(a, _tokens(a.get("company", ""))) for a in apps]

    STATUS_RANK = {"Applied": 1, "Phone Screen": 2, "Interview": 3, "Offer": 4}

    def classify(text):
        t = text.lower()
        # Rejection wins if present (but Offer is terminal and trumps it)
        rejection = any(p in t for p in [
            "unfortunately", "not moving forward", "not be moving forward",
            "other candidates", "decided to pursue", "decided to move forward with",
            "not selected", "will not be proceeding", "regret to inform",
            "unable to offer", "decided not to move", "moved forward with other",
            "not be a fit", "not a match at this time", "not to advance",
        ])
        if any(p in t for p in [
            "pleased to offer", "offer letter", "extend an offer",
            "extend you an offer", "extending an offer", "formal offer",
            "written offer", "compensation package",
        ]):
            return "Offer"
        if rejection:
            return "Rejected"
        if any(p in t for p in [
            "technical interview", "onsite interview", "on-site interview",
            "panel interview", "final round", "final interview",
            "hiring manager", "video interview", "second round",
            "team interview", "loop interview",
        ]):
            return "Interview"
        if any(p in t for p in [
            "phone screen", "phone interview", "initial call",
            "intro call", "introductory call", "quick chat",
            "brief call", "brief chat", "schedule a call",
            "schedule a chat", "initial conversation", "30-minute call",
            "30 minute call", "15-minute call", "15 minute call",
            "recruiter screen", "screening call",
        ]):
            return "Phone Screen"
        if any(p in t for p in [
            "thank you for applying", "application received",
            "we have received your application", "we've received your application",
            "received your application", "your application has been received",
            "confirming your application",
        ]):
            return "Applied"
        return None

    def should_update(current, proposed):
        if proposed == "Rejected":
            return current not in ("Rejected", "Offer", "Withdrawn")
        if current == "Rejected" or current == "Withdrawn":
            return False
        return STATUS_RANK.get(proposed, 0) > STATUS_RANK.get(current, 0)

    def match_app(from_addr, subject, snippet):
        blob = (from_addr + " " + subject + " " + snippet).lower()
        best, best_score = None, 0
        for app, tokens in app_tokens:
            if not tokens:
                continue
            score = sum(1 for t in tokens if t in blob)
            if score > best_score:
                best, best_score = app, score
        return best if best_score >= 1 else None

    # Connect to Gmail IMAP
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_user, gmail_pass)
        mail.select("INBOX")
    except Exception as e:
        return jsonify({"error": f"IMAP connect/login failed: {e}"}), 500

    try:
        since = (datetime.utcnow() - timedelta(days=30)).strftime("%d-%b-%Y")
        typ, data = mail.search(None, f'(SINCE {since})')
        ids = (data[0].split() if data and data[0] else [])[-200:]  # cap at 200

        # Collect best proposal per application (forward-only ranking)
        proposals = {}  # app_id -> { status, subject, from, current, date }
        scanned = 0
        matched = 0

        for i in ids:
            try:
                typ, md = mail.fetch(i, "(RFC822)")
                if not md or not md[0]:
                    continue
                msg = _email.message_from_bytes(md[0][1])

                # Decode subject
                subject = ""
                for chunk, enc in decode_header(msg.get("Subject", "") or ""):
                    if isinstance(chunk, bytes):
                        subject += chunk.decode(enc or "utf-8", errors="ignore")
                    else:
                        subject += chunk
                from_addr = msg.get("From", "") or ""

                # Extract plain text body (fall back to HTML stripped)
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain" and not part.get_filename():
                            try:
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                            except Exception:
                                pass
                    if not body:
                        for part in msg.walk():
                            if part.get_content_type() == "text/html" and not part.get_filename():
                                try:
                                    html_body = part.get_payload(decode=True).decode(errors="ignore")
                                    body = re.sub(r"<[^>]+>", " ", html_body)
                                    break
                                except Exception:
                                    pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(errors="ignore") or ""
                    except Exception:
                        body = msg.get_payload() or ""

                snippet = body[:2000]
                scanned += 1

                proposed = classify(subject + " " + snippet)
                if not proposed:
                    continue
                app = match_app(from_addr, subject, snippet)
                if not app:
                    continue
                matched += 1

                # Keep the highest-ranked proposal per app
                prev = proposals.get(app["id"])
                prev_rank = (1000 if prev and prev["status"] == "Rejected"
                             else STATUS_RANK.get(prev["status"], 0) if prev else -1)
                new_rank = 1000 if proposed == "Rejected" else STATUS_RANK.get(proposed, 0)
                if new_rank > prev_rank:
                    proposals[app["id"]] = {
                        "status": proposed,
                        "subject": (subject or "(no subject)").strip()[:140],
                        "from": from_addr.strip()[:100],
                        "current": app["status"],
                    }
            except Exception:
                continue

        try: mail.close()
        except Exception: pass
        try: mail.logout()
        except Exception: pass

        # Apply updates (or just report if dry_run)
        updated = 0
        hits = []
        now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        if dry_run:
            for app_id, p in proposals.items():
                if should_update(p["current"], p["status"]):
                    hits.append({
                        "app_id": app_id,
                        "from": p["current"],
                        "to": p["status"],
                        "subject": p["subject"],
                        "email_from": p["from"],
                    })
        else:
            with _db_conn() as conn:
                for app_id, p in proposals.items():
                    row = conn.execute(
                        "SELECT status, notes, company FROM applications WHERE id = ?",
                        (app_id,)
                    ).fetchone()
                    if not row:
                        continue
                    current = row["status"]
                    if not should_update(current, p["status"]):
                        continue
                    audit = (f"[Gmail scan {now_iso[:10]}] {current} → {p['status']} · "
                             f"from {p['from']} · {p['subject']}")
                    existing = (row["notes"] or "").strip()
                    new_notes = (existing + "\n" + audit).strip() if existing else audit
                    conn.execute(
                        "UPDATE applications SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
                        (p["status"], new_notes, now_iso, app_id),
                    )
                    updated += 1
                    hits.append({
                        "app_id": app_id,
                        "company": row["company"],
                        "from": current,
                        "to": p["status"],
                        "subject": p["subject"],
                        "email_from": p["from"],
                    })

        return jsonify({
            "scanned": scanned,
            "matched": matched,
            "updated": updated,
            "dry_run": dry_run,
            "hits": hits,
        })
    except Exception as e:
        try: mail.logout()
        except Exception: pass
        return jsonify({"error": str(e)}), 500


@app.route("/applications")
def applications_page():
    """Self-contained applications tracker UI."""
    statuses_json = json.dumps(APP_STATUSES)
    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Applications Tracker</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
  body{background:#f0f4f8;padding-bottom:60px}
  .app-header{background:linear-gradient(135deg,#0d6efd 0%,#0043a8 100%);color:white;padding:14px 16px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
  .app-header h1{font-size:1.2rem;margin:0;font-weight:700}
  .app-header .subtitle{font-size:.75rem;opacity:.85;margin:0}
  .stat-card{background:white;border-radius:12px;box-shadow:0 2px 6px rgba(0,0,0,.08);padding:10px;text-align:center}
  .stat-card .n{font-size:1.4rem;font-weight:700;color:#0d6efd}
  .stat-card .label{font-size:.7rem;color:#6c757d;text-transform:uppercase;letter-spacing:.5px}
  .app-row{background:white;border-radius:12px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
  .app-row .company{font-weight:700;font-size:.95rem;color:#212529}
  .app-row .title{font-size:.85rem;color:#495057}
  .app-row .meta{font-size:.72rem;color:#6c757d;margin-top:4px}
  .status-pill{font-size:.72rem;padding:3px 10px;border-radius:20px;font-weight:600}
  .status-Applied{background:#e7f1ff;color:#0d6efd}
  .status-Phone.Screen,.status-Phone-Screen{background:#fff3cd;color:#856404}
  .status-Interview{background:#d1e7dd;color:#0f5132}
  .status-Offer{background:#d4edda;color:#155724}
  .status-Rejected{background:#f8d7da;color:#721c24}
  .status-Withdrawn,.status-No.Response,.status-No-Response{background:#e2e3e5;color:#41464b}
  .btn-fab{position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:50%;box-shadow:0 4px 12px rgba(13,110,253,.4);z-index:10}
  .days-badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:.65rem;font-weight:600;margin-left:4px;vertical-align:middle}
  .days-fresh{background:#d1e7dd;color:#0f5132}
  .days-warm{background:#fff3cd;color:#856404}
  .days-stale{background:#ffe5d0;color:#b8541a}
  .days-cold{background:#f8d7da;color:#721c24}
  .stale-border{border-left:4px solid #fd7e14}
  .quick-actions{display:flex;gap:4px;margin-top:8px;flex-wrap:wrap}
  .quick-actions button{font-size:.7rem;padding:3px 9px;border-radius:12px;border:1px solid #dee2e6;background:white;color:#495057;cursor:pointer}
  .quick-actions button:hover{background:#f0f4f8}
  .quick-actions .qa-reject{border-color:#f8d7da;color:#721c24}
  .quick-actions .qa-reject:hover{background:#f8d7da}
  .quick-actions .qa-offer{border-color:#d4edda;color:#155724}
  .quick-actions .qa-offer:hover{background:#d4edda}
  .hit-row{padding:8px 10px;border-radius:8px;background:#f8f9fa;margin-bottom:6px;font-size:.82rem}
  .hit-row .from-to{font-weight:600}
  .hit-row .subj{color:#6c757d;font-size:.75rem;margin-top:2px}
</style></head>
<body>
<div class="app-header">
  <div class="d-flex justify-content-between align-items-center">
    <div>
      <h1>📋 Applications Tracker</h1>
      <p class="subtitle">Track every job you apply to</p>
    </div>
    <a href="/" class="btn btn-sm btn-light">← App</a>
  </div>
</div>

<div class="container py-3">
  <div class="row g-2 mb-3" id="stats"></div>

  <div class="d-flex gap-2 mb-3 flex-wrap">
    <select id="filter" class="form-select form-select-sm" style="max-width:160px">
      <option value="">All statuses</option>
    </select>
    <select id="sort" class="form-select form-select-sm" style="max-width:170px">
      <option value="newest">Newest first</option>
      <option value="oldest">Oldest first</option>
      <option value="stale">Stale first</option>
      <option value="company">Company A–Z</option>
    </select>
    <button class="btn btn-sm btn-outline-secondary" onclick="load()" title="Reload"><i class="bi bi-arrow-clockwise"></i></button>
    <button class="btn btn-sm btn-outline-primary ms-auto" onclick="scanGmail(false)" title="Scan Gmail for status updates">
      <i class="bi bi-envelope-check"></i> Scan Gmail
    </button>
  </div>

  <div id="list"></div>
  <div id="empty" class="text-center text-muted py-5" style="display:none">
    <i class="bi bi-inbox" style="font-size:3rem;opacity:.4"></i>
    <p class="mt-2">No applications yet. Tap + to add one.</p>
  </div>
</div>

<button class="btn btn-primary btn-fab" data-bs-toggle="modal" data-bs-target="#addModal">
  <i class="bi bi-plus-lg fs-4"></i>
</button>

<!-- Add/Edit modal -->
<div class="modal fade" id="addModal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header"><h5 class="modal-title" id="modalTitle">New Application</h5>
        <button class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">
        <input type="hidden" id="editId">
        <div class="mb-2"><label class="form-label small">Company *</label>
          <input type="text" class="form-control" id="f-company"></div>
        <div class="mb-2"><label class="form-label small">Job Title *</label>
          <input type="text" class="form-control" id="f-title"></div>
        <div class="mb-2"><label class="form-label small">Application URL</label>
          <input type="url" class="form-control" id="f-url"></div>
        <div class="row g-2">
          <div class="col-6 mb-2"><label class="form-label small">ATS</label>
            <select class="form-select" id="f-ats">
              <option value="">—</option><option>Greenhouse</option><option>Lever</option>
              <option>Workable</option><option>Workday</option><option>iCIMS</option>
              <option>ADP Workforce Now</option><option>Taleo</option><option>BambooHR</option>
              <option>SmartRecruiters</option><option>Jobvite</option><option>Other</option>
            </select></div>
          <div class="col-6 mb-2"><label class="form-label small">Status</label>
            <select class="form-select" id="f-status"></select></div>
        </div>
        <div class="row g-2">
          <div class="col-6 mb-2"><label class="form-label small">Location</label>
            <input type="text" class="form-control" id="f-location" placeholder="Remote / City, ST"></div>
          <div class="col-6 mb-2"><label class="form-label small">Salary</label>
            <input type="text" class="form-control" id="f-salary" placeholder="$45k"></div>
        </div>
        <div class="mb-2"><label class="form-label small">Source</label>
          <input type="text" class="form-control" id="f-source" placeholder="Apify / LinkedIn / Referral"></div>
        <div class="mb-2"><label class="form-label small">Notes</label>
          <textarea class="form-control" id="f-notes" rows="2"></textarea></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-outline-danger me-auto" id="deleteBtn" style="display:none" onclick="del()">
          <i class="bi bi-trash"></i> Delete</button>
        <button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" onclick="save()">Save</button>
      </div>
    </div>
  </div>
</div>

<!-- Gmail scan result modal -->
<div class="modal fade" id="scanModal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-envelope-check"></i> Gmail Scan</h5>
        <button class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div id="scanBody" class="text-center py-3">
          <div class="spinner-border text-primary" role="status"></div>
          <p class="mt-2 small text-muted" id="scanStatus">Connecting to Gmail…</p>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Close</button>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
const STATUSES = """ + statuses_json + """;

(function(){
  const sel = document.getElementById('f-status');
  const fil = document.getElementById('filter');
  STATUSES.forEach(s => {
    sel.insertAdjacentHTML('beforeend', `<option>${s}</option>`);
    fil.insertAdjacentHTML('beforeend', `<option>${s}</option>`);
  });
  document.getElementById('filter').addEventListener('change', load);
  document.getElementById('sort').addEventListener('change', load);
  load();
})();

function daysSince(iso){
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d)) return null;
  return Math.floor((Date.now() - d.getTime()) / (86400000));
}
function daysBadge(days){
  if (days == null) return '';
  const cls = days <= 7 ? 'days-fresh' : days <= 14 ? 'days-warm' : days <= 30 ? 'days-stale' : 'days-cold';
  const label = days === 0 ? 'today' : days === 1 ? '1d' : `${days}d`;
  return `<span class="days-badge ${cls}">${label}</span>`;
}
function sortApps(apps, mode){
  const copy = apps.slice();
  if (mode === 'oldest')       copy.sort((a,b) => (a.applied_at||'').localeCompare(b.applied_at||''));
  else if (mode === 'company') copy.sort((a,b) => (a.company||'').localeCompare(b.company||''));
  else if (mode === 'stale'){
    // Stale first = oldest applied_at where status is still pending
    const pending = s => s === 'Applied' || s === 'Phone Screen' || s === 'Interview';
    copy.sort((a,b) => {
      const pa = pending(a.status) ? 0 : 1;
      const pb = pending(b.status) ? 0 : 1;
      if (pa !== pb) return pa - pb;
      return (a.applied_at||'').localeCompare(b.applied_at||'');
    });
  }
  // default: newest first (already server-sorted DESC, so no-op)
  return copy;
}

async function load(){
  const status = document.getElementById('filter').value;
  const sort   = document.getElementById('sort').value;
  const qs = status ? `?status=${encodeURIComponent(status)}` : '';
  const [list, stats] = await Promise.all([
    fetch('/api/applications' + qs).then(r => r.json()),
    fetch('/api/applications/stats').then(r => r.json())
  ]);
  renderStats(stats);
  renderList(sortApps(list.applications, sort));
}

function renderStats(s){
  const order = ['Applied','Phone Screen','Interview','Offer','Rejected'];
  const html = [`<div class="col"><div class="stat-card"><div class="n">${s.total}</div><div class="label">Total</div></div></div>`]
    .concat(order.map(st => `<div class="col"><div class="stat-card"><div class="n">${s.by_status[st]||0}</div><div class="label">${st}</div></div></div>`));
  document.getElementById('stats').innerHTML = html.join('');
}

// Allowed forward transitions per current status — for quick-action buttons
const NEXT_STATUSES = {
  'Applied':      ['Phone Screen', 'Interview', 'Rejected', 'No Response'],
  'Phone Screen': ['Interview', 'Rejected'],
  'Interview':    ['Offer', 'Rejected'],
  'Offer':        ['Rejected'],
  'Rejected':     [],
  'Withdrawn':    [],
  'No Response':  ['Rejected'],
};

function renderList(apps){
  const list = document.getElementById('list');
  const empty = document.getElementById('empty');
  if (!apps.length){ list.innerHTML=''; empty.style.display='block'; return; }
  empty.style.display='none';
  list.innerHTML = apps.map(a => {
    const cls = 'status-' + a.status.replaceAll(' ', '-');
    const dt = a.applied_at ? a.applied_at.slice(0,10) : '';
    const days = daysSince(a.applied_at);
    const isPending = a.status === 'Applied' || a.status === 'Phone Screen' || a.status === 'Interview';
    const staleCls = (isPending && days != null && days > 14) ? ' stale-border' : '';
    const meta = [a.location, a.salary, a.ats, a.source].filter(Boolean).join(' · ');
    const nexts = (NEXT_STATUSES[a.status] || []).map(s => {
      const qaCls = s === 'Rejected' ? 'qa-reject' : s === 'Offer' ? 'qa-offer' : '';
      return `<button class="${qaCls}" onclick="event.stopPropagation();quickSet(${a.id}, '${s}')">→ ${s}</button>`;
    }).join('');
    return `
      <div class="app-row${staleCls}" onclick='edit(${JSON.stringify(a)})' style="cursor:pointer">
        <div class="d-flex justify-content-between align-items-start">
          <div class="flex-grow-1">
            <div class="company">${escapeHtml(a.company)}</div>
            <div class="title">${escapeHtml(a.title)}</div>
            <div class="meta">${escapeHtml(meta)}${meta?' · ':''}${dt}${daysBadge(days)}</div>
          </div>
          <span class="status-pill ${cls}">${a.status}</span>
        </div>
        ${a.url ? `<div class="mt-2"><a href="${escapeHtml(a.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="small"><i class="bi bi-box-arrow-up-right"></i> Posting</a></div>` : ''}
        ${nexts ? `<div class="quick-actions">${nexts}</div>` : ''}
      </div>`;
  }).join('');
}

async function quickSet(id, status){
  const res = await fetch('/api/applications/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ status })
  });
  if (!res.ok){ alert('Update failed'); return; }
  load();
}

async function scanGmail(dryRun){
  const modal = new bootstrap.Modal(document.getElementById('scanModal'));
  const body = document.getElementById('scanBody');
  const statusEl = document.getElementById('scanStatus');
  body.innerHTML = '<div class="spinner-border text-primary" role="status"></div><p class="mt-2 small text-muted" id="scanStatus">Connecting to Gmail + classifying recent emails…</p>';
  modal.show();
  try {
    const res = await fetch('/api/gmail/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ dry_run: !!dryRun })
    });
    const j = await res.json();
    if (j.error){
      body.innerHTML = `<div class="alert alert-danger mb-0"><b>Error:</b> ${escapeHtml(j.error)}</div>`;
      return;
    }
    const hitsHtml = (j.hits || []).map(h =>
      `<div class="hit-row">
         <div class="from-to">${escapeHtml(h.company || '#' + h.app_id)} — ${escapeHtml(h.from)} → ${escapeHtml(h.to)}</div>
         <div class="subj">${escapeHtml(h.subject || '')}</div>
       </div>`
    ).join('') || '<p class="text-muted small mb-0">No status changes detected.</p>';
    body.innerHTML = `
      <div class="row text-center mb-3">
        <div class="col"><div class="fw-bold fs-5">${j.scanned}</div><div class="small text-muted">Scanned</div></div>
        <div class="col"><div class="fw-bold fs-5">${j.matched}</div><div class="small text-muted">Matched</div></div>
        <div class="col"><div class="fw-bold fs-5 text-success">${j.updated}</div><div class="small text-muted">Updated</div></div>
      </div>
      ${hitsHtml}`;
    load(); // refresh tracker
  } catch (e) {
    body.innerHTML = `<div class="alert alert-danger mb-0">Scan failed: ${escapeHtml(String(e))}</div>`;
  }
}

function escapeHtml(s){ return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function edit(a){
  document.getElementById('editId').value = a.id;
  document.getElementById('modalTitle').textContent = 'Edit Application';
  document.getElementById('f-company').value = a.company || '';
  document.getElementById('f-title').value = a.title || '';
  document.getElementById('f-url').value = a.url || '';
  document.getElementById('f-ats').value = a.ats || '';
  document.getElementById('f-status').value = a.status || 'Applied';
  document.getElementById('f-location').value = a.location || '';
  document.getElementById('f-salary').value = a.salary || '';
  document.getElementById('f-source').value = a.source || '';
  document.getElementById('f-notes').value = a.notes || '';
  document.getElementById('deleteBtn').style.display = 'inline-block';
  new bootstrap.Modal(document.getElementById('addModal')).show();
}

document.getElementById('addModal').addEventListener('show.bs.modal', e => {
  if (e.relatedTarget){ // opened from FAB, not from edit()
    document.getElementById('editId').value = '';
    document.getElementById('modalTitle').textContent = 'New Application';
    document.getElementById('deleteBtn').style.display = 'none';
    ['f-company','f-title','f-url','f-ats','f-location','f-salary','f-source','f-notes'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('f-status').value = 'Applied';
  }
});

async function save(){
  const id = document.getElementById('editId').value;
  const body = {
    company:  document.getElementById('f-company').value.trim(),
    title:    document.getElementById('f-title').value.trim(),
    url:      document.getElementById('f-url').value.trim(),
    ats:      document.getElementById('f-ats').value,
    status:   document.getElementById('f-status').value,
    location: document.getElementById('f-location').value.trim(),
    salary:   document.getElementById('f-salary').value.trim(),
    source:   document.getElementById('f-source').value.trim(),
    notes:    document.getElementById('f-notes').value.trim(),
  };
  if (!body.company || !body.title){ alert('Company and title are required.'); return; }
  const res = await fetch('/api/applications' + (id ? '/' + id : ''), {
    method: id ? 'PATCH' : 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!res.ok){ alert('Save failed: ' + (await res.text())); return; }
  bootstrap.Modal.getInstance(document.getElementById('addModal')).hide();
  load();
}

async function del(){
  const id = document.getElementById('editId').value;
  if (!id) return;
  if (!confirm('Delete this application?')) return;
  const res = await fetch('/api/applications/' + id, { method: 'DELETE' });
  if (!res.ok){ alert('Delete failed'); return; }
  bootstrap.Modal.getInstance(document.getElementById('addModal')).hide();
  load();
}
</script>
</body></html>"""


# ── Startup ───────────────────────────────────────────────────────────────────

def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    local_ip = _get_local_ip()
    ai_enabled = bool(os.environ.get("ANTHROPIC_API_KEY") and _anthropic)
    print("=" * 56)
    print("  Remote Job Search — Mobile PWA")
    print("=" * 56)
    print(f"  Local:    http://localhost:{port}")
    print(f"  Network:  http://{local_ip}:{port}  << open on phone")
    print("  Sources:  Apify, Adzuna, JSearch  (paid/keyed APIs only)")
    print(f"  AI (Claude Sonnet): {'Enabled — cover letters, CV parsing, job scoring' if ai_enabled else 'Disabled (set ANTHROPIC_API_KEY)'}")
    print("=" * 56)
    app.run(host="0.0.0.0", port=port, debug=False)
