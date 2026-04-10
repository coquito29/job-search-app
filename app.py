"""
app.py  —  Remote Job Search Mobile PWA
Sources: Remotive · RemoteOK · Jobicy · Arbeitnow · Himalayas · Apify · Adzuna · JSearch
AI Cover Letters: Claude Haiku (set ANTHROPIC_API_KEY env var)
"""
import io, json, os, re, socket, xml.etree.ElementTree as ET
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

app = Flask(__name__)

TOP_JOBS_LIMIT = 50
FETCH_LIMIT = 150
DEFAULT_TIMERANGE = "7d"
NO_GO_TERMS = ["cold calling", "commission only", "door to door"]

# Domains that require sign-in to view/apply — skip any job URL from these
SIGNIN_WALL_DOMAINS = [
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerbuilder.com", "simplyhired.com", "jobicy.com",
]

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

def fetch_remotive(skills, limit=100):
    try:
        # Remotive works best with a single short keyword — multi-word kills results
        query = skills[0] if skills else "remote"
        url = f"https://remotive.com/api/remote-jobs?search={http_req.utils.quote(query)}&limit={limit}"
        r = http_req.get(url, timeout=20)
        r.raise_for_status()
        jobs_raw = r.json().get("jobs", [])
        results = []
        for j in jobs_raw:
            sal = j.get("salary", "") or ""
            results.append({
                "url": j.get("url", ""),
                "title": j.get("title", ""),
                "company_name": j.get("company_name", ""),
                "location": j.get("candidate_required_location") or "Remote",
                "salary": sal,
                "description": j.get("description", ""),
                "posted": j.get("publication_date", ""),
                "source": "Remotive",
            })
        return results
    except Exception:
        return []


def fetch_remoteok(skills, limit=100):
    try:
        tag = skills[0].replace(" ", "-") if skills else "remote"
        url = f"https://remoteok.com/api?tag={http_req.utils.quote(tag)}"
        r = http_req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return []
        results = []
        for j in data[1:]:
            if not isinstance(j, dict):
                continue
            sal_min = j.get("salary_min")
            sal_max = j.get("salary_max")
            if sal_min and sal_max:
                sal = f"${int(sal_min)//1000}k–${int(sal_max)//1000}k"
            elif sal_min:
                sal = f"${int(sal_min)//1000}k"
            else:
                sal = ""
            results.append({
                "url": j.get("url", ""),
                "title": j.get("position", ""),
                "company_name": j.get("company", ""),
                "location": j.get("location", "Remote") or "Remote",
                "salary": sal,
                "description": j.get("description", ""),
                "posted": j.get("date", ""),
                "source": "RemoteOK",
            })
        return results
    except Exception:
        return []


def fetch_jobicy(skills, limit=50):
    try:
        # Jobicy search param is unreliable — fetch all and let scoring rank them
        url = f"https://jobicy.com/api/v2/remote-jobs?count={limit}"
        r = http_req.get(url, timeout=20)
        r.raise_for_status()
        jobs_raw = r.json().get("jobs", [])
        results = []
        for j in jobs_raw:
            sal_min = j.get("annualSalaryMin")
            sal_max = j.get("annualSalaryMax")
            if sal_min and sal_max:
                sal = f"${int(sal_min)//1000}k–${int(sal_max)//1000}k"
            elif sal_min:
                sal = f"${int(sal_min)//1000}k"
            else:
                sal = ""
            results.append({
                "url": j.get("url", ""),
                "title": j.get("jobTitle", ""),
                "company_name": j.get("companyName", ""),
                "location": j.get("jobGeo") or "Remote",
                "salary": sal,
                "description": j.get("jobDescription", ""),
                "posted": j.get("pubDate", ""),
                "source": "Jobicy",
            })
        return results
    except Exception:
        return []


def fetch_arbeitnow(limit=50):
    try:
        url = "https://www.arbeitnow.com/api/job-board-api"
        r = http_req.get(url, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
        results = []
        for j in data:
            if not isinstance(j, dict):
                continue
            if not j.get("remote", False):
                continue
            results.append({
                "url": j.get("url", ""),
                "title": j.get("title", ""),
                "company_name": j.get("company_name", ""),
                "location": j.get("location", "Remote") or "Remote",
                "salary": "",
                "description": j.get("description", ""),
                "posted": str(j.get("created_at", "")),
                "source": "Arbeitnow",
            })
        return results
    except Exception:
        return []


def fetch_himalayas(skills, limit=50):
    try:
        query = " ".join(skills[:3])
        url = f"https://himalayas.app/jobs/api?q={http_req.utils.quote(query)}&limit={limit}"
        r = http_req.get(url, timeout=20)
        r.raise_for_status()
        jobs_raw = r.json().get("jobs", [])
        results = []
        for j in jobs_raw:
            results.append({
                "url": j.get("applicationLink") or j.get("url", ""),
                "title": j.get("title", ""),
                "company_name": j.get("companyName", ""),
                "location": "Remote",
                "salary": j.get("salary", ""),
                "description": j.get("description", ""),
                "posted": j.get("createdAt", ""),
                "source": "Himalayas",
            })
        return results
    except Exception:
        return []


def fetch_adzuna(skills, app_id, app_key, limit=50):
    """Fetch from Adzuna API (free key from developer.adzuna.com) — remote only."""
    if not app_id or not app_key:
        return []
    query = " ".join(skills[:5]) + " remote"
    try:
        r = http_req.get(
            "https://api.adzuna.com/v1/api/jobs/us/search/1",
            params={
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": limit,
                "what": query,
                "where": "remote",
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
        sal = ""
        lo = j.get("salary_min")
        hi = j.get("salary_max")
        if lo and hi:
            sal = f"${int(lo)//1000}k-${int(hi)//1000}k"
        elif lo:
            sal = f"${int(lo)//1000}k+"
        results.append({
            "title": j.get("title", ""),
            "company_name": (j.get("company") or {}).get("display_name", ""),
            "location": (j.get("location") or {}).get("display_name", "Remote"),
            "salary": sal,
            "description": j.get("description", ""),
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


def fetch_apify_jobs(skills, token, limit=150, time_range="7d"):
    if not token:
        return []
    run_input = {
        "timeRange": time_range,
        "limit": int(max(10, min(limit, 5000))),
        "includeAi": True,
        "includeLinkedIn": True,
        "aiWorkArrangementFilter": ["Remote OK", "Remote Solely"],
        "titleSearch": skills[:10],
        "descriptionSearch": skills[:10],
    }
    url = ("https://api.apify.com/v2/acts/fantastic-jobs~career-site-job-listing-api"
           "/run-sync-get-dataset-items")
    r = http_req.post(url, params={"token": token}, json=run_input, timeout=120)
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


def fetch_themuse(skills, limit=50):
    """Fetch from The Muse API (free, no auth required) — remote only."""
    try:
        r = http_req.get(
            "https://www.themuse.com/api/public/jobs",
            params={"page": 0, "level": "Entry Level", "location": "Flexible / Remote"},
            timeout=20,
        )
        r.raise_for_status()
        results = []
        for j in r.json().get("results", [])[:limit]:
            locs = j.get("locations", [])
            loc  = locs[0].get("name", "Remote") if locs else "Remote"
            # Double-check: skip if location has no "remote" or "flexible" hint
            if locs and not any("remote" in l.get("name","").lower() or
                                 "flexible" in l.get("name","").lower()
                                 for l in locs):
                continue
            url  = (j.get("refs") or {}).get("landing_page", "")
            if not url:
                continue
            results.append({
                "url": url,
                "title": j.get("name", ""),
                "company_name": (j.get("company") or {}).get("name", ""),
                "location": loc,
                "salary": "",
                "description": re.sub(r"<[^>]+>", " ", j.get("contents", "") or ""),
                "posted": j.get("publication_date", ""),
                "source": "TheMuse",
            })
        return results
    except Exception:
        return []


def fetch_weworkremotely(skills, limit=50):
    """Fetch from We Work Remotely RSS feed (free, no auth)."""
    try:
        r = http_req.get(
            "https://weworkremotely.com/remote-jobs.rss",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        r.raise_for_status()
        root    = ET.fromstring(r.content)
        channel = root.find("channel")
        if channel is None:
            return []
        skill_lower = [s.lower() for s in skills]
        results = []
        for item in channel.findall("item"):
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link") or "").strip()
            desc    = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip()
            pub     = (item.findtext("pubDate") or "").strip()
            company = ""
            if ": " in title:
                parts   = title.split(": ", 1)
                company = parts[0].strip()
                title   = parts[1].strip()
            if not link:
                continue
            combo = (title + " " + desc).lower()
            if skill_lower and not any(s in combo for s in skill_lower):
                continue
            results.append({
                "url": link,
                "title": title,
                "company_name": company,
                "location": "Remote",
                "salary": "",
                "description": desc,
                "posted": pub,
                "source": "WeWorkRemotely",
            })
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


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
    total = skill_score + ratio_bonus + exp_bonus + fresh_bonus + hire_bonus
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
    if not reasons:
        reasons.append("Keyword match")
    return {
        "match_pct": pct,
        "match_why": " · ".join(reasons),
        "matched_skills": unique,
        "hire_signals": hire_labels,
        "years_req": years_req,
        "days_old": days_old,
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
    """Return server-side config so the frontend can auto-fill the token and check AI."""
    token = os.environ.get("APIFY_TOKEN", "")
    ai_enabled = bool(os.environ.get("ANTHROPIC_API_KEY") and _anthropic)
    return jsonify({
        "apify_token": token,
        "ai_enabled": ai_enabled,
        "adzuna_enabled": bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")),
        "jsearch_enabled": bool(os.environ.get("RAPIDAPI_KEY")),
    })


@app.route("/api/status")
def status():
    """Visual status page — open this in your browser to check all sources."""
    sources = {
        "Remotive":  {"free": True,  "ok": None},
        "RemoteOK":  {"free": True,  "ok": None},
        "Jobicy":    {"free": True,  "ok": None},
        "Arbeitnow": {"free": True,  "ok": None},
        "Himalayas": {"free": True,  "ok": None},
        "Apify":     {"free": False, "ok": bool(os.environ.get("APIFY_TOKEN"))},
        "Adzuna":    {"free": False, "ok": bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))},
        "JSearch":   {"free": False, "ok": bool(os.environ.get("RAPIDAPI_KEY"))},
        "Reed":      {"free": False, "ok": bool(os.environ.get("REED_API_KEY"))},
    }
    # Quick ping test for the free sources
    ping_urls = {
        "Remotive":  "https://remotive.com/api/remote-jobs?limit=1",
        "RemoteOK":  "https://remoteok.com/api?limit=1",
        "Jobicy":    "https://jobicy.com/api/v2/remote-jobs?count=1",
        "Arbeitnow": "https://www.arbeitnow.com/api/job-board-api",
        "Himalayas": "https://himalayas.app/jobs/api?limit=1",
    }
    for name, url in ping_urls.items():
        try:
            r = http_req.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            sources[name]["ok"] = r.status_code == 200
        except Exception:
            sources[name]["ok"] = False

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
    token      = (data.get("token") or "").strip()
    skills     = data.get("skills") or []
    time_range = data.get("time_range") or DEFAULT_TIMERANGE
    sources    = data.get("sources") or ["remotive","remoteok","arbeitnow","himalayas","apify","themuse","weworkremotely"]

    if not skills:
        return jsonify({"error": "At least one skill is required"}), 400

    # Build fetch tasks based on selected sources
    fetch_tasks = {}
    if "remotive" in sources:
        fetch_tasks["remotive"] = lambda: fetch_remotive(skills, limit=100)
    if "remoteok" in sources:
        fetch_tasks["remoteok"] = lambda: fetch_remoteok(skills, limit=100)
    if "jobicy" in sources:
        fetch_tasks["jobicy"] = lambda: fetch_jobicy(skills, limit=50)
    if "arbeitnow" in sources:
        fetch_tasks["arbeitnow"] = lambda: fetch_arbeitnow(limit=50)
    if "himalayas" in sources:
        fetch_tasks["himalayas"] = lambda: fetch_himalayas(skills, limit=50)
    if "apify" in sources and token:
        fetch_tasks["apify"] = lambda: fetch_apify_jobs(skills, token, limit=FETCH_LIMIT, time_range=time_range)
    adzuna_id  = os.environ.get("ADZUNA_APP_ID", "")
    adzuna_key = os.environ.get("ADZUNA_APP_KEY", "")
    if "adzuna" in sources and adzuna_id and adzuna_key:
        fetch_tasks["adzuna"] = lambda: fetch_adzuna(skills, adzuna_id, adzuna_key, limit=50)
    rapidapi_key = os.environ.get("RAPIDAPI_KEY", "")
    if "jsearch" in sources and rapidapi_key:
        fetch_tasks["jsearch"] = lambda: fetch_jsearch(skills, rapidapi_key, limit=50)
    if "themuse" in sources:
        fetch_tasks["themuse"] = lambda: fetch_themuse(skills, limit=50)
    if "weworkremotely" in sources:
        fetch_tasks["weworkremotely"] = lambda: fetch_weworkremotely(skills, limit=50)

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
            if any(d in url for d in SIGNIN_WALL_DOMAINS):
                continue  # skip Indeed, LinkedIn, Glassdoor etc.
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
    print("  Sources:  Remotive, RemoteOK, Jobicy, Arbeitnow")
    print("            Himalayas, Apify, TheMuse, WeWorkRemotely")
    print(f"  AI (Claude Sonnet): {'Enabled — cover letters, CV parsing, job scoring' if ai_enabled else 'Disabled (set ANTHROPIC_API_KEY)'}")
    print("=" * 56)
    app.run(host="0.0.0.0", port=port, debug=False)
