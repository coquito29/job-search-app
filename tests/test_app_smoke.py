"""Smoke tests for app.py — verify the Flask app boots and the most basic
read-only routes respond. Not a replacement for end-to-end testing, but it
catches the easy class of regression where an import or @route breaks during
a refactor.
"""
import os
import tempfile

import pytest


@pytest.fixture
def client(monkeypatch):
    """Spin up the Flask app pointed at a temp sqlite file."""
    tmpdir = tempfile.mkdtemp(prefix="jobsearch-test-")
    path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("APPLICATIONS_DB", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FOUNDER_USER_KEYS", raising=False)

    # Force fresh module state so the schema initializes against the temp DB
    import importlib
    import db as db_mod
    importlib.reload(db_mod)
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


def test_index_renders(client):
    rv = client.get("/")
    assert rv.status_code == 200


def test_config_endpoint(client):
    rv = client.get("/api/config")
    assert rv.status_code == 200
    assert rv.is_json


def test_status_endpoint(client):
    rv = client.get("/api/status")
    assert rv.status_code == 200


def test_auth_status_returns_json(client):
    rv = client.get("/api/auth/status")
    assert rv.status_code == 200
    assert rv.is_json


def test_extension_preflight_returns_204(client):
    rv = client.open("/api/auth/login", method="OPTIONS")
    assert rv.status_code == 204


def test_applications_list_starts_empty(client):
    rv = client.get("/api/applications")
    assert rv.status_code == 200
    data = rv.get_json()
    # Could be {"applications": []} or [] depending on contract; assert it's
    # at least a JSON value with no records.
    if isinstance(data, dict):
        assert data.get("applications", []) == []
    else:
        assert data == []
