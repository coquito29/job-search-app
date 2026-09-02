# The rules in RULES.md that a machine can check, checked.
#
#   python3 test_rules.py
#
# A rules document nobody enforces becomes fiction within a month: someone
# adds a provider "just for this one feature", or a guard gets refactored
# away, and the file still claims otherwise. Everything here exists because
# the corresponding line in RULES.md would otherwise be a wish.
#
# Rules about behaviour on a form (consent, protected status, CAPTCHAs,
# required fields) are enforced in chrome-extension/tests/autofill.accuracy.
# test.mjs instead, against a real DOM. RULES.md says which is where.

import os
import re
import tempfile

os.environ["APPLICATIONS_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="rules-test-"), "test.db")

import app as appmod  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


HERE = os.path.dirname(os.path.abspath(__file__))
APP_SRC = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
REQS = open(os.path.join(HERE, "requirements.txt"), encoding="utf-8").read()

# ── One external API ────────────────────────────────────────────────────────
# Apify is the job source. Nothing else may be reached over the network for a
# decision the app makes.
OTHER_PROVIDERS = [
    "openai", "google.generativeai", "generativeai", "mistralai", "cohere",
    "ollama", "huggingface", "replicate", "together", "groq", "vertexai",
    "langchain",
]
for name in OTHER_PROVIDERS:
    check(f"no {name} dependency",
          not re.search(r"(?mi)^\s*" + re.escape(name), REQS),
          "found in requirements.txt")
check("no anthropic dependency", not re.search(r"(?mi)^\s*anthropic\b", REQS),
      "found in requirements.txt")

check("the Anthropic client is pinned off", appmod._anthropic is None,
      repr(appmod._anthropic))
check("nothing can reach Claude even with a key set",
      not any(re.search(r"^\s*import anthropic", line)
              for line in APP_SRC.splitlines() if not line.lstrip().startswith("#")),
      "app.py still imports anthropic")
check("Apify is still the job source", "api.apify.com" in APP_SRC)

# Every host app.py can CALL. Scanning every https:// in the file was too
# blunt -- it flagged the Indeed/LinkedIn/FlexJobs shortcut links on the page
# and George's own LinkedIn profile, none of which are integrations. So look
# only at lines that perform a request, plus the URL constants a request is
# handed later.
ALLOWED_CALL_HOSTS = {
    "api.apify.com",              # the one external API: job search
    # The user's OWN mailbox, for delivering his digest email. Not a data
    # provider and not consulted for any decision -- RULES.md states the
    # one-API rule in those terms rather than absolutely.
    "login.microsoftonline.com",
    "outlook.office.com",
}
called = set()
for line in APP_SRC.splitlines():
    is_request = any(v in line for v in ("http_req.", "requests.", "urlopen("))
    is_url_const = re.search(r"^\s*_?[A-Z][A-Z0-9_]*URL\w*\s*=", line)
    if not (is_request or is_url_const):
        continue
    called.update(h.lower() for h in re.findall(r"https://([a-z0-9.\-]+)", line, re.I))
check("no unexpected host is called", called <= ALLOWED_CALL_HOSTS,
      "unexpected: " + str(sorted(called - ALLOWED_CALL_HOSTS)))
check("the check is actually looking at something", "api.apify.com" in called,
      "found no call hosts at all -- the scan is broken, not the code")

# ── The robot applies only to jobs it is allowed to ─────────────────────────
# The gates themselves are exercised job-by-job in test_autopilot_requeue.py;
# here we check the classification tables they read from.
check("aggregators are blocked outright",
      all(appmod._classify_ats("https://" + d + "/jobs/1")[0] == "blocked"
          for d in ("jobleads.com", "lensa.com", "jobrapido.com")))
check("account-walled ATSes are never fast",
      all(appmod._classify_ats("https://x." + d + "/jobs/1")[0] == "walled"
          for d in ("myworkdayjobs.com", "icims.com", "taleo.net")))
check("the fast list is direct-apply only",
      not (set(appmod.ATS_FAST) & set(appmod.ATS_WALLED)),
      "a domain is in both tables")
check("no aggregator leaked into the fast list",
      not (set(appmod.ATS_FAST) & set(appmod.ATS_BLOCKED)))

# ── Nothing is submitted on the user's behalf without them ──────────────────
ENGINE = open(os.path.join(HERE, "chrome-extension", "autofill.js"), encoding="utf-8").read()
check("auto-submit is off unless explicitly requested",
      "opts.autoSubmit" in ENGINE or "autoSubmit" in ENGINE)
check("a CAPTCHA is never solved, only waited for",
      "captchaSatisfied" in ENGINE and "solve" not in ENGINE.lower().split("captcha")[0][-200:])
check("consent boxes have their own guard", "isConsentControl" in ENGINE)

# ── The traps that have bitten this repo before ─────────────────────────────
for path in ("app.py", os.path.join("chrome-extension", "autofill.js")):
    raw = open(os.path.join(HERE, path), "rb").read()
    stray = [i for i, c in enumerate(raw) if c in (0x07, 0x08, 0x0b, 0x0c, 0x1b)]
    check(f"no control characters in {path}", not stray,
          "at byte offsets " + str(stray[:4]))

for path in ("app.py", os.path.join("templates", "index.html")):
    raw = open(os.path.join(HERE, path), "rb").read()
    check(f"{path} is still CRLF", b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"),
          "line endings were rewritten")

print()
print(("FAILED: " + ", ".join(fails)) if fails else "All rule assertions passed")
raise SystemExit(1 if fails else 0)
