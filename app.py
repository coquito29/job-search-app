"""
app.py  —  Remote Job Search Mobile PWA (Flask backend)
"""

import io
import os
import re
import socket
import json
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any

import requests
from flask import Flask, request, jsonify, render_template, send_file, Response

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except ImportError:
    pdf_extract_text = None

try:
    import docx as _docx
except ImportError:
    _docx = None

app = Flask(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

TOP_JOBS_LIMIT    = 30
FETCH_LIMIT       = 150
DEFAULT_TIMERANGE = "7d"

NO_GO_TERMS = ["cold calling", "commission only", "door to door"]

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


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class UserProfile:
    summary: str
    skills: List[str]
    no_go_terms: List[str]


# ── Core logic (verbatim from job_search_gui.py) ─────────────────────────────

def fetch_apify_jobs(skills, token, limit=150, time_range="7d"):
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
    r = requests.post(url, params={"token": token}, json=run_input, timeout=120)
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
                cur = sv.get("currency","")
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
        results.append({"title": title, "company_name": company, "location": loc,
                         "salary": sal, "description": str(desc), "url": url_val,
                         "posted": posted, "id": pick(item.get("id"), url_val)})
    return results


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
    s = str(posted).replace("Z","").replace("T"," ").strip()
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
    title = (job.get("title","") or "").lower()
    desc  = (job.get("description","") or "").lower()
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
    loc   = (job.get("location","") or "").lower()
    title = (job.get("title","") or "").lower()
    desc  = (job.get("description","") or "").lower()[:400]
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
    if not sal or sal.strip() in ("-","","None"):
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
    """Extract known skills from CV/resume text."""
    text_lower = text.lower()
    found = []
    for skill in KNOWN_SKILLS:
        if skill.lower() in text_lower and skill not in found:
            found.append(skill)
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


# ── Cover letter generator ────────────────────────────────────────────────────

def generate_cover_letter(job, skills):
    title   = job.get("title", "this position")
    company = job.get("company_name", "your company")
    desc    = (job.get("description", "") or "")[:1200]
    matched = job.get("matched_skills", skills[:8])
    skills_str = ", ".join(matched[:8]) if matched else ", ".join(skills[:8])
    salary_line = ""
    if job.get("salary"):
        salary_line = f"\n\nRegarding compensation, I noted the listed range of {job['salary']} and this aligns with my expectations."

    letter = f"""Dear Hiring Team at {company},

I am writing to express my strong interest in the {title} role. Having reviewed the job description, I am confident that my background and skills make me an excellent fit for this position.

My relevant skills include: {skills_str}. These align directly with the requirements outlined in your posting, and I am eager to bring this expertise to {company}.

{desc[:300].strip()}{"..." if len(desc) > 300 else ""}

I thrive in remote work environments and have a proven ability to collaborate effectively across distributed teams. I am detail-oriented, self-motivated, and committed to delivering high-quality results.{salary_line}

I would welcome the opportunity to discuss how my background aligns with your team's goals. Thank you for your time and consideration.

Sincerely,
[Your Name]
[Your Email]
[Your Phone]
[LinkedIn / Portfolio URL]
"""
    return letter


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config")
def config():
    """Return server-side config so the frontend can auto-fill the token."""
    token = os.environ.get("APIFY_TOKEN", "")
    return jsonify({"apify_token": token})


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

    skills = extract_skills_from_text(text)
    # Build a brief summary from the first non-empty lines
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    summary = " ".join(lines[:5])[:300] if lines else ""

    return jsonify({"skills": skills, "summary": summary, "skill_count": len(skills)})


@app.route("/api/search", methods=["POST"])
def search_jobs():
    data = request.get_json(force=True)
    token      = (data.get("token") or "").strip()
    skills     = data.get("skills") or []
    time_range = data.get("time_range") or DEFAULT_TIMERANGE

    if not token:
        return jsonify({"error": "Apify token is required"}), 400
    if not skills:
        return jsonify({"error": "At least one skill is required"}), 400

    try:
        raw_jobs = fetch_apify_jobs(skills, token, limit=FETCH_LIMIT, time_range=time_range)
    except Exception as e:
        return jsonify({"error": f"API call failed: {str(e)}"}), 502

    total_fetched = len(raw_jobs)

    # Enrich with days_old
    for job in raw_jobs:
        job["days_old"] = _days_since_posted(job.get("posted"))

    # Filter remote only
    remote_jobs = [j for j in raw_jobs if is_remote_job(j)]
    total_remote = len(remote_jobs)

    # Score and sort
    profile = UserProfile(summary="", skills=skills, no_go_terms=NO_GO_TERMS)
    scored = []
    for job in remote_jobs:
        s = score_job(job, profile)
        job.update(s)
        job["salary_clean"] = _clean_salary(job.get("salary",""))
        job["date_fmt"] = fmt_date(job.get("posted"))
        scored.append(job)

    scored.sort(key=lambda j: j["match_pct"], reverse=True)
    top = scored[:TOP_JOBS_LIMIT]

    return jsonify({
        "jobs": top,
        "total_fetched": total_fetched,
        "total_remote": total_remote,
        "total_shown": len(top),
    })


@app.route("/api/cover-letter", methods=["POST"])
def cover_letter():
    data   = request.get_json(force=True)
    job    = data.get("job") or {}
    skills = data.get("skills") or []

    text = generate_cover_letter(job, skills)

    buf = io.BytesIO(text.encode("utf-8"))
    buf.seek(0)
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title","cover_letter")).strip().replace(" ","_").lower()
    filename = f"cover_letter_{safe_title}.txt"

    return send_file(
        buf,
        mimetype="text/plain",
        as_attachment=True,
        download_name=filename,
    )


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
    print("=" * 50)
    print("  Remote Job Search - Mobile PWA")
    print("=" * 50)
    print(f"  Local:    http://localhost:{port}")
    print(f"  Network:  http://{local_ip}:{port}")
    print("  (Open the Network URL on your phone)")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
