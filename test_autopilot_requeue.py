# Which jobs reach the robot, and what happens to the ones that stall:
# ATS classification (the queue's supply gate), re-queueing attempts the
# engine can now finish, and surfacing the questions that stopped them.
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

# ── The blocker questions behind those rows ─────────────────────────────────
# Same detail strings, read for a different purpose: each "required: <label>"
# is a question the rules could not answer, and a saved answer fills it on
# every future form.

bq = appmod._blocker_questions
check("a required question is extracted",
      bq(REQUIRED_ONLY) == ["Will you now or in the future require sponsorship "
                            "for employment visa status?"], str(bq(REQUIRED_ONLY)))
check("the 'filled; waiting on you' lead-in is stripped even though it "
      "contains its own semicolon",
      bq("filled; waiting on you \u2014 required: Desired salary?") == ["Desired salary?"],
      str(bq("filled; waiting on you \u2014 required: Desired salary?")))
check("a CAPTCHA is not a question", bq(CAPTCHA_ONLY) == [])
check("an upload slot is not a question", bq(FILE_ONLY) == [])
check("a consent box is not a question", bq(CONSENT_ONLY) == [])
check("both blockers of a mixed row are read, minus the CAPTCHA",
      bq(BOTH) == ["Are you a protected veteran?"], str(bq(BOTH)))
check("a trailing star is trimmed off the label",
      bq("required: Desired pay*") == ["Desired pay"])
check("an anonymous field carries no question to answer",
      bq("required: field; required: Real question?") == ["Real question?"],
      str(bq("required: field; required: Real question?")))
check("empty detail is safe", bq("") == [])
check("None detail is safe", bq(None) == [])

# The endpoint de-duplicates across jobs and ranks by how many each one cost.
# Seeded fresh so the counts are unambiguous.
with appmod._db_conn() as conn:
    conn.execute("DELETE FROM autopilot_attempts WHERE user_id = ?", (uid,))
    for u, detail in (
        ("https://x/1", "filled; waiting on you \u2014 required: Preferred management style?; "
                        "CAPTCHA present \u2014 needs a human"),
        ("https://x/2", "filled; waiting on you \u2014 required: Preferred management style?"),
        ("https://x/3", "filled; waiting on you \u2014 required: Timezone?"),
        ("https://x/4", "filled; waiting on you \u2014 CAPTCHA present \u2014 needs a human"),
    ):
        conn.execute(
            """INSERT INTO autopilot_attempts
               (user_id, url, title, company, result, detail, filled, total, attempted_at)
               VALUES (?, ?, ?, ?, 'needs_review', ?, 7, 12, '2026-09-02T09:00:00Z')""",
            (uid, u, "Helpdesk Analyst", "Acme", detail))

data = client.get("/api/autopilot/blockers").get_json()
check("every needs_review row is counted", data["needs_review"] == 4, str(data))
check("the CAPTCHA-only row has no question", data["no_question"] == 1, str(data))
check("questions are de-duplicated across jobs", len(data["questions"]) == 2,
      str([q["question"] for q in data["questions"]]))
check("the costliest question ranks first",
      data["questions"][0]["question"] == "Preferred management style?"
      and data["questions"][0]["jobs"] == 2, str(data["questions"][0]))
check("an example job is attached", "Helpdesk Analyst" in data["questions"][0]["example"])
check("nothing is saved yet", data["questions"][0]["saved"] == "")

# An answer saved through the extension's own learning endpoint comes back
# attached to the question, so the UI can show and edit it.
check("saving an answer succeeds",
      client.post("/api/qa/learn", json={
          "answers": [["Preferred management style?", "Clear priorities, regular feedback"]]
      }).status_code == 200)
after = client.get("/api/autopilot/blockers").get_json()
check("the saved answer comes back with its question",
      after["questions"][0]["saved"] == "Clear priorities, regular feedback",
      str(after["questions"][0]))
check("the unanswered question stays empty", after["questions"][1]["saved"] == "")


# ── The supply gate: ats_class decides what the robot ever sees ─────────────
# autopilot_queue serves ats_class == "fast" and nothing else, so an ATS
# missing from the tables is invisible to the robot no matter how simple its
# forms are. That is what happened to Teamtailor in July and to Zoho Recruit
# until now: the extension had been running on zohorecruit for a while while
# the queue skipped every posting.

cls = appmod._classify_ats
check("Zoho Recruit is fast-apply",
      cls("https://acme.zohorecruit.com/jobs/Careers/1/IT-Support") == ("fast", "Zoho Recruit"),
      str(cls("https://acme.zohorecruit.com/jobs/Careers/1/IT-Support")))
check("the EU Zoho domain too",
      cls("https://acme.zohorecruit.eu/jobs/Careers/9/Helpdesk") == ("fast", "Zoho Recruit"))
check("Paycom postings are walled, not unknown",
      cls("https://acme.paycomonline.net/v4/ats/web.php/jobs/1") == ("walled", "Paycom"),
      str(cls("https://acme.paycomonline.net/v4/ats/web.php/jobs/1")))
check("the Paycom marketing domain still classifies",
      cls("https://www.paycom.com/careers")[0] == "walled")
check("an unknown host stays unknown",
      cls("https://careers.example.com/jobs/1") == ("unknown", ""))
check("aggregators stay blocked", cls("https://www.jobleads.com/x")[0] == "blocked")

# End to end: a Zoho posting scored by the real classifier now reaches the
# queue. Seeded through _classify_ats rather than a hand-written ats_class,
# so the table and the gate are tested together rather than in isolation.
zoho_url = "https://acme.zohorecruit.com/jobs/Careers/77/Helpdesk-Analyst"
z_class, z_name = cls(zoho_url)
with appmod._db_conn() as conn:
    conn.execute("DELETE FROM autopilot_attempts WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM daily_searches WHERE user_id = ?", (uid,))
    conn.execute(
        "INSERT INTO daily_searches (user_id, run_at, jobs, total_fetched) VALUES (?, ?, ?, ?)",
        (uid, "2026-09-02T15:00:00Z", json.dumps([{
            "url": zoho_url, "title": "Helpdesk Analyst", "company_name": "Acme",
            "ats_class": z_class, "ats_name": z_name,
            "match_pct": 6, "scam_flags": [], "blockers": [], "loc_class": "US",
        }]), 1))

served = [j["url"] for j in client.get("/api/autopilot/queue").get_json()["jobs"]]
check("a Zoho posting is queued for the robot", served == [zoho_url], str(served))


# ── The queue re-scores on read, so a fix reaches the robot ─────────────────
# A digest row freezes ats_class at search time. /api/daily-results has always
# re-scored on read; the queue did not, so 49f44ec's classifier fix promoted
# two jobs to "fast" on the page while the robot's queue stayed empty. The
# stored class below is deliberately the STALE one.
with appmod._db_conn() as conn:
    conn.execute("DELETE FROM daily_searches WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM autopilot_attempts WHERE user_id = ?", (uid,))
    conn.execute(
        "INSERT INTO profiles (user_id, skills, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET skills = excluded.skills",
        (uid, json.dumps(["IT Support", "Windows", "Troubleshooting"]),
         "2026-09-02T16:00:00Z"))
    embed_url = "http://stability.ai/careers?gh_jid=4965729101"
    conn.execute(
        "INSERT INTO daily_searches (user_id, run_at, jobs, total_fetched) VALUES (?, ?, ?, ?)",
        (uid, "2026-09-02T16:00:00Z", json.dumps([{
            "url": embed_url, "title": "Junior IT Support Engineer",
            "company_name": "Stability AI",
            "description": "Provide technical support across hardware and software.",
            "ats_class": "unknown", "ats_name": "",
            "match_pct": 0, "scam_flags": [], "blockers": [], "loc_class": "US",
        }]), 1))

served = [j["url"] for j in client.get("/api/autopilot/queue").get_json()["jobs"]]
check("a stale 'unknown' class is re-scored and queued", served == [embed_url], str(served))
check("the badge the robot is handed says Greenhouse",
      appmod._classify_ats(embed_url) == ("fast", "Greenhouse"))

# ── Rows the ENGINE gave up on ──────────────────────────────────────────────
# no_form and timeout carry no blocker text, so _requeue_class never sees them
# and nothing could reopen them -- while v0.18.5 and v0.18.7 were written for
# exactly those failures. Opt-in, because a posting with genuinely no form
# will just fail again.
WORKABLE = "https://jobs.workable.com/view/abc/helpdesk-analyst"
GREENHOUSE = "https://boards.greenhouse.io/acme/jobs/999"
with appmod._db_conn() as conn:
    conn.execute("DELETE FROM daily_searches WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM autopilot_attempts WHERE user_id = ?", (uid,))
    conn.execute(
        "INSERT INTO daily_searches (user_id, run_at, jobs, total_fetched) VALUES (?, ?, ?, ?)",
        (uid, "2026-09-02T17:00:00Z", json.dumps([
            {"url": WORKABLE, "title": "Helpdesk Analyst", "company_name": "Acme",
             "ats_class": "fast", "ats_name": "Workable", "match_pct": 8,
             "scam_flags": [], "blockers": [], "loc_class": "US"},
            {"url": GREENHOUSE, "title": "Support Engineer", "company_name": "Acme",
             "ats_class": "fast", "ats_name": "Greenhouse", "match_pct": 9,
             "scam_flags": [], "blockers": [], "loc_class": "US"},
        ]), 2))
    for url, result, detail in (
        (WORKABLE,   "no_form", "apply button did not reveal a form"),
        (GREENHOUSE, "timeout", "fill/submit timed out"),
    ):
        conn.execute(
            """INSERT INTO autopilot_attempts
               (user_id, url, title, company, result, detail, filled, total, attempted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, url, "T", "Acme", result, detail, 0, 0, "2026-09-02T17:05:00Z"))

check("an engine failure is excluded from the queue to begin with",
      queued_urls() == set(), str(queued_urls()))

plain = client.get("/api/autopilot/requeue").get_json()
check("the default dry run leaves engine failures alone",
      plain["reopened"] == [], str(plain["reopened"]))
check("and reports no needs_review rows, because there are none",
      plain["needs_review_total"] == 0, str(plain["needs_review_total"]))

dry = client.get("/api/autopilot/requeue?include_failed=1").get_json()
check("include_failed lists both engine failures",
      {r["url"] for r in dry["reopened"]} == {WORKABLE, GREENHOUSE},
      str([r["url"] for r in dry["reopened"]]))
check("the dry run counts them by result",
      dry["counts"]["no_form"] == 1 and dry["counts"]["timeout"] == 1, str(dry["counts"]))
check("a dry run changes nothing", queued_urls() == set(), str(queued_urls()))

client.post("/api/autopilot/requeue?include_failed=1")
check("after applying, both are served to the robot again",
      queued_urls() == {WORKABLE, GREENHOUSE}, str(queued_urls()))
check("the rows are marked requeued, not deleted",
      result_for(WORKABLE) == appmod.REQUEUED_RESULT, str(result_for(WORKABLE)))

print()
print(("FAILED: " + ", ".join(fails)) if fails else "All autopilot recovery assertions passed")
raise SystemExit(1 if fails else 0)
