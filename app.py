"""
app.py  —  Remote Job Search Mobile PWA
Sources: Apify · Adzuna · JSearch  (paid/keyed APIs only — free sources removed for signal quality)
AI Cover Letters: Claude Haiku (set ANTHROPIC_API_KEY env var)
"""
import io, json, os, re, secrets, socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests as http_req
from flask import Flask, request, jsonify, render_template, send_file, session
from werkzeug.security import generate_password_hash, check_password_hash

# Pure helpers (scoring, ATS classification, remote detection, date parsing).
# Re-exported below so existing call sites in this file don't need updating.
from scoring import (
    KNOWN_SKILLS,
    NO_GO_TERMS,
    ATS_FAST, ATS_WALLED, ATS_BLOCKED, ATS_BOOST_DOMAINS,
    SIGNIN_WALL_DOMAINS,
    UserProfile,
    _classify_ats, _url_host_matches, _aggregator_denylist,
    _parse_years_required, _days_since_posted,
    score_job, is_remote_job, _clean_salary, fmt_date,
    extract_skills_from_text,
)

# DB plumbing (sqlite/Postgres adapter) + auth helpers.
from db import (
    USE_POSTGRES, APPLICATIONS_DB, APP_STATUSES,
    _db_conn, _init_applications_db,
    _row_get, _default_user, _current_user_id, _auth_required,
)

# CV/resume parsing primitives (pdfminer/docx/txt + AI extraction).
from cv_parsing import _parse_pdf, _parse_docx, _parse_txt, _parse_cv_ai

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
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

# Expose psycopg2 (or None on sqlite-only installs) for the few call sites that
# need _psycopg2.Binary(...) to wrap BYTEA blobs. The actual import + sqlite
# fallback lives in db.py.
from db import _psycopg2

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

# DB schema bootstrap. Safe to call repeatedly — _init_applications_db is
# idempotent (CREATE TABLE IF NOT EXISTS + try/except ALTERs).
_init_applications_db()
print(f"[db] Applications tracker backend: {'Postgres' if USE_POSTGRES else 'SQLite (' + APPLICATIONS_DB + ')'}")


TOP_JOBS_LIMIT = 50
FETCH_LIMIT = 100  # per Apify search — ~$1.20/search at $0.012/job (opt-in from UI)
DEFAULT_TIMERANGE = "7d"

# Constants, dataclasses, and pure helpers (KNOWN_SKILLS, NO_GO_TERMS,
# ATS_FAST/WALLED/BLOCKED, _classify_ats, UserProfile, …) live in scoring.py
# and are imported above. Keep adding new scoring logic there, not here.


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
# (Scoring/ATS/skill helpers extracted to scoring.py — imported at top of file.)

# CV parsing primitives (_parse_pdf, _parse_docx, _parse_txt, _parse_cv_ai)
# live in cv_parsing.py — imported at top of file.


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
            result = _parse_cv_ai(text, api_key)
            _billing_increment_quota(_db_conn, uid, amount=1)
            return jsonify(result)
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


# Applications tracker routes live in blueprints/applications.py — registered
# at the bottom of this file. New CRUD routes for /api/applications/* should
# go in that blueprint, not here.


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


# Cover-letter library routes + helpers live in blueprints/cover_letters.py.
# Registered at the bottom of this file.


# CV library routes + categorization helpers live in blueprints/cvs.py.
# _select_best_cv_row is exported from there for the autofill route below.


# ── AI field mapping (Chrome extension Phase 2) ──────────────────────────────
# _select_best_cv_row lives in blueprints/cvs.py — imported below where the
# autofill route needs it.
from blueprints.cvs import _select_best_cv_row


def _autofill_cors_response(rv):
    """Add the CORS headers the Chrome extension needs to a Flask response
    or a (body, status) tuple."""
    resp = app.make_response(rv) if isinstance(rv, tuple) else rv
    resp.headers["Access-Control-Allow-Origin"]      = "*"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Methods"]     = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"]     = "Content-Type"
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


# Cover-letter routes live in blueprints/cover_letters.py.


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
    t.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:2147483647;'
      + 'padding:12px 16px;border-radius:8px;color:#fff;font:600 13px/1.4 system-ui,sans-serif;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,.25);max-width:340px;'
      + 'background:' + (kind === 'err' ? '#b91c1c' : '#198754');
    t.textContent = text;
    document.documentElement.appendChild(t);
    setTimeout(() => t.remove(), 12000);
  }
  if (!window.__jobTrackerAutofill) {
    toast('Autofill engine failed to load', 'err'); return;
  }
  const profile = window.__jt_bookmarklet_profile;
  try {
    const r1 = await window.__jobTrackerAutofill.run(profile);
    // Phase 2 AI — fetches /api/autofill cross-origin with cookies.
    const unfilled = window.__jobTrackerAutofill.collectUnfilledFields(profile);
    let aiCount = 0;
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
          if (Array.isArray(body.fills)) {
            const apply = await window.__jobTrackerAutofill.applyAiFills(body.fills);
            aiCount = apply.applied;
          }
        }
      } catch (e) { /* AI is opportunistic — fail silently */ }
    }
    const ruleCount = r1.filled - aiCount;
    toast(
      'Auto-filled ' + ruleCount + ' field' + (ruleCount === 1 ? '' : 's')
      + (aiCount ? ' + ' + aiCount + ' AI' : '')
      + ' — review before submitting'
    );
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
    t.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:2147483647;'
      + 'padding:12px 16px;border-radius:8px;color:#fff;font:600 13px/1.4 system-ui,sans-serif;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,.25);max-width:340px;'
      + 'background:' + (kind === 'err' ? '#b91c1c' : '#198754');
    t.textContent = text;
    document.documentElement.appendChild(t);
    setTimeout(() => t.remove(), 12000);
  }
  if (!window.__jobTrackerAutofill) { toast('Autofill engine failed to load', 'err'); return; }
  try {
    const r = await window.__jobTrackerAutofill.run(window.__jt_bookmarklet_profile);
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


# ── Blueprint registration ───────────────────────────────────────────────────
# Routes are migrating out of this file one functional area at a time. Register
# each blueprint here once it's extracted. See blueprints/__init__.py for the
# pattern.
from blueprints.applications   import bp as applications_bp
from blueprints.cover_letters  import bp as cover_letters_bp
from blueprints.cvs            import bp as cvs_bp
app.register_blueprint(applications_bp)
app.register_blueprint(cover_letters_bp)
app.register_blueprint(cvs_bp)


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
