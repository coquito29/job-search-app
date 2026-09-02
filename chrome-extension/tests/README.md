# Extension tests

Five layers, smallest first (0-4 are the suites; 5 is a diagnostic):

## 0. `autofill.accuracy.test.mjs` — accuracy regressions (jsdom)

Every fixture here reproduces a failure seen on a REAL application form
(July 2026). Run it before shipping any matcher change:

```
cd chrome-extension/tests
node autofill.accuracy.test.mjs
```

Covers:

- **Ashby button chips consult saved answers.** Pass 2 used to check only the
  rule list, so Yes/No screeners stayed blank even when the answer was saved.
- **Fuzzy Q&A matching.** A saved "...work in the United States?" answer must
  still match a form asking "...United States **or Canada**?".
- **Closed comboboxes.** Greenhouse's compensation-range field only accepts
  its own options; free text ("Negotiable") is rejected *after* Submit. The
  engine now picks the nearest legal band, or clears the field if none fits.
- **Decoy `<select>`s.** BambooHR's State field has no real options — the
  widget is a separate search panel. Also checks the short→long fallback
  ("NJ" → "New Jersey") and exact-match preference over "New Hampshire".
- **Auto-submit gates.** Off by default; fires only when every required field
  is filled; never fires past a CAPTCHA.
- **Consent guards, both directions.** A machine-named box (`candidate_consent`
  — underscores defeat `\b`) still blocks auto-submit even when its label is
  in a language CONSENT_RE doesn't know; an OPTIONAL opt-in that merely sits
  near a starred field must NOT block (probeText's walk-up used to hand it
  the `*`, freezing every Breezy form).
- **Greenhouse value-holder twins.** Each screener question is a visible
  `input#question_<id>` plus an unnamed `input[required]` twin that
  Greenhouse's validation actually reads. Pass 1c mirrors the answer into the
  twin (and again after `applyAiFills`); anonymous-but-not-required inputs
  and hidden inputs are never touched.

Expected: all assertions pass, exit 0.

## 1. `autofill.test.mjs` — matcher unit tests (jsdom)

Loads `autofill.js` into a jsdom window and runs it against synthetic
Greenhouse / Lever / Workable / Ashby form fixtures. Fast, no browser needed.

```
cd chrome-extension/tests
npm install jsdom
node autofill.test.mjs
```

Expected: 11/11, 8/11 (Lever's Portfolio field is empty in the profile), 7/7, 4/4.

These four numbers come from the DIAGNOSTIC half of the file, which for a
while reported `0/0` on every fixture with each input listed as a matcher
gap: `runOn()` was missing the zero-rect `getBoundingClientRect` stub that
`runAssertions()` has, so the engine's own `isOffscreen()` rejected every
field. If you see all-zeros again, suspect the harness before the matchers —
the assertion half stays green either way, so it will not fail the run.

## 2. `extension-smoke.test.mjs` — real Chrome, popup-injected path

Launches real Chromium under xvfb with the extension loaded, seeds
`chrome.storage.local` with a profile via the popup page, then uses
`chrome.scripting.executeScript` to inject the autofill engine into a
synthetic form served from a local HTTP origin. Catches: manifest parse
errors, popup load errors, `chrome.scripting` regressions.

```
cd chrome-extension/tests
npm install playwright
PLAYWRIGHT_BROWSERS_PATH=/path/to/pw-browsers xvfb-run -a node extension-smoke.test.mjs
```

## 3. `extension-integration.test.mjs` — full user journey

Starts the real Flask backend on localhost, initializes a passcode, launches
Chromium with the extension, drives the popup signin UI as a user would,
then opens a form on an `/etc/hosts`-aliased Greenhouse hostname so the
manifest's `content_scripts` match fires and the floating Autofill button
appears. Verifies the full chain: SameSite=None cookie flowing cross-origin,
profile cached, content script auto-injection, button click, form values
actually updated.

```
sudo bash -c 'echo "127.0.0.1 boards.greenhouse.io" >> /etc/hosts'   # one-time
cd chrome-extension/tests
PLAYWRIGHT_BROWSERS_PATH=/path/to/pw-browsers xvfb-run -a node extension-integration.test.mjs
```

## 4. `bookmarklet_drive.mjs` — mobile bookmarklet against captured forms

Drives the MOBILE BOOKMARKLET path end-to-end in headless Chromium: rebuilds
each captured form from `../form_fixtures.json` on a local origin, then
injects `<script src="<app>/bookmarklet/run.js">` exactly as a phone tap
does. The Flask app must be running — it inlines the profile + engine into
the response. Diagnostic like the fixture runner: read the per-field report.
(The Phase 2 AI call targets the production URL, so locally it reports
"AI skipped (network/CORS)" — expected.)

```
flask --app app run --port 5054                                # repo root
cd chrome-extension/tests
APP=http://127.0.0.1:5054 node bookmarklet_drive.mjs           # CHROMIUM=/path/to/chrome to pin the binary
```

Add new fixtures or matcher rules in `../autofill.js`, then re-run `node
autofill.test.mjs`. If you change the manifest / popup / content script,
re-run the smoke + integration tests too.

## 5. `fixture_runner.mjs` — the engine against real captured markup

Rebuilds every form in `../form_fixtures.json` (captured from live ATS pages)
as jsdom and reports what each field ended up holding. Diagnostic, not
pass/fail — read the per-field report.

```
cd chrome-extension/tests
node fixture_runner.mjs          # ENGINE= / FIXTURES= override the defaults
```

Two harness bugs used to make this lie about the engine: the default
`ENGINE`/`FIXTURES` paths resolved against the shell's cwd (so the documented
invocation died on ENOENT), and without the zero-rect stub every fixture came
back "filled 0 of 0" with every field blank. Both fixed — but if the numbers
ever collapse to zero across ALL fixtures at once, that is the harness, not
the matchers.

## What still isn't covered

- Real Workday / SuccessFactors pages with shadow-DOM combobox widgets
- React-rendered selects (react-select etc.) that look like text inputs but
  are actually portal-rendered overlays
- Captcha-walled ATSes
- Mobile Safari / iOS — different runtime, see iOS bookmarklet (TBD)
