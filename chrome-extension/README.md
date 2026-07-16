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

## v0.10.0 — Auto-upload resume/CV file

Browsers block setting `<input type="file">.value` for security, but they
allow assigning `input.files` via a DataTransfer-constructed FileList.
Password managers and credential autofill extensions use this technique;
turns out it also works for résumé file inputs.

Flow:
1. Server reads the user's default CV from the CV library
   (`SELECT ... WHERE is_default = 1`; falls back to most-recent if
   none flagged) and base64-encodes the bytes into the
   `/bookmarklet/run.js` response as `window.__jt_cv_b64` plus
   `__jt_cv_filename` + `__jt_cv_mime`.
2. Engine adds a Pass 4 that finds `<input type="file">` elements
   labeled resume / CV / curriculum vitae / upload, decodes the
   base64 → Uint8Array → File, then assigns via DataTransfer.
3. The native `change` event fires, so the page's "Selected: cv.pdf"
   indicator renders.

Limitations:
- Strict-CSP ATSes (Greenhouse, Ashby, Workday) block the script-tag
  load itself, so the engine never gets to Pass 4. On those, the user
  manually uploads.
- Multi-MB CVs make the bookmarklet payload bigger — a 300 KB PDF
  expands to ~400 KB base64. Modern browsers handle script tags this
  size fine but the network transfer is noticeable.

## v0.9.0 — Pronouns + qa_defaults expansion + country code + Submit highlight

Four small additions, all from real-world ATS forms we hadn't covered:

- **Pronouns**: new profile field, fills selects whose label matches
  /\\bpronoun(s)?\\b/i with "He/Him". Common DEI field at Lever / Workable.
- **Phone country-code split**: many ATSes have separate inputs for the
  +1 country code and the 10-digit phone. New rule fires on
  /country_code|dial_code|calling_code/, fills with "+1"; the
  /phone_number/ rule then fills the digits from profile.phone_digits.
  Order matters — country-code rule must precede the generic phone
  rule.
- **Built-in qa_defaults**: 11 common questions with canonical answers
  (over 18, valid driver's licence, "how did you hear about us" →
  LinkedIn, "can you start within 2 weeks", "non-compete agreement",
  remote work, English fluency, etc.). User's profile.qa_defaults
  still wins as override.
- **Submit-button highlight (NOT auto-click)**: after fill, finds the
  submit button (type=submit OR text matches "Submit"/"Submit
  Application"/"Apply Now") and pulses a green outline for 5 seconds.
  Helps the user spot the button on long forms. The extension still
  NEVER clicks Submit on the user's behalf — the value of this tool is
  filling fast so the user reviews and submits themselves.

## v0.8.0 — Native date input handling

HTML5 `<input type="date">` requires `YYYY-MM-DD` and rejects shorter
formats silently — the field stays empty even if our rule fired with
the right value. The profile stores dates as `YYYY-MM` ("2024-07")
because that's the precision the user typed. Bridge added:

  normalizeDate(value, el):
    type="date"   → append "-01" (first of the month)
    type="month"  → leave YYYY-MM as-is
    text input    → consult placeholder/aria for format hints
                    (mm/yyyy, yyyy-mm-dd, etc.)
    YYYY-MM-DD    → return as-is

applyValue routes type="date" / type="month" through fillDate before
the generic fillTextLike path so the format conversion lands before
setNativeValue's React dispatch.

## v0.7.0 — "Add Another" button auto-expansion

Workday + Greenhouse + most ATSes render only the FIRST education and
the FIRST work-history row by default. Additional rows appear after the
user clicks an "Add Another" / "+" button.

New Pass -1 (runs before Pass 0):
- Scans every visible <button>, [role="button"], and <a> on the page
- Filters to elements whose text matches ADD_BTN_RE — accepts shapes
  like "Add", "+ Add", "Add Another", "Add another education",
  "+ Add work experience", "Add new entry"
- For each candidate, looks at the closest fieldset / section /
  card / form ancestor and reads its text. If it contains education
  keywords → treat as education button; work keywords → work button.
- Counts existing indexed rows in that ancestor (parsing names like
  `education[0][school]`)
- Clicks the button (profile_array_length - existing_rows) times,
  waiting 300ms after each click for the new row's DOM to render
- Capped at 5 clicks per button to bound runtime in pathological cases

After Pass -1, the existing Pass 0 sees the newly-created rows and
fills them with the right profile[N] data.

ADD_BTN_RE intentionally rejects things like "Add LinkedIn", "Add to
cart", "Add reference" — only matches "Add (another) (educational
section keyword)" shapes. The ancestor's keyword check provides
further safety: a generic "Add" button outside a recognized section
gets ignored.

## v0.6.0 — multi-row education + work history

Adds a new Pass 0 that runs BEFORE the rule-based pass. It scans every
field for indexed names like:

  education[0][school]              → profile.education[0].school
  education_1_school                → profile.education[1].school
  work_experience[1][company]       → profile.work_experience[1].company
  job_application[work_experience_attributes][0][title]
                                    → profile.work_experience[0].title

Pass 0 marks each filled element so Pass 1 (single-row rules) skips
it — otherwise the unindexed `\bschool\b/` pattern would re-fill row 1
with row 0's school (Franklin → Stockton, Stockton → Franklin etc).

The field-type detection (school vs degree vs field of study vs
graduation; company vs title vs dates vs current) lives in
pickRepeatingValue() and uses the same regex shape as the single-row
rules — just scoped to the indexed haystack.

Covers Workday, Greenhouse, Lever, iCIMS — all of which use
`education[N][field]` / `work[N][field]` markup for repeating rows.

## v0.5.0 — single-row work experience

Adds 5 rules covering the most-recent job (profile.work_experience[0]):
company/employer, job title/position/role, employment start date,
employment end date, and a "currently employed" Yes/No radio.

Date rules require an explicit `employment|job|work` qualifier in the
label or surrounding section, so they don't clobber the education end-date
rule that matches `graduation year`. The "current position" Yes/No
resolves to "Yes" when `work_experience[0].current === true` and the
existing fillRadio path clicks the matching radio.

Multi-row support (Stockton degree + Ocean Casino roles) is the next
phase — needs `name="education[1][school]"` / `work[1][company]` index
parsing plus "Add another" button detection.

## v0.4.0 — single-row education fields

Most ATSes (Workday, Greenhouse, Lever, iCIMS, Ashby) ask for the user's
most recent school + degree + major + graduation date. The profile already
has this in `education[]`; previously the engine skipped it entirely.

New rules in autofill.js match the first/most-recent row:
- `\b(school|university|college|institution)\b` → `education[0].school`
- `\bdegree\b` / `\blevel of education\b` → `education[0].degree`
- `\bfield of study\b` / `major|concentration|discipline` → `education[0].field`
- `\bgraduation (year|date)\b` / `expected graduation` / `completion year`
  → `education[0].end_date`

The start-date rule is intentionally omitted for v1 — generic `\bstart date\b`
overlaps with employment history fields and we don't yet have row-scoping
to disambiguate. Multi-row repeating education sections + work history
rows are the next phase.

## v0.3.1 — sibling-label radio groups + "no"/"now" false-match fix

Dogfood on `/test-form` exposed two more gaps:

- **Bootstrap-style radio groups** (Bootstrap form-check pattern, also used
  by Workday and some custom forms) put the question label as a sibling of
  the input row, not as a parent `<label>` or `[for]` target. `probeText`
  now walks up 5 levels and pulls in label-like siblings of ancestors —
  but **only siblings that don't contain other inputs**, so a Phone Number
  field doesn't pick up "First Name" from a neighboring form-group and
  misfire the first_name rule.

- **fillRadio's `text.includes("no")` was false-matching the substring
  inside "now"** — "Will you **now** or in the future require sponsorship?"
  was clicking the Yes radio. Switched to word-boundary regex
  (`\bno\b`) and prioritized value-attr matching over text matching.
  Yes/No → true/false/1/0 aliases still work.

## v0.3.0 — button-radio chips + location autocomplete

Two new field shapes are handled by the rule pass:

- **Ashby-style button-radio groups**: Yes/No questions rendered as `<button>`
  chips (no `<input type="radio">`). Pass 2 detects sibling button groups,
  finds the question label above them, runs the rule pass against that
  label, and clicks the matching button.
- **Google-Places location autocomplete** (Ashby / Greenhouse / Lever):
  inputs marked `role="combobox"` / `aria-autocomplete="list"` /
  `aria-haspopup="listbox"` get focused, typed into, polled for ~3s for a
  dropdown option, then clicked via a full pointer-event sequence so React
  pickers register the selection. Falls through to plain `setNativeValue`
  if no dropdown surfaces.

Also expanded rules: "previously **worked** at this company" now matches the
previously_employed rule alongside "previously employed".

`run()` is now async (returns `Promise<{filled, total}>`). The popup's
`chrome.scripting.executeScript` already awaits Promise returns, so no
caller change is required.

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
