"""Integration tests for blueprints/applications.py — the applications
tracker CRUD. Covers the happy path + the easy-to-regress validation cases.
"""
import os
import tempfile

import pytest


@pytest.fixture
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="jobsearch-bp-test-")
    path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("APPLICATIONS_DB", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FOUNDER_USER_KEYS", raising=False)

    import importlib
    import db as db_mod
    importlib.reload(db_mod)
    import blueprints.applications as bp_mod
    importlib.reload(bp_mod)
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


def test_list_empty(client):
    rv = client.get("/api/applications")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["applications"] == []
    assert data["count"] == 0


def test_create_requires_company_and_title(client):
    rv = client.post("/api/applications", json={})
    assert rv.status_code == 400
    rv = client.post("/api/applications", json={"company": "Acme"})
    assert rv.status_code == 400
    rv = client.post("/api/applications", json={"title": "Engineer"})
    assert rv.status_code == 400


def test_create_rejects_invalid_status(client):
    rv = client.post("/api/applications", json={
        "company": "Acme", "title": "Engineer", "status": "NotAStatus",
    })
    assert rv.status_code == 400


def test_full_crud_roundtrip(client):
    # Create
    rv = client.post("/api/applications", json={
        "company": "Acme", "title": "Support Engineer",
        "url": "https://example.com/job/1",
        "ats": "Greenhouse", "status": "Applied",
    })
    assert rv.status_code == 200
    new_id = rv.get_json()["id"]
    assert new_id is not None

    # List
    rv = client.get("/api/applications")
    data = rv.get_json()
    assert data["count"] == 1
    assert data["applications"][0]["company"] == "Acme"

    # Filter by status (existing match)
    rv = client.get("/api/applications?status=Applied")
    assert rv.get_json()["count"] == 1

    # Filter by status (no match)
    rv = client.get("/api/applications?status=Offer")
    assert rv.get_json()["count"] == 0

    # Update
    rv = client.patch(f"/api/applications/{new_id}", json={"status": "Interview"})
    assert rv.status_code == 200

    rv = client.get("/api/applications?status=Interview")
    assert rv.get_json()["count"] == 1

    # Delete
    rv = client.delete(f"/api/applications/{new_id}")
    assert rv.status_code == 200

    rv = client.get("/api/applications")
    assert rv.get_json()["count"] == 0


def test_update_returns_404_for_missing(client):
    rv = client.patch("/api/applications/99999", json={"status": "Offer"})
    assert rv.status_code == 404


def test_delete_returns_404_for_missing(client):
    rv = client.delete("/api/applications/99999")
    assert rv.status_code == 404


def test_applied_urls_indexes_by_url_and_fingerprint(client):
    client.post("/api/applications", json={
        "company": "Acme", "title": "Engineer",
        "url": "https://jobs.example.com/1",
    })
    rv = client.get("/api/applications/urls")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "https://jobs.example.com/1" in data["urls"]
    assert "acme|engineer" in data["fingerprints"]


def test_stats_counts_by_status(client):
    client.post("/api/applications", json={"company": "A", "title": "T1", "status": "Applied"})
    client.post("/api/applications", json={"company": "B", "title": "T2", "status": "Applied"})
    client.post("/api/applications", json={"company": "C", "title": "T3", "status": "Interview"})

    rv = client.get("/api/applications/stats")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["total"] == 3
    assert data["by_status"]["Applied"] == 2
    assert data["by_status"]["Interview"] == 1
    assert data["by_status"]["Offer"] == 0
