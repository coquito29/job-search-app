# ATS triage: which class a job URL lands in, and therefore whether the
# autopilot queue is allowed to hand it to the robot.
#
#   python3 test_ats_classify.py
#
# The queue serves ats_class == "fast" and nothing else, so a misclassified
# host is not a cosmetic badge problem -- it silently removes the job from
# autopilot entirely. That has now happened three times (Teamtailor, Zoho
# Recruit, and the Greenhouse embeds below), each time discovered only by
# reading a digest by hand and noticing a direct-apply job scored "unknown".

import os
import tempfile

os.environ["APPLICATIONS_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="ats-classify-test-"), "test.db")

import app as appmod  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def cls(url):
    return appmod._classify_ats(url)


# ── Plain domain matching still works ───────────────────────────────────────
check("a Greenhouse board is fast",
      cls("https://boards.greenhouse.io/acme/jobs/123") == ("fast", "Greenhouse"))
check("Workday is walled",
      cls("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x")[0] == "walled")
check("an aggregator is blocked",
      cls("https://www.lensa.com/it-support-job/abc")[0] == "blocked")
check("an unrecognised host stays unknown",
      cls("https://careers.chenega.com/jobs/41965") == ("unknown", ""))
check("longest match wins",
      cls("https://jobs.smartrecruiters.com/Sutherland/744000146793229") ==
      ("fast", "SmartRecruiters"))

# ── Embedded boards on a company's own domain ───────────────────────────────
# The 2026-09-02 digest carried two of these and nothing else applyable.
check("a Greenhouse embed on a company domain is fast",
      cls("http://stability.ai/careers?gh_jid=4965729101") == ("fast", "Greenhouse"))
check("the gh_src variant is fast too",
      cls("https://example.com/careers?gh_src=abc123") == ("fast", "Greenhouse"))
check("the marker is case-insensitive",
      cls("http://stability.ai/careers?GH_JID=4965729101") == ("fast", "Greenhouse"))

# ── ...but a marker never rescues a host we already judged ──────────────────
check("an aggregator passing gh_jid through stays blocked",
      cls("https://www.lensa.com/redirect?gh_jid=999")[0] == "blocked",
      str(cls("https://www.lensa.com/redirect?gh_jid=999")))
check("a walled host passing gh_jid through stays walled",
      cls("https://acme.myworkdayjobs.com/job?gh_jid=999")[0] == "walled")

# ── Degenerate input ────────────────────────────────────────────────────────
check("an empty url is unknown", cls("") == ("unknown", ""))
check("None is unknown", cls(None) == ("unknown", ""))

# ── The gate the queue actually applies ─────────────────────────────────────
# score_job is what writes ats_class onto a digest row, and scoring re-runs on
# read -- so fixing the classifier promotes jobs already sitting in a stored
# digest, without waiting for the next Apify run.
profile = appmod.UserProfile(summary="", skills=["IT Support", "Windows"],
                             no_go_terms=appmod.NO_GO_TERMS)
scored = appmod.score_job(
    {"url": "http://stability.ai/careers?gh_jid=4965729101",
     "title": "Junior IT Support Engineer",
     "company_name": "Stability AI",
     "description": "Provide technical support across hardware and software."},
    profile)
check("score_job marks the embed fast", scored.get("ats_class") == "fast",
      str(scored.get("ats_class")))
check("score_job labels it Greenhouse", scored.get("ats_name") == "Greenhouse",
      str(scored.get("ats_name")))

print()
print(("FAILED: " + ", ".join(fails)) if fails else "All ATS classification assertions passed")
raise SystemExit(1 if fails else 0)
