"""
cv_parsing.py  —  CV/resume file parsing primitives.

Pure-ish helpers: take file bytes (or text + API key), return plain Python
(text or dict). No Flask coupling. The /api/parse-cv route and the CV upload
flow both build on these.

pdfminer.six and python-docx are optional imports — degrade with a clear
RuntimeError if a needed parser isn't installed.
"""
import io
import json
import os
import re

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


def parse_file(file_bytes, filename):
    """Dispatch on extension. Returns plain text or raises ValueError."""
    low = (filename or "").lower()
    if   low.endswith(".pdf"):  return _parse_pdf(file_bytes)
    elif low.endswith(".docx"): return _parse_docx(file_bytes)
    elif low.endswith(".txt"):  return _parse_txt(file_bytes)
    raise ValueError("Unsupported file type. Use PDF, DOCX, or TXT.")


def _parse_cv_ai(text, api_key):
    """Use Claude to extract a rich structured profile from CV text.

    Returns a plain dict — callers wrap with jsonify if they want a Flask
    response. Raises if the model is unreachable or returns bad JSON.
    """
    if not _anthropic:
        raise RuntimeError("anthropic SDK not installed")
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

    return {
        "skills":           result.get("skills", []),
        "summary":          result.get("summary", ""),
        "skill_count":      len(result.get("skills", [])),
        "job_titles":       result.get("job_titles", []),
        "experience_years": result.get("experience_years", 0),
        "education":        result.get("education", ""),
        "search_terms":     result.get("search_terms", []),
        "ai_parsed":        True,
    }
