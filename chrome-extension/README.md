# Job Tracker Autofill (Chrome extension)

Reads your profile from the job-tracker app's `/api/profile/full` endpoint and
fills ATS application forms (Greenhouse, Lever, Workable, Ashby, SmartRecruiters,
Workday, BambooHR, Recruitee, Breezy, JazzHR, Jobvite, Rippling, Polymer).

## Install (unpacked)

1. Open `chrome://extensions`.
2. Toggle **Developer mode** (top right).
3. Click **Load unpacked** and select this `chrome-extension/` folder.
4. Pin the extension to the toolbar.

## Use

1. Open the popup, enter your app URL (e.g. `https://your-app.onrender.com`)
   and your passcode, click **Sign in & sync profile**. The profile is cached
   in `chrome.storage.local`.
2. Navigate to an application form.
3. Either:
   - Click the floating **Autofill** button (appears on supported ATS domains), or
   - Open the popup and click **Autofill this tab** (works on any page).
4. Review the filled fields and submit yourself. The extension never submits.

## How field matching works

For each input/select/textarea on the page it concatenates the surrounding
label text, `aria-label`, `placeholder`, `name`, `id`, and `data-qa`, then
matches that against a prioritized list of regexes mapped to profile fields.
Selects pick an option whose visible text or value matches; radios/checkboxes
click the matching choice. The user's `qa_defaults` from the app act as a
fuzzy fallback when no structured rule fires.

## Phase 2 — AI fill for custom questions (v0.2.0)

When the popup's **Autofill this tab** button runs, after the rule pass it
collects any field that didn't match a rule (custom free-text questions,
unusual selects), POSTs them to `/api/autofill` on your app, and applies
the per-field suggestions Claude returns. Resume text from your default CV
grounds the answers so they reference real experience, not invented detail.

The fetch is relayed through `background.js` so the 3-5s Claude round-trip
survives the popup closing. The popup status line shows
`Filled 7 + 3 AI of 12 fields`. If the server returns 503 (i.e.
`ANTHROPIC_API_KEY` isn't set on the deploy), the AI pass silently no-ops
and you still get the rule fills. Production needs the env var set; without
it Phase 2 is dormant.

## What it does NOT do

- It does not auto-submit forms. You always click Submit yourself.
- It does not upload your resume/CV file (browsers can't synthesise file
  uploads from script for security reasons — that input is left alone).
- It does not work cross-account. Cookies live in your extension's jar only.
