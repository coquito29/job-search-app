"""Tests for blueprints/cvs.py — categorization helpers + the routes that
don't require Anthropic credentials.

We don't test the AI categorization paths here; those go through the network
to Claude. The keyword path (_categorize_text, _select_best_cv_row's fallback
order) is the contract worth locking in.
"""
import io
import os
import tempfile

import pytest


@pytest.fixture
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="jobsearch-cvs-test-")
    path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("APPLICATIONS_DB", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FOUNDER_USER_KEYS", raising=False)
    # Ensure the AI path is skipped — we want the keyword fallbacks
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import importlib
    import db as db_mod
    importlib.reload(db_mod)
    import blueprints.cvs as cvs_mod
    importlib.reload(cvs_mod)
    import app as app_mod
    importlib.reload(app_mod)

    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c

    try:
        os.remove(path)
        os.rmdir(tmpdir)
    except OSError:
        pass


# ── _categorize_text (pure helper) ────────────────────────────────────────────

@pytest.mark.parametrize("text, expected_category", [
    ("Senior SOC Analyst with SIEM and Splunk experience", "cybersecurity"),
    ("Help Desk Tier 1 IT Support", "it"),
    ("Software Engineer · React · Node.js · Backend", "developer"),
    ("Bartender at the hotel bar", "bartender"),
    ("Casino Shift Manager — Pit Boss", "casino"),
    ("Hotel front desk concierge", "hospitality"),
    ("Generic resume with no obvious focus", "general"),
    ("", "general"),
])
def test_categorize_text(text, expected_category):
    from blueprints.cvs import _categorize_text
    cat, score = _categorize_text(text)
    assert cat == expected_category


def test_categorize_text_returns_score():
    from blueprints.cvs import _categorize_text
    cat, score = _categorize_text("SOC analyst, incident response, SIEM, Splunk")
    assert cat == "cybersecurity"
    assert score > 0


# ── /api/cvs routes ───────────────────────────────────────────────────────────

def test_cvs_list_empty(client):
    rv = client.get("/api/cvs")
    assert rv.status_code == 200
    assert rv.get_json()["cvs"] == []


def test_cvs_upload_rejects_missing_file(client):
    rv = client.post("/api/cvs", data={})
    assert rv.status_code == 400


def test_cvs_upload_rejects_unsupported_format(client):
    rv = client.post(
        "/api/cvs",
        data={"file": (io.BytesIO(b"binary stuff"), "resume.exe")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400


def test_cvs_upload_txt_roundtrip(client):
    body = b"George Tupayachi\nHelp Desk Tier 1 IT Support experience.\nLinux, Windows, Active Directory."
    rv = client.post(
        "/api/cvs",
        data={"file": (io.BytesIO(body), "my_cv.txt")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200, rv.get_data(as_text=True)
    data = rv.get_json()
    assert data["filename"] == "my_cv.txt"
    assert data["category"] == "it"  # detected via keyword fallback
    assert data["is_default"] is True  # first CV becomes default

    # List shows it
    rv = client.get("/api/cvs")
    cvs = rv.get_json()["cvs"]
    assert len(cvs) == 1
    assert cvs[0]["id"] == data["id"]

    # Download returns the bytes
    rv = client.get(f"/api/cvs/{data['id']}")
    assert rv.status_code == 200
    assert rv.data == body


def test_cvs_delete_promotes_remaining_to_default(client):
    def upload(text, name):
        return client.post(
            "/api/cvs",
            data={"file": (io.BytesIO(text.encode()), name)},
            content_type="multipart/form-data",
        ).get_json()

    a = upload("Help Desk Tier 1 support", "it.txt")
    b = upload("Bartender at hotel bar", "bartender.txt")

    assert a["is_default"] is True
    assert b["is_default"] is False  # only the first upload becomes default

    # Deleting the default should promote the most-recent remaining
    rv = client.delete(f"/api/cvs/{a['id']}")
    assert rv.status_code == 200

    rv = client.get("/api/cvs")
    remaining = rv.get_json()["cvs"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == b["id"]
    assert remaining[0]["is_default"] is True


def test_cv_pick_404_when_no_cvs(client):
    rv = client.post("/api/cv-pick", json={"job": {"title": "Help Desk"}})
    assert rv.status_code == 404


def test_cv_pick_routes_by_category(client):
    # Upload one IT CV and one bartender CV
    client.post(
        "/api/cvs",
        data={"file": (io.BytesIO(b"IT Support tier 1 help desk"), "it.txt")},
        content_type="multipart/form-data",
    )
    client.post(
        "/api/cvs",
        data={"file": (io.BytesIO(b"Bartender mixologist hotel bar"), "bartender.txt")},
        content_type="multipart/form-data",
    )

    # IT job should route to IT CV
    rv = client.post("/api/cv-pick", json={"job": {
        "title": "Help Desk Specialist",
        "description": "Tier 1 IT support, ticketing, Active Directory.",
    }})
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["job_category"] == "it"
    assert data["cv"]["category"] == "it"

    # Bartender job should route to bartender CV
    rv = client.post("/api/cv-pick", json={"job": {
        "title": "Bartender",
        "description": "Mix drinks at the hotel bar.",
    }})
    data = rv.get_json()
    assert data["cv"]["category"] == "bartender"
