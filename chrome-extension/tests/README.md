# Extension tests

Three layers, smallest first:

## 1. `autofill.test.mjs` — matcher unit tests (jsdom)

Loads `autofill.js` into a jsdom window and runs it against synthetic
Greenhouse / Lever / Workable / Ashby form fixtures. Fast, no browser needed.

```
cd chrome-extension/tests
npm install jsdom
node autofill.test.mjs
```

Expected: 11/11, 7/11 (4 legitimate profile-empty skips), 7/7, 4/4.

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

Add new fixtures or matcher rules in `../autofill.js`, then re-run `node
autofill.test.mjs`. If you change the manifest / popup / content script,
re-run the smoke + integration tests too.

## What still isn't covered

- Real Workday / SuccessFactors pages with shadow-DOM combobox widgets
- React-rendered selects (react-select etc.) that look like text inputs but
  are actually portal-rendered overlays
- Captcha-walled ATSes
- Mobile Safari / iOS — different runtime, see iOS bookmarklet (TBD)
