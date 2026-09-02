# The mobile bookmarklet: the served payload, and the endpoints it calls.
#
# This is the only fill path available without a desktop, so it is worth a
# suite of its own.
#
#   python3 test_bookmarklet.py
#
# The control-character check is the important one. The invocation lives in a
# plain (non-raw) Python string inside app.py, so a single-backslash \b in a
# regex there becomes a BACKSPACE byte at runtime and ships a pattern that
# silently matches nothing. That shipped twice: once in the engine (0bfecbf,
# which killed every negative Yes/No answer) and once in this invocation,
# where it disabled the Apply-button click. Both were invisible in the source
# and in git diff. \s survives unescaped, so half the regex looks correct.

import json
import os
import tempfile

os.environ["APPLICATIONS_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="bookmarklet-test-"), "test.db")

import app as appmod  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


client = appmod.app.test_client()
uid = appmod._default_user()[0]
token = appmod._bookmarklet_token(uid)

# ── The served payload ──────────────────────────────────────────────────────
res = client.get("/bookmarklet/run.js?k=" + token)
body = res.get_data()
text = body.decode("utf-8")

check("run.js is served", res.status_code == 200, str(res.status_code))
check("it is javascript", "javascript" in res.headers.get("Content-Type", ""))
check("it carries the engine", "__jobTrackerAutofill" in text)
check("it carries the profile", "__jt_bookmarklet_profile" in text)
check("the token placeholder is substituted", "__JT_TOKEN__" not in text)

CONTROL = {0x07, 0x08, 0x0b, 0x0c, 0x1b}
stray = [i for i, c in enumerate(body) if c in CONTROL]
check("no control characters in the payload", not stray,
      "at byte offsets " + str(stray[:5]) + " -> "
      + repr(body[max(0, stray[0] - 60):stray[0] + 30].decode("utf8", "replace") if stray else ""))

# Word boundaries must survive as two characters, not one control byte.
check("the Apply matcher kept its word boundaries",
      r"apply\b" in text, "APPLY_RE lost its \\b")
check("the third-party sign-in exclusion kept its word boundaries",
      r"\b(linkedin" in text, "APPLY_SKIP lost its \\b")

# ── What the invocation does now ────────────────────────────────────────────
check("it learns answers before filling", "collectLearnableAnswers" in text)
check("it posts them to the learning endpoint", "/api/qa/learn?k=" in text)
check("it can dismiss a consent banner", "DECLINE_RE" in text)
check("it never accepts cookies on the user's behalf",
      "accept all" not in text.lower())
check("the dead AI round trip is gone from the invocation",
      "/api/autofill?k=" not in text)

# ── The learning endpoint the bookmarklet calls cross-origin ────────────────
check("OPTIONS preflight is answered",
      client.open("/api/qa/learn", method="OPTIONS").status_code == 204)

saved = client.post("/api/qa/learn?k=" + token,
                    json={"answers": [["Which shift patterns can you cover?", "Weekdays"]]})
check("a same-origin POST is accepted", saved.status_code == 200, str(saved.status_code))
check("the answer is stored",
      any(q == "Which shift patterns can you cover?"
          for q, _a in (saved.get_json() or {}).get("qa_defaults", [])),
      str(saved.get_json()))

# A cross-origin caller without the token must be turned away: any page the
# user visits could otherwise write junk into their saved answers.
drive_by = client.post("/api/qa/learn",
                       json={"answers": [["Injected?", "yes"]]},
                       headers={"Origin": "https://evil.example"})
check("a cross-origin POST without the token is refused",
      drive_by.status_code == 403, str(drive_by.status_code))

with_token = client.post("/api/qa/learn?k=" + token,
                         json={"answers": [["Timezone?", "Eastern"]]},
                         headers={"Origin": "https://jobs.workable.com"})
check("a cross-origin POST with the token is accepted",
      with_token.status_code == 200, str(with_token.status_code))
check("the reply carries CORS headers",
      "Access-Control-Allow-Origin" in with_token.headers)

print()
print(("FAILED: " + ", ".join(fails)) if fails else "All bookmarklet assertions passed")
raise SystemExit(1 if fails else 0)
