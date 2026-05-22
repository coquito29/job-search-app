"""
db.py  —  Database plumbing + auth helpers extracted from app.py.

Postgres on Render (DATABASE_URL set) / SQLite locally. The _UniformConn
adapter lets the rest of the code use a single `conn.execute("... ? ...",
(params,))` API and get dict-like rows back regardless of backend.

Auth helpers (_current_user_id, _auth_required) live here too because they're
the gateway through which every authenticated route loads its user — keeping
them next to the user table they read from.
"""
import os
import sqlite3
from datetime import datetime

from flask import jsonify, session

# psycopg2 is only imported when DATABASE_URL is set; local dev uses sqlite.
try:
    import psycopg2 as _psycopg2
    from psycopg2.extras import RealDictCursor as _RealDictCursor
except ImportError:
    _psycopg2 = None
    _RealDictCursor = None


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL) and _psycopg2 is not None

APPLICATIONS_DB = os.environ.get(
    "APPLICATIONS_DB",
    os.path.join(os.path.dirname(__file__), "applications.db"),
)

APP_STATUSES = [
    "Applied", "Phone Screen", "Interview", "Offer",
    "Rejected", "Withdrawn", "No Response",
]


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
            pass

    # Seed the default user if missing
    with _db_conn() as conn:
        cur = conn.execute("SELECT id FROM users WHERE user_key = ?", ("default",))
        if not cur.fetchone():
            conn.execute(
                "INSERT INTO users (user_key, passcode_hash, created_at) VALUES (?, ?, ?)",
                ("default", None, datetime.utcnow().isoformat()),
            )

    # Founder allowlist: comma-separated user_key values that get unlimited
    # free access. Must run AFTER the seed above so a fresh DB still flips the
    # freshly-inserted default user.
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
      - Else if no passcode is set on the default user, return the default id
        (legacy unlocked mode — keeps existing deployments working).
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
