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

Run every suite before pushing:

```
python run_tests.py
```

That runs all seven suites — five Python, two Node — and exits non-zero if
any fail. GitHub Actions runs the same command on every pull request and on
pushes to `main` (`.github/workflows/tests.yml`), so a green run locally and
a green check on the PR mean the same thing.

The one thing it does not run is the diagnostic, which is pass/fail-less and
the most useful tool here:

```
cd chrome-extension/tests && node fixture_runner.mjs
```

It prints per-ATS fill counts against 11 fixtures. That is how you tell an
ENGINE gap from a DATA gap — on 2026-09-03 it showed Greenhouse filling
13/25 offline while the live sweep filled 2/25, which located the bug in the
cached profile rather than in the engine, without spending real applications
to find out.

Node lives under `Program Files/nodejs` and is not on the Bash tool's PATH;
`run_tests.py` finds it anyway. Calling `node` directly needs
`export PATH="/c/Program Files/nodejs:$PATH"` first. `jsdom` must be
installed in `chrome-extension/tests/` (`npm install jsdom`) — its
`package.json` is gitignored, so a fresh checkout needs that step.

`chrome-extension/tests/README.md` explains each layer.

## Line endings

`.gitattributes` and `.editorconfig` now enforce this; the note below is
why they exist.

The repo has `core.autocrlf=true`, so most files are stored LF in the blob
and checked out CRLF. **Five files are stored WITH CRLF in the blob itself**
(measured 2026-09-03, not assumed):

```
app.py                            templates/index.html
chrome-extension/popup.html       chrome-extension/popup.js
chrome-extension/tests/README.md
```

An earlier version of this section named only the first two — popup.js and
popup.html carry the same hazard and were missing. Editing any of the five
with a tool that rewrites in text mode converts it to LF and turns a
one-line change into a whole-file diff. `.editorconfig` pins `end_of_line`
for exactly those five, and `.gitattributes` marks them `-text` so no future
`core.autocrlf` change renormalises them.

Still worth checking `git diff --numstat` before committing: if it reports
thousands of changed lines, the tooling was bypassed — restore CRLF and
re-check.

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
