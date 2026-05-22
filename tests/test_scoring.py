"""Unit tests for the pure helpers in scoring.py.

These exist so the next time someone tweaks the scoring weights or the ATS
tables, regressions in the obvious behaviour fail loudly here instead of
silently changing rankings in prod.
"""
from datetime import datetime, timedelta

import pytest

from scoring import (
    UserProfile,
    _classify_ats,
    _clean_salary,
    _days_since_posted,
    _parse_years_required,
    _url_host_matches,
    extract_skills_from_text,
    fmt_date,
    is_remote_job,
    score_job,
)


# ── _classify_ats ─────────────────────────────────────────────────────────────

def test_classify_ats_returns_unknown_for_empty():
    assert _classify_ats("") == ("unknown", "")
    assert _classify_ats(None) == ("unknown", "")


def test_classify_ats_detects_greenhouse_fast():
    cls, name = _classify_ats("https://boards.greenhouse.io/acme/jobs/123")
    assert cls == "fast"
    assert name == "Greenhouse"


def test_classify_ats_detects_workday_walled():
    cls, name = _classify_ats("https://acme.myworkdayjobs.com/careers/job/123")
    assert cls == "walled"
    assert name == "Workday"


def test_classify_ats_detects_aggregator_blocked():
    cls, _ = _classify_ats("https://jobleads.com/listing/456")
    assert cls == "blocked"


def test_classify_ats_longest_match_wins():
    # 'boards.greenhouse.io' (20 chars) should beat 'greenhouse.io' (13 chars).
    cls, name = _classify_ats("https://boards.greenhouse.io/role")
    assert (cls, name) == ("fast", "Greenhouse")


# ── _url_host_matches ─────────────────────────────────────────────────────────

def test_url_host_matches_case_insensitive():
    assert _url_host_matches("HTTPS://Indeed.com/jobs", ["indeed.com"]) is True
    assert _url_host_matches("https://example.com", ["indeed.com"]) is False
    assert _url_host_matches("", ["indeed.com"]) is False
    assert _url_host_matches(None, ["indeed.com"]) is False


# ── _parse_years_required ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("5+ years of experience required", 5),
    ("Minimum of 3 years experience", 3),
    ("at least 2 years of relevant experience", 2),
    # Range pattern matches the lower bound when the phrasing is direct.
    ("1-3 years of experience", 1),
    ("entry level role, no experience needed", 0),
    ("", 0),
    # Out-of-range numbers (e.g. "100 years of history") are ignored.
    ("Founded 100 years ago. Entry-level role.", 0),
])
def test_parse_years_required(text, expected):
    assert _parse_years_required(text) == expected


# ── _days_since_posted ────────────────────────────────────────────────────────

def test_days_since_posted_recent():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    assert _days_since_posted(today) == 0


def test_days_since_posted_week_old():
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    assert _days_since_posted(week_ago) == 7


def test_days_since_posted_invalid_returns_sentinel():
    assert _days_since_posted("") == 999
    assert _days_since_posted(None) == 999
    assert _days_since_posted("not a date") == 999


# ── is_remote_job ─────────────────────────────────────────────────────────────

def test_is_remote_job_default_remote():
    assert is_remote_job({"title": "Support Engineer", "location": "Remote", "description": ""}) is True


def test_is_remote_job_rejects_onsite_location():
    assert is_remote_job({"title": "x", "location": "On-site, NYC", "description": ""}) is False


def test_is_remote_job_rejects_hybrid_title():
    assert is_remote_job({"title": "Support Engineer (Hybrid)", "location": "Remote", "description": ""}) is False


def test_is_remote_job_rejects_onsite_in_description():
    job = {"title": "Engineer", "location": "Remote", "description": "On-site only — must report to office."}
    assert is_remote_job(job) is False


# ── _clean_salary ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("", ""),
    ("-", ""),
    ("None", ""),
    ("50000-75000", "$50k–$75k"),
    ("$120,000", "$120k"),
    ("up to 200000", "$200k"),
])
def test_clean_salary(raw, expected):
    assert _clean_salary(raw) == expected


# ── fmt_date ──────────────────────────────────────────────────────────────────

def test_fmt_date_iso():
    assert fmt_date("2026-01-15") == "Jan 15, 2026"


def test_fmt_date_empty():
    assert fmt_date("") == ""
    assert fmt_date(None) == ""


def test_fmt_date_timestamp():
    # 2026-01-01 00:00:00 UTC = 1767225600
    assert fmt_date(1767225600).startswith("Jan 01, 2026") or fmt_date(1767225600).startswith("Dec 31, 2025")


# ── extract_skills_from_text ──────────────────────────────────────────────────

def test_extract_skills_picks_up_known_skills():
    text = "Skilled in Python, SQL, and customer support. Familiar with Linux."
    found = extract_skills_from_text(text)
    # Known skills are matched case-insensitively
    assert "python" in [s.lower() for s in found]
    assert "sql"    in [s.lower() for s in found]
    assert "linux"  in [s.lower() for s in found]


def test_extract_skills_fallback_for_thin_text():
    # No known skills, but repeated words ≥2× should populate the fallback list.
    text = "alpha beta alpha beta gamma alpha"
    found = extract_skills_from_text(text)
    assert len(found) > 0


# ── score_job ─────────────────────────────────────────────────────────────────

def _profile(skills):
    return UserProfile(summary="", skills=skills, no_go_terms=["cold calling"])


def test_score_job_returns_expected_shape():
    job = {
        "title": "Tier 1 Support Engineer",
        "description": "Entry level. Help desk. Bilingual Spanish a plus.",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "days_old": 2,
        "salary": "$50k",
    }
    result = score_job(job, _profile(["python", "help desk"]))
    expected_keys = {
        "match_pct", "match_why", "matched_skills", "hire_signals",
        "years_req", "days_old", "is_ats", "ats_class", "ats_name",
        "blockers", "fits",
    }
    assert expected_keys.issubset(result.keys())
    assert 0 <= result["match_pct"] <= 100


def test_score_job_high_match_for_entry_level_greenhouse():
    job = {
        "title": "Junior IT Support",
        "description": "Entry level role, will train, no experience required. Help desk experience a plus.",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "days_old": 1,
    }
    score_high = score_job(job, _profile(["help desk", "it support"]))["match_pct"]
    assert score_high >= 60


def test_score_job_penalises_senior_role():
    job_senior = {
        "title": "Senior Support Engineer",
        "description": "10+ years of experience required. Architect-level role.",
        "url": "https://example.com",
        "days_old": 1,
    }
    job_entry = {
        "title": "Junior Support Engineer",
        "description": "Entry level, no experience required.",
        "url": "https://example.com",
        "days_old": 1,
    }
    profile = _profile(["python"])
    assert score_job(job_senior, profile)["match_pct"] < score_job(job_entry, profile)["match_pct"]


def test_score_job_blocks_clearance_requirement():
    job = {
        "title": "Cyber Analyst",
        "description": "Active security clearance required. TS/SCI preferred.",
        "url": "https://example.com",
        "days_old": 1,
    }
    result = score_job(job, _profile(["cybersecurity"]))
    assert any("Clearance" in b or "TS/SCI" in b for b in result["blockers"])


def test_score_job_no_go_term_drops_score():
    job_normal = {
        "title": "Support Specialist",
        "description": "Help customers via chat and email.",
        "url": "https://example.com", "days_old": 1,
    }
    job_nogo = dict(job_normal, description="Cold calling required. Commission only.")
    profile = _profile(["customer support"])
    assert score_job(job_nogo, profile)["match_pct"] < score_job(job_normal, profile)["match_pct"]
