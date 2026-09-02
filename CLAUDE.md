# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Standing constraint: one external API

**This project relies on Apify only. Do not add, restore, or extend any
second external API.**

Apify is the job-search source (`fetch_jobs_from_apify` in `app.py`, the
`fantastic-jobs~career-site-job-listing-api` actor, reached either through a
saved `APIFY_TASK_ID` or the direct actor URL). It stays.

Anthropic is being dropped. Anything gated on `ANTHROPIC_API_KEY` is on its
way out, not something to build on:

- AI cover letters (`app.py`, the `_anthropic.Anthropic(...)` call sites)
- Phase 2 AI form fill — `POST /api/ai/fill`, called by the extension's
  `aiFillRequest` (`chrome-extension/background.js`) from `runFullAutofill`
  (`chrome-extension/content.js`)
- The `ai_enabled` / `ai` flags reported by the status and health endpoints

When a feature seems to want an LLM, do it with rules instead, or leave the
field for the user to fill. The autofill engine is rule-based by design
(`chrome-extension/autofill.js`): identity, contact, address, EEO, work
history, Yes/No screeners and button chips are all matched without a model,
and that is the path to extend.

If a task genuinely cannot be done within this constraint, say so and stop —
do not reach for a second provider.

## The product is desktop autopilot. He is currently building from a phone

Two different things, and conflating them sends work in the wrong direction.

**What the app is for: unattended auto-apply on a PC.** The Chrome extension
sweeps the queue, opens each job in a background tab, fills it and submits.
That is the point of the product. Work that raises how many jobs autopilot
reaches and completes is the work that matters -- the queue's supply
(`ATS_FAST` membership and the `ats_class == "fast"` gate in
`autopilot_queue`), and its conversion (whatever `validateBeforeSubmit` is
still blocking on). Extension-only changes are worth making even when he
cannot run them yet.

**Where he is building from right now: a phone.** So:

- He can ship normally -- merging from GitHub's mobile site, Render deploying
  from `main`. Server and template changes reach him in minutes.
- He cannot RUN the extension. Chrome on Android and iOS has no extension
  support and no `chrome://extensions` page, so nothing that needs a sweep
  can be tested until he is back at a desktop. Say plainly when a change is
  extension-only rather than letting a version bump imply it went live, and
  do not write instructions that begin "at the desktop" as though they were
  actionable today.
- Autopilot data is therefore frozen. `/api/autopilot/blockers` and Re-queue
  read rows only a sweep can create; they work, but nothing new arrives.
- The mobile bookmarklet (`/bookmarklet/run.js`, same engine off disk at
  request time) is a stopgap for applying by hand in the meantime. It is not
  the destination -- do not optimise the product around it.

## Tests

No CI runs on pull requests; the only workflow is a scheduled digest cron.
Run the suites directly before pushing:

```
python3 test_location_filter.py
python3 test_work_history.py
python3 test_autopilot_requeue.py
python3 test_bookmarklet.py
cd chrome-extension/tests && node autofill.accuracy.test.mjs && node autofill.test.mjs
node fixture_runner.mjs          # diagnostic, not pass/fail
```

`chrome-extension/tests/README.md` explains each layer.

## Line endings

`app.py` and `templates/index.html` are **CRLF**. Most other files are LF.
Editing either of those with a tool that rewrites the file in text mode
converts it to LF and turns a small change into a whole-file diff. Check
`git diff --numstat` before committing; if it reports thousands of changed
lines, restore CRLF and re-check.

## Deploying

- The Flask app runs on Render from `main`.
- The Chrome extension is loaded from a desktop checkout: a code change
  reaches it only after `git pull` plus a reload (or a Chrome restart), and
  `manifest.json`'s version is what tells you which engine is running. Bump
  it for engine changes — read the file fully, then open for write; the
  reverse truncates it (see 987ab65).
- The mobile bookmarklet reads `chrome-extension/autofill.js` off disk at
  request time, so it picks up engine changes on deploy with no reinstall.
  The `/bookmarklet/inline` variant bakes the engine into the bookmark URL
  and does need reinstalling.
