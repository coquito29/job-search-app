"""
blueprints/cover_letters.py  —  Cover-letter library routes.

Routes:
  GET  /api/cover-letters              list (filename + role + preview)
  GET  /api/cover-letters/<filename>   download a single letter body
  POST /api/cover-letters/suggest      AI (or keyword fallback) picks the best
                                       reusable letter for a given job

Letters live as cover_letter_*.txt files on disk. The library is read-only
from this blueprint's perspective — uploads/edits happen outside the app.
"""
import json
import os
import re

from flask import Blueprint, jsonify, request

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None


bp = Blueprint("cover_letters", __name__)


# Search both: the bundled cover_letters/ subfolder (production deploy) and
# the repo's parent directory (local dev — drafts often live in project root).
# First-found wins per filename.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


@bp.route("/api/cover-letters", methods=["GET"])
def cover_letters_list():
    return jsonify({"letters": _list_cover_letters()})


@bp.route("/api/cover-letters/<path:filename>", methods=["GET"])
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


@bp.route("/api/cover-letters/suggest", methods=["POST"])
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
