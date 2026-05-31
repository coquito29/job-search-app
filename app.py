"""
app.py  —  Remote Job Search Mobile PWA
Sources: Apify · Adzuna · JSearch  (paid/keyed APIs only — free sources removed for signal quality)
AI Cover Letters: Claude Haiku (set ANTHROPIC_API_KEY env var)
"""
import io, json, os, re, secrets, socket, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Any

import requests as http_req
from flask import Flask, request, jsonify, render_template, send_file, session
from werkzeug.security import generate_password_hash, check_password_hash

# Billing helpers live in their own module so app.py stays focused on
# search/AI/applications logic. billing.py is import-safe even when the
# `stripe` SDK or env vars are missing (it degrades to no-op).
from billing import (
    TIERS as _BILLING_TIERS,
    stripe_ready as _stripe_ready,
    stripe_sdk as _stripe_sdk,
    check_quota as _billing_check_quota,
    increment_quota as _billing_increment_quota,
    get_user as _billing_get_user,
    create_checkout_session as _billing_create_checkout,
    create_portal_session as _billing_create_portal,
    apply_subscription_event as _billing_apply_event,
)

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
# Signed session cookie. Set FLASK_SECRET_KEY in Render env vars so sessions
# survive deploys; otherwise we burn a random key per process (dev only).
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)
# Auto-reload templates so editing index.html doesn't require a server restart.
# Cheap (mtime check) and harmless in production.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Cookie policy for the Chrome extension. A chrome-extension:// origin counts
# as cross-site, so the session cookie needs SameSite=None; Secure to flow.
# Render's HTTPS edge satisfies Secure; local HTTP dev keeps the Lax default.
if os.environ.get("RENDER") or os.environ.get("FLASK_SAMESITE_NONE") == "1":
    app.config["SESSION_COOKIE_SAMESITE"] = "None"
    app.config["SESSION_COOKIE_SECURE"]   = True

# Routes that the Chrome extension hits cross-origin. Preflights and credentials
# need a precise Allow-Origin (reflected from the request) rather than '*'.
_CORS_PATHS = ("/api/auth/login", "/api/auth/logout", "/api/auth/status",
               "/api/profile/full")

@app.after_request
def _extension_cors(resp):
    origin = request.headers.get("Origin", "")
    if origin.startswith("chrome-extension://") and request.path in _CORS_PATHS:
        resp.headers["Access-Control-Allow-Origin"]      = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Vary"]                             = "Origin"
        resp.headers["Access-Control-Allow-Methods"]     = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"]     = "Content-Type"
    return resp

@app.route("/api/auth/login",   methods=["OPTIONS"])
@app.route("/api/auth/logout",  methods=["OPTIONS"])
@app.route("/api/auth/status",  methods=["OPTIONS"])
@app.route("/api/profile/full", methods=["OPTIONS"])
def _extension_preflight():
    return ("", 204)

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
    id_col   = "id SERIAL PRIMARY KEY" if USE_POSTGRES else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    blob_col = "BYTEA" if USE_POSTGRES else "BLOB"
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
        # Single-user now, multi-user later. user_key="default" for the only seat;
        # passcode_hash is nullable until the user sets one.
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                {id_col},
                user_key      TEXT UNIQUE NOT NULL,
                passcode_hash TEXT,
                created_at    TEXT NOT NULL
            )
        """)
        # CV library: stores the file blob so it survives Render redeploys.
        # category drives auto-pick: 'it', 'cybersecurity', 'bartender',
        # 'casino', 'hospitality', 'general'. is_default = the fallback CV.
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS cvs (
                {id_col},
                user_id      INTEGER NOT NULL,
                filename     TEXT NOT NULL,
                category     TEXT NOT NULL DEFAULT 'general',
                mime_type    TEXT NOT NULL,
                file_blob    {blob_col} NOT NULL,
                parsed_text  TEXT,
                ai_summary   TEXT,
                ai_skills    TEXT,
                is_default   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL
            )
        """)
        # Per-user profile: replaces browser-localStorage so profile follows
        # the passcode across devices. settings is a free-form JSON blob.
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS profiles (
                {id_col},
                user_id     INTEGER UNIQUE NOT NULL,
                summary     TEXT,
                skills      TEXT,
                settings    TEXT,
                updated_at  TEXT NOT NULL
            )
        """)
        # Daily digest snapshot: cron-triggered /api/digest writes the top
        # scored jobs here so the frontend can render "Today's Matches" on
        # boot without re-running Apify ($1.20/search). One row per run.
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS daily_searches (
                {id_col},
                user_id        INTEGER NOT NULL,
                run_at         TEXT NOT NULL,
                jobs           TEXT NOT NULL,
                source_counts  TEXT,
                total_fetched  INTEGER NOT NULL DEFAULT 0,
                emailed_to     TEXT
            )
        """)

    # Idempotent migrations. Each in its own connection so a "column exists"
    # error doesn't poison the broader CREATE TABLE transaction on Postgres.
    for ddl in (
        "ALTER TABLE applications ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE applications ADD COLUMN follow_up_sent_at TEXT",
        "ALTER TABLE applications ADD COLUMN last_contact_email TEXT",
        # Billing columns. Free tier is default; Stripe webhook flips users to
        # 'pro' / 'autopilot' on successful checkout, back to 'free' on cancel.
        # is_founder=1 bypasses all quota checks (set via FOUNDER_USER_KEYS env).
        "ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'free'",
        "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT",
        "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT",
        "ALTER TABLE users ADD COLUMN subscription_status TEXT",
        "ALTER TABLE users ADD COLUMN current_period_end TEXT",
        "ALTER TABLE users ADD COLUMN monthly_quota_used INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN quota_reset_at TEXT",
        "ALTER TABLE users ADD COLUMN is_founder INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            with _db_conn() as conn:
                conn.execute(ddl)
        except Exception:
            pass  # column already exists, fine

    # Seed the default user if missing
    with _db_conn() as conn:
        cur = conn.execute("SELECT id FROM users WHERE user_key = ?", ("default",))
        if not cur.fetchone():
            conn.execute(
                "INSERT INTO users (user_key, passcode_hash, created_at) VALUES (?, ?, ?)",
                ("default", None, datetime.utcnow().isoformat()),
            )

    # Founder allowlist: comma-separated user_key values that get unlimited free
    # access. George's "default" user_key is whitelisted automatically so the
    # paywall never blocks him on his own deployed app. Must run AFTER the
    # seed above so a fresh DB still flips the freshly-inserted default user.
    _founder_keys = os.environ.get("FOUNDER_USER_KEYS", "default").strip()
    if _founder_keys:
        for k in [s.strip() for s in _founder_keys.split(",") if s.strip()]:
            try:
                with _db_conn() as conn:
                    conn.execute(
                        "UPDATE users SET is_founder = 1 WHERE user_key = ?", (k,)
                    )
            except Exception:
                pass


_init_applications_db()
print(f"[db] Applications tracker backend: {'Postgres' if USE_POSTGRES else 'SQLite (' + APPLICATIONS_DB + ')'}")


def _row_get(row, key, default=None):
    """sqlite3.Row supports row[key] but not row.get(); RealDictCursor returns
    real dicts. Normalise both to a single .get-style accessor."""
    if row is None:
        return default
    try:
        v = row[key]
        return default if v is None else v
    except (KeyError, IndexError):
        return default


def _default_user():
    """Return (id, passcode_hash) for the seeded default user, or (None, None)."""
    with _db_conn() as conn:
        cur = conn.execute(
            "SELECT id, passcode_hash FROM users WHERE user_key = ?", ("default",)
        )
        row = cur.fetchone()
    if not row:
        return (None, None)
    return (_row_get(row, "id"), _row_get(row, "passcode_hash"))


def _current_user_id():
    """Active user id, or None if the app is locked and not logged in.
    Auth model:
      - If a session uid is set, that wins (logged in).
      - Else if no passcode is set on the default user, return the default
        id (legacy unlocked mode — keeps existing deployments working).
      - Else None (forces /api/auth/login).
    Multi-user (path b) replaces this logic but the call sites stay identical."""
    uid = session.get("uid")
    if uid:
        return int(uid)
    default_id, passcode_hash = _default_user()
    if not passcode_hash:
        return default_id
    return None


def _auth_required():
    """For endpoints that should 401 instead of silently using the default."""
    uid = _current_user_id()
    if uid is None:
        return None, (jsonify({"error": "Locked. Log in with your passcode."}), 401)
    return uid, None

TOP_JOBS_LIMIT = 50
FETCH_LIMIT = 100  # per Apify search — ~$1.20/search at $0.012/job (opt-in from UI)
DEFAULT_TIMERANGE = "7d"
NO_GO_TERMS = ["cold calling", "commission only", "door to door"]

# Domains that require sign-in to view/apply — skip any job URL from these
SIGNIN_WALL_DOMAINS = [
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerbuilder.com", "simplyhired.com", "jobicy.com",
]

# Back-compat shim: the search endpoint reads AGGREGATOR_DENYLIST when filtering.
# The authoritative list now lives in ATS_BLOCKED below; this just wraps the keys.
def _aggregator_denylist():
    return list(ATS_BLOCKED.keys())

# ── ATS triage ───────────────────────────────────────────────────────────────
# Three classes, derived from George's actual application logs:
#   "fast"   = clean Easy-Apply, no account wall, no OTP loop. Submit in <5 min.
#   "walled" = usable but slow — account creation, OTP, multi-step flows,
#              short session timeouts. Worth it for high-match jobs only.
#   "blocked"= aggregators that Cloudflare-block, force signup, or hide the
#              real employer. Skip entirely.
# Anything else stays "unknown" and renders neutral.
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
    # Added 2026-05-06 after dogfood test: daily digest was returning
    # ~70% of results from these redirect-style aggregators. Real ATS
    # links (Greenhouse/Workable/SmartRecruiters) drown out the noise
    # once these get filtered.
    "jooble.org":       "Aggregator",
    "dailyremote.com":  "Aggregator",
    "tallo.com":        "Aggregator",
    "himalayas.app":    "Aggregator",  # job board, but most listings redirect
    "apexsystems.com":  "Aggregator",  # staffing agency w/ low-conversion direct apply
    "socalnonprofitjobs.org": "Aggregator",
    "remoterocketship.com":   "Aggregator",
}


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


# Back-compat: a few callers still want a quick "is this any direct ATS?" check
ATS_BOOST_DOMAINS = list(ATS_FAST.keys()) + list(ATS_WALLED.keys())


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
    # ATS triage: fast = +18, walled = +4 (still doable), unknown = 0,
    # blocked = -50 (effectively buries the result; search-stage filter
    # should already drop these but we keep the score honest)
    ats_class, ats_name = _classify_ats(job.get("url", ""))
    ats_bonus = {"fast": 18, "walled": 4, "unknown": 0, "blocked": -50}[ats_class]

    # Penalties for things George structurally cannot take
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

    # Language-requirement detection: George speaks EN + ES. Any job that
    # REQUIRES a different language (French, Italian, Portuguese, German,
    # Mandarin, etc.) is structurally a no-go. Catches a variety of phrasings:
    #   "Bilingual French/English", "Fluent Italian and French",
    #   "Portuguese-speaking", "must speak German", "French required", etc.
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
        # Patterns that signal the language is REQUIRED (not just nice-to-have).
        # Word-boundaries on either side prevent "german" matching "germane".
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
            break   # one language penalty is enough — don't stack

    # Boosts for things directly in George's profile
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
        "is_ats":         ats_class in ("fast", "walled"),  # back-compat
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


#: Map near-synonyms to a single canonical form so the saved skill list
#: doesn't show 'network' AND 'networking', or 'help desk' AND 'helpdesk'.
SKILL_SYNONYMS = {
    "networking":       "network",
    "helpdesk":         "help desk",
    "customer support": "customer service",
    "node":             "node.js",
    "nodejs":           "node.js",
    "ts":               "typescript",
    "ms office":        "microsoft office",
    "m365":             "office 365",
    "powerbi":          "power bi",
    "voice over ip":    "voip",
    "voice-over-ip":    "voip",
}

#: Short or ambiguous skill tokens that false-positive on common English
#: words ('go' on 'go above', 'r' on 'a R&D role', 'ruby' as a name).
#: These ONLY count when one of the listed anchor phrases is in the
#: text — no "X plain mentions is enough" fallback, because the prose
#: false-positive rate dominates the real signal. To list 'go' as a
#: skill the CV needs to say 'golang' or 'go programming' explicitly.
#:
#: Less ambiguous but still risky tokens ('java', 'php', 'c#', 'c++')
#: have anchors AND a fallback of ≥2 hits — they rarely false-positive
#: in normal prose so two standalone mentions is decent signal.
_STRICT_ANCHOR_SKILLS = {
    "go":   ("golang", "go programming", "go (programming"),
    "r":    ("r programming", "rstudio", "r and python", "r for data"),
    "ruby": ("ruby on rails", "rubyonrails", ".rb ", " .rb", "ruby developer"),
}
_LENIENT_ANCHOR_SKILLS = {
    "java": ("java se", "java ee", "java 8", "java 11", "java 17",
             "spring boot", "core java", "java developer"),
    "php":  (" .php", "php developer", "laravel", "symfony", "wordpress"),
    "c#":   ("c# developer", "dotnet", ".net"),
    "c++":  ("c++ developer", "modern c++"),
}

def _canonical_skill(skill_lower):
    return SKILL_SYNONYMS.get(skill_lower, skill_lower)

def extract_skills_from_text(text, max_skills=25):
    """Extract known skills from CV/resume text.

    Improvements over the v1 substring-match implementation:

    * Real word boundaries (`\\b`) — 'java' no longer matches 'javascript'.
    * Longest-first scan — multi-word skills like 'help desk',
      'active directory', 'microsoft office' match before any of their
      single-word components. Matched character spans are masked out so
      a shorter skill can't re-match what a longer one already claimed.
    * Synonym collapse via SKILL_SYNONYMS so 'network' and 'networking'
      contribute to one canonical entry.
    * Ambiguous short tokens (`go`, `ruby`, `java`, `php`, `r`, `c#`,
      `c++`) require either an explicit context anchor (e.g. 'golang',
      '.rb', 'java developer') or 2+ standalone mentions before they
      count. Without this 'go' fires on every 'going' / 'ago' in prose.
    * Hard cap at `max_skills` — defaults to 25 instead of unbounded.

    Returns a list of canonical lowercase skill names in roughly
    relevance order: explicit-anchor matches first, then frequency-
    ranked plain matches.
    """
    if not text:
        return []
    text_lower = text.lower()
    # Iterate longest-first so multi-word skills win over their tokens.
    sorted_skills = sorted(KNOWN_SKILLS, key=lambda s: (-len(s), s))
    # Mask is a mutable copy of text_lower where matched spans get
    # replaced with spaces — keeps offsets stable for later regexes.
    mask_chars = list(text_lower)

    found = {}      # canonical name -> {"hits": int, "anchored": bool}

    def _record(canonical, hits, anchored):
        rec = found.setdefault(canonical, {"hits": 0, "anchored": False})
        rec["hits"]    += hits
        rec["anchored"] = rec["anchored"] or anchored

    for skill in sorted_skills:
        skill_l = skill.lower()
        # Build a word-boundary-anchored regex. Escape regex metacharacters
        # but allow flexible internal whitespace so 'help desk' matches
        # 'help  desk' and 'help-desk'.
        escaped = re.escape(skill_l).replace(r"\ ", r"[\s\-]+")
        try:
            pat = re.compile(rf"(?:(?<=^)|(?<=\W)){escaped}(?=\W|$)", re.IGNORECASE)
        except re.error:
            continue
        # Search against the current MASKED text so a shorter skill can't
        # claim characters a longer one already grabbed.
        masked = "".join(mask_chars)
        spans = [m.span() for m in pat.finditer(masked)]
        if not spans:
            continue
        # Check ambiguity rules. Strict-anchor skills (go, r, ruby) are
        # so noisy in prose that we require an anchor phrase or skip.
        # Lenient-anchor skills (java, php, c#, c++) also accept ≥2
        # plain hits since standalone occurrences are unlikely outside
        # programming contexts.
        anchored = False
        strict_anchors = _STRICT_ANCHOR_SKILLS.get(skill_l)
        if strict_anchors is not None:
            anchored = any(a in text_lower for a in strict_anchors)
            if not anchored:
                continue
        else:
            lenient_anchors = _LENIENT_ANCHOR_SKILLS.get(skill_l)
            if lenient_anchors is not None:
                anchored = any(a in text_lower for a in lenient_anchors)
                if not anchored and len(spans) < 2:
                    continue
        # Mask out the matched spans before the next iteration.
        for start, end in spans:
            for i in range(start, end):
                mask_chars[i] = " "
        canonical = _canonical_skill(skill_l)
        _record(canonical, hits=len(spans), anchored=anchored)

    if not found:
        return []

    # Rank: anchored matches first, then by hit count, then alphabetically.
    ranked = sorted(
        found.items(),
        key=lambda kv: (-int(kv[1]["anchored"]), -kv[1]["hits"], kv[0]),
    )
    return [name for name, _ in ranked[:max_skills]]


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

def generate_cover_letter(job, skills, resume_summary="", cv_text=""):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and _anthropic:
        try:
            return _generate_cover_letter_ai(job, skills, api_key, resume_summary, cv_text)
        except Exception:
            pass
    return _generate_cover_letter_template(job, skills)


def _generate_cover_letter_ai(job, skills, api_key, resume_summary="", cv_text=""):
    client = _anthropic.Anthropic(api_key=api_key)
    desc_excerpt = re.sub(r"<[^>]+>", "", job.get("description", "") or "")[:2000]
    matched = job.get("matched_skills", skills[:10])
    skills_str = ", ".join(matched[:10]) if matched else ", ".join(skills[:10])
    summary_line = f"\nMy Background: {resume_summary}" if resume_summary else ""
    # When we have the actual CV, give the model real bullet points to draw
    # from instead of letting it invent details from skill keywords alone.
    cv_block = ""
    if cv_text:
        cv_excerpt = re.sub(r"\s+", " ", cv_text[:3000]).strip()
        cv_block = f"\n\nMY RESUME (excerpt — quote real experience, do NOT invent):\n{cv_excerpt}"
    prompt = f"""You are an expert career coach writing a highly personalized, compelling cover letter.

CANDIDATE PROFILE:
Skills: {skills_str}{summary_line}{cv_block}

JOB POSTING:
Title: {job.get('title', 'the position')}
Company: {job.get('company_name', 'the company')}
Description:
{desc_excerpt}

Write a 3-paragraph cover letter that:
1. Opening: Shows genuine enthusiasm for THIS specific role. Reference something concrete from the job description (a tool, responsibility, or company mission).
2. Middle: Connects 2-3 of my actual experiences (from MY RESUME above) to specific requirements in the job description. Use brief, concrete examples that demonstrate real value. Quote real employers/tools from the resume — never invent.
3. Closing: Confident call to action. Express readiness to contribute immediately.

Start with "Dear Hiring Team," and end with "Sincerely,\\n[Your Name]\\n[Your Email]\\n[Your Phone]"
Rules: No generic filler phrases ("I am excited to apply", "I believe I am a great fit"). Be direct, specific, and professional. Reference actual job requirements by name. Only mention facts that appear in MY RESUME — if uncertain, omit rather than fabricate."""
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
    """Return server-side config booleans so the frontend can light up source
    badges and step indicators. Raw API keys are NEVER returned — the server
    uses its own env vars when the client doesn't supply one (see
    /api/search), so there's no reason to send secrets to the browser."""
    apify_token  = os.environ.get("APIFY_TOKEN", "")
    rapidapi_key = os.environ.get("RAPIDAPI_KEY", "")
    adzuna_id    = os.environ.get("ADZUNA_APP_ID", "")
    adzuna_key   = os.environ.get("ADZUNA_APP_KEY", "")
    return jsonify({
        "ai_enabled":         bool(os.environ.get("ANTHROPIC_API_KEY") and _anthropic),
        "apify_token_set":    bool(apify_token),
        "rapidapi_key_set":   bool(rapidapi_key),
        "adzuna_id_set":      bool(adzuna_id),
        "adzuna_key_set":     bool(adzuna_key),
        "adzuna_enabled":     bool(adzuna_id and adzuna_key),
        "jsearch_enabled":    bool(rapidapi_key),
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


# ── Auth (single-user passcode) ──────────────────────────────────────────────
# Designed so swapping in real multi-user auth later just replaces these four
# routes. Every other endpoint already calls _auth_required() / _current_user_id().

@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    _, passcode_hash = _default_user()
    return jsonify({
        "has_passcode": bool(passcode_hash),
        "logged_in":    bool(session.get("uid")),
    })


@app.route("/api/auth/init", methods=["POST"])
def auth_init():
    """Set the very first passcode. Refuses if one is already set
    (use /api/auth/change for that)."""
    data = request.get_json(force=True) or {}
    passcode = (data.get("passcode") or "").strip()
    if len(passcode) < 4:
        return jsonify({"error": "Passcode must be at least 4 characters"}), 400
    uid, existing = _default_user()
    if existing:
        return jsonify({"error": "Passcode already set. Use change instead."}), 409
    pw_hash = generate_password_hash(passcode)
    with _db_conn() as conn:
        conn.execute("UPDATE users SET passcode_hash = ? WHERE id = ?", (pw_hash, uid))
    session.permanent = True
    session["uid"] = uid
    return jsonify({"ok": True, "uid": uid})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True) or {}
    passcode = (data.get("passcode") or "").strip()
    uid, pw_hash = _default_user()
    if not pw_hash:
        return jsonify({"error": "No passcode set yet"}), 400
    if not passcode or not check_password_hash(pw_hash, passcode):
        return jsonify({"error": "Wrong passcode"}), 401
    session.permanent = True
    session["uid"] = uid
    return jsonify({"ok": True, "uid": uid})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("uid", None)
    return jsonify({"ok": True})


@app.route("/api/auth/change", methods=["POST"])
def auth_change():
    data = request.get_json(force=True) or {}
    current = (data.get("current") or "").strip()
    new_pc  = (data.get("new") or "").strip()
    if len(new_pc) < 4:
        return jsonify({"error": "New passcode must be at least 4 characters"}), 400
    uid, pw_hash = _default_user()
    if pw_hash and not check_password_hash(pw_hash, current):
        return jsonify({"error": "Current passcode is wrong"}), 401
    with _db_conn() as conn:
        conn.execute("UPDATE users SET passcode_hash = ? WHERE id = ?",
                     (generate_password_hash(new_pc), uid))
    session.permanent = True
    session["uid"] = uid
    return jsonify({"ok": True})


# ── Billing (Stripe checkout, webhook, status, portal) ───────────────────────
# Every route returns a clear error when Stripe isn't configured, so the
# rest of the app keeps working without billing env vars set (useful for
# local dev and as a graceful fallback if Stripe is down).

def _base_url():
    """Best-guess of our public origin for building Stripe redirect URLs.
    Honors X-Forwarded-Proto (Render sets this) and falls back to request.url_root."""
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    host  = request.headers.get("X-Forwarded-Host",  request.host)
    if proto and host:
        return f"{proto}://{host}"
    return request.url_root.rstrip("/")


@app.route("/api/billing/status", methods=["GET"])
def billing_status():
    """Return the current user's tier + quota usage. Public — works while
    locked too (returns 'free' tier so the landing/login page can show
    pricing without forcing a login)."""
    uid = _current_user_id()
    if uid is None:
        # Anonymous/locked view: just expose tier pricing for the pricing UI
        return jsonify({
            "logged_in":  False,
            "tier":       "free",
            "is_founder": False,
            "tiers":      _BILLING_TIERS,
            "stripe_ready": _stripe_ready(),
            "publishable_key": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        })
    user = _billing_get_user(_db_conn, uid)
    if user is None:
        return jsonify({"error": "User not found"}), 404
    # Run a quota check just to ensure the meter is rolled over if the
    # window has elapsed — caller may rely on `used` being current.
    _, info = _billing_check_quota(_db_conn, uid, feature="cover_letter")
    return jsonify({
        "logged_in":            True,
        "tier":                 user["tier"],
        "is_founder":           user["is_founder"],
        "subscription_status":  user["subscription_status"],
        "current_period_end":   user["current_period_end"],
        "cover_letter_used":    info.get("used", 0),
        "cover_letter_limit":   info.get("limit"),
        "cover_letter_remaining": info.get("remaining"),
        "quota_reset_at":       info.get("reset_at", ""),
        "tiers":                _BILLING_TIERS,
        "stripe_ready":         _stripe_ready(),
        "publishable_key":      os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
    })


@app.route("/api/billing/checkout", methods=["POST"])
def billing_checkout():
    """Create a Stripe Checkout Session and return its URL. Body: {tier: "pro"|"autopilot"}."""
    uid, err = _auth_required()
    if err: return err
    if not _stripe_ready():
        return jsonify({"error": "Billing is not configured on this server."}), 503
    data = request.get_json(force=True) or {}
    tier = (data.get("tier") or "").strip().lower()
    url, e = _billing_create_checkout(_db_conn, uid, tier, _base_url(),
                                       email=data.get("email"))
    if e:
        return jsonify({"error": e}), 400
    return jsonify({"url": url})


@app.route("/api/billing/portal", methods=["POST"])
def billing_portal():
    """Return a Stripe Billing Portal URL so the user can manage their sub."""
    uid, err = _auth_required()
    if err: return err
    if not _stripe_ready():
        return jsonify({"error": "Billing is not configured on this server."}), 503
    url, e = _billing_create_portal(_db_conn, uid, _base_url())
    if e:
        return jsonify({"error": e}), 400
    return jsonify({"url": url})


@app.route("/api/billing/webhook", methods=["POST"])
def billing_webhook():
    """Stripe webhook receiver. Verifies signature, dispatches to
    apply_subscription_event. Stripe retries on non-2xx so we MUST return
    200 even if the event was a no-op (otherwise we get duplicate fires)."""
    sdk = _stripe_sdk()
    if sdk is None:
        # No Stripe config means we shouldn't even be receiving webhooks.
        # Returning 200 prevents a retry storm if a misconfigured Stripe
        # dashboard points at us with the SDK absent.
        return jsonify({"ok": True, "skipped": "stripe not configured"})
    payload   = request.get_data(as_text=False)
    sig       = request.headers.get("Stripe-Signature", "")
    secret    = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if secret:
        try:
            event = sdk.Webhook.construct_event(payload, sig, secret)
        except Exception as e:
            return jsonify({"error": f"Invalid signature: {e}"}), 400
    else:
        # No secret configured — accept unverified events but log loudly.
        # Render dev environments sometimes run without the secret before
        # the user creates the Stripe webhook endpoint.
        try:
            event = json.loads(payload.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Bad JSON"}), 400
        print("[billing] WARNING: STRIPE_WEBHOOK_SECRET not set — event unverified")
    note = _billing_apply_event(_db_conn, event)
    print(f"[billing] webhook {event.get('type','?')}: {note}")
    return jsonify({"ok": True, "note": note})


@app.route("/billing")
def billing_page():
    """Simple HTML billing/pricing page. Renders `billing.html` if the template
    exists (the landing-page session will likely add this); otherwise serves a
    minimal inline fallback so the route never 404s while UI is in flight."""
    try:
        return render_template("billing.html", tiers=_BILLING_TIERS)
    except Exception:
        # Inline fallback — purely functional, the landing session will
        # replace this with proper styling.
        rows = ""
        for key, t in _BILLING_TIERS.items():
            price = "Free" if t["price_monthly"] == 0 else f"${t['price_monthly']}/mo"
            cl    = "Unlimited" if t["cover_letters_month"] is None else f"{t['cover_letters_month']} / month"
            aa    = "Yes" if t["auto_apply"] else "—"
            btn   = ("" if key == "free"
                     else f"<button onclick=\"checkout('{key}')\">Get {t['label']}</button>")
            rows += (f"<tr><td><b>{t['label']}</b></td><td>{price}</td>"
                     f"<td>{cl}</td><td>{aa}</td><td>{btn}</td></tr>")
        return f"""<!DOCTYPE html><html><head><meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Billing — Job Search App</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:16px}}
  table{{width:100%;border-collapse:collapse}}
  th,td{{padding:10px 12px;border-bottom:1px solid #ddd;text-align:left}}
  button{{background:#0d6efd;color:white;border:0;padding:8px 14px;border-radius:6px;cursor:pointer}}
</style></head><body>
<h1>Pricing</h1>
<table><tr><th>Plan</th><th>Price</th><th>Cover letters</th><th>Auto-apply</th><th></th></tr>
{rows}
</table>
<p><a href="/">← Back to app</a> · <button onclick="portal()">Manage subscription</button></p>
<script>
async function checkout(tier){{
  const r=await fetch('/api/billing/checkout',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tier}}),credentials:'include'}});
  const d=await r.json(); if(d.url) location=d.url; else alert(d.error||'Error');
}}
async function portal(){{
  const r=await fetch('/api/billing/portal',{{method:'POST',credentials:'include'}});
  const d=await r.json(); if(d.url) location=d.url; else alert(d.error||'Error');
}}
</script></body></html>"""


# ── Profile (cross-device sync of skills / summary / settings) ───────────────

@app.route("/api/profile", methods=["GET"])
def profile_get():
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        cur = conn.execute(
            "SELECT summary, skills, settings, updated_at FROM profiles WHERE user_id = ?",
            (uid,),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"summary": "", "skills": [], "settings": {},
                        "updated_at": None})
    skills_raw   = _row_get(row, "skills")
    settings_raw = _row_get(row, "settings")
    return jsonify({
        "summary":    _row_get(row, "summary", "") or "",
        "skills":     json.loads(skills_raw)   if skills_raw   else [],
        "settings":   json.loads(settings_raw) if settings_raw else {},
        "updated_at": _row_get(row, "updated_at"),
    })


@app.route("/api/profile", methods=["POST"])
def profile_save():
    uid, err = _auth_required()
    if err: return err
    data = request.get_json(force=True) or {}
    summary  = (data.get("summary") or "").strip()
    skills   = data.get("skills") or []
    settings = data.get("settings") or {}
    if not isinstance(skills, list):   skills = []
    if not isinstance(settings, dict): settings = {}
    now = datetime.utcnow().isoformat()
    with _db_conn() as conn:
        cur = conn.execute("SELECT id FROM profiles WHERE user_id = ?", (uid,))
        if cur.fetchone():
            conn.execute(
                "UPDATE profiles SET summary = ?, skills = ?, settings = ?, "
                "updated_at = ? WHERE user_id = ?",
                (summary, json.dumps(skills), json.dumps(settings), now, uid),
            )
        else:
            conn.execute(
                "INSERT INTO profiles (user_id, summary, skills, settings, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, summary, json.dumps(skills), json.dumps(settings), now),
            )
    return jsonify({"ok": True, "updated_at": now})


@app.route("/api/profile/skills/refresh", methods=["POST"])
def profile_skills_refresh():
    """Re-run skill extraction against the user's default CV's parsed_text
    using the current extractor (with all bug fixes), then replace the
    saved skills list. Returns the new list. Useful after the extractor
    has been improved — the saved list otherwise retains old false
    positives indefinitely."""
    uid, err = _auth_required()
    if err: return err

    # Pull parsed_text for the default CV (or most recent if no default set).
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT id, filename, parsed_text FROM cvs "
            "WHERE user_id = ? AND is_default = 1 LIMIT 1",
            (uid,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT id, filename, parsed_text FROM cvs "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (uid,),
            ).fetchone()
    if not row:
        return jsonify({"error": "no CV in library to re-extract from"}), 400
    parsed = _row_get(row, "parsed_text") or ""
    if not parsed.strip():
        return jsonify({"error": "default CV has no parsed text — re-upload it"}), 400

    new_skills = extract_skills_from_text(parsed)

    # Persist back to the user's profile, preserving summary + settings.
    now = datetime.utcnow().isoformat()
    with _db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM profiles WHERE user_id = ?", (uid,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE profiles SET skills = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(new_skills), now, uid),
            )
        else:
            conn.execute(
                "INSERT INTO profiles (user_id, summary, skills, settings, updated_at) "
                "VALUES (?, '', ?, '{}', ?)",
                (uid, json.dumps(new_skills), now),
            )

    return jsonify({
        "ok":       True,
        "skills":   new_skills,
        "count":    len(new_skills),
        "from_cv":  _row_get(row, "filename"),
        "from_cv_id": _row_get(row, "id"),
    })


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
    # Quota gate: AI CV parsing is the second-most expensive feature. Unlike
    # cover-letter (hard 402), here we *fall through* to keyword extraction
    # when the user is over quota — the result is worse but still useful, and
    # the visible quality drop is a natural upgrade nudge.
    uid = _current_user_id()
    ai_allowed = False
    if uid is not None and api_key and _anthropic:
        ai_allowed, _ = _billing_check_quota(_db_conn, uid, feature="cv_parse")

    if ai_allowed:
        try:
            resp = _parse_cv_ai(text, api_key)
            _billing_increment_quota(_db_conn, uid, amount=1)
            return resp
        except Exception:
            pass

    # Fallback: keyword matching (also used when locked, over quota, or AI fails)
    skills = extract_skills_from_text(text)
    lines  = [ln.strip() for ln in text.splitlines() if ln.strip()]
    summary = " ".join(lines[:5])[:300] if lines else ""
    return jsonify({"skills": skills, "summary": summary, "skill_count": len(skills), "ai_parsed": False})


@app.route("/api/search", methods=["POST"])
def search_jobs():
    data = request.get_json(force=True)
    # Env-var fallback for every key — the client used to send raw secrets
    # received from /api/config, but /api/config now returns booleans only.
    token        = (data.get("token") or os.environ.get("APIFY_TOKEN", "")).strip()
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
            if _url_host_matches(url, _aggregator_denylist()):
                continue  # skip jobleads, lensa, bebee etc. (Cloudflare-blocked)
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
    # Paywall gate: cover-letter is the AI-heaviest feature (real Anthropic
    # spend per call). Free tier gets 3/month, paid tiers unlimited. Founders
    # bypass entirely. We return 402 Payment Required so the frontend can
    # show an upgrade modal — falling back to a template-only letter would
    # devalue the paid plan since the template path produces a generic letter.
    uid, err = _auth_required()
    if err: return err
    allowed, qinfo = _billing_check_quota(_db_conn, uid, feature="cover_letter")
    if not allowed:
        return jsonify({
            "error":       "Monthly cover-letter quota reached. Upgrade to keep generating.",
            "quota":       qinfo,
            "upgrade_url": "/billing",
        }), 402

    data           = request.get_json(force=True)
    job            = data.get("job") or {}
    skills         = data.get("skills") or []
    resume_summary = data.get("resume_summary", "")
    cv_id          = data.get("cv_id")

    # If the caller passes a cv_id, pull the picked CV's parsed text/summary
    # so the AI letter is grounded in the actual CV (not just keyword skills).
    cv_text = ""
    if cv_id:
        try:
            with _db_conn() as conn:
                cur = conn.execute(
                    "SELECT parsed_text, ai_summary, ai_skills FROM cvs "
                    "WHERE id = ? AND user_id = ?", (int(cv_id), uid),
                )
                row = cur.fetchone()
            if row:
                if not resume_summary:
                    resume_summary = _row_get(row, "ai_summary", "") or ""
                cv_text = _row_get(row, "parsed_text", "") or ""
                if not skills:
                    sk_raw = _row_get(row, "ai_skills")
                    if sk_raw:
                        try: skills = json.loads(sk_raw)
                        except Exception: pass
        except Exception:
            pass  # fall through to the original (skills-only) path

    text = generate_cover_letter(job, skills, resume_summary, cv_text=cv_text)

    # Only meter the quota for actual AI calls. The template fallback runs
    # when ANTHROPIC_API_KEY is unset and shouldn't burn the user's allowance.
    if os.environ.get("ANTHROPIC_API_KEY") and _anthropic:
        _billing_increment_quota(_db_conn, uid, amount=1)

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
            if _url_host_matches(url, _aggregator_denylist()):
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

    # Score jobs using the same pipeline as /api/search so the daily-digest
    # results render identically (ATS class badges, blocker pills, match_why).
    profile = UserProfile(summary="", skills=skills, no_go_terms=NO_GO_TERMS)
    remote_jobs = [j for j in all_jobs if is_remote_job(j)]
    for job in remote_jobs:
        job["days_old"] = _days_since_posted(job.get("posted"))
        job.update(score_job(job, profile))
        job["salary_clean"] = _clean_salary(job.get("salary", ""))
        job["date_fmt"]     = fmt_date(job.get("posted"))
    remote_jobs.sort(key=lambda j: j["match_pct"], reverse=True)
    top10 = remote_jobs[:10]

    # Persist to daily_searches BEFORE sending email — so even if SMTP fails,
    # the frontend still has fresh "Today's Matches" to show.
    source_counts = {src: len(jobs_list) for src, jobs_list in source_results.items()}
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    try:
        # Single-user for now → user_id=1. When multi-user lands, the cron
        # will iterate per user using their own profile.skills + DIGEST_TO.
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO daily_searches (user_id, run_at, jobs, source_counts, "
                "total_fetched, emailed_to) VALUES (?, ?, ?, ?, ?, ?)",
                (1, now_iso, json.dumps(top10), json.dumps(source_counts),
                 len(all_jobs), digest_to),
            )
    except Exception as e:
        # Non-fatal — email still goes out, frontend just won't see this run.
        print(f"[digest] daily_searches write failed: {e}")

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


@app.route("/api/daily-results", methods=["GET"])
def daily_results():
    """Return the most recent stored daily-digest run for the current user.

    The frontend calls this on app boot to render a "Today's Matches" section
    above the manual search button — so the user sees fresh jobs the moment
    they open the app, without paying for another Apify run."""
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        cur = conn.execute(
            "SELECT id, run_at, jobs, source_counts, total_fetched, emailed_to "
            "FROM daily_searches WHERE user_id = ? "
            "ORDER BY run_at DESC LIMIT 1", (uid,),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"jobs": [], "run_at": None,
                        "reason": "No daily run yet. Cron fires at 9am."})
    try:
        jobs = json.loads(_row_get(row, "jobs", "[]") or "[]")
    except Exception:
        jobs = []
    try:
        source_counts = json.loads(_row_get(row, "source_counts", "{}") or "{}")
    except Exception:
        source_counts = {}

    # Re-filter against the latest aggregator blocklist. A daily run stored
    # before the list grew might contain URLs that should now be dropped —
    # this lets us tighten the filter without re-running ($1.20 Apify).
    blocked = _aggregator_denylist()
    jobs = [j for j in jobs if not _url_host_matches(j.get("url", ""), blocked)]

    return jsonify({
        "id":            _row_get(row, "id"),
        "run_at":        _row_get(row, "run_at"),
        "jobs":          jobs,
        "source_counts": source_counts,
        "total_fetched": _row_get(row, "total_fetched", 0),
        "emailed_to":    _row_get(row, "emailed_to") or "",
        "count":         len(jobs),
    })


# ── Applications tracker routes ───────────────────────────────────────────────

@app.route("/api/applications", methods=["GET"])
def list_applications():
    uid, err = _auth_required()
    if err: return err
    status_filter = request.args.get("status")
    with _db_conn() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM applications WHERE user_id = ? AND status = ? ORDER BY applied_at DESC",
                (uid, status_filter),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM applications WHERE user_id = ? ORDER BY applied_at DESC",
                (uid,),
            ).fetchall()
    return jsonify({"applications": [dict(r) for r in rows], "count": len(rows)})


@app.route("/api/applications", methods=["POST"])
def create_application():
    uid, err = _auth_required()
    if err: return err
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
               (user_id, company, title, url, ats, location, salary, source, status, notes, applied_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (
                uid, company, title,
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


# ── ATS provenance helpers (used by /api/applications/autolog) ───────────────
_ATS_BY_HOSTNAME = (
    ("lever.co",            "lever"),
    ("greenhouse.io",       "greenhouse"),
    ("workable.com",        "workable"),
    ("ashbyhq.com",         "ashby"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("myworkdayjobs.com",   "workday"),
    ("workday.com",         "workday"),
    ("bamboohr.com",        "bamboohr"),
    ("recruitee.com",       "recruitee"),
    ("breezy.hr",           "breezy"),
    ("applytojob.com",      "jazzhr"),
    ("jobvite.com",         "jobvite"),
    ("rippling.com",        "rippling"),
    ("polymer.co",          "polymer"),
    ("icims.com",           "icims"),
    ("oraclecloud.com",     "oracle"),
    ("paycomonline.net",    "paycom"),
    ("paylocity.com",       "paylocity"),
    ("ultipro.com",         "ukg"),
    ("dayforcehcm.com",     "dayforce"),
    ("brassring.com",       "brassring"),
    ("taleo.net",           "taleo"),
    ("successfactors.com",  "successfactors"),
    ("adp.com",             "adp"),
)

def _infer_ats_from_hostname(hostname):
    h = (hostname or "").lower()
    for needle, name in _ATS_BY_HOSTNAME:
        if needle in h:
            return name
    return None

def _slug_to_title(slug):
    """'solar-landscape' / 'solar_landscape' → 'Solar Landscape'. Single-word
    lowercase slugs like 'solarlandscape' stay as 'Solarlandscape' — best
    effort, not perfect (Lever and Greenhouse normalize slug casing
    differently)."""
    if not slug: return None
    return slug.replace("-", " ").replace("_", " ").strip().title()

def _infer_company_from_url(url, hostname, page_title):
    """ATS URL patterns put the company in a predictable path segment or
    subdomain. Falls back to parsing 'Title at Company' / 'Company - Title'
    out of the page <title> when the URL pattern doesn't match."""
    if not url:
        return None
    try:
        # Path-based patterns (Lever, Greenhouse, Workable, Ashby, SmartRecruiters)
        for pat in (
            r"jobs\.lever\.co/([^/?#]+)",
            r"boards\.greenhouse\.io/([^/?#]+)",
            r"jobs\.workable\.com/([^/?#]+)",
            r"jobs\.ashbyhq\.com/([^/?#]+)",
            r"jobs\.smartrecruiters\.com/([^/?#]+)",
        ):
            m = re.search(pat, url, re.IGNORECASE)
            if m: return _slug_to_title(m.group(1))
        # Subdomain-based patterns (Workable tenant, Workday tenant, BambooHR)
        for pat in (
            r"https?://([^.]+)\.workable\.com",
            r"https?://([^.]+)\.myworkdayjobs\.com",
            r"https?://([^.]+)\.bamboohr\.com",
            r"https?://([^.]+)\.recruitee\.com",
            r"https?://([^.]+)\.breezy\.hr",
        ):
            m = re.match(pat, url, re.IGNORECASE)
            if m: return _slug_to_title(m.group(1))
    except Exception:
        pass
    # Last-resort: parse page title
    t = (page_title or "").strip()
    if t:
        if " at " in t:
            parts = t.split(" at ")
            if len(parts) >= 2 and parts[-1].strip():
                return parts[-1].strip()
        for sep in (" - ", " | ", " · "):
            if sep in t:
                left, _, right = t.partition(sep)
                # Heuristic: Lever titles pages "Company - Job title"; assume
                # the longer side is the title and the shorter is the company.
                if left and right:
                    return left.strip() if len(left) <= len(right) else right.strip()
    return None

def _infer_title_from_signals(h1, page_title, company):
    """Prefer H1 (cleanest); otherwise strip company from page title."""
    if h1 and h1.strip():
        return h1.strip()[:120]
    t = (page_title or "").strip()
    if not t:
        return None
    if company and company in t:
        t = t.replace(company, "").strip(" -|·:")
    return t[:120] if t else None


@app.route("/api/applications/autolog", methods=["POST", "OPTIONS"])
def autolog_application():
    """Engine-triggered "I clicked Submit on a real ATS form" auto-logger.

    Called fire-and-forget from autofill.js via keepalive: true so the POST
    survives the page navigating to the ATS thank-you screen. Idempotent
    per (user_id, url) so re-clicking the same form doesn't double-log.

    Request:  {url, page_title?, h1?, hostname?, source?}
    Response: {id, deduped, company, title, ats, ok: true}
    """
    if request.method == "OPTIONS":
        return _autofill_cors_response(("", 204))
    uid, err = _auth_required()
    if err:
        return _autofill_cors_response(err)

    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return _autofill_cors_response(
            (jsonify({"error": "url required"}), 400))

    # Dedupe — if we already have this URL for this user, just return
    # the existing row (don't overwrite a status the user may have moved
    # to "Interviewing" or similar).
    with _db_conn() as conn:
        existing = conn.execute(
            "SELECT id, company, title, status, ats FROM applications "
            "WHERE user_id = ? AND url = ? LIMIT 1",
            (uid, url),
        ).fetchone()
    if existing:
        return _autofill_cors_response(jsonify({
            "id":       _row_get(existing, "id"),
            "deduped":  True,
            "company":  _row_get(existing, "company"),
            "title":    _row_get(existing, "title"),
            "status":   _row_get(existing, "status"),
            "ats":      _row_get(existing, "ats"),
            "ok":       True,
        }))

    page_title = (data.get("page_title") or "").strip()
    h1         = (data.get("h1") or "").strip()
    hostname   = (data.get("hostname") or "").strip().lower()
    source     = (data.get("source") or "autofill").strip() or "autofill"

    ats     = _infer_ats_from_hostname(hostname) or _infer_ats_from_hostname(url)
    company = _infer_company_from_url(url, hostname, page_title) or "(unknown company)"
    title   = _infer_title_from_signals(h1, page_title, company) or "(unknown role)"

    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with _db_conn() as conn:
        cur = conn.execute(
            """INSERT INTO applications
               (user_id, company, title, url, ats, location, salary, source, status, notes, applied_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?)
               RETURNING id""",
            (uid, company, title, url, ats, source, "Applied", now, now),
        )
        row = cur.fetchone()
        new_id = _row_get(row, "id") if row is not None else None

    return _autofill_cors_response(jsonify({
        "id":       new_id,
        "deduped":  False,
        "company":  company,
        "title":    title,
        "ats":      ats,
        "ok":       True,
    }))


@app.route("/api/applications/<int:app_id>", methods=["PATCH"])
def update_application(app_id):
    uid, err = _auth_required()
    if err: return err
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
    values.append(uid)
    with _db_conn() as conn:
        cur = conn.execute(
            f"UPDATE applications SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
            values,
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/applications/<int:app_id>", methods=["DELETE"])
def delete_application(app_id):
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM applications WHERE id = ? AND user_id = ?", (app_id, uid)
        )
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
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT id, company, title, url, status, applied_at, notes "
            "FROM applications WHERE user_id = ?",
            (uid,),
        ).fetchall()
    urls = {}
    fingerprints = {}
    for r in rows:
        meta = {
            "id":         r["id"],
            "applied_at": r["applied_at"],
            "status":     r["status"],
            "company":    r["company"],
            "title":      r["title"],
            "notes":      r["notes"] or "",
        }
        if r["url"]:
            urls[r["url"]] = meta
        co = (r["company"] or "").lower().strip()
        ti = (r["title"]   or "").lower().strip()
        if co and ti:
            fingerprints[f"{co}|{ti}"] = {
                "id":         r["id"],
                "applied_at": r["applied_at"],
                "status":     r["status"],
                "notes":      r["notes"] or "",
            }
    return jsonify({"urls": urls, "fingerprints": fingerprints})


@app.route("/api/applications/stats", methods=["GET"])
def application_stats():
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications WHERE user_id = ? GROUP BY status",
            (uid,),
        ).fetchall()
        total = _row_get(
            conn.execute(
                "SELECT COUNT(*) AS n FROM applications WHERE user_id = ?", (uid,)
            ).fetchone(),
            "n", 0,
        )
    by_status = {s: 0 for s in APP_STATUSES}
    for r in rows:
        by_status[r["status"]] = r["n"]
    return jsonify({"total": total, "by_status": by_status})


# ── Profile export for the Chrome extension auto-fill ───────────────────────

def _build_full_profile(uid):
    """Build the autofill profile dict for a given user. Shared by
    /api/profile/full (Chrome extension) and /bookmarklet/run.js (mobile)."""
    # Pull user's custom Q&A defaults from profile.settings if they edited them
    qa_defaults = []
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT settings FROM profiles WHERE user_id = ?", (uid,)
            ).fetchone()
        if row:
            settings_raw = _row_get(row, "settings")
            if settings_raw:
                settings = json.loads(settings_raw)
                qa = settings.get("qa_defaults") or []
                if isinstance(qa, list):
                    qa_defaults = qa
    except Exception:
        pass

    # Build a flat dict of common-label → answer lookup from the user's
    # Q&A defaults. The extension can use this to match by label substring.
    qa_map = {}
    for entry in qa_defaults:
        if isinstance(entry, list) and len(entry) == 2:
            qa_map[entry[0].lower()] = entry[1]

    profile = {
        # Identity
        "first_name":   "George",
        "last_name":    "Tupayachi",
        "full_name":    "George Tupayachi",
        "preferred_name": "George",
        "pronouns":     "He/Him",
        # Contact
        "email":        "georgetupayachijobs@outlook.com",
        "phone":        "+1 (609) 553-6215",
        "phone_digits": "6095536215",
        "phone_e164":   "+16095536215",
        # Address
        "address": {
            "street":       "1412 Doughty Road",
            "city":         "Egg Harbor Township",
            "state":        "NJ",
            "state_full":   "New Jersey",
            "zip":          "08234",
            "country":      "United States",
            "country_code": "US",
        },
        # Links
        "linkedin":  "https://www.linkedin.com/in/george-tupayachi",
        "portfolio": "",
        # Default answers — these get pasted into yes/no and demographic dropdowns
        "answers": {
            "work_authorized_us":       qa_map.get("authorized to work in us", "Yes"),
            "sponsorship_needed":       qa_map.get("sponsorship needed", "No"),
            "veteran_status":           qa_map.get("veteran", "Not a protected veteran"),
            "disability":               qa_map.get("disability", "No"),
            "gender":                   qa_map.get("gender", "Male"),
            "hispanic_latino":          qa_map.get("hispanic / latino", "Yes"),
            "race":                     qa_map.get("race", "White"),
            "salary_expectation":       qa_map.get("salary expectation", "Negotiable"),
            "notice_period":            qa_map.get("notice period", "Available immediately (2-week notice)"),
            "esignature":               qa_map.get("e-signature", "George Tupayachi"),
            "previously_employed":      "No",
            "willing_to_relocate":      "Yes",
            "active_security_clearance":"None",
            "us_gov_employment":        "Never",
        },
        # Education (most ATSes ask)
        "education": [
            {
                "school":      "Franklin University",
                "degree":      "Masters",
                "field":       "Cybersecurity",
                "start_date":  "2024-09",
                "end_date":    "2027-01",
                "graduated":   False,
                "location":    "Remote (Columbus, OH)",
            },
            {
                "school":      "Stockton University",
                "degree":      "Bachelors",
                "field":       "Business Management Studies",
                "start_date":  "2017-01",
                "end_date":    "2021-05",
                "graduated":   True,
                "location":    "Galloway, NJ",
            },
        ],
        # Work history (most recent first)
        "work_experience": [
            {
                "company":  "Harrah's Casino",
                "title":    "Bartender",
                "start_date":"2024-07",
                "end_date":  "",
                "current":  True,
                "location": "Atlantic City, NJ",
                "address":  "777 Harrah's Blvd, Atlantic City, NJ 08401",
            },
            {
                "company":  "Ocean Casino Resort",
                "title":    "Casino Shift Manager",
                "start_date":"2021-06",
                "end_date":  "2022-07",
                "current":  False,
                "location": "Atlantic City, NJ",
                "address":  "500 Boardwalk, Atlantic City, NJ 08401",
            },
            {
                "company":  "Ocean Casino Resort",
                "title":    "Bartender",
                "start_date":"2019-06",
                "end_date":  "2020-08",
                "current":  False,
                "location": "Atlantic City, NJ",
                "address":  "500 Boardwalk, Atlantic City, NJ 08401",
            },
        ],
        # Raw Q&A list so the extension can do fuzzy matching too
        "qa_defaults": qa_defaults,
    }
    return profile


@app.route("/api/profile/full", methods=["GET"])
def profile_full():
    """Return the autofill profile dict (Chrome extension uses this)."""
    uid, err = _auth_required()
    if err: return err
    return jsonify(_build_full_profile(uid))


# ── Rich stats dashboard ──────────────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
def stats_dashboard():
    """Aggregated view for the Stats step. One endpoint, all the numbers."""
    uid, err = _auth_required()
    if err: return err

    now = datetime.utcnow()
    week_cutoff  = (now - timedelta(days=7)).isoformat(timespec="seconds") + "Z"
    month_cutoff = (now - timedelta(days=30)).isoformat(timespec="seconds") + "Z"
    month_label  = now.strftime("%Y-%m")

    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT id, company, title, url, ats, source, status, applied_at, "
            "follow_up_sent_at FROM applications WHERE user_id = ?", (uid,),
        ).fetchall()
        # Number of digest cron runs this month — proxy for cron-side Apify spend
        digest_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM daily_searches "
            "WHERE user_id = ? AND run_at >= ?", (uid, month_cutoff),
        ).fetchone()
        digest_runs_30d = _row_get(digest_rows, "n", 0) or 0

    apps = [dict(r) for r in rows]
    total = len(apps)

    # Bucket counts
    by_status   = {s: 0 for s in APP_STATUSES}
    by_ats_cls  = {"fast": 0, "walled": 0, "blocked": 0, "unknown": 0}
    by_source   = {}
    by_ats_name = {}
    company_counts = {}
    follow_ups_sent = 0
    applied_7d  = 0
    applied_30d = 0
    last_apply_at = None

    # Recent-activity heatmap (last 30 days, count per day)
    activity = {}
    for offset in range(30):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        activity[day] = 0

    for a in apps:
        st = a.get("status") or "Applied"
        by_status[st] = by_status.get(st, 0) + 1

        cls, name = _classify_ats(a.get("url", "") or "")
        by_ats_cls[cls] = by_ats_cls.get(cls, 0) + 1
        if name:
            by_ats_name[name] = by_ats_name.get(name, 0) + 1
        elif a.get("ats"):
            # Fall back to whatever was stored in the ats column
            by_ats_name[a["ats"]] = by_ats_name.get(a["ats"], 0) + 1

        src = a.get("source") or "Unknown"
        by_source[src] = by_source.get(src, 0) + 1

        co = (a.get("company") or "").strip()
        if co:
            company_counts[co] = company_counts.get(co, 0) + 1

        if a.get("follow_up_sent_at"):
            follow_ups_sent += 1

        ap = a.get("applied_at") or ""
        if ap:
            if last_apply_at is None or ap > last_apply_at:
                last_apply_at = ap
            if ap >= week_cutoff:
                applied_7d += 1
            if ap >= month_cutoff:
                applied_30d += 1
            day = ap[:10]
            if day in activity:
                activity[day] += 1

    # Top 5 companies (multi-applications signals interest)
    top_companies = sorted(company_counts.items(), key=lambda x: -x[1])[:5]
    # Top 5 ATSes used (by app count)
    top_ats_names = sorted(by_ats_name.items(), key=lambda x: -x[1])[:6]

    # Response rate: anything past Applied counts as a response
    responded = sum(
        by_status.get(s, 0)
        for s in ("Phone Screen", "Interview", "Offer", "Rejected")
    )
    response_rate = round((responded / total) * 100) if total else 0

    # Apify spend rough estimate. Daily-digest runs are $1.20 each; manual
    # /api/search is also $1.20/run but we don't track those server-side
    # (client-side localStorage has the manual count). Report only the
    # server-known portion so the number doesn't lie.
    apify_cron_spend = round(digest_runs_30d * 1.20, 2)

    return jsonify({
        "total":              total,
        "applied_this_week":  applied_7d,
        "applied_this_month": applied_30d,
        "follow_ups_sent":    follow_ups_sent,
        "responded":          responded,
        "response_rate_pct":  response_rate,
        "last_apply_at":      last_apply_at,
        "by_status":          by_status,
        "by_ats_class":       by_ats_cls,
        "by_source":          by_source,
        "top_companies":      [{"name": n, "count": c} for n, c in top_companies],
        "top_ats_names":      [{"name": n, "count": c} for n, c in top_ats_names],
        "activity_30d":       activity,
        "digest_runs_30d":    digest_runs_30d,
        "apify_cron_spend_mtd": apify_cron_spend,
        "month_label":        month_label,
    })


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
    - Captures the latest non-noreply sender as last_contact_email so the
      follow-up flow has a real human to reply to.
    - Appends a note documenting each auto-change.

    Requires GMAIL_USER + GMAIL_APP_PASSWORD env vars.
    Optional JSON body: { "dry_run": true } previews without writing.
    """
    import imaplib
    import email as _email
    from email.header import decode_header
    from datetime import timedelta

    uid, err = _auth_required()
    if err: return err

    body_json = request.get_json(silent=True) or {}
    dry_run  = bool(body_json.get("dry_run"))
    verbose  = bool(body_json.get("verbose"))

    # Provider selection. "gmail" reads imap.gmail.com using GMAIL_USER /
    # GMAIL_APP_PASSWORD. "outlook" reads outlook.office365.com using
    # OUTLOOK_USER / OUTLOOK_APP_PASSWORD — works for personal outlook.com
    # accounts that have 2FA + an app password generated. Default = gmail
    # for back-compat with the existing daily-digest cron.
    provider = (body_json.get("provider") or "gmail").lower().strip()
    if provider == "outlook":
        imap_host    = os.environ.get("OUTLOOK_IMAP_HOST", "outlook.office365.com")
        imap_user    = os.environ.get("OUTLOOK_USER", "")
        imap_pass    = os.environ.get("OUTLOOK_APP_PASSWORD", "")
        provider_lbl = "Outlook"
    elif provider == "gmail":
        imap_host    = "imap.gmail.com"
        imap_user    = os.environ.get("GMAIL_USER", "")
        imap_pass    = os.environ.get("GMAIL_APP_PASSWORD", "")
        provider_lbl = "Gmail"
    else:
        return jsonify({"error": f"Unknown provider {provider!r} — use 'gmail' or 'outlook'"}), 400

    if not imap_user or not imap_pass:
        var_a = "OUTLOOK_USER" if provider == "outlook" else "GMAIL_USER"
        var_b = "OUTLOOK_APP_PASSWORD" if provider == "outlook" else "GMAIL_APP_PASSWORD"
        return jsonify({"error": f"{var_a} / {var_b} not set on server"}), 500
    # Allow overriding the per-scan cap. 300 default keeps the request under
    # the gunicorn 300s timeout for typical inboxes (each IMAP fetch is ~0.3s).
    # Ceiling at 1000 so power users can do an exhaustive sweep when needed.
    try:
        max_emails = max(50, min(1000, int(body_json.get("max_emails", 300))))
    except Exception:
        max_emails = 300

    # Load THIS user's applications only — other tenants' data must not leak.
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM applications WHERE user_id = ?", (uid,)
        ).fetchall()
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
            # Word-boundary match — short tokens like "vxi", "gnc", "tds" used
            # to substring-match unrelated emails (Klarna OTPs, Popeyes coupons).
            # \b on each end forces a real word boundary.
            score = sum(1 for t in tokens if re.search(r"\b" + re.escape(t) + r"\b", blob))
            if score > best_score:
                best, best_score = app, score
        # Single-token apps (just the company name, e.g. "Endava") are common.
        # For those we still require the one token to match. For multi-token
        # apps we require at least 1 (which is what we had).
        return best if best_score >= 1 else None

    def _extract_email(from_header):
        """Pull "user@host" out of "Name <user@host>" or just "user@host"."""
        if not from_header: return ""
        m = re.search(r"<([^>]+)>", from_header)
        if m: return m.group(1).strip().lower()
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", from_header)
        return m.group(0).lower() if m else ""

    def _is_real_human(email):
        if not email: return False
        local, _, _ = email.partition("@")
        l = local.lower()
        # noreply, no-reply, donotreply, mailer-daemon, etc.
        return not any(p in l for p in ("noreply", "no-reply", "donotreply",
                                        "do-not-reply", "mailer-daemon",
                                        "notifications", "automated"))

    # Connect to IMAP. Default folder = INBOX. Gmail also exposes
    # "[Gmail]/All Mail" for archived messages; Outlook stores everything
    # in INBOX by default but has "Archive" as a real folder if the user
    # uses sweep rules.
    folder = body_json.get("folder") or "INBOX"
    try:
        mail = imaplib.IMAP4_SSL(imap_host, 993)
        mail.login(imap_user, imap_pass)
        # Folder names with spaces / brackets need quoting for IMAP SELECT.
        sel_typ, sel_data = mail.select(f'"{folder}"')
        if sel_typ != "OK":
            return jsonify({"error": f"IMAP folder '{folder}' not selectable on "
                                     f"{provider_lbl}: {sel_data}"}), 500
    except Exception as e:
        return jsonify({"error": f"IMAP connect/login failed for {provider_lbl} "
                                 f"({imap_host}): {e}"}), 500

    try:
        since = (datetime.utcnow() - timedelta(days=30)).strftime("%d-%b-%Y")
        # Narrow at IMAP-search time to subjects that look like ATS replies.
        # Without this we spend the whole budget parsing newsletters/marketing.
        # Each query returns a UID list; we union them. Falls back to a plain
        # SINCE search if none of the targeted searches return anything.
        ats_subject_filters = [
            "application", "applying", "applied", "candidacy", "candidate",
            "interview", "phone screen", "screen", "next step", "next steps",
            "received your", "received from you", "thank you for",
            "regret to inform", "moving forward", "not moving forward",
            "offer", "position", "role", "opportunity",
        ]
        candidate_ids = set()
        for kw in ats_subject_filters:
            try:
                typ, data = mail.search(None, f'(SINCE {since} SUBJECT "{kw}")')
                if typ == "OK" and data and data[0]:
                    candidate_ids.update(data[0].split())
            except Exception:
                continue
        if candidate_ids:
            # Sort so we process newest-first (IMAP UIDs are roughly chronological)
            ids = sorted(candidate_ids, key=lambda x: int(x))[-max_emails:]
        else:
            # Fallback: scan whatever's there (matches old behaviour)
            typ, data = mail.search(None, f'(SINCE {since})')
            ids = (data[0].split() if data and data[0] else [])[-max_emails:]

        # Collect best proposal per application (forward-only ranking)
        proposals = {}  # app_id -> { status, subject, from, current, date }
        scanned = 0
        matched = 0
        # Diagnostic buckets — only populated when verbose=true.
        # classified_no_app: looked like ATS reply but no application token matched.
        # matched_no_status: matched an application but didn't fit any status keyword.
        # noise: passed the subject filter but neither classified nor matched —
        #        helps spot whether the IMAP pre-filter is letting non-ATS emails through.
        diag_classified_no_app = []
        diag_matched_no_status = []
        diag_noise             = []

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
                app = match_app(from_addr, subject, snippet)
                if not proposed and not app:
                    if verbose and len(diag_noise) < 25:
                        diag_noise.append({
                            "from": from_addr.strip()[:80],
                            "subject": (subject or "(no subject)").strip()[:120],
                        })
                    continue
                if not proposed and app:
                    if verbose and len(diag_matched_no_status) < 25:
                        diag_matched_no_status.append({
                            "company": app.get("company"),
                            "from": from_addr.strip()[:80],
                            "subject": (subject or "(no subject)").strip()[:120],
                        })
                    continue
                if proposed and not app:
                    if verbose and len(diag_classified_no_app) < 25:
                        diag_classified_no_app.append({
                            "proposed_status": proposed,
                            "from": from_addr.strip()[:80],
                            "subject": (subject or "(no subject)").strip()[:120],
                        })
                    continue
                matched += 1

                from_email = _extract_email(from_addr)

                # Keep the highest-ranked proposal per app, but ALWAYS update
                # last_contact_email if we now have a better (human) sender.
                prev = proposals.get(app["id"])
                prev_rank = (1000 if prev and prev["status"] == "Rejected"
                             else STATUS_RANK.get(prev["status"], 0) if prev else -1)
                new_rank = 1000 if proposed == "Rejected" else STATUS_RANK.get(proposed, 0)
                # Real-human contact email: capture the latest one we see, even
                # if the proposed status doesn't beat the existing one.
                contact_email = ""
                if _is_real_human(from_email):
                    contact_email = from_email
                if new_rank > prev_rank:
                    proposals[app["id"]] = {
                        "status": proposed,
                        "subject": (subject or "(no subject)").strip()[:140],
                        "from": from_addr.strip()[:100],
                        "from_email": from_email,
                        "contact_email": contact_email or (prev or {}).get("contact_email", ""),
                        "current": app["status"],
                    }
                elif prev and contact_email and not prev.get("contact_email"):
                    prev["contact_email"] = contact_email
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
                        "SELECT status, notes, company, last_contact_email FROM applications "
                        "WHERE id = ? AND user_id = ?", (app_id, uid),
                    ).fetchone()
                    if not row:
                        continue
                    current = row["status"]
                    do_status = should_update(current, p["status"])
                    new_contact = p.get("contact_email") or _row_get(row, "last_contact_email", "") or ""
                    if not do_status and new_contact == _row_get(row, "last_contact_email", ""):
                        continue
                    if do_status:
                        audit = (f"[Gmail scan {now_iso[:10]}] {current} -> {p['status']} - "
                                 f"from {p['from']} - {p['subject']}")
                        existing = (row["notes"] or "").strip()
                        new_notes = (existing + "\n" + audit).strip() if existing else audit
                        conn.execute(
                            "UPDATE applications SET status = ?, notes = ?, "
                            "last_contact_email = ?, updated_at = ? WHERE id = ?",
                            (p["status"], new_notes, new_contact or None, now_iso, app_id),
                        )
                        updated += 1
                    else:
                        # Just refresh the contact email — status unchanged
                        conn.execute(
                            "UPDATE applications SET last_contact_email = ?, updated_at = ? WHERE id = ?",
                            (new_contact, now_iso, app_id),
                        )
                    hits.append({
                        "app_id":      app_id,
                        "company":     row["company"],
                        "from":        current,
                        "to":          p["status"] if do_status else current,
                        "subject":     p["subject"],
                        "email_from":  p["from"],
                        "contact":     new_contact,
                    })

        result = {
            "scanned":    scanned,
            "matched":    matched,
            "updated":    updated,
            "dry_run":    dry_run,
            "max_emails": max_emails,
            "hits":       hits,
        }
        result["provider"] = provider
        if verbose:
            result["diag"] = {
                "classified_no_app": diag_classified_no_app,
                "matched_no_status": diag_matched_no_status,
                "noise":             diag_noise,
                "folder":            folder,
                "provider":          provider,
                "imap_host":         imap_host,
                "applications_in_db": len(apps),
                "company_tokens_sample": [
                    {"company": a.get("company"), "tokens": t}
                    for a, t in app_tokens[:30]
                ],
            }
        return jsonify(result)
    except Exception as e:
        try: mail.logout()
        except Exception: pass
        return jsonify({"error": str(e)}), 500


# ── Follow-up tracker ────────────────────────────────────────────────────────
# Surfaces apps stuck at "Applied" for 7+ days where Gmail scan didn't already
# bump them forward. Auto-drafts a tailored follow-up using the picked CV and
# the original job description; user reviews + sends with one tap.

FOLLOW_UP_MIN_DAYS_SINCE_APPLY = 7   # don't pester before this
FOLLOW_UP_COOLDOWN_DAYS        = 14  # don't follow up twice within this window


def _days_between(iso_a, iso_b):
    try:
        a = datetime.fromisoformat(iso_a.rstrip("Z"))
        b = datetime.fromisoformat(iso_b.rstrip("Z"))
        return abs((a - b).total_seconds()) / 86400
    except Exception:
        return None


@app.route("/api/follow-up/candidates", methods=["GET", "POST"])
def follow_up_candidates():
    """Return apps that look ripe for a follow-up.

    Filters to status=Applied AND applied_at >= 7 days ago, EXCLUDING apps
    where follow_up_sent_at is within the cooldown window. The caller is
    expected to POST /api/gmail/scan first if they want fresh statuses (the
    frontend wires this as a single 'Refresh & find' button)."""
    uid, err = _auth_required()
    if err: return err

    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM applications WHERE user_id = ? AND status = 'Applied' "
            "ORDER BY applied_at ASC", (uid,),
        ).fetchall()

    candidates = []
    for r in rows:
        applied_at = _row_get(r, "applied_at", "")
        days_ago = _days_between(applied_at, now_iso)
        if days_ago is None or days_ago < FOLLOW_UP_MIN_DAYS_SINCE_APPLY:
            continue
        sent_at = _row_get(r, "follow_up_sent_at", "")
        if sent_at:
            cooldown = _days_between(sent_at, now_iso)
            if cooldown is not None and cooldown < FOLLOW_UP_COOLDOWN_DAYS:
                continue
        candidates.append({
            "id":                 _row_get(r, "id"),
            "company":            _row_get(r, "company", ""),
            "title":              _row_get(r, "title", ""),
            "url":                _row_get(r, "url", ""),
            "ats":                _row_get(r, "ats", ""),
            "applied_at":         applied_at,
            "days_ago":           int(days_ago),
            "last_contact_email": _row_get(r, "last_contact_email", "") or "",
            "follow_up_sent_at":  sent_at or "",
            "notes":              _row_get(r, "notes", "") or "",
        })

    return jsonify({"candidates": candidates, "count": len(candidates)})


@app.route("/api/follow-up/draft", methods=["POST"])
def follow_up_draft():
    """Generate a tailored follow-up email for a tracked application.

    Returns: { subject, body, to_email, to_name, ai }
    Uses Claude when available; falls back to a sane template otherwise.
    The frontend can present this in a textarea for review before sending.
    """
    uid, err = _auth_required()
    if err: return err

    data = request.get_json(force=True) or {}
    app_id = data.get("app_id")
    if not app_id:
        return jsonify({"error": "app_id required"}), 400

    # Load the application + its default CV (for grounding)
    with _db_conn() as conn:
        app_row = conn.execute(
            "SELECT * FROM applications WHERE id = ? AND user_id = ?", (int(app_id), uid),
        ).fetchone()
        if not app_row:
            return jsonify({"error": "Application not found"}), 404
        cv_row = conn.execute(
            "SELECT parsed_text, ai_summary, filename FROM cvs "
            "WHERE user_id = ? ORDER BY is_default DESC, id DESC LIMIT 1", (uid,),
        ).fetchone()

    company  = _row_get(app_row, "company", "") or "the company"
    title    = _row_get(app_row, "title", "") or "the role"
    applied  = _row_get(app_row, "applied_at", "")
    contact  = _row_get(app_row, "last_contact_email", "") or ""
    cv_text  = _row_get(cv_row, "parsed_text", "") if cv_row else ""
    cv_summary = _row_get(cv_row, "ai_summary", "") if cv_row else ""

    days_ago = _days_between(applied, datetime.utcnow().isoformat(timespec="seconds") + "Z")
    days_label = f"{int(days_ago)} days" if days_ago else "a couple of weeks"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    subject = f"Following up — {title} application"
    body    = ""
    used_ai = False

    if api_key and _anthropic and cv_text:
        try:
            client = _anthropic.Anthropic(api_key=api_key)
            cv_excerpt = re.sub(r"\s+", " ", cv_text[:2500]).strip()
            prompt = f"""Write a polite, concise, professional follow-up email for a job application.

CONTEXT:
- I applied to {company} for the {title} role about {days_label} ago.
- I haven't received a response yet.
- The recipient is {("a specific person: " + contact) if contact else "the hiring team (no specific contact known)"}.

MY RESUME (excerpt — pull one or two real, specific points; do NOT invent):
{cv_excerpt}

Write:
1. SUBJECT: a short subject line (under 60 chars). Just the subject text, no quotes.
2. BODY: 4–6 short sentences. Polite, not pushy. Reference the role + that I applied. Mention ONE specific qualification from my resume that matches this role. Express continued interest. End with "Best, George Tupayachi".

Return ONLY this exact format (no markdown, no extra commentary):
SUBJECT: <subject>
BODY:
<body lines>"""
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (msg.content[0].text or "").strip()
            # Parse SUBJECT: ... \n BODY: ...
            m = re.match(r"SUBJECT:\s*(.+?)\s*\n+BODY:\s*\n?(.+)", raw, re.DOTALL | re.IGNORECASE)
            if m:
                subject = m.group(1).strip()[:120]
                body    = m.group(2).strip()
                used_ai = True
        except Exception:
            pass

    if not body:
        # Template fallback — still useful, just not tailored to the CV.
        salutation = ("Hi" if contact else "Hello") + (" there," if not contact else ",")
        # IMPORTANT: cv_summary often holds the first ~240 chars of the parsed
        # CV (header + contact line + "PROFESSIONAL SUMMARY...") when CV upload
        # ran without an Anthropic key. Dropping that verbatim into a follow-up
        # email looks awful. Detect a header dump and fall back to a canonical,
        # category-aware bio instead.
        def _looks_like_header_dump(s):
            if not s: return True
            s_low = s.lower()
            # Phone, email, "professional summary", or a name in ALL CAPS at start
            if "@" in s_low or "professional summary" in s_low: return True
            first = (s.split()[0] if s.split() else "")
            if first.isupper() and len(first) >= 4: return True
            return False
        # Pick a category-aware canonical bio. Defaults to IT.
        cv_cat = (_row_get(cv_row, "filename", "") or "").lower()
        # Use the actual category if we have one; for now infer from filename.
        if any(k in cv_cat for k in ("bartender", "mixologist", "casino", "hospitality")):
            canonical_bio = (
                "5+ years of high-volume customer-facing operations at Ocean Casino "
                "Resort and Harrah's, plus a Cybersecurity Masters in progress at "
                "Franklin University. Bilingual EN/ES, multitasks under pressure, "
                "and learns new tools rapidly."
            )
        else:
            canonical_bio = (
                "Bilingual EN/ES IT Support candidate with 3+ years of technical "
                "experience at Cholo Tech (development, troubleshooting, "
                "hardware/software support) plus 5+ years of high-volume "
                "customer-facing operations at Ocean Casino Resort and Harrah's. "
                "Currently pursuing a Cybersecurity Masters at Franklin University "
                "with hands-on labs in Windows administration, networking, endpoint "
                "hardening, and incident response. Holds the Google Cybersecurity "
                "Professional Certificate."
            )
        bullet = canonical_bio if _looks_like_header_dump(cv_summary) else cv_summary
        body = (f"{salutation}\n\n"
                f"I'm following up on my application for the {title} role at {company}, "
                f"submitted about {days_label} ago. I'm still very interested and wanted to "
                f"check on next steps.\n\n"
                f"Quick refresher on my background: {bullet}\n\n"
                f"Happy to share more or hop on a quick call. Thanks for your time.\n\n"
                f"Best,\nGeorge Tupayachi\n"
                f"georgetupayachijobs@outlook.com\n+1 (609) 553-6215")

    return jsonify({
        "subject":  subject,
        "body":     body,
        "to_email": contact,
        "ai":       used_ai,
        "app_id":   int(app_id),
    })


@app.route("/api/follow-up/send", methods=["POST"])
def follow_up_send():
    """Send a follow-up email via Gmail SMTP and mark the app accordingly.

    Body: { app_id, to_email, subject, body }
    Refuses if follow_up_sent_at is within the cooldown window — caller must
    explicitly pass force=true to override.
    """
    import smtplib
    from email.mime.text import MIMEText

    uid, err = _auth_required()
    if err: return err

    data = request.get_json(force=True) or {}
    app_id   = data.get("app_id")
    to_email = (data.get("to_email") or "").strip()
    subject  = (data.get("subject") or "").strip()
    body     = (data.get("body") or "").strip()
    force    = bool(data.get("force"))

    if not (app_id and to_email and subject and body):
        return jsonify({"error": "app_id, to_email, subject, body all required"}), 400
    if "@" not in to_email or "." not in to_email.split("@", 1)[-1]:
        return jsonify({"error": "to_email looks invalid"}), 400

    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_user or not gmail_pass:
        return jsonify({"error": "GMAIL_USER / GMAIL_APP_PASSWORD not set on server"}), 500

    # Cooldown check
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT id, status, follow_up_sent_at, notes FROM applications "
            "WHERE id = ? AND user_id = ?", (int(app_id), uid),
        ).fetchone()
    if not row:
        return jsonify({"error": "Application not found"}), 404
    sent_at = _row_get(row, "follow_up_sent_at", "")
    if sent_at and not force:
        cooldown = _days_between(
            sent_at, datetime.utcnow().isoformat(timespec="seconds") + "Z"
        )
        if cooldown is not None and cooldown < FOLLOW_UP_COOLDOWN_DAYS:
            return jsonify({
                "error": f"Follow-up already sent {int(cooldown)} days ago. "
                         f"Pass force=true to override.",
                "last_sent_at": sent_at,
            }), 409

    # Send
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = to_email
    msg["Reply-To"] = "georgetupayachijobs@outlook.com"
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to_email, msg.as_string())
    except Exception as e:
        return jsonify({"sent": False, "error": str(e)}), 500

    # Mark on the application
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    audit = (f"[Follow-up sent {now_iso[:10]}] to {to_email} - {subject[:120]}")
    existing = (_row_get(row, "notes", "") or "").strip()
    new_notes = (existing + "\n" + audit).strip() if existing else audit
    with _db_conn() as conn:
        conn.execute(
            "UPDATE applications SET follow_up_sent_at = ?, notes = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (now_iso, new_notes, now_iso, int(app_id), uid),
        )

    return jsonify({"sent": True, "to": to_email, "sent_at": now_iso})


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


# ── Cover-letter library ──────────────────────────────────────────────────────
# Reuse the 14+ cover letters George has already written. Faster than starting
# from scratch — pick the closest one, tweak the opener, ship it.

# Search both: the bundled cover_letters/ subfolder (production deploy) and the
# repo's parent directory (local dev — George keeps drafts in the project root).
# First-found wins per filename.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
COVER_LETTER_DIRS = [
    os.path.join(_APP_DIR, "cover_letters"),
    os.path.dirname(_APP_DIR),
]


def _cover_letter_role_from_filename(fn):
    """cover_letter_technical_support_engineer.txt → 'technical support engineer'"""
    base = os.path.splitext(fn)[0]
    if base.startswith("cover_letter_"):
        base = base[len("cover_letter_"):]
    return base.replace("_", " ").strip()


def _resolve_cover_letter_path(filename):
    """Find a cover letter by filename across all search dirs. Returns the
    first existing path or None."""
    for d in COVER_LETTER_DIRS:
        path = os.path.join(d, filename)
        if os.path.isfile(path):
            return path
    return None


def _list_cover_letters():
    """Scan all configured dirs for cover_letter_*.txt files, dedupe by stem
    (so the bundled subfolder shadows the parent), and return metadata."""
    seen = set()
    out = []
    for d in COVER_LETTER_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            low = fn.lower()
            if not low.startswith("cover_letter_") or not low.endswith(".txt"):
                continue
            stem = os.path.splitext(fn)[0]
            if stem in seen:
                continue
            seen.add(stem)
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except Exception:
                continue
            out.append({
                "filename": fn,
                "role":     _cover_letter_role_from_filename(fn),
                "preview":  body.strip()[:240],
                "length":   len(body),
            })
    return out


def _suggest_cover_letter_keyword(job, letters):
    """Fallback: pick the letter whose role tokens best overlap with the job title."""
    title = (job.get("title") or "").lower()
    desc  = (job.get("description") or "").lower()[:600]
    blob  = title + " " + desc
    best, best_score = None, 0
    for L in letters:
        tokens = [t for t in re.split(r"\W+", L["role"]) if len(t) >= 3]
        score  = sum(2 if t in title else 1 if t in blob else 0 for t in tokens)
        if score > best_score:
            best, best_score = L, score
    return best, best_score


# ── CV Library ───────────────────────────────────────────────────────────────
# Auto-routes the right CV per job: IT job → IT CV, casino job → bartender CV.
# Files are stored as BLOBs in DB so they survive Render redeploys (no
# persistent FS on the free tier).

CV_CATEGORIES = ["it", "cybersecurity", "developer", "bartender",
                 "casino", "hospitality", "general"]

# Keyword sets for categorisation — used both to tag a CV on upload and to
# tag a job at auto-pick time. Order matters: more specific first.
_CV_CATEGORY_KEYWORDS = [
    ("cybersecurity", ["soc analyst", "security analyst", "cybersecurity",
                       "incident response", "siem", "splunk", "sentinel",
                       "mitre att&ck", "nist csf", "blue team", "red team"]),
    ("developer",     ["software developer", "software engineer", "full stack",
                       "frontend", "backend", "react", "node.js", "python developer",
                       "javascript developer", "web developer"]),
    ("it",            ["help desk", "service desk", "it support",
                       "technical support", "desktop support", "tier 1",
                       "tier i", "level 1", "level i ", "it technician",
                       "service desk analyst", "support specialist"]),
    ("casino",        ["casino", "table games", "shift manager", "pit boss",
                       "dealer", "gaming"]),
    ("bartender",     ["bartender", "mixologist", "bar manager"]),
    ("hospitality",   ["server", "hostess", "host ", "front desk",
                       "concierge", "hotel", "restaurant", "guest service"]),
]


def _categorize_text(text):
    """Pure-keyword categoriser. Returns (category, confidence_score).
    Used both for CVs (entire CV text) and jobs (title + description)."""
    if not text:
        return ("general", 0)
    t = text.lower()
    best, best_score = "general", 0
    for cat, keywords in _CV_CATEGORY_KEYWORDS:
        score = sum(t.count(k) for k in keywords)
        if score > best_score:
            best, best_score = cat, score
    return (best, best_score)


def _categorize_cv_ai(text, api_key):
    """AI categorisation — returns category string from CV_CATEGORIES."""
    client = _anthropic.Anthropic(api_key=api_key)
    excerpt = text[:3500]
    prompt = f"""Classify this resume into ONE of these categories based on the candidate's primary skill set:
- it          (Help Desk, Service Desk, IT Support, Technical Support, Desktop Support)
- cybersecurity (SOC Analyst, Security Analyst, Incident Response, Blue/Red Team)
- developer   (Software Developer, Web Developer, Backend/Frontend Engineer)
- bartender   (Bartender, Mixologist, Bar Manager)
- casino      (Casino Dealer, Casino Shift Manager, Pit Boss, Gaming)
- hospitality (Server, Host, Front Desk, Hotel, Restaurant)
- general     (mixed / unclear / fallback)

RESUME:
{excerpt}

Return ONLY one of the category strings above, lowercase, nothing else."""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": prompt}],
    )
    out = msg.content[0].text.strip().lower().split()[0] if msg.content else "general"
    return out if out in CV_CATEGORIES else "general"


def _detect_cv_category(text):
    """Try AI, fall back to keywords."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and _anthropic:
        try:
            return _categorize_cv_ai(text, api_key)
        except Exception:
            pass
    return _categorize_text(text)[0]


def _cv_to_dict(row, include_blob=False):
    """Normalise a cvs row into the JSON shape the frontend consumes."""
    out = {
        "id":         _row_get(row, "id"),
        "filename":   _row_get(row, "filename"),
        "category":   _row_get(row, "category", "general"),
        "mime_type":  _row_get(row, "mime_type", ""),
        "ai_summary": _row_get(row, "ai_summary", "") or "",
        "is_default": bool(_row_get(row, "is_default", 0)),
        "created_at": _row_get(row, "created_at", ""),
    }
    skills_raw = _row_get(row, "ai_skills")
    if skills_raw:
        try:    out["skills"] = json.loads(skills_raw)
        except Exception: out["skills"] = []
    else:
        out["skills"] = []
    if include_blob:
        out["file_blob"] = _row_get(row, "file_blob")
    return out


@app.route("/api/cvs", methods=["GET"])
def cvs_list():
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        cur = conn.execute(
            "SELECT id, filename, category, mime_type, ai_summary, ai_skills, "
            "is_default, created_at FROM cvs WHERE user_id = ? ORDER BY is_default DESC, id DESC",
            (uid,),
        )
        rows = cur.fetchall() or []
    return jsonify({"cvs": [_cv_to_dict(r) for r in rows]})


@app.route("/api/cvs", methods=["POST"])
def cvs_upload():
    uid, err = _auth_required()
    if err: return err
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    filename = (f.filename or "").strip()
    if not filename:
        return jsonify({"error": "Empty filename"}), 400
    file_bytes = f.read()
    if not file_bytes:
        return jsonify({"error": "Empty file"}), 400
    if len(file_bytes) > 10 * 1024 * 1024:
        return jsonify({"error": "File too large (>10MB)"}), 400

    low = filename.lower()
    if   low.endswith(".pdf"):  mime, parser = "application/pdf", _parse_pdf
    elif low.endswith(".docx"): mime, parser = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", _parse_docx
    elif low.endswith(".txt"):  mime, parser = "text/plain", _parse_txt
    else:
        return jsonify({"error": "Unsupported file type. Use PDF, DOCX, or TXT."}), 400

    try:
        parsed_text = parser(file_bytes)
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {str(e)}"}), 500

    # Frontend can override the auto-detected category
    override = (request.form.get("category") or "").strip().lower()
    category = override if override in CV_CATEGORIES else _detect_cv_category(parsed_text)

    # Pull skills + summary so we don't have to re-parse on every search.
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    summary, skills = "", []
    if api_key and _anthropic:
        try:
            r = _parse_cv_ai(parsed_text, api_key).get_json()
            summary = r.get("summary", "")
            skills  = r.get("skills", []) or []
        except Exception:
            skills  = extract_skills_from_text(parsed_text)
            summary = " ".join(parsed_text.split())[:240]
    else:
        skills  = extract_skills_from_text(parsed_text)
        summary = " ".join(parsed_text.split())[:240]

    now = datetime.utcnow().isoformat()

    with _db_conn() as conn:
        # First CV uploaded for this user becomes the default automatically
        cur = conn.execute("SELECT COUNT(*) AS n FROM cvs WHERE user_id = ?", (uid,))
        is_default = 1 if (_row_get(cur.fetchone(), "n", 0) == 0) else 0

        cur = conn.execute(
            "INSERT INTO cvs (user_id, filename, category, mime_type, file_blob, "
            "parsed_text, ai_summary, ai_skills, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" + (" RETURNING id" if USE_POSTGRES else ""),
            (uid, filename, category, mime, _psycopg2.Binary(file_bytes) if USE_POSTGRES else file_bytes,
             parsed_text[:200000], summary, json.dumps(skills), is_default, now),
        )
        if USE_POSTGRES:
            new_id = _row_get(cur.fetchone(), "id")
        else:
            new_id = cur.lastrowid

    return jsonify({
        "id":         new_id,
        "filename":   filename,
        "category":   category,
        "summary":    summary,
        "skills":     skills,
        "is_default": bool(is_default),
    })


@app.route("/api/cvs/<int:cv_id>", methods=["GET"])
def cvs_download(cv_id):
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        cur = conn.execute(
            "SELECT filename, mime_type, file_blob FROM cvs WHERE id = ? AND user_id = ?",
            (cv_id, uid),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    blob = _row_get(row, "file_blob")
    # psycopg2 returns memoryview for BYTEA; sqlite returns bytes
    if hasattr(blob, "tobytes"): blob = blob.tobytes()
    return send_file(
        io.BytesIO(blob),
        mimetype=_row_get(row, "mime_type", "application/octet-stream"),
        as_attachment=True,
        download_name=_row_get(row, "filename", f"cv_{cv_id}"),
    )


@app.route("/api/cvs/<int:cv_id>", methods=["DELETE"])
def cvs_delete(cv_id):
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        cur = conn.execute(
            "SELECT is_default FROM cvs WHERE id = ? AND user_id = ?", (cv_id, uid)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        was_default = bool(_row_get(row, "is_default", 0))
        conn.execute("DELETE FROM cvs WHERE id = ? AND user_id = ?", (cv_id, uid))
        # If the deleted one was the default, promote the most recent remaining
        if was_default:
            cur = conn.execute(
                "SELECT id FROM cvs WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,)
            )
            r2 = cur.fetchone()
            if r2:
                conn.execute("UPDATE cvs SET is_default = 1 WHERE id = ?",
                             (_row_get(r2, "id"),))
    return jsonify({"deleted": cv_id})


@app.route("/api/cvs/<int:cv_id>/default", methods=["POST"])
def cvs_set_default(cv_id):
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        cur = conn.execute("SELECT id FROM cvs WHERE id = ? AND user_id = ?",
                           (cv_id, uid))
        if not cur.fetchone():
            return jsonify({"error": "Not found"}), 404
        conn.execute("UPDATE cvs SET is_default = 0 WHERE user_id = ?", (uid,))
        conn.execute("UPDATE cvs SET is_default = 1 WHERE id = ? AND user_id = ?",
                     (cv_id, uid))
    return jsonify({"id": cv_id, "is_default": True})


@app.route("/api/cv-pick", methods=["POST"])
def cv_pick():
    """Given a job (title, description, url), return the best-matching CV.
    Tie-break order: exact category match → default CV → most recent."""
    uid, err = _auth_required()
    if err: return err
    data = request.get_json(force=True) or {}
    job  = data.get("job") or {}
    blob = (job.get("title", "") or "") + " " + (job.get("description", "") or "")
    job_cat, score = _categorize_text(blob)

    with _db_conn() as conn:
        cur = conn.execute(
            "SELECT id, filename, category, ai_summary, is_default "
            "FROM cvs WHERE user_id = ? ORDER BY is_default DESC, id DESC",
            (uid,),
        )
        rows = cur.fetchall() or []

    if not rows:
        return jsonify({"error": "No CVs uploaded yet",
                        "job_category": job_cat}), 404

    cvs = [_cv_to_dict(r) for r in rows]
    # 1) Exact category match
    match = next((c for c in cvs if c["category"] == job_cat), None)
    reason = f"Job classified as '{job_cat}' — matched by category."
    if not match:
        # 2) Fall back to default
        match = next((c for c in cvs if c["is_default"]), None)
        reason = f"No CV tagged '{job_cat}'. Using default CV."
    if not match:
        # 3) Fall back to most recent
        match = cvs[0]
        reason = f"No CV tagged '{job_cat}' and no default set. Using most recent."

    return jsonify({
        "cv":            match,
        "job_category":  job_cat,
        "match_score":   score,
        "reason":        reason,
        "all_cvs":       cvs,
    })


# ── AI field mapping (Chrome extension Phase 2) ──────────────────────────────

def _select_best_cv_row(uid, job_text):
    """Internal version of /api/cv-pick. Returns the best-match CV row as a
    dict with parsed_text loaded, or None if the user has no CVs yet.
    Tie-break order matches /api/cv-pick: category → default → most recent."""
    job_cat, _ = _categorize_text(job_text or "")
    with _db_conn() as conn:
        cur = conn.execute(
            "SELECT id, filename, category, parsed_text, is_default "
            "FROM cvs WHERE user_id = ? ORDER BY is_default DESC, id DESC",
            (uid,),
        )
        rows = cur.fetchall() or []
    if not rows:
        return None
    cvs = [{
        "id":          _row_get(r, "id"),
        "filename":    _row_get(r, "filename"),
        "category":    _row_get(r, "category"),
        "parsed_text": _row_get(r, "parsed_text") or "",
        "is_default":  bool(_row_get(r, "is_default")),
    } for r in rows]
    match = next((c for c in cvs if c["category"] == job_cat), None)
    if not match: match = next((c for c in cvs if c["is_default"]), None)
    if not match: match = cvs[0]
    return match


def _autofill_cors_response(rv):
    """Add CORS headers for the autofill cross-origin POST.

    Credentialed CORS (the bookmarklet sends `credentials:'include'` so the
    Render session cookie flows) requires the response to name a SPECIFIC
    origin in Access-Control-Allow-Origin — '*' is rejected by browsers when
    Allow-Credentials is true. Echo the request's Origin header so any ATS
    host (jobs.lever.co, boards.greenhouse.io, etc.) is accepted. Vary:
    Origin keeps shared caches from leaking one tenant's response to
    another. Same-origin fetches (no Origin header) fall back to '*'.
    """
    resp = app.make_response(rv) if isinstance(rv, tuple) else rv
    origin = request.headers.get("Origin", "")
    resp.headers["Access-Control-Allow-Origin"]      = origin or "*"
    resp.headers["Vary"]                             = "Origin"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Methods"]     = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"]     = "Content-Type"
    resp.headers["Access-Control-Max-Age"]           = "600"
    return resp


def _build_autofill_prompt(fields, page_context, cv_text):
    fields_json = json.dumps(
        [{"id":          f.get("id"),
          "label":       (f.get("label") or "")[:200],
          "type":        f.get("type"),
          "options":     (f.get("options") or [])[:25],
          "placeholder": (f.get("placeholder") or "")[:120],
          "maxlength":   f.get("maxlength")} for f in fields],
        indent=2,
    )
    page_line = (
        f"Page title: {page_context.get('title','')} | "
        f"H1: {page_context.get('h1','')} | "
        f"URL: {page_context.get('url','')}"
    )
    cv_block = cv_text or "(no CV available — answer from general profile only)"
    return f"""You are filling out a job application form on behalf of George Tupayachi.

GEORGE'S RESUME (use these for grounded answers — never invent companies or dates):
{cv_block}

PAGE CONTEXT:
{page_line}

FORM FIELDS TO FILL:
{fields_json}

For each field above, return ONE entry in a JSON array with this shape:
{{"id": "<field id verbatim>", "value": "<answer>", "skip": false, "reason": "<one-line why>", "confidence": <0.0-1.0>}}

Rules:
- If a field is a select/radio/button-group with options, the "value" MUST be one of the listed options (case-sensitive match preferred).
- For free-text/textarea, write 2-4 sentences in George's voice — professional, concrete, no filler. Reference real experience from the resume when relevant.
- Never invent employers, certifications, or dates not in the resume.
- If a field is too personal, too speculative, or you can't ground it in the resume + page context, set skip=true and explain briefly in reason.
- Respect maxlength when provided.
- Return ONLY the JSON array — no markdown, no prose before or after."""


@app.route("/api/autofill", methods=["POST", "OPTIONS"])
def autofill():
    """AI-powered field mapping for the Chrome extension Phase 2.

    Request body:
      {
        "fields": [{"id","label","type","options","placeholder","maxlength"}, ...],
        "page_context": {"url","hostname","title","h1","company_hint"},
        "cv_id": <int>  // optional; server picks best if omitted
      }
    Response (200): {"ai_used": true, "cv_used": {...}, "fills": [...]}
    Response (503): {"ai_used": false, "error": "AI not configured ..."}
    Response (502): {"ai_used": false, "error": "AI call failed: ...", "fills": []}
    """
    if request.method == "OPTIONS":
        return _autofill_cors_response(("", 204))

    uid, err = _auth_required()
    if err: return _autofill_cors_response(err)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not _anthropic:
        return _autofill_cors_response(
            (jsonify({"ai_used": False,
                      "error": "AI not configured — set ANTHROPIC_API_KEY",
                      "fills": []}), 503))

    data         = request.get_json(force=True) or {}
    fields       = (data.get("fields") or [])[:25]   # cap prompt size
    page_context = data.get("page_context") or {}
    cv_id        = data.get("cv_id")

    if not fields:
        return _autofill_cors_response(
            (jsonify({"ai_used": False, "fills": []}), 200))

    # Resolve CV: explicit cv_id wins, otherwise pick best for the page context.
    cv_row = None
    if cv_id:
        try:
            with _db_conn() as conn:
                row = conn.execute(
                    "SELECT id, filename, parsed_text FROM cvs "
                    "WHERE id = ? AND user_id = ?", (int(cv_id), uid)
                ).fetchone()
            if row:
                cv_row = {
                    "id":          _row_get(row, "id"),
                    "filename":    _row_get(row, "filename"),
                    "parsed_text": _row_get(row, "parsed_text") or "",
                }
        except Exception:
            pass
    if not cv_row:
        job_blob = " ".join([
            page_context.get("title", "") or "",
            page_context.get("h1", "") or "",
            page_context.get("company_hint", "") or "",
        ])
        cv_row = _select_best_cv_row(uid, job_blob)

    cv_text = (cv_row.get("parsed_text") or "")[:3500] if cv_row else ""
    prompt  = _build_autofill_prompt(fields, page_context, cv_text)

    try:
        client  = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text  = message.content[0].text.strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        fills = json.loads(match.group() if match else text)
        if not isinstance(fills, list):
            raise ValueError("AI did not return a JSON list")
    except Exception as e:
        return _autofill_cors_response(
            (jsonify({"ai_used": False,
                      "error": f"AI call failed: {e}",
                      "fills": []}), 502))

    # Validate + dedupe by id
    seen, clean = set(), []
    for f in fills:
        if not isinstance(f, dict): continue
        fid = str(f.get("id") or "").strip()
        if not fid or fid in seen: continue
        seen.add(fid)
        clean.append({
            "id":         fid,
            "value":      "" if f.get("value") is None else str(f.get("value")),
            "skip":       bool(f.get("skip")),
            "reason":     str(f.get("reason", ""))[:240],
            "confidence": float(f.get("confidence") or 0.0),
        })

    return _autofill_cors_response((jsonify({
        "ai_used": True,
        "cv_used": {"id": cv_row["id"], "filename": cv_row["filename"]} if cv_row else None,
        "fills":   clean,
    }), 200))


@app.route("/api/cover-letters", methods=["GET"])
def cover_letters_list():
    return jsonify({"letters": _list_cover_letters()})


@app.route("/api/cover-letters/<path:filename>", methods=["GET"])
def cover_letter_get(filename):
    # Path-traversal guard: only allow filenames that look like our convention
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "Invalid filename"}), 400
    if not filename.lower().startswith("cover_letter_") or not filename.lower().endswith(".txt"):
        return jsonify({"error": "Not a cover letter"}), 400
    path = _resolve_cover_letter_path(filename)
    if path is None:
        return jsonify({"error": "Not found"}), 404
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return jsonify({"filename": filename, "body": f.read()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cover-letters/suggest", methods=["POST"])
def cover_letter_suggest():
    """Given a job, suggest which existing letter to reuse + 2-3 customization
    lines tailored to this specific posting. Uses Claude when available, falls
    back to keyword overlap so it works without an API key."""
    data = request.get_json(force=True) or {}
    job  = data.get("job") or {}
    letters = _list_cover_letters()
    if not letters:
        return jsonify({"error": "No cover letters found",
                        "search_dirs": COVER_LETTER_DIRS}), 404

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and _anthropic:
        try:
            client = _anthropic.Anthropic(api_key=api_key)
            roles  = [{"filename": L["filename"], "role": L["role"]} for L in letters]
            desc_excerpt = re.sub(r"<[^>]+>", "", job.get("description", "") or "")[:1500]
            prompt = f"""You are helping a job seeker reuse one of their existing cover letters.

EXISTING COVER LETTERS (filename → role):
{json.dumps(roles, indent=2)}

JOB POSTING:
Title: {job.get("title", "")}
Company: {job.get("company_name", "")}
Description excerpt:
{desc_excerpt}

Choose the SINGLE best-matching existing letter to reuse for this job, then
suggest 2-3 short customization edits the candidate should make before sending.
Each edit should be specific to THIS posting (mention the company name, a
specific tool/responsibility from the job description, etc.) — not generic.

Return ONLY valid JSON (no markdown fence, no extra text):
{{
  "filename": "<one of the filenames above>",
  "reason": "<one sentence on why this letter is the best fit>",
  "edits": [
    "<specific edit 1 — what to change/add and why>",
    "<specific edit 2>",
    "<specific edit 3 (optional)>"
  ]
}}"""
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            m   = re.search(r"\{.*\}", raw, re.DOTALL)
            result = json.loads(m.group() if m else raw)

            # Validate filename is one we actually have
            picked = next((L for L in letters if L["filename"] == result.get("filename")), None)
            if picked is None:
                # AI hallucinated a filename — fall through to keyword match
                raise ValueError("AI returned unknown filename")

            picked_path = _resolve_cover_letter_path(picked["filename"])
            if picked_path is None:
                raise ValueError("Picked cover letter file disappeared")
            with open(picked_path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()

            return jsonify({
                "filename": picked["filename"],
                "role":     picked["role"],
                "reason":   result.get("reason", ""),
                "edits":    result.get("edits", []),
                "body":     body,
                "ai":       True,
            })
        except Exception:
            pass  # fall through to keyword match

    # Keyword fallback
    best, score = _suggest_cover_letter_keyword(job, letters)
    if best is None:
        return jsonify({"error": "Could not pick a letter"}), 404
    best_path = _resolve_cover_letter_path(best["filename"])
    if best_path is None:
        return jsonify({"error": "Cover letter file missing"}), 500
    with open(best_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    return jsonify({
        "filename": best["filename"],
        "role":     best["role"],
        "reason":   f"Best keyword overlap with job title (score {score}). Set ANTHROPIC_API_KEY for AI-powered suggestions.",
        "edits": [
            f"Replace the company name with “{job.get('company_name', '[Company]')}” throughout.",
            f"Update the role title to “{job.get('title', '[Role]')}” in the opening line.",
            "Add one sentence that references a specific tool or responsibility from the job description.",
        ],
        "body": body,
        "ai":   False,
    })


# ── Daily checklist (job-search routine page) ─────────────────────────────────

@app.route("/checklist")
def checklist_page():
    """Self-contained daily job-search routine. State is localStorage-only —
    we don't need a DB for what's essentially a daily todo list. Resets at
    local midnight (when the date changes)."""
    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Job-Search Plan</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
  body{background:#f0f4f8;padding-bottom:60px;font-size:.92rem}
  .app-header{background:linear-gradient(135deg,#0d6efd 0%,#0043a8 100%);color:white;padding:14px 16px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
  .app-header h1{font-size:1.2rem;margin:0;font-weight:700}
  .app-header .subtitle{font-size:.75rem;opacity:.85;margin:0}
  .block{background:white;border-radius:14px;padding:14px 16px;margin:12px;box-shadow:0 2px 6px rgba(0,0,0,.08)}
  .block.morning{border-left:5px solid #f59e0b}
  .block.midday {border-left:5px solid #0d6efd}
  .block.evening{border-left:5px solid #6f42c1}
  .block.weekly {border-left:5px solid #198754}
  .block-title{font-weight:700;font-size:.98rem;margin-bottom:4px;display:flex;align-items:center;gap:8px}
  .block-time{font-size:.72rem;color:#6c757d;text-transform:uppercase;letter-spacing:.4px;margin-bottom:10px}
  .check-item{display:flex;align-items:flex-start;gap:10px;padding:6px 0;cursor:pointer}
  .check-item input{margin-top:3px;flex-shrink:0;width:18px;height:18px;cursor:pointer}
  .check-item.done .label{color:#94a3b8;text-decoration:line-through}
  .label{font-size:.88rem;line-height:1.45;color:#212529}
  .label .hint{display:block;font-size:.75rem;color:#6c757d;margin-top:2px}
  .progress-bar-wrap{height:8px;background:#e9ecef;border-radius:4px;overflow:hidden;margin-top:8px}
  .progress-bar-fill{height:100%;background:linear-gradient(90deg,#0d6efd,#198754);transition:width .3s}
  .stat-row{display:flex;justify-content:space-around;padding:14px;background:white;border-radius:14px;margin:12px;box-shadow:0 2px 6px rgba(0,0,0,.08);text-align:center}
  .stat-row .v{font-size:1.4rem;font-weight:700;color:#0d6efd}
  .stat-row .k{font-size:.7rem;color:#6c757d;text-transform:uppercase;letter-spacing:.5px}
  .streak-flame{color:#fd7e14}
  .reset-btn{font-size:.7rem;padding:2px 10px}
  .quick-link{display:inline-flex;align-items:center;gap:5px;background:#0d6efd;color:white;border-radius:8px;padding:6px 12px;text-decoration:none;font-size:.78rem;font-weight:600;margin:3px 4px 3px 0}
  .quick-link:hover{background:#0043a8;color:white}
  .quick-link.linkedin{background:#0a66c2}
  .quick-link.linkedin:hover{background:#08518f}
  .quick-link.indeed{background:#003a9b}
  .quick-link.flexjobs{background:#1a936f}
  .quick-link.tracker{background:#6f42c1}
</style></head>
<body>
<div class="app-header">
  <div class="d-flex justify-content-between align-items-center">
    <div>
      <h1>📋 Today's Job-Search Plan</h1>
      <p class="subtitle" id="todayLabel"></p>
    </div>
    <a href="/" class="btn btn-sm btn-light">← App</a>
  </div>
</div>

<div class="stat-row">
  <div><div class="v" id="statDone">0</div><div class="k">Done today</div></div>
  <div><div class="v" id="statTotal">0</div><div class="k">Total tasks</div></div>
  <div><div class="v" id="statStreak"><span class="streak-flame">🔥</span><span id="streakNum">0</span></div><div class="k">Day streak</div></div>
  <div><button class="btn btn-outline-danger btn-sm reset-btn" onclick="resetToday()">Reset</button></div>
</div>

<div class="progress-bar-wrap" style="margin:0 12px"><div class="progress-bar-fill" id="overallBar" style="width:0%"></div></div>

<!-- Quick links -->
<div class="block">
  <div class="block-title"><i class="bi bi-link-45deg"></i> Quick links</div>
  <a class="quick-link" href="/" target="_blank"><i class="bi bi-search"></i> Job Search</a>
  <a class="quick-link tracker" href="/applications" target="_blank"><i class="bi bi-bookmark-check"></i> Tracker</a>
  <a class="quick-link linkedin" href="https://www.linkedin.com/jobs/search/?f_TPR=r86400&f_WT=2&keywords=technical%20support" target="_blank" rel="noopener"><i class="bi bi-linkedin"></i> LinkedIn (24h, Remote)</a>
  <a class="quick-link indeed" href="https://www.indeed.com/jobs?q=technical+support+remote&fromage=1&sc=0kf%3Aattr%28DSQF7%29%3B" target="_blank" rel="noopener"><i class="bi bi-search"></i> Indeed (24h)</a>
  <a class="quick-link flexjobs" href="https://www.flexjobs.com/search?searchkeyword=technical+support&jobtypes=Telecommuting" target="_blank" rel="noopener"><i class="bi bi-briefcase"></i> FlexJobs</a>
</div>

<div id="checklist"></div>

<script>
// ─────────────────────────────────────────────────────────────────
// Daily routine — designed for ~2-3 hours of focused job-search/day.
// Items with bonus=true count toward "daily target" but are nice-to-have.
// ─────────────────────────────────────────────────────────────────
const TASKS = [
  { block: 'morning', time: '15 min', title: 'Morning sweep (8-9am)', items: [
    { id: 'm1', label: 'Run job search app for fresh listings', hint: 'Visit / and click Find My Top Jobs' },
    { id: 'm2', label: 'Open LinkedIn jobs (last 24h, Remote filter)', hint: 'Use the quick link above' },
    { id: 'm3', label: 'Check FlexJobs ExpertApply queue', hint: 'Members.flexjobs.com → Saved searches' },
    { id: 'm4', label: 'Star 8-10 promising postings', hint: 'Save to tracker as Applied=No yet' },
  ]},
  { block: 'midday', time: '60-90 min', title: 'Apply (10am-12pm)', items: [
    { id: 'a1', label: 'Apply to 5-7 starred jobs',          hint: 'Direct via company site, not Easy Apply' },
    { id: 'a2', label: 'For each: pick existing cover letter + customize 3 lines', hint: 'Use the picker in the job detail modal' },
    { id: 'a3', label: 'Log every application in tracker',   hint: 'Use Apply & Log button — prevents duplicates' },
    { id: 'a4', label: 'Tailor CV keywords to top 2 roles',  hint: 'Mirror exact phrases from the job description' },
  ]},
  { block: 'evening', time: '30-45 min', title: 'Network + follow-ups (2-3pm)', items: [
    { id: 'n1', label: 'Send 3 LinkedIn connection requests to recruiters/hiring managers', hint: 'At companies you applied to today' },
    { id: 'n2', label: 'Follow up on 1-2 applications past 7 days', hint: 'Tracker → Sort by stale first' },
    { id: 'n3', label: 'Run Gmail scan to auto-update statuses', hint: 'Tracker → Scan Gmail button' },
    { id: 'n4', label: 'Engage with 2-3 LinkedIn posts in your industry', hint: 'Bonus visibility to recruiter searches' },
  ]},
  { block: 'weekly', time: 'Once a week', title: 'Weekly habits', items: [
    { id: 'w1', label: 'Update LinkedIn headline + Open to Work',     hint: 'Recruiters-only mode' },
    { id: 'w2', label: 'Post or comment on LinkedIn once',             hint: 'Builds visibility for searches' },
    { id: 'w3', label: 'Review tracker stats → drop sources that underperform', hint: 'Cut what isn\\'t working' },
    { id: 'w4', label: 'Ask 1 connection for a referral or coffee chat', hint: 'Referrals → 4x interview rate' },
  ]},
];

const ICONS = { morning: 'bi-sunrise',  midday: 'bi-clipboard-check', evening: 'bi-people', weekly: 'bi-calendar-week' };

function todayKey()   { return 'checklist:' + (new Date()).toISOString().slice(0,10); }
function streakKey()  { return 'checklist:streak'; }

function load() {
  try { return JSON.parse(localStorage.getItem(todayKey()) || '{}'); }
  catch(e) { return {}; }
}
function save(state) { localStorage.setItem(todayKey(), JSON.stringify(state)); }

function loadStreak() {
  try { return JSON.parse(localStorage.getItem(streakKey()) || '{"count":0,"last":""}'); }
  catch(e) { return {count:0, last:''}; }
}
function saveStreak(s) { localStorage.setItem(streakKey(), JSON.stringify(s)); }

function isCompleteEnough(state) {
  // "Complete" = at least all morning + midday core items checked
  const required = ['m1','m2','m3','m4','a1','a2','a3'];
  return required.every(k => state[k]);
}

function bumpStreakIfDone(state) {
  const today = (new Date()).toISOString().slice(0,10);
  const s = loadStreak();
  if (!isCompleteEnough(state)) return;
  if (s.last === today) return;            // already counted
  // Check if the streak should continue (yesterday) or reset (gap)
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0,10);
  if (s.last === yesterday) s.count += 1;
  else                       s.count = 1;
  s.last = today;
  saveStreak(s);
  document.getElementById('streakNum').textContent = s.count;
}

function render() {
  const state = load();
  const root = document.getElementById('checklist');
  root.innerHTML = TASKS.map(blk => `
    <div class="block ${blk.block}">
      <div class="block-title"><i class="bi ${ICONS[blk.block]||'bi-check2-square'}"></i> ${blk.title}</div>
      <div class="block-time">${blk.time}</div>
      ${blk.items.map(it => `
        <label class="check-item ${state[it.id] ? 'done' : ''}">
          <input type="checkbox" ${state[it.id] ? 'checked' : ''} onchange="toggle('${it.id}', this.checked)">
          <span class="label">${escapeHtml(it.label)}<span class="hint">${escapeHtml(it.hint || '')}</span></span>
        </label>
      `).join('')}
    </div>
  `).join('');

  const all = TASKS.flatMap(b => b.items);
  const done = all.filter(it => state[it.id]).length;
  document.getElementById('statDone').textContent = done;
  document.getElementById('statTotal').textContent = all.length;
  document.getElementById('overallBar').style.width = `${Math.round((done / all.length) * 100)}%`;
  document.getElementById('streakNum').textContent = loadStreak().count;
  document.getElementById('todayLabel').textContent =
    (new Date()).toLocaleDateString('en-US', { weekday:'long', month:'short', day:'numeric' });
}

function toggle(id, val) {
  const state = load();
  if (val) state[id] = true; else delete state[id];
  save(state);
  if (val) bumpStreakIfDone(state);
  render();
}

function resetToday() {
  if (!confirm('Reset today\\'s checklist?')) return;
  localStorage.removeItem(todayKey());
  render();
}

function escapeHtml(s){ return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

render();
</script>
</body></html>"""


# ── Extension auto-fill sandbox (dogfood without using a real application) ────

@app.route("/test-form")
def test_form_page():
    """Self-contained ATS-shaped form for dogfooding the Chrome extension.

    Covers every field the rule-based filler targets (Phase 1) plus a few
    custom free-text questions for Phase 2 (AI). Submitting renders a
    preview of what would have been sent — there is NO server roundtrip,
    no application data leaves the browser."""
    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auto-fill Sandbox · Job Search App</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
  body{background:#f0f4f8;padding:0 0 80px;font-size:.95rem}
  .hdr{background:linear-gradient(135deg,#0d6efd 0%,#0043a8 100%);color:white;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
  .hdr h1{font-size:1.35rem;margin:0;font-weight:700}
  .hdr .subtitle{font-size:.82rem;opacity:.9;margin:4px 0 0}
  .card{border:none;border-radius:14px;box-shadow:0 2px 6px rgba(0,0,0,.08);margin:14px;background:white}
  .card .ph{font-weight:700;color:#0d6efd;font-size:1rem;margin:0 0 14px;padding-bottom:6px;border-bottom:1px solid #e9ecef}
  .req::after{content:" *";color:#dc3545;font-weight:700}
  .ashby-radio{display:inline-flex;gap:8px;margin-top:4px}
  .ashby-radio .opt{padding:8px 18px;border:1.5px solid #ced4da;border-radius:8px;background:white;cursor:pointer;font-weight:500;font-size:.92rem;transition:all .15s}
  .ashby-radio .opt:hover{border-color:#0d6efd;background:#f0f7ff}
  .ashby-radio .opt.selected{background:#0d6efd;color:white;border-color:#0d6efd}
  .preview{background:#212529;color:#e9ecef;padding:14px;border-radius:8px;font-family:Consolas,Monaco,monospace;font-size:.78rem;white-space:pre-wrap;word-break:break-word;max-height:480px;overflow-y:auto}
  .instructions{background:#e7f5ff;border:1px solid #b3daff;border-radius:10px;padding:12px 14px;margin:14px;font-size:.86rem;line-height:1.45}
  .instructions code{background:white;padding:1px 6px;border-radius:3px;color:#0d4a8b}
  .instructions ol{margin:6px 0 0;padding-left:22px}
  .instructions li{margin:3px 0}
  .badge-phase{font-size:.66rem;padding:3px 7px;border-radius:999px;font-weight:600;letter-spacing:.3px;vertical-align:middle;margin-left:6px}
  .badge-phase1{background:#d4edda;color:#155724}
  .badge-phase2{background:#f3e8ff;color:#6f42c1}
  label{font-size:.86rem;font-weight:600;color:#212529;margin-bottom:3px}
  .help{font-size:.74rem;color:#6c757d;display:block;margin-top:2px}
</style>
</head>
<body>

<div class="hdr">
  <h1><i class="bi bi-clipboard-check"></i> Auto-fill Sandbox</h1>
  <p class="subtitle">Pretend ATS form — exercise every field type the Chrome extension targets without touching a real employer.</p>
</div>

<div class="instructions">
  <strong>How to use:</strong>
  <ol>
    <li>Make sure you've already logged into the web app (you're seeing this page, so you have).</li>
    <li>Click the extension's toolbar icon (the black feather) once so it caches your profile. Status should say <em>"Logged in — ready to fill"</em>.</li>
    <li>Then click <strong>"Auto-fill this form"</strong> in the popup. Filled fields flash green for ~1 second.</li>
    <li>The popup will list every field it matched (and why it skipped any). The toast at bottom-right shows totals.</li>
    <li>Click <strong>Submit</strong> at the bottom to render what would have been sent — nothing leaves the browser.</li>
  </ol>
  <div style="margin-top:8px;font-size:.78rem;color:#856404;background:#fff3cd;padding:6px 10px;border-radius:6px">
    <i class="bi bi-info-circle"></i>
    Auto-fire only triggers on supported ATS hosts. This sandbox is on <code>job-search-app-9pnx.onrender.com</code>, so you trigger it manually via the popup.
    Phase 2 (AI free-text) requires <code>ANTHROPIC_API_KEY</code> on the server; if unset, the AI fields stay empty and Phase 1 still runs. Check the AI badge in the main app header to confirm it's enabled.
  </div>
</div>

<form id="sandboxForm" autocomplete="off">

  <div class="card">
    <div class="card-body">
      <p class="ph">Personal Information <span class="badge-phase badge-phase1">Phase 1 rules</span></p>

      <div class="row g-3">
        <div class="col-md-6">
          <label for="legalName" class="req">Legal First & Last Name</label>
          <input type="text" id="legalName" name="legal_name" class="form-control" required>
          <span class="help">Should fill: George Tupayachi (combined-name rule)</span>
        </div>
        <div class="col-md-6">
          <label for="preferredName">Preferred Name</label>
          <input type="text" id="preferredName" name="preferred_name" class="form-control">
        </div>
        <div class="col-md-6">
          <label for="firstName">First Name</label>
          <input type="text" id="firstName" name="first_name" class="form-control">
        </div>
        <div class="col-md-6">
          <label for="lastName">Last Name</label>
          <input type="text" id="lastName" name="last_name" class="form-control">
        </div>
        <div class="col-md-7">
          <label for="email" class="req">Email Address</label>
          <input type="email" id="email" name="email" class="form-control" required>
        </div>
        <div class="col-md-5">
          <label for="phone" class="req">Phone Number</label>
          <input type="tel" id="phone" name="phone" class="form-control" required>
        </div>
        <div class="col-md-8">
          <label for="linkedin">LinkedIn URL</label>
          <input type="url" id="linkedin" name="linkedin" class="form-control" placeholder="https://www.linkedin.com/in/...">
        </div>
        <div class="col-md-4">
          <label for="portfolio">Portfolio / GitHub</label>
          <input type="url" id="portfolio" name="portfolio" class="form-control">
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Location <span class="badge-phase badge-phase1">Phase 1 rules</span></p>
      <div class="row g-3">
        <div class="col-md-12">
          <label for="loc">Current Location / City and State</label>
          <input type="text" id="loc" name="location" class="form-control" placeholder="Start typing your city...">
          <span class="help">Plain text input — the autocomplete handler will still try the dropdown wait, fall through to setNativeValue.</span>
        </div>
        <div class="col-md-8">
          <label for="street">Street Address</label>
          <input type="text" id="street" name="street" class="form-control">
        </div>
        <div class="col-md-4">
          <label for="apt">Address Line 2 / Apt</label>
          <input type="text" id="apt" name="apt" class="form-control">
        </div>
        <div class="col-md-5">
          <label for="city">City</label>
          <input type="text" id="city" name="city" class="form-control">
        </div>
        <div class="col-md-4">
          <label for="state">State</label>
          <select id="state" name="state" class="form-select">
            <option value="">Select...</option>
            <option value="AL">Alabama</option>
            <option value="CA">California</option>
            <option value="FL">Florida</option>
            <option value="NJ">New Jersey</option>
            <option value="NY">New York</option>
            <option value="TX">Texas</option>
          </select>
          <span class="help">Should fill: New Jersey (state_full) or NJ (abbreviation).</span>
        </div>
        <div class="col-md-3">
          <label for="zip">ZIP Code</label>
          <input type="text" id="zip" name="zip" class="form-control">
        </div>
        <div class="col-md-6">
          <label for="country">Country</label>
          <select id="country" name="country" class="form-select">
            <option value="">Select...</option>
            <option value="US">United States</option>
            <option value="CA">Canada</option>
            <option value="MX">Mexico</option>
            <option value="GB">United Kingdom</option>
          </select>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Work Authorization & Demographics <span class="badge-phase badge-phase1">Phase 1 rules</span></p>
      <div class="row g-3">
        <div class="col-md-6">
          <label class="d-block">Are you authorized to work in the United States?</label>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="work_auth" id="wa_yes" value="Yes"><label class="form-check-label" for="wa_yes">Yes</label></div>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="work_auth" id="wa_no"  value="No"><label class="form-check-label" for="wa_no">No</label></div>
        </div>
        <div class="col-md-6">
          <label class="d-block">Will you now or in the future require sponsorship?</label>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="sponsor" id="sp_yes" value="Yes"><label class="form-check-label" for="sp_yes">Yes</label></div>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="sponsor" id="sp_no"  value="No"><label class="form-check-label" for="sp_no">No</label></div>
        </div>
        <div class="col-md-4">
          <label for="gender">Gender</label>
          <select id="gender" name="gender" class="form-select">
            <option value="">Decline to answer</option>
            <option>Male</option><option>Female</option><option>Non-binary</option>
          </select>
        </div>
        <div class="col-md-4">
          <label for="race">Race / Ethnicity</label>
          <select id="race" name="race" class="form-select">
            <option value="">Decline to answer</option>
            <option>White</option>
            <option>Black or African American</option>
            <option>Asian</option>
            <option>Two or more races</option>
          </select>
        </div>
        <div class="col-md-4">
          <label for="hispanic">Hispanic or Latino?</label>
          <select id="hispanic" name="hispanic_latino" class="form-select">
            <option value="">Decline to answer</option>
            <option>Yes</option><option>No</option>
          </select>
        </div>
        <div class="col-md-6">
          <label for="vet">Veteran Status</label>
          <select id="vet" name="veteran" class="form-select">
            <option value="">Decline to answer</option>
            <option>Not a protected veteran</option>
            <option>Protected veteran</option>
          </select>
        </div>
        <div class="col-md-6">
          <label for="disab">Disability Status</label>
          <select id="disab" name="disability" class="form-select">
            <option value="">Decline to answer</option>
            <option>Yes</option><option>No</option>
            <option>I don't want to answer</option>
          </select>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Custom button-radio group <span class="badge-phase badge-phase1">Phase 1 rules (Ashby-style)</span></p>
      <label class="d-block mb-1">Are you willing to relocate for this position?</label>
      <div class="ashby-radio" id="relocateBtns">
        <button type="button" class="opt" data-val="Yes">Yes</button>
        <button type="button" class="opt" data-val="No">No</button>
      </div>
      <input type="hidden" name="willing_to_relocate" id="relocateVal">
      <span class="help">Should fill: Yes (button click — clickMatchingButton path).</span>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Logistics <span class="badge-phase badge-phase1">Phase 1 rules</span></p>
      <div class="row g-3">
        <div class="col-md-6">
          <label for="salary">Salary Expectation</label>
          <input type="text" id="salary" name="salary" class="form-control">
        </div>
        <div class="col-md-6">
          <label for="notice">Notice Period / Start Date</label>
          <input type="text" id="notice" name="notice" class="form-control">
        </div>
        <div class="col-md-6">
          <label class="d-block">Have you previously worked at this company?</label>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="prev_emp" id="pe_yes" value="Yes"><label class="form-check-label" for="pe_yes">Yes</label></div>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="prev_emp" id="pe_no"  value="No"><label class="form-check-label" for="pe_no">No</label></div>
        </div>
        <div class="col-md-6">
          <label for="clearance">Active Security Clearance</label>
          <input type="text" id="clearance" name="clearance" class="form-control">
        </div>
        <div class="col-md-12">
          <label for="sig" class="req">Electronic Signature</label>
          <input type="text" id="sig" name="esignature" class="form-control" required>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Work Experience <span class="badge-phase badge-phase1">Phase 1 rules</span></p>
      <div class="row g-3">
        <div class="col-md-7">
          <label for="emp_company">Company / Employer</label>
          <input type="text" id="emp_company" name="company" class="form-control">
          <span class="help">Should fill: Harrah's Casino (most recent from profile.work_experience[0]).</span>
        </div>
        <div class="col-md-5">
          <label for="emp_title">Job Title</label>
          <input type="text" id="emp_title" name="job_title" class="form-control">
        </div>
        <div class="col-md-6">
          <label for="emp_start">Employment Start Date</label>
          <input type="text" id="emp_start" name="employment_start_date" class="form-control" placeholder="YYYY-MM">
        </div>
        <div class="col-md-6">
          <label for="emp_end">Employment End Date</label>
          <input type="text" id="emp_end" name="employment_end_date" class="form-control" placeholder="YYYY-MM or leave blank">
        </div>
        <div class="col-md-12">
          <label class="d-block">Is this your current position?</label>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="current_position" id="cp_yes" value="Yes"><label class="form-check-label" for="cp_yes">Yes</label></div>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="current_position" id="cp_no"  value="No"><label class="form-check-label" for="cp_no">No</label></div>
          <span class="help">Should fill: Yes (Harrah's is profile.work_experience[0].current=true).</span>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Education <span class="badge-phase badge-phase1">Phase 1 rules</span></p>
      <div class="row g-3">
        <div class="col-md-7">
          <label for="school">School / University</label>
          <input type="text" id="school" name="school" class="form-control">
          <span class="help">Should fill: Franklin University (most recent from profile.education[0]).</span>
        </div>
        <div class="col-md-5">
          <label for="degree">Degree</label>
          <select id="degree" name="degree" class="form-select">
            <option value="">Select...</option>
            <option>High School</option>
            <option>Associates</option>
            <option>Bachelors</option>
            <option>Masters</option>
            <option>Doctorate</option>
            <option>Other</option>
          </select>
        </div>
        <div class="col-md-7">
          <label for="major">Field of Study / Major</label>
          <input type="text" id="major" name="field_of_study" class="form-control">
        </div>
        <div class="col-md-5">
          <label for="grad_year">Expected Graduation Year</label>
          <input type="text" id="grad_year" name="graduation_year" class="form-control" placeholder="YYYY-MM">
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Multi-row Education <span class="badge-phase badge-phase1">Phase 1 rules (v0.6)</span></p>
      <p style="font-size:.78rem;color:#6c757d;margin:0 0 10px">Two rows with indexed names (<code>education[0][...]</code>, <code>education[1][...]</code>). Pass 0 should fill row 0 with Franklin / Masters / Cybersecurity and row 1 with Stockton / Bachelors / Business Management Studies.</p>
      <div class="row g-3" style="border-top:1px dashed #dee2e6;padding-top:10px">
        <div class="col-md-7">
          <label for="edu0_school">Row 0 — School</label>
          <input type="text" id="edu0_school" name="education[0][school]" class="form-control">
        </div>
        <div class="col-md-5">
          <label for="edu0_degree">Row 0 — Degree</label>
          <input type="text" id="edu0_degree" name="education[0][degree]" class="form-control">
        </div>
        <div class="col-md-7">
          <label for="edu0_major">Row 0 — Field of Study</label>
          <input type="text" id="edu0_major" name="education[0][field_of_study]" class="form-control">
        </div>
        <div class="col-md-5">
          <label for="edu0_grad">Row 0 — Graduation Year</label>
          <input type="text" id="edu0_grad" name="education[0][graduation_year]" class="form-control">
        </div>
      </div>
      <div class="row g-3" style="border-top:1px dashed #dee2e6;padding-top:10px;margin-top:14px">
        <div class="col-md-7">
          <label for="edu1_school">Row 1 — School</label>
          <input type="text" id="edu1_school" name="education[1][school]" class="form-control">
        </div>
        <div class="col-md-5">
          <label for="edu1_degree">Row 1 — Degree</label>
          <input type="text" id="edu1_degree" name="education[1][degree]" class="form-control">
        </div>
        <div class="col-md-7">
          <label for="edu1_major">Row 1 — Field of Study</label>
          <input type="text" id="edu1_major" name="education[1][field_of_study]" class="form-control">
        </div>
        <div class="col-md-5">
          <label for="edu1_grad">Row 1 — Graduation Year</label>
          <input type="text" id="edu1_grad" name="education[1][graduation_year]" class="form-control">
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Multi-row Work History <span class="badge-phase badge-phase1">Phase 1 rules (v0.6)</span></p>
      <p style="font-size:.78rem;color:#6c757d;margin:0 0 10px">Two rows. Row 0 should fill from <code>work_experience[0]</code> (Harrah's Casino / Bartender / 2024-07 / current=Yes), row 1 from <code>work_experience[1]</code> (Ocean Casino Resort / Casino Shift Manager / 2021-06 / 2022-07).</p>
      <div class="row g-3" style="border-top:1px dashed #dee2e6;padding-top:10px">
        <div class="col-md-7">
          <label for="we0_company">Row 0 — Company</label>
          <input type="text" id="we0_company" name="work_experience[0][company]" class="form-control">
        </div>
        <div class="col-md-5">
          <label for="we0_title">Row 0 — Job Title</label>
          <input type="text" id="we0_title" name="work_experience[0][title]" class="form-control">
        </div>
        <div class="col-md-4">
          <label for="we0_start">Row 0 — Start</label>
          <input type="text" id="we0_start" name="work_experience[0][start_date]" class="form-control">
        </div>
        <div class="col-md-4">
          <label for="we0_end">Row 0 — End</label>
          <input type="text" id="we0_end" name="work_experience[0][end_date]" class="form-control">
        </div>
        <div class="col-md-4">
          <label for="we0_current">Row 0 — Current?</label>
          <input type="text" id="we0_current" name="work_experience[0][current]" class="form-control">
        </div>
      </div>
      <div class="row g-3" style="border-top:1px dashed #dee2e6;padding-top:10px;margin-top:14px">
        <div class="col-md-7">
          <label for="we1_company">Row 1 — Company</label>
          <input type="text" id="we1_company" name="work_experience[1][company]" class="form-control">
        </div>
        <div class="col-md-5">
          <label for="we1_title">Row 1 — Job Title</label>
          <input type="text" id="we1_title" name="work_experience[1][title]" class="form-control">
        </div>
        <div class="col-md-4">
          <label for="we1_start">Row 1 — Start</label>
          <input type="text" id="we1_start" name="work_experience[1][start_date]" class="form-control">
        </div>
        <div class="col-md-4">
          <label for="we1_end">Row 1 — End</label>
          <input type="text" id="we1_end" name="work_experience[1][end_date]" class="form-control">
        </div>
        <div class="col-md-4">
          <label for="we1_current">Row 1 — Current?</label>
          <input type="text" id="we1_current" name="work_experience[1][current]" class="form-control">
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">"Add Another" Education <span class="badge-phase badge-phase1">Phase 1 rules (v0.7)</span></p>
      <p style="font-size:.78rem;color:#6c757d;margin:0 0 10px">Workday/Greenhouse style — only row 0 is rendered initially. The engine should detect the <strong>+ Add Another Education</strong> button, click it once to surface row 1, then fill both rows.</p>
      <div id="add-edu-rows">
        <div class="row g-3" data-row="0" style="border-top:1px dashed #dee2e6;padding-top:10px">
          <div class="col-md-7">
            <label for="add_edu0_school">Row 0 — School</label>
            <input type="text" id="add_edu0_school" name="education[0][school]" class="form-control">
          </div>
          <div class="col-md-5">
            <label for="add_edu0_degree">Row 0 — Degree</label>
            <input type="text" id="add_edu0_degree" name="education[0][degree]" class="form-control">
          </div>
        </div>
      </div>
      <button type="button" id="add-edu-btn" class="btn btn-outline-primary btn-sm mt-3">+ Add Another Education</button>
      <script>
        (() => {
          let next = 1;
          document.getElementById('add-edu-btn').addEventListener('click', () => {
            const i = next++;
            const row = document.createElement('div');
            row.className = 'row g-3';
            row.setAttribute('data-row', String(i));
            row.style.borderTop = '1px dashed #dee2e6';
            row.style.paddingTop = '10px';
            row.style.marginTop = '14px';
            row.innerHTML = `
              <div class="col-md-7">
                <label for="add_edu${i}_school">Row ${i} — School</label>
                <input type="text" id="add_edu${i}_school" name="education[${i}][school]" class="form-control">
              </div>
              <div class="col-md-5">
                <label for="add_edu${i}_degree">Row ${i} — Degree</label>
                <input type="text" id="add_edu${i}_degree" name="education[${i}][degree]" class="form-control">
              </div>`;
            document.getElementById('add-edu-rows').appendChild(row);
          });
        })();
      </script>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Resume / CV Upload <span class="badge-phase badge-phase1">Phase 1 rules (v0.10)</span></p>
      <p style="font-size:.78rem;color:#6c757d;margin:0 0 10px">The engine should detect this file input, decode the baked-in CV blob (your default from the CV library), and assign it via DataTransfer. The file name and "Selected: ..." text should appear next to the input.</p>
      <div class="mb-3">
        <label for="resume_upload" class="form-label">Upload your resume / CV</label>
        <input type="file" id="resume_upload" name="resume" class="form-control" accept=".pdf,.doc,.docx">
        <div id="resume_status" style="font-size:.85rem;color:#198754;margin-top:6px"></div>
        <script>
          (() => {
            const el = document.getElementById('resume_upload');
            el.addEventListener('change', () => {
              const f = el.files && el.files[0];
              document.getElementById('resume_status').textContent =
                f ? `✓ Selected: ${f.name} (${(f.size/1024).toFixed(1)} KB)` : '';
            });
          })();
        </script>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Pronouns + DEI + Country Code + Built-in Q&amp;A <span class="badge-phase badge-phase1">Phase 1 rules (v0.9)</span></p>
      <p style="font-size:.78rem;color:#6c757d;margin:0 0 10px">Pronouns select, phone country-code split, and built-in qa_defaults for common questions.</p>
      <div class="row g-3">
        <div class="col-md-6">
          <label for="pronouns">Pronouns</label>
          <select id="pronouns" name="pronouns" class="form-select">
            <option value="">Select...</option>
            <option>He/Him</option>
            <option>She/Her</option>
            <option>They/Them</option>
            <option>Prefer not to say</option>
          </select>
        </div>
        <div class="col-md-3">
          <label for="phone_country_code">Country Code</label>
          <input type="text" id="phone_country_code" name="country_code" class="form-control" placeholder="+1">
        </div>
        <div class="col-md-3">
          <label for="phone_number">Phone Number</label>
          <input type="text" id="phone_number" name="phone_number" class="form-control">
        </div>
        <div class="col-md-12">
          <label class="d-block">Are you over 18 years of age?</label>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="over_18" id="o18_yes" value="Yes"><label class="form-check-label" for="o18_yes">Yes</label></div>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="over_18" id="o18_no"  value="No"><label class="form-check-label" for="o18_no">No</label></div>
        </div>
        <div class="col-md-6">
          <label for="referral_source">How did you hear about us?</label>
          <input type="text" id="referral_source" name="referral_source" class="form-control">
        </div>
        <div class="col-md-6">
          <label class="d-block">Are you able to work remotely?</label>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="remote_ok" id="rok_yes" value="Yes"><label class="form-check-label" for="rok_yes">Yes</label></div>
          <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="remote_ok" id="rok_no"  value="No"><label class="form-check-label" for="rok_no">No</label></div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Native Date Inputs <span class="badge-phase badge-phase1">Phase 1 rules (v0.8)</span></p>
      <p style="font-size:.78rem;color:#6c757d;margin:0 0 10px">HTML5 native <code>type="date"</code> and <code>type="month"</code>. Profile stores "2024-07" — engine should normalize: date inputs get "2024-07-01", month inputs get "2024-07" as-is.</p>
      <div class="row g-3">
        <div class="col-md-6">
          <label for="emp_start_date">Employment Start Date (type=date)</label>
          <input type="date" id="emp_start_date" name="employment_start_date" class="form-control">
        </div>
        <div class="col-md-6">
          <label for="emp_start_month">Employment Start Month (type=month)</label>
          <input type="month" id="emp_start_month" name="employment_start_month" class="form-control">
        </div>
        <div class="col-md-6">
          <label for="grad_month">Graduation Month (type=month)</label>
          <input type="month" id="grad_month" name="graduation_month" class="form-control">
        </div>
        <div class="col-md-6">
          <label for="grad_full_date">Graduation Date (type=date)</label>
          <input type="date" id="grad_full_date" name="graduation_date" class="form-control">
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <p class="ph">Free-text questions <span class="badge-phase badge-phase2">Phase 2 AI</span></p>
      <div class="mb-3">
        <label for="why">Why are you interested in this role?</label>
        <textarea id="why" name="why" class="form-control" rows="3" maxlength="500"></textarea>
        <span class="help">Won't fill until ANTHROPIC_API_KEY is set on Render.</span>
      </div>
      <div class="mb-2">
        <label for="story">Describe a time you handled a difficult customer.</label>
        <textarea id="story" name="story" class="form-control" rows="3" maxlength="500"></textarea>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <button type="submit" class="btn btn-primary btn-lg w-100">
        <i class="bi bi-eye"></i> Submit (preview only — nothing leaves your browser)
      </button>
    </div>
  </div>

</form>

<div class="card" id="previewCard" style="display:none">
  <div class="card-body">
    <p class="ph">What would have been sent <span class="badge-phase badge-phase1" style="background:#fff3cd;color:#856404">Preview only</span></p>
    <div class="preview" id="previewJson"></div>
  </div>
</div>

<script>
  // Ashby-style button-radio wiring
  document.querySelectorAll('#relocateBtns .opt').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#relocateBtns .opt').forEach(b => b.classList.remove('selected'));
      btn.classList.add('selected');
      document.getElementById('relocateVal').value = btn.dataset.val;
    });
  });

  // Submit handler: just preview, don't POST anywhere.
  document.getElementById('sandboxForm').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const obj = {};
    fd.forEach((v, k) => obj[k] = v);
    // Reflect the button-radio's hidden value
    obj.willing_to_relocate = document.getElementById('relocateVal').value || '';
    document.getElementById('previewJson').textContent = JSON.stringify(obj, null, 2);
    document.getElementById('previewCard').style.display = '';
    document.getElementById('previewCard').scrollIntoView({behavior:'smooth', block:'start'});
  });
</script>

</body></html>"""


# ── Mobile bookmarklet (autofill without an extension) ──────────────────────

import base64 as _base64

_AUTOFILL_JS_PATH = os.path.join(
    os.path.dirname(__file__), "chrome-extension", "autofill.js"
)
_AUTOFILL_JS_MTIME = None
_AUTOFILL_JS_CACHED = None


def _get_default_cv_blob(uid):
    """Returns (file_bytes, filename, mime_type) for the user's default CV
    (or most recent if none flagged default). Returns None if no CVs."""
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT filename, mime_type, file_blob FROM cvs "
                "WHERE user_id = ? AND is_default = 1 LIMIT 1",
                (uid,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT filename, mime_type, file_blob FROM cvs "
                    "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                    (uid,),
                ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    blob = _row_get(row, "file_blob")
    if blob is None:
        return None
    # psycopg2 returns memoryview for BYTEA; sqlite returns bytes
    if isinstance(blob, memoryview):
        blob = bytes(blob)
    return (blob, _row_get(row, "filename") or "cv.pdf",
            _row_get(row, "mime_type") or "application/pdf")

def _read_autofill_js():
    """Cache the autofill.js content keyed on mtime so dev reloads pick up
    edits without an app restart."""
    global _AUTOFILL_JS_MTIME, _AUTOFILL_JS_CACHED
    try:
        mtime = os.path.getmtime(_AUTOFILL_JS_PATH)
    except OSError:
        return ""
    if mtime != _AUTOFILL_JS_MTIME:
        with open(_AUTOFILL_JS_PATH, encoding="utf-8") as f:
            _AUTOFILL_JS_CACHED = f.read()
        _AUTOFILL_JS_MTIME = mtime
    return _AUTOFILL_JS_CACHED or ""


@app.route("/bookmarklet")
def bookmarklet_install():
    """Install page for the mobile autofill bookmarklet. The user lands here
    after logging in, drags / long-presses the link into their bookmarks,
    then taps it on any ATS form to fill. Works on iOS Safari + Android
    Chrome (Chrome extensions aren't supported on mobile Chrome)."""
    uid, err = _auth_required()
    if err: return err
    # The bookmarklet itself: one-line script injection. The cache-buster
    # query string forces a fresh fetch each tap so profile updates take
    # effect immediately.
    site = "https://job-search-app-9pnx.onrender.com"
    bookmarklet = (
        "javascript:(()=>{const s=document.createElement('script');"
        f"s.src='{site}/bookmarklet/run.js?t='+Date.now();"
        "s.onerror=()=>alert('Job Search Autofill: open "
        f"{site}"
        " first and sign in.');"
        "document.head.appendChild(s);})()"
    )
    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mobile Autofill Bookmarklet · Job Search App</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
  body{background:#f0f4f8;padding:0 0 80px;font-size:.95rem;line-height:1.5}
  .hdr{background:linear-gradient(135deg,#0d6efd 0%,#0043a8 100%);color:white;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
  .hdr h1{font-size:1.35rem;margin:0;font-weight:700}
  .hdr .subtitle{font-size:.82rem;opacity:.9;margin:4px 0 0}
  .card{border:none;border-radius:14px;box-shadow:0 2px 6px rgba(0,0,0,.08);margin:14px;background:white}
  .ph{font-weight:700;color:#0d6efd;font-size:1rem;margin:0 0 14px;padding-bottom:6px;border-bottom:1px solid #e9ecef}
  .install-btn{display:inline-block;background:#198754;color:white;padding:12px 22px;border-radius:10px;font-weight:700;font-size:1.05rem;text-decoration:none;box-shadow:0 4px 12px rgba(25,135,84,.35)}
  .install-btn:hover{background:#157347;color:white}
  .step{padding:8px 12px;background:#f8f9fa;border-left:4px solid #0d6efd;border-radius:6px;margin-bottom:8px;font-size:.9rem}
  .step b{color:#0d6efd}
  pre{background:#212529;color:#e9ecef;padding:14px;border-radius:8px;font-size:.78rem;overflow-x:auto;white-space:pre-wrap;word-break:break-all;line-height:1.45}
  .platform{margin-top:14px;padding:12px 16px;background:#fff8e1;border:1px solid #ffe082;border-radius:10px;font-size:.86rem}
  .platform h4{font-size:.95rem;margin:0 0 8px;color:#856404;font-weight:700}
</style>
</head>
<body>

<div class="hdr">
  <h1><i class="bi bi-bookmark-fill"></i> Mobile Autofill Bookmarklet</h1>
  <p class="subtitle">Tap one bookmark on any application form to fill it. Works on iOS Safari + Android Chrome — no extension needed.</p>
</div>

<div class="card">
  <div class="card-body">
    <p class="ph">1. Install the bookmark</p>
    <p>On <strong>desktop Chrome / Firefox</strong>, drag this button to your bookmarks bar:</p>
    <p style="text-align:center;margin:18px 0">
      <a class="install-btn" href=\"""" + bookmarklet.replace('"', '&quot;') + """\">📝 Job Search Autofill</a>
    </p>
    <p style="font-size:.8rem;color:#6c757d">Browsers may strip <code>javascript:</code> URLs from dragged links for security. If drag-to-bookmark doesn't work, see the platform-specific instructions below.</p>

    <div class="platform">
      <h4>📱 iOS Safari</h4>
      <ol style="padding-left:22px;margin:0">
        <li>Tap the <strong>Share</strong> button at the bottom of Safari → <strong>Add Bookmark</strong>.</li>
        <li>Save the bookmark anywhere (Favorites is good).</li>
        <li>Open <strong>Bookmarks</strong> (book icon at bottom) → tap <strong>Edit</strong>.</li>
        <li>Tap the bookmark you just saved → tap the <strong>URL</strong> field → <strong>delete</strong> everything → paste the code below.</li>
        <li>Rename it "Autofill". Done.</li>
      </ol>
    </div>

    <div class="platform">
      <h4>📱 Android Chrome</h4>
      <ol style="padding-left:22px;margin:0">
        <li>Tap the <strong>⋮</strong> menu → <strong>Bookmarks</strong> → ⋮ → <strong>Add new bookmark</strong>.</li>
        <li>Name it "Autofill", paste the code below into the URL field. Save.</li>
        <li>Trigger by typing "Autofill" into the URL bar and tapping the suggestion. Or open Bookmarks and tap it.</li>
      </ol>
      <p style="margin:8px 0 0;font-size:.78rem;color:#856404"><strong>Heads up:</strong> Chrome on Android sometimes blocks <code>javascript:</code> URLs when typed/pasted directly. If yours strips out the <code>javascript:</code> prefix, type the address bar prefix manually then paste the rest.</p>
    </div>

    <p style="margin-top:18px"><strong>Bookmarklet code</strong> (copy/paste):</p>
    <pre id="bm-code">""" + bookmarklet.replace('<', '&lt;').replace('>', '&gt;') + """</pre>
    <button class="btn btn-outline-primary btn-sm" onclick="navigator.clipboard.writeText(document.getElementById('bm-code').textContent).then(()=>this.textContent='Copied ✓')">📋 Copy to clipboard</button>
  </div>
</div>

<div class="card">
  <div class="card-body">
    <p class="ph">2. Use it</p>
    <ol style="padding-left:22px">
      <li>Open a job application form (Greenhouse, Ashby, Lever, Workable, etc.) in your phone's browser.</li>
      <li>Make sure you're signed in to <a href=\"""" + site + """\">""" + site + """</a> on the SAME browser. The session cookie is what authorizes the fill.</li>
      <li>Tap the <strong>Autofill</strong> bookmark in your bookmarks bar / library.</li>
      <li>A green toast appears at the bottom right: <em>"Auto-filled N fields"</em>. Review every field, then tap Submit on the form yourself.</li>
    </ol>
    <p style="margin-top:14px"><a class="btn btn-primary" href="/test-form">Try it on the sandbox /test-form first →</a></p>
  </div>
</div>

<div class="card">
  <div class="card-body">
    <p class="ph">How it works</p>
    <p style="font-size:.88rem">Tapping the bookmarklet injects a <code>&lt;script&gt;</code> tag pointing at <code>/bookmarklet/run.js</code>. Your session cookie travels along (SameSite=None, secure). The server returns the same autofill engine the Chrome extension uses, with your profile + answer defaults inlined. The engine runs the rule pass, then if needed posts unfilled fields to <code>/api/autofill</code> for AI grounding from your default CV — same Phase 2 path as the desktop extension.</p>
    <p style="font-size:.88rem">No data leaves your browser except: <strong>(1)</strong> the cookie-authenticated fetch of <code>/bookmarklet/run.js</code> (your profile flows in), and <strong>(2)</strong> the optional <code>/api/autofill</code> POST when there are custom free-text questions (form labels + page URL flow out). Nothing is submitted on your behalf.</p>
  </div>
</div>

<div class="card" style="border-left:4px solid #6f42c1">
  <div class="card-body">
    <p class="ph">For strict-CSP ATSes (Greenhouse / Ashby / Workday)</p>
    <p>This standard bookmarklet fetches the engine over HTTPS from <code>job-search-app-9pnx.onrender.com</code>. <strong>Strict-CSP ATSes block that fetch</strong> (Greenhouse + Workday + Ashby all do). If you tap the bookmark on those sites and nothing happens, use the offline version instead:</p>
    <p style="text-align:center;margin:14px 0"><a class="btn btn-primary" href="/bookmarklet/inline">→ Install the offline (CSP-bypass) bookmarklet</a></p>
    <p style="font-size:.82rem;color:#6c757d">Trade-off: the offline bookmarklet has Phase 1 rule fills only — no AI on free-text questions, because the AI call needs a cross-origin fetch that CSP also blocks. For Lever / Workable / less-strict ATSes, stick with the regular bookmarklet above.</p>
  </div>
</div>

</body></html>"""


@app.route("/bookmarklet/run.js")
def bookmarklet_run_js():
    """Serve the autofill engine + the user's profile + a one-line invocation
    that runs the engine on whatever page injected this script. The
    bookmarklet is `<script src="/bookmarklet/run.js?t=...">` injected into
    the ATS form's DOM, so this code runs in that page's origin with the
    Render session cookie attached (SameSite=None + Secure)."""
    uid = _current_user_id()
    if uid is None:
        # Return a tiny JS that alerts the user — script-tag injection can't
        # easily handle non-200, so we 200 with a "you're locked" message.
        body = (
            'alert("Job Search Autofill: you are not signed in. Open '
            'https://job-search-app-9pnx.onrender.com on this device first, '
            'unlock with your passcode, then tap the bookmark again.");'
        )
        resp = app.make_response((body, 200))
        resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
        return resp

    profile = _build_full_profile(uid)
    engine  = _read_autofill_js()
    if not engine:
        body = 'alert("Job Search Autofill: engine missing on server.");'
        resp = app.make_response((body, 500))
        resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
        return resp

    # The engine is an IIFE that registers window.__jobTrackerAutofill. After
    # it runs, we kick off Phase 1 + Phase 2 with the inlined profile.
    invocation = """
(async () => {
  // Inject a green toast on the page so the user knows we ran.
  function toast(text, kind) {
    let t = document.getElementById('__jt_bookmarklet_toast');
    if (t) t.remove();
    t = document.createElement('div');
    t.id = '__jt_bookmarklet_toast';
    const bg = kind === 'err' ? '#b91c1c'
             : kind === 'warn' ? '#b45309'
             : '#198754';
    t.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:2147483647;'
      + 'padding:12px 16px;border-radius:8px;color:#fff;font:600 13px/1.4 system-ui,sans-serif;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,.25);max-width:340px;'
      + 'background:' + bg;
    t.textContent = text;
    document.documentElement.appendChild(t);
    setTimeout(() => t.remove(), 12000);
  }
  if (!window.__jobTrackerAutofill) {
    toast('Autofill engine failed to load', 'err'); return;
  }
  const profile = window.__jt_bookmarklet_profile;
  try {
    const r1 = await window.__jobTrackerAutofill.run(profile, {
      autologUrl: 'https://job-search-app-9pnx.onrender.com/api/applications/autolog',
    });
    // Phase 2 AI — fetches /api/autofill cross-origin with cookies.
    const unfilled = window.__jobTrackerAutofill.collectUnfilledFields(profile);
    let aiCount  = 0;
    let aiStatus = '';   // '', 'skipped', 'network', 'config', 'server'
    if (unfilled.length) {
      try {
        const ar = await fetch('https://job-search-app-9pnx.onrender.com/api/autofill', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            fields: unfilled,
            page_context: {
              url: location.href, hostname: location.hostname,
              title: (document.title || '').slice(0, 200),
              h1: (document.querySelector('h1')?.innerText || '').slice(0, 200),
            },
          }),
        });
        if (ar.ok) {
          const body = await ar.json();
          if (Array.isArray(body.fills) && body.fills.length) {
            const apply = await window.__jobTrackerAutofill.applyAiFills(body.fills);
            aiCount = apply.applied;
          } else {
            aiStatus = 'skipped';
          }
        } else if (ar.status === 503) {
          aiStatus = 'config';
        } else {
          aiStatus = 'server';
        }
      } catch (e) {
        aiStatus = 'network';
      }
    }
    const ruleCount = r1.filled - aiCount;
    let msg = 'Auto-filled ' + ruleCount + ' field' + (ruleCount === 1 ? '' : 's');
    if (aiCount)               msg += ' + ' + aiCount + ' AI';
    else if (aiStatus === 'network') msg += ' — AI skipped (network/CORS)';
    else if (aiStatus === 'config')  msg += ' — AI skipped (no API key)';
    else if (aiStatus === 'server')  msg += ' — AI skipped (server error)';
    else if (aiStatus === 'skipped' && unfilled.length) msg += ' — AI returned no fills';
    msg += ' — review before submitting';
    toast(msg, aiStatus && aiStatus !== '' && !aiCount ? 'warn' : 'ok');
  } catch (e) {
    toast('Autofill error: ' + (e.message || e), 'err');
  }
})();
"""
    # Bake the user's default CV into the response as base64 so the engine
    # can auto-fill resume file inputs without a separate cross-origin
    # fetch (which CSP often blocks on strict ATSes anyway).
    cv_data = _get_default_cv_blob(uid)
    cv_js = ""
    if cv_data:
        blob, filename, mime = cv_data
        try:
            cv_b64 = _base64.b64encode(blob).decode("ascii")
            cv_js = (
                "window.__jt_cv_b64 = " + json.dumps(cv_b64) + ";\n"
                + "window.__jt_cv_filename = " + json.dumps(filename) + ";\n"
                + "window.__jt_cv_mime = " + json.dumps(mime) + ";\n"
            )
        except Exception:
            pass

    body = (
        "window.__jt_bookmarklet_profile = " + json.dumps(profile) + ";\n"
        + cv_js
        + engine + "\n"
        + invocation
    )
    resp = app.make_response((body, 200))
    resp.headers["Content-Type"]  = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/bookmarklet/inline")
def bookmarklet_inline():
    """Self-contained bookmarklet that bypasses strict-CSP ATSes like
    Greenhouse / Ashby / Workday. The entire autofill engine + the user's
    profile are encoded into the javascript: URL itself, so the bookmarklet
    runs without making ANY cross-origin fetches. CSP doesn't apply to
    javascript: URLs that the user triggers themselves (per spec).

    Trade-off: Phase 2 AI is offline. The /api/autofill cross-origin POST
    would still be blocked by connect-src on the same hosts. Phase 1 rule
    fills cover ~80% of typical ATS forms; for the remaining free-text
    questions the user just types those manually on mobile-CSP-locked
    sites. On Lever / Workable / friendlier ATSes, the regular
    /bookmarklet (external fetch) works AND keeps Phase 2 AI on."""
    from urllib.parse import quote
    uid, err = _auth_required()
    if err: return err

    profile = _build_full_profile(uid)
    engine  = _read_autofill_js()
    if not engine:
        return ("Autofill engine missing on server", 500)

    # Phase 1 only — strip the Phase 2 AI block from the invocation. On
    # mobile-CSP-locked sites the cross-origin POST would error anyway;
    # baking it in would just add bytes and a confusing console error.
    invocation = """
(async () => {
  function toast(text, kind) {
    let t = document.getElementById('__jt_bookmarklet_toast');
    if (t) t.remove();
    t = document.createElement('div');
    t.id = '__jt_bookmarklet_toast';
    const bg = kind === 'err' ? '#b91c1c'
             : kind === 'warn' ? '#b45309'
             : '#198754';
    t.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:2147483647;'
      + 'padding:12px 16px;border-radius:8px;color:#fff;font:600 13px/1.4 system-ui,sans-serif;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,.25);max-width:340px;'
      + 'background:' + bg;
    t.textContent = text;
    document.documentElement.appendChild(t);
    setTimeout(() => t.remove(), 12000);
  }
  if (!window.__jobTrackerAutofill) { toast('Autofill engine failed to load', 'err'); return; }
  try {
    const r = await window.__jobTrackerAutofill.run(window.__jt_bookmarklet_profile, {
      autologUrl: 'https://job-search-app-9pnx.onrender.com/api/applications/autolog',
    });
    toast('Auto-filled ' + r.filled + ' field' + (r.filled === 1 ? '' : 's')
      + ' — review before submitting');
  } catch (e) {
    toast('Autofill error: ' + (e.message || e), 'err');
  }
})();
"""

    # The full self-contained JS — engine + profile + invocation
    full_js = (
        "window.__jt_bookmarklet_profile = " + json.dumps(profile) + ";"
        + engine
        + invocation
    )

    # Wrap in IIFE then URL-encode for the javascript: URL. quote(safe="")
    # is paranoid (encodes everything that isn't unreserved) which is what
    # we want — bookmark URLs go through the address bar parser which is
    # strict about syntax characters like (, ), ;, =, &.
    iife = "(()=>{" + full_js + "})()"
    bookmarklet_url = "javascript:" + quote(iife, safe="")

    site = "https://job-search-app-9pnx.onrender.com"

    # HTML-escape the bookmarklet text for safe rendering inside <pre>.
    bm_for_pre = bookmarklet_url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # For the draggable <a href>, only & needs to be escaped (already not <>).
    bm_for_href = bookmarklet_url.replace("&", "&amp;").replace('"', "&quot;")

    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inline Mobile Bookmarklet · Job Search App</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
  body{background:#f0f4f8;padding:0 0 80px;font-size:.95rem;line-height:1.5}
  .hdr{background:linear-gradient(135deg,#6f42c1 0%,#3a1d6a 100%);color:white;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
  .hdr h1{font-size:1.35rem;margin:0;font-weight:700}
  .hdr .subtitle{font-size:.82rem;opacity:.9;margin:4px 0 0}
  .card{border:none;border-radius:14px;box-shadow:0 2px 6px rgba(0,0,0,.08);margin:14px;background:white}
  .ph{font-weight:700;color:#6f42c1;font-size:1rem;margin:0 0 14px;padding-bottom:6px;border-bottom:1px solid #e9ecef}
  .install-btn{display:inline-block;background:#6f42c1;color:white;padding:12px 22px;border-radius:10px;font-weight:700;font-size:1.05rem;text-decoration:none;box-shadow:0 4px 12px rgba(111,66,193,.35)}
  .install-btn:hover{background:#5e35a8;color:white}
  pre{background:#212529;color:#e9ecef;padding:14px;border-radius:8px;font-size:.7rem;overflow-x:auto;word-break:break-all;line-height:1.45;max-height:300px}
  .platform{margin-top:14px;padding:12px 16px;background:#fff8e1;border:1px solid #ffe082;border-radius:10px;font-size:.86rem}
  .platform h4{font-size:.95rem;margin:0 0 8px;color:#856404;font-weight:700}
  .warning{background:#f8d7da;border:1px solid #f5c6cb;color:#721c24;padding:12px 16px;border-radius:10px;margin:14px;font-size:.86rem}
</style>
</head>
<body>

<div class="hdr">
  <h1><i class="bi bi-shield-lock"></i> Inline Mobile Bookmarklet (CSP-bypass)</h1>
  <p class="subtitle">Self-contained — the entire engine + your profile are baked into the bookmark itself. Works on strict-CSP ATSes (Greenhouse, Ashby, Workday) where the regular bookmarklet's external fetch is blocked.</p>
</div>

<div class="warning">
  <strong>Trade-off:</strong> Phase 2 AI (free-text answers from your CV) is OFF in this mode — those questions need a cross-origin fetch that CSP also blocks. You'll still get all the Phase 1 rule fills (name / contact / address / EEO / education / work history / button-radios). For Lever / Workable / less-strict ATSes, prefer the regular <a href="/bookmarklet">/bookmarklet</a> which keeps AI on.
</div>

<div class="card">
  <div class="card-body">
    <p class="ph">1. Install the bookmark</p>
    <p>Bookmark size: roughly """ + str(len(bookmarklet_url) // 1024) + """ KB. Modern browsers handle this fine; very old mobile browsers may truncate.</p>
    <p style="text-align:center;margin:18px 0">
      <a class="install-btn" href=\"""" + bm_for_href + """\">📝 Job Search Autofill (Offline)</a>
    </p>
    <div class="platform">
      <h4>📱 iOS Safari</h4>
      <ol style="padding-left:22px;margin:0">
        <li>Tap <strong>Share</strong> → <strong>Add Bookmark</strong>. Save anywhere.</li>
        <li>Open <strong>Bookmarks</strong> → <strong>Edit</strong> → tap the bookmark → tap the URL field → <strong>delete it all</strong> → tap the Copy button below this list, then paste into the URL field.</li>
        <li>Rename it "Autofill (Offline)". Done.</li>
      </ol>
    </div>
    <div class="platform">
      <h4>📱 Android Chrome</h4>
      <ol style="padding-left:22px;margin:0">
        <li>Tap <strong>⋮</strong> menu → <strong>Bookmarks</strong> → <strong>⋮</strong> → <strong>Add new bookmark</strong>.</li>
        <li>Name "Autofill (Offline)", paste the code below into the URL field. Save.</li>
        <li>To trigger: type "Autofill" into the URL bar and tap the suggestion.</li>
      </ol>
    </div>
    <p style="margin-top:18px"><strong>Bookmarklet code</strong> (copy/paste):</p>
    <pre id="bm-code">""" + bm_for_pre + """</pre>
    <button class="btn btn-outline-primary btn-sm" onclick="navigator.clipboard.writeText(document.getElementById('bm-code').textContent).then(()=>this.textContent='Copied ✓')">📋 Copy to clipboard</button>
  </div>
</div>

<div class="card">
  <div class="card-body">
    <p class="ph">2. Use it</p>
    <ol style="padding-left:22px">
      <li>Open the strict-CSP application form (Greenhouse, Ashby, Workday).</li>
      <li>Tap the <strong>Autofill (Offline)</strong> bookmark.</li>
      <li>Green toast confirms: <em>"Auto-filled N fields"</em>. The free-text questions still need manual answers.</li>
      <li>Review every field. Click Submit yourself.</li>
    </ol>
  </div>
</div>

<div class="card">
  <div class="card-body">
    <p class="ph">How it bypasses CSP</p>
    <p style="font-size:.88rem">CSP's <code>script-src</code> directive controls EXTERNAL <code>&lt;script src&gt;</code> tags, NOT bookmarklets. Bookmarklets execute via the address bar (a user-initiated action) and run outside the page's script-src restrictions. By inlining the entire engine + your profile into the <code>javascript:</code> URL, there are no external fetches at all — just code that runs in the page's context the moment you tap the bookmark.</p>
    <p style="font-size:.88rem">If you update your profile on the web app (new CV, edited Q&A defaults), the bookmark's inlined profile becomes stale. Re-visit this page and re-install to refresh it.</p>
  </div>
</div>

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
