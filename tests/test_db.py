"""Smoke tests for db.py — schema init and the _UniformConn adapter.

We don't need a real Postgres to test the SQLite path (the only one local dev
ever exercises). These tests use a temp DB file via monkeypatching so they
don't pollute the real applications.db.
"""
import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    """Point db.APPLICATIONS_DB at a fresh temp file, then re-init the schema."""
    tmpdir = tempfile.mkdtemp(prefix="jobsearch-test-")
    path = os.path.join(tmpdir, "test.db")
    # Re-import after monkeypatching env so module-level DATABASE_URL is unset
    # (we want the sqlite branch, not the postgres branch).
    monkeypatch.setenv("APPLICATIONS_DB", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FOUNDER_USER_KEYS", raising=False)

    import importlib
    import db as db_mod
    importlib.reload(db_mod)
    db_mod._init_applications_db()
    yield db_mod, path
    # Cleanup
    try:
        os.remove(path)
        os.rmdir(tmpdir)
    except OSError:
        pass


def test_schema_creates_all_tables(temp_db):
    db_mod, path = temp_db
    expected = {"applications", "users", "cvs", "profiles", "daily_searches"}
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert expected.issubset(names), f"Missing tables: {expected - names}"


def test_default_user_is_seeded(temp_db):
    db_mod, _ = temp_db
    uid, _hash = db_mod._default_user()
    assert uid is not None
    assert uid >= 1


def test_uniform_conn_translates_placeholders(temp_db):
    db_mod, _ = temp_db
    with db_mod._db_conn() as conn:
        # sqlite ignores the translation but the API stays uniform
        cur = conn.execute(
            "INSERT INTO applications (company, title, applied_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("Acme", "Engineer", "2026-01-01", "2026-01-01"),
        )
        # sqlite cursor lastrowid; PG would use RETURNING
        assert cur is not None

    with db_mod._db_conn() as conn:
        row = conn.execute(
            "SELECT company, title FROM applications WHERE company = ?",
            ("Acme",),
        ).fetchone()
        # Both backends should give a dict-like row
        assert row["company"] == "Acme"
        assert row["title"] == "Engineer"


def test_row_get_handles_missing_and_none(temp_db):
    db_mod, _ = temp_db
    assert db_mod._row_get(None, "anything") is None
    assert db_mod._row_get(None, "anything", "fallback") == "fallback"

    fake = {"a": 1, "b": None}
    assert db_mod._row_get(fake, "a") == 1
    assert db_mod._row_get(fake, "b", "default") == "default"
    assert db_mod._row_get(fake, "missing", "default") == "default"
