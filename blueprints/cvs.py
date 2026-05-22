"""
blueprints/cvs.py  —  CV library + auto-categorization.

CVs are stored as BLOBs in the cvs table so they survive Render redeploys
(free tier has no persistent FS). Each CV is auto-tagged with a category
(it / cybersecurity / developer / bartender / casino / hospitality / general)
based on its text — the /api/cv-pick endpoint then routes the right CV to
the right job (IT job → IT CV, casino job → bartender CV).

Routes:
  GET    /api/cvs                       list user's CVs (no blob)
  POST   /api/cvs                       upload (PDF/DOCX/TXT, ≤10MB)
  GET    /api/cvs/<id>                  download the binary
  DELETE /api/cvs/<id>                  delete; promotes most-recent to default
  POST   /api/cvs/<id>/default          mark a CV as the default
  POST   /api/cv-pick                   pick best CV for a given job

_select_best_cv_row() is exported for the autofill route (still in app.py)
and the bookmarklet — same logic as /api/cv-pick but returns parsed_text.
"""
import io
import json
import os
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file

from db import (
    USE_POSTGRES, _auth_required, _db_conn, _row_get, _psycopg2,
)
from cv_parsing import _parse_pdf, _parse_docx, _parse_txt, _parse_cv_ai
from scoring import extract_skills_from_text

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None


bp = Blueprint("cvs", __name__)


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


def _select_best_cv_row(uid, job_text):
    """Internal version of /api/cv-pick — used by the autofill route and the
    bookmarklet. Returns the best-match CV row as a dict with parsed_text
    loaded, or None if the user has no CVs yet. Tie-break order matches
    /api/cv-pick: category → default → most recent."""
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


@bp.route("/api/cvs", methods=["GET"])
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


@bp.route("/api/cvs", methods=["POST"])
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
            r = _parse_cv_ai(parsed_text, api_key)
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


@bp.route("/api/cvs/<int:cv_id>", methods=["GET"])
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


@bp.route("/api/cvs/<int:cv_id>", methods=["DELETE"])
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


@bp.route("/api/cvs/<int:cv_id>/default", methods=["POST"])
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


@bp.route("/api/cv-pick", methods=["POST"])
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
