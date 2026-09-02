# Re-queueing needs_review attempts after a matcher fix.
#
# The queue's skip set is every URL this user has ever attempted, so a job that
# came back needs_review is never served again. When the reason was a FILL BUG
# rather than a human-owned step, fixing the bug does nothing on its own --
# those jobs stay stuck. /api/autopilot/requeue hands the fixable ones back.
#
#   python3 test_autopilot_requeue.py
#
# Uses a throwaway SQLite DB (APPLICATIONS_DB) and the Flask test client. A
# fresh DB has no passcode, so the app is in unlocked mode and the endpoints
# authenticate as the default user.

import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="requeue-test-")
os.environ["APPLICATIONS_DB"] = os.path.join(_TMP, "test.db")

import app as appmod  # noqa: E402  (must follow the env var)

fails = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ── The classifier ──────────────────────────────────────────────────────────
# Blocker strings as submitIfComplete() writes them, joined into `detail` by
# the content script.
REQUIRED_ONLY = ("filled; waiting on you — required: Will you now or in the "
                 "future require sponsorship for employment visa status?")
CAPTCHA_ONLY  = "filled; waiting on you — CAPTCHA present — needs a human"
BOTH          = ("filled; waiting on you — required: Are you a protected veteran?; "
                 "CAPTCHA present — needs a human")
FILE_ONLY     = "filled; waiting on you — required file: Resume"
CONSENT_ONLY  = "filled; waiting on you — consent needed: I agree to the Privacy Policy"

check("an unanswered required field alone is answerable",
      appmod._requeue_class(REQUIRED_ONLY) == "answerable")
check("a CAPTCHA alone is not re-queued",
      appmod._requeue_class(CAPTCHA_ONLY) == "skip")
check("required + CAPTCHA is its own class",
      appmod._requeue_class(BOTH) == "captcha_too")
check("an empty upload slot is not a matcher failure",
      appmod._requeue_class(FILE_ONLY) == "skip", appmod._requeue_class(FILE_ONLY))
check("a consent box we must not tick is left alone",
      appmod._requeue_class(CONSENT_ONLY) == "skip")
check("no detail at all is skipped", appmod._requeue_class("") == "skip")
check("None detail is safe", appmod._requeue_class(None) == "skip")

# ── Seed a digest + four attempts ───────────────────────────────────────────
JOBS = [
    {"url": f"https://jobs.workable.com/view/{slug}", "title": t,
     "company_name": "Acme", "ats_class": "fast", "ats_name": "Workable",
     "match_pct": 8, "scam_flags": [], "blockers": [], "loc_class": "US"}
    for slug, t in (("req", "Helpdesk Analyst"), ("cap", "Support Engineer"),
                    ("both", "IT Specialist"), ("done", "Web Developer"))
]
URL = {j["title"]: j["url"] for j in JOBS}

with appmod._db_conn() as conn:
    uid = appmod._default_user()[0]
    conn.execute(
        "INSERT INTO daily_searches (user_id, run_at, jobs, total_fetched) VALUES (?, ?, ?, ?)",
        (uid, "2026-09-02T08:00:00Z", json.dumps(JOBS), len(JOBS)))
    for url, result, detail in (
        (URL["Helpdesk Analyst"],  "needs_review", REQUIRED_ONLY),
        (URL["Support Engineer"],  "needs_review", CAPTCHA_ONLY),
        (URL["IT Specialist"],     "needs_review", BOTH),
        (URL["Web Developer"],     "submitted",    ""),
    ):
        conn.execute(
            """INSERT INTO autopilot_attempts
               (user_id, url, title, company, result, detail, filled, total, attempted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, url, "T", "Acme", result, detail, 7, 12, "2026-09-02T09:00:00Z"))

client = appmod.app.test_client()


def queued_urls():
    return {j["url"] for j in client.get("/api/autopilot/queue").get_json()["jobs"]}


def result_for(url):
    with appmod._db_conn() as conn:
        row = conn.execute(
            "SELECT result FROM autopilot_attempts WHERE url = ? ORDER BY id DESC LIMIT 1",
            (url,)).fetchone()
    return appmod._row_get(row, "result")


# ── Every attempted job starts out excluded ─────────────────────────────────
check("an attempted job is not re-served", queued_urls() == set(), str(queued_urls()))

# ── GET is a dry run and changes nothing ────────────────────────────────────
dry = client.get("/api/autopilot/requeue").get_json()
check("dry run says so", dry["dry_run"] is True)
check("dry run counts all three needs_review rows", dry["needs_review_total"] == 3, str(dry))
check("dry run counts one answerable", dry["counts"]["answerable"] == 1, str(dry["counts"]))
check("dry run counts one captcha_too", dry["counts"]["captcha_too"] == 1, str(dry["counts"]))
check("dry run counts one skip", dry["counts"]["skip"] == 1, str(dry["counts"]))
check("dry run lists only the answerable row",
      [r["url"] for r in dry["reopened"]] == [URL["Helpdesk Analyst"]], str(dry["reopened"]))
check("dry run wrote nothing", result_for(URL["Helpdesk Analyst"]) == "needs_review")
check("dry run left the queue alone", queued_urls() == set())

# ── POST applies ────────────────────────────────────────────────────────────
applied = client.post("/api/autopilot/requeue").get_json()
check("apply is not a dry run", applied["dry_run"] is False)
check("the answerable row is now requeued",
      result_for(URL["Helpdesk Analyst"]) == "requeued")
check("the CAPTCHA-only row is untouched",
      result_for(URL["Support Engineer"]) == "needs_review")
check("the required+CAPTCHA row is untouched without the flag",
      result_for(URL["IT Specialist"]) == "needs_review")
check("a submitted job is never reopened", result_for(URL["Web Developer"]) == "submitted")
check("the requeued job is served again",
      queued_urls() == {URL["Helpdesk Analyst"]}, str(queued_urls()))

# ── include_captcha reaches the second class ────────────────────────────────
more = client.post("/api/autopilot/requeue?include_captcha=1").get_json()
check("include_captcha reopens the required+CAPTCHA row",
      result_for(URL["IT Specialist"]) == "requeued", str(more["counts"]))
check("include_captcha still leaves CAPTCHA-only alone",
      result_for(URL["Support Engineer"]) == "needs_review")
check("both reopened jobs are queued",
      queued_urls() == {URL["Helpdesk Analyst"], URL["IT Specialist"]}, str(queued_urls()))

# ── A re-queued row must not vanish from the digest de-dup either ───────────
# The queue only serves what recent digests contain, so if _already_applied
# still dropped these URLs the re-queue would be undone by the next search.
# The digest builds this set inline, so mirror its query rather than call it.
with appmod._db_conn() as conn:
    tried = conn.execute(
        "SELECT url FROM autopilot_attempts WHERE user_id = ? AND result != ?",
        (uid, appmod.REQUEUED_RESULT)).fetchall()
applied_urls = {appmod._row_get(r, "url") for r in tried}
check("a requeued URL is not in the digest's applied set",
      URL["Helpdesk Analyst"] not in applied_urls)
check("an attempted-and-left-alone URL still is",
      URL["Support Engineer"] in applied_urls)
check("a submitted URL still is", URL["Web Developer"] in applied_urls)

print()
print(("FAILED: " + ", ".join(fails)) if fails else "All autopilot re-queue assertions passed")
raise SystemExit(1 if fails else 0)
