# Job Search App — Auto-fill (Chrome extension)

Fills ATS application forms (Greenhouse, Workable, Workday, iCIMS, ADP, SmartRecruiters, etc.) from your Job Search App profile. You always review the fields and click Submit yourself — the extension never submits forms for you.

## What gets filled (Phase 1 — rules)

- Name (first / last / full / preferred) — combined labels like "Legal First & Last Name" get the full name
- Email + phone
- Address (street / city / state / zip / country) — state and country selects try both abbreviation and full name
- **Location autocomplete** (Ashby, Greenhouse, Lever — Google-Places-backed pickers): types value, waits for dropdown, clicks first option
- LinkedIn URL
- Work-authorisation answers (authorized to work in US, sponsorship needed, etc.)
- EEO / demographics (gender, race, ethnicity, veteran, disability)
- Salary expectation, notice period, e-signature
- Common yes/no questions ("previously employed here", "willing to relocate", etc.)
- **Custom button-based "radio" groups** (Ashby yes/no chips, Lever custom radios) — clicks the button whose text matches the answer

## What also gets filled (Phase 2 — AI, v0.4)

When the Job Search App server has `ANTHROPIC_API_KEY` set, a second pass kicks in for anything Phase 1 couldn't match:

- Custom free-text questions like *"Why are you interested in this role?"*, *"Describe a time you handled a difficult customer."*
- Custom select / radio / button-group questions whose label didn't match any rule (the AI is shown the option list and must pick one of them)

The content script POSTs unfilled-field metadata (label, type, options, placeholder) + your default CV's parsed text + page context to `POST /api/autofill`. Claude returns one suggestion per field with a confidence score. The toast shows `Auto-filled 7 + 3 AI — review before submitting`.

If the server returns 503 (no API key set), Phase 2 silently no-ops and the flag is cached for the session so we don't keep pinging the endpoint. You still get Phase 1 fills.

## v0.2 fixes

- Rule-ordering bug: "Legal First & Last Name" now wins the full-name rule before the last_name singleton sees it (used to fill only "Tupayachi").
- Ashby location field: was a plain `setNativeValue` that didn't trigger the Google Places dropdown — now focuses, types, waits, clicks the first option.
- Custom button radios (Ashby Yes/No chips) are now detected and clicked.
- Labels with trailing `*` / `(required)` / `(optional)` markers are normalized so they match cleanly.

## What's NOT filled yet

- Education rows
- Work-experience rows
- Resume upload (always manual — Chrome blocks programmatic file uploads)

## Install

1. **Clone or download this folder** (`chrome_extension/`) to your computer.
2. Open Chrome → navigate to `chrome://extensions`.
3. Toggle **"Developer mode"** in the top-right corner.
4. Click **"Load unpacked"**, select the `chrome_extension/` folder.
5. The extension's icon (a generic puzzle piece for now) appears in the toolbar. Pin it for one-click access.

## Use (v0.3 — auto-fill on supported hosts)

1. **Once per session**: open the extension popup once so it caches your profile to `chrome.storage.local`. (The content script can't fetch the API directly, so the popup is the only place that refreshes the cache.)
2. Navigate to an application form on a supported ATS host (see list below).
3. The extension auto-fills after a brief wait for the form to render. A green toast appears bottom-right: **"Auto-filled N fields — review before submitting"** with **↩ Undo** and **×** buttons. The toast auto-dismisses after 12s.
4. Review every field. Manually handle anything that wasn't filled (custom questions, file upload, etc.).
5. Click Submit on the ATS yourself when ready.

**Manual fill (any host)**: click the toolbar icon → "Auto-fill this form". This works on every page, even non-supported hosts.

### Supported hosts for auto-fill

`ashbyhq.com`, `greenhouse.io`, `workable.com`, `lever.co`, `smartrecruiters.com`, `bamboohr.com`, `applytojob.com` (JazzHR), `rippling.com`, `breezy.hr`, `icims.com`, `myworkdayjobs.com`, `workday.com`, `workforcenow.adp.com`, `dayforcehcm.com`, `successfactors.com`, `taleo.net`, `oraclecloud.com`, `ukgpro.com`, `polymerhr.com`, `recruitee.com`.

On any host not in this list, use the popup to fill manually.

## Local dev

If you're running the Flask app locally on `localhost:5000`, open the extension popup → click **"API server"** → select **Local**. The extension will fetch your profile from the local instance instead of production.

## Privacy / security

- The extension talks only to the configured Job Search App server (production by default).
- Your profile data lives on the server and is sent over HTTPS.
- The extension never auto-submits, never reads pages outside the active tab, and never sends form contents to any third party.
- Session is via the same cookie the web app uses. Logging out of the web app invalidates the extension's access until you log back in.

## Files

- `manifest.json` — Chrome extension manifest (Manifest V3)
- `popup.html` / `popup.css` / `popup.js` — toolbar popup UI
- `content.js` — the form filler (runs on every page, idle until you click Auto-fill)

## Roadmap

- **Phase 2 (v0.4 — shipped):** AI-powered field mapping. Sends form-field metadata + CV text + page context to `/api/autofill`, Claude returns per-field suggestions, the content script applies them. Requires `ANTHROPIC_API_KEY` set on the deployed server. Falls back silently to Phase 1 rules when AI is offline (503).
- **Phase 3:** Per-ATS handlers for Workday (React event firing), iCIMS (Country dropdown), Workable (shadow DOM), Oracle HCM. Education/work-history row filling.
- **Phase 4:** Chrome Web Store submission so installation is a one-click thing instead of unpacked.
