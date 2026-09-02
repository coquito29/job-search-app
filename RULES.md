# Rules

Two sets: what the robot may do on George's behalf, and how anyone — human or
AI — works in this repo.

Every rule names where it is enforced and what proves it. A rule with no test
is a wish, so if you add one, add the assertion with it. If you need to break
one, change it here first and say why in the commit.

Run the proofs:

```
python3 test_rules.py            # the rules a machine can check
python3 test_autopilot_requeue.py    # which jobs the robot may touch
cd chrome-extension/tests && node autofill.accuracy.test.mjs   # what it does on a form
```

---

## Part 1 — What the robot may do

The robot applies for jobs unattended. These are the limits on that.

George settled the four open ones on 2026-09-02, and they are decisions, not
defaults to revisit on a whim:

1. **The job list must be trustworthy.** Confidence in *which* jobs the robot
   touches matters more than volume. Every gate below stays.
2. **A CAPTCHA is his.** The robot fills, he ticks and submits. It never
   tries to get past one.
3. **Missing information is his to supply**, in the wording he wants. The
   robot never invents an answer to make a form go through.
4. **A blank beats a guess**, always.

Change one only when he says so, and change the rule here in the same commit
as the code.

### It applies only to jobs that pass every gate

| Rule | Why | Proof |
|---|---|---|
| Fast-apply ATS only — never account-walled, never an aggregator | A walled ATS needs an account and an OTP; an aggregator hides the real employer | `test_autopilot_requeue.py` — one job per gate |
| Never a job with scam flags | Upfront fees, pay-to-train, wire-transfer roles | " |
| Never a job with blockers | Security clearance, a language he doesn't have, a known ghost posting | " |
| US-eligible only | He can't take non-US roles | " |
| Never the same job twice | One shot per URL, so he is never seen applying twice | " |
| Never an unclassified ATS | If we don't know the site, we don't let the robot loose on it | " |

An ATS reaches the fast list only when its forms are known to be plain
direct-apply. Adding one is a decision about letting the robot act somewhere
new — it belongs in a commit that says so, not in a drive-by edit.

### It never speaks for him on things that are his to say

| Rule | Why | Proof |
|---|---|---|
| Never tick a consent, privacy-policy, terms or marketing box | Agreeing to terms is a legal act. The one exception is an answer he explicitly saved for that question | `autofill.accuracy.test.mjs` — both directions, including a required box whose text is only in a sibling span |
| Never claim veteran, disability or any protected status from a fuzzy match | It ticked "Veteran" on a real application (Cyberhaven, 2026-09-01) because the saved answer *"I am not a protected veteran"* contained the word. Only a plainly affirmative answer may tick one | `autofill.accuracy.test.mjs` |
| Never solve, bypass or click past a CAPTCHA | A CAPTCHA is a site asking for a human. We wait for one | `autofill.accuracy.test.mjs` |
| Decline cookie banners, never accept | Consent is his to give. If a banner offers only "accept", leave it and let the form stay hidden | `test_bookmarklet.py` |
| Leave a field blank rather than guess | A wrong answer on an application is worse than a missing one. "Are you located in one of the following countries?" has no visible list — so it stays empty until he answers it once | by design; see the note in PR #17 |

### It never submits something incomplete

| Rule | Proof |
|---|---|
| Auto-submit is off unless explicitly requested | `autofill.accuracy.test.mjs` |
| Never submit while any required field is empty | " |
| Never submit past a CAPTCHA or an unticked required consent box | " |
| A submitted application is logged once, never twice | `autopilot_report` dedupes per (user, url) |

---

## Part 2 — How to work in this repo

### One external API

**Apify is the only external API this project relies on.** It is the job
source (`fetch_jobs_from_apify`, the `fantastic-jobs~career-site-job-listing-api`
actor). Nothing else may be called to obtain data or make a decision.

Two things are deliberately not counted against that rule, because neither is
a data provider:

- **His own Outlook mailbox** (`login.microsoftonline.com`,
  `outlook.office.com`) — used only to deliver his digest email to his own
  account.
- **This app's own URL**, which the mobile bookmarklet calls back into.

Anything else is a second provider. Anthropic was removed under this rule and
`_anthropic` is pinned to `None`; do not reach for it or any other model.
When a feature seems to want an LLM, do it with rules, or leave the field for
him to fill.

*Proof:* `test_rules.py` checks the dependency list, that the client is off,
and that no unexpected host is called.

### Before pushing

No CI runs on pull requests — the only workflow is a scheduled digest cron.
Run the suites yourself:

```
python3 test_rules.py
python3 test_location_filter.py
python3 test_work_history.py
python3 test_autopilot_requeue.py
python3 test_bookmarklet.py
cd chrome-extension/tests && node autofill.accuracy.test.mjs && node autofill.test.mjs
```

### Say what actually shipped

The Chrome extension only updates when George pulls and reloads it at a
desktop. A version bump does not make a change live. Say plainly when a change
is extension-only rather than letting it read as deployed.

### Two traps this repo has already fallen into

- **`\b` in a non-raw string becomes a backspace byte.** It has shipped twice:
  in the engine (`0bfecbf`, which silently killed every negative Yes/No
  answer) and in the bookmarklet invocation in `app.py` (which stopped the
  Apply button ever being clicked). Both were invisible in the source and in
  `git diff`, because `\s` is not an escape and survives, so half the pattern
  still looks right. *Proof:* `test_rules.py` fails on any control character.
- **`app.py` and `templates/index.html` are CRLF.** Editing either with a tool
  that rewrites in text mode converts the file to LF and turns a five-line
  change into a 13,000-line diff. *Proof:* `test_rules.py` checks both.
