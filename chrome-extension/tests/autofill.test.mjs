// Synthetic ATS form tests. Each fixture below mirrors the markup patterns
// I've observed in real Greenhouse / Lever / Workable / Ashby application
// forms. We load autofill.js into jsdom, run the matcher with a realistic
// profile, and check which fields actually got filled.

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const AUTOFILL_SRC = fs.readFileSync(
  path.join(__dirname, "..", "autofill.js"), "utf8");

const PROFILE = {
  first_name: "George", last_name: "Tupayachi",
  full_name: "George Tupayachi", preferred_name: "George",
  pronouns: "He/Him",
  phone_digits: "6095536215",
  email: "georgetupayachijobs@outlook.com",
  phone: "+1 (609) 553-6215",
  address: {
    street: "1412 Doughty Road", city: "Egg Harbor Township",
    state: "NJ", state_full: "New Jersey",
    zip: "08234", country: "United States", country_code: "US",
  },
  linkedin: "https://www.linkedin.com/in/george-tupayachi",
  portfolio: "",
  work_experience: [
    { company: "Harrah's Casino",   title: "Bartender",            start_date: "2024-07", end_date: "",        current: true },
    { company: "Ocean Casino Resort", title: "Casino Shift Manager", start_date: "2021-06", end_date: "2022-07", current: false },
  ],
  education: [
    {
      school:     "Franklin University",
      degree:     "Masters",
      field:      "Cybersecurity",
      start_date: "2024-09",
      end_date:   "2027-01",
      graduated:  false,
    },
    {
      school:     "Stockton University",
      degree:     "Bachelors",
      field:      "Business Management Studies",
      start_date: "2017-01",
      end_date:   "2021-05",
      graduated:  true,
    },
  ],
  answers: {
    work_authorized_us: "Yes",
    sponsorship_needed: "No",
    veteran_status:     "I am not a protected veteran",
    disability:         "No",
    gender:             "Male",
    hispanic_latino:    "Yes",
    race:               "White",
    salary_expectation: "Negotiable",
    notice_period:      "Available immediately (2-week notice)",
    esignature:         "George Tupayachi",
    willing_to_relocate:"Yes",
    previously_employed:"No",
    active_security_clearance:"None",
    us_gov_employment:  "Never",
  },
  qa_defaults: [
    ["how did you hear about us", "LinkedIn"],
    ["referral", ""],
  ],
};

// ── Fixtures ───────────────────────────────────────────────────────────────

const GREENHOUSE = `
<form id="application_form">
  <label for="first_name">First Name <span class="required">*</span></label>
  <input type="text" name="first_name" id="first_name" />

  <label for="last_name">Last Name *</label>
  <input type="text" name="last_name" id="last_name" />

  <label for="email">Email *</label>
  <input type="email" name="email" id="email" />

  <label for="phone">Phone *</label>
  <input type="tel" name="phone" id="phone" />

  <label for="resume">Resume/CV *</label>
  <input type="file" name="resume" id="resume" />

  <fieldset>
    <label for="job_application_answers_attributes_0_text_value">
      LinkedIn Profile
    </label>
    <input type="text" id="job_application_answers_attributes_0_text_value"
           name="job_application[answers_attributes][0][text_value]" />
  </fieldset>

  <fieldset>
    <label for="job_application_answers_attributes_1_boolean_value">
      Are you legally authorized to work in the United States?
    </label>
    <select id="job_application_answers_attributes_1_boolean_value"
            name="job_application[answers_attributes][1][boolean_value]">
      <option value=""></option>
      <option value="1">Yes</option>
      <option value="0">No</option>
    </select>
  </fieldset>

  <fieldset>
    <label for="job_application_answers_attributes_2_boolean_value">
      Will you now or in the future require sponsorship for employment visa status?
    </label>
    <select id="job_application_answers_attributes_2_boolean_value">
      <option value=""></option>
      <option value="1">Yes</option>
      <option value="0">No</option>
    </select>
  </fieldset>

  <!-- EEOC block -->
  <label for="gender">Gender</label>
  <select id="gender" name="job_application[gender]">
    <option value="">Please select</option>
    <option value="m">Male</option>
    <option value="f">Female</option>
    <option value="d">Decline to self identify</option>
  </select>

  <label for="hispanic_ethnicity">Are you Hispanic/Latino?</label>
  <select id="hispanic_ethnicity">
    <option value="">Please select</option>
    <option>Yes</option>
    <option>No</option>
    <option>Decline to self identify</option>
  </select>

  <label for="veteran_status">Veteran Status</label>
  <select id="veteran_status">
    <option value="">Please select</option>
    <option>I identify as one or more of the classifications of a protected veteran</option>
    <option>I am not a protected veteran</option>
    <option>I don't wish to answer</option>
  </select>

  <label for="disability_status">Disability Status</label>
  <select id="disability_status">
    <option value="">Please select</option>
    <option>Yes, I have a disability</option>
    <option>No, I do not have a disability</option>
    <option>I don't wish to answer</option>
  </select>
</form>`;

const LEVER = `
<form>
  <label>Full name<input type="text" name="name" data-qa="name-input" /></label>
  <label>Email<input type="email" name="email" data-qa="email-input" /></label>
  <label>Phone<input type="tel" name="phone" data-qa="phone-input" /></label>
  <label>Current company<input type="text" name="org" /></label>
  <label>LinkedIn URL<input type="url" name="urls[LinkedIn]" /></label>
  <label>Portfolio or website<input type="url" name="urls[Other]" /></label>

  <fieldset>
    <legend>What's your current location?</legend>
    <input type="text" name="location" placeholder="City, State, Country" />
  </fieldset>

  <fieldset>
    <legend>Are you legally authorized to work in the US?</legend>
    <label><input type="radio" name="cards[work-auth][answer]" value="Yes" /> Yes</label>
    <label><input type="radio" name="cards[work-auth][answer]" value="No" /> No</label>
  </fieldset>

  <fieldset>
    <legend>Will you require sponsorship?</legend>
    <label><input type="radio" name="cards[sponsorship][answer]" value="Yes" /> Yes</label>
    <label><input type="radio" name="cards[sponsorship][answer]" value="No" /> No</label>
  </fieldset>
</form>`;

const WORKABLE = `
<form>
  <div><label for="firstname">First name *</label>
    <input id="firstname" name="firstname" type="text" /></div>
  <div><label for="lastname">Last name *</label>
    <input id="lastname" name="lastname" type="text" /></div>
  <div><label for="email">Email address *</label>
    <input id="email" name="email" type="email" /></div>
  <div><label for="phone">Phone *</label>
    <input id="phone" name="phone" type="tel" /></div>

  <div><label for="address">Current location (city)</label>
    <input id="address" name="address" type="text" /></div>

  <div><label for="linkedin">LinkedIn URL</label>
    <input id="linkedin" name="linkedin" type="url" /></div>

  <div><label>Are you authorized to work in the United States?
    <select name="q_work_auth">
      <option value=""></option>
      <option>Yes</option>
      <option>No</option>
    </select>
  </label></div>
</form>`;

const ASHBY = `
<form>
  <label for="_systemfield_name">Name</label>
  <input id="_systemfield_name" name="name" type="text" />

  <label for="_systemfield_email">Email</label>
  <input id="_systemfield_email" name="email" type="email" />

  <label for="_systemfield_phone">Phone Number</label>
  <input id="_systemfield_phone" name="phone" type="tel" />

  <label for="linkedin_url">LinkedIn URL</label>
  <input id="linkedin_url" name="linkedin_url" type="url" />
</form>`;

// Workday / Hinge Health Ashby variant: combined "Legal First & Last Name"
// in a single text input. Regression for the bug where the last_name regex
// fired first and filled only the surname.
const COMBINED_NAME = `
<form>
  <label for="legal_name">Legal First & Last Name *</label>
  <input id="legal_name" name="legal_name" type="text" />

  <label for="preferred">Preferred Name</label>
  <input id="preferred" name="preferred_name" type="text" />

  <label for="applicant_name">Applicant Name</label>
  <input id="applicant_name" name="applicant_name" type="text" />

  <label for="full_legal_name">Full Legal Name</label>
  <input id="full_legal_name" name="full_legal_name" type="text" />

  <label for="email_combined">Email</label>
  <input id="email_combined" name="email" type="email" />
</form>`;

// Ashby-style: Yes/No question rendered as <button> chips, not <input
// type="radio">. The button-group pass should find the question label, match
// it against a rule (relocate → Yes), and click the matching button.
const BUTTON_RADIO = `
<form>
  <label for="email_btn">Email</label>
  <input id="email_btn" name="email" type="email" />

  <div class="field">
    <label>Are you willing to relocate for this position?</label>
    <div class="ashby-radio">
      <button id="relo_yes" type="button">Yes</button>
      <button id="relo_no"  type="button">No</button>
    </div>
  </div>

  <div class="field">
    <label>Have you previously worked at this company?</label>
    <div class="ashby-radio">
      <button id="prev_yes" type="button">Yes</button>
      <button id="prev_no"  type="button">No</button>
    </div>
  </div>
</form>`;

// Pronouns + country code + built-in qa_defaults (v0.9).
const DEI_AND_QA = `
<form>
  <label for="pronouns">Pronouns</label>
  <select id="pronouns" name="pronouns">
    <option value="">Select...</option>
    <option>He/Him</option>
    <option>She/Her</option>
    <option>They/Them</option>
  </select>

  <label for="cc">Country Code</label>
  <input id="cc" name="country_code" type="text" />

  <label for="ph">Phone Number</label>
  <input id="ph" name="phone_number" type="text" />

  <div>
    <label class="d-block">Are you over 18 years of age?</label>
    <div><input type="radio" name="over_18" id="o18_yes" value="Yes"><label for="o18_yes">Yes</label></div>
    <div><input type="radio" name="over_18" id="o18_no"  value="No"><label for="o18_no">No</label></div>
  </div>

  <label for="src">How did you hear about us?</label>
  <input id="src" name="referral_source" type="text" />
</form>`;

// Native date inputs — type="date" wants YYYY-MM-DD (so YYYY-MM gets
// "-01" appended), type="month" accepts YYYY-MM as-is.
const NATIVE_DATES = `
<form>
  <label for="emp_start_date">Employment Start Date</label>
  <input id="emp_start_date" name="employment_start_date" type="date" />

  <label for="emp_start_month">Employment Start Month</label>
  <input id="emp_start_month" name="employment_start_month" type="month" />

  <label for="grad_month">Graduation Month</label>
  <input id="grad_month" name="graduation_month" type="month" />

  <label for="grad_full_date">Graduation Date</label>
  <input id="grad_full_date" name="graduation_date" type="date" />
</form>`;

// "Add Another" button. Starts with only row 0 visible. The engine
// should detect the + Add Another Education button, click it once,
// then fill both rows from profile.education[].
const ADD_ANOTHER = `
<form>
  <div class="education-section">
    <h3>Education History</h3>
    <div id="edu-rows">
      <div data-row="0">
        <label for="add_edu0_school">Row 0 — School</label>
        <input id="add_edu0_school" name="education[0][school]" type="text" />
      </div>
    </div>
    <button type="button" id="add-edu-btn">+ Add Another Education</button>
  </div>
</form>
<script>
  (() => {
    let next = 1;
    document.getElementById('add-edu-btn').addEventListener('click', () => {
      const i = next++;
      const div = document.createElement('div');
      div.setAttribute('data-row', String(i));
      div.innerHTML =
        '<label for="add_edu' + i + '_school">Row ' + i + ' — School</label>' +
        '<input id="add_edu' + i + '_school" name="education[' + i + '][school]" type="text" />';
      document.getElementById('edu-rows').appendChild(div);
    });
  })();
<\/script>`;

// Multi-row education + work history. Indexed field names like
// education[0][school] / work_experience[1][title] should resolve to
// the matching profile array entry — NOT all fill from row 0.
const MULTI_ROW = `
<form>
  <label for="edu0_school">Row 0 — School</label>
  <input id="edu0_school" name="education[0][school]" type="text" />
  <label for="edu0_degree">Row 0 — Degree</label>
  <input id="edu0_degree" name="education[0][degree]" type="text" />

  <label for="edu1_school">Row 1 — School</label>
  <input id="edu1_school" name="education[1][school]" type="text" />
  <label for="edu1_degree">Row 1 — Degree</label>
  <input id="edu1_degree" name="education[1][degree]" type="text" />

  <label for="we0_company">Row 0 — Company</label>
  <input id="we0_company" name="work_experience[0][company]" type="text" />
  <label for="we0_title">Row 0 — Title</label>
  <input id="we0_title" name="work_experience[0][title]" type="text" />

  <label for="we1_company">Row 1 — Company</label>
  <input id="we1_company" name="work_experience[1][company]" type="text" />
  <label for="we1_title">Row 1 — Title</label>
  <input id="we1_title" name="work_experience[1][title]" type="text" />
</form>`;

// Work experience single row — fills from profile.work_experience[0].
// Covers the most common shape: company + title + start/end dates + a
// "currently employed" Yes/No radio.
const WORK_EXPERIENCE = `
<form>
  <label for="emp_company">Company</label>
  <input id="emp_company" name="company" type="text" />

  <label for="emp_title">Job Title</label>
  <input id="emp_title" name="job_title" type="text" />

  <label for="emp_start">Employment Start Date</label>
  <input id="emp_start" name="employment_start_date" type="text" />

  <label for="emp_end">Employment End Date</label>
  <input id="emp_end" name="employment_end_date" type="text" />

  <div>
    <label class="d-block">Is this your current position?</label>
    <div class="form-check"><input type="radio" name="current_position" id="cp_yes" value="Yes"><label for="cp_yes">Yes</label></div>
    <div class="form-check"><input type="radio" name="current_position" id="cp_no"  value="No"><label for="cp_no">No</label></div>
  </div>
</form>`;

// Education section — single row, fills from profile.education[0]. Covers
// common ATS naming: school/university/college, degree (select), field
// of study/major, graduation date. Skipping multi-row repeating
// sections for now; that needs its own pass to detect "Add another"
// buttons + per-row index parsing in field names.
const EDUCATION = `
<form>
  <label for="school">School / University</label>
  <input id="school" name="school" type="text" />

  <label for="degree">Degree</label>
  <select id="degree" name="degree">
    <option value="">Select...</option>
    <option>High School</option>
    <option>Bachelors</option>
    <option>Masters</option>
    <option>Doctorate</option>
  </select>

  <label for="major">Field of Study</label>
  <input id="major" name="field_of_study" type="text" />

  <label for="grad_year">Expected Graduation Year</label>
  <input id="grad_year" name="graduation_year" type="text" />
</form>`;

// Bootstrap-style radios: the QUESTION is a sibling-of-ancestor <label
// class="d-block">, not a parent label or [for] target. The walk-up in
// probeText catches this. Also exercises the "now"-substring regression
// fix: the question contains the word "now" which would have false-
// matched the want="no" plain-substring check before fillRadio was
// switched to word-boundary regex.
const BOOTSTRAP_RADIOS = `
<form>
  <div class="col-md-6">
    <label class="d-block">Are you authorized to work in the United States?</label>
    <div class="form-check"><input type="radio" name="work_auth" id="wa_yes" value="Yes"><label for="wa_yes">Yes</label></div>
    <div class="form-check"><input type="radio" name="work_auth" id="wa_no"  value="No"><label for="wa_no">No</label></div>
  </div>
  <div class="col-md-6">
    <label class="d-block">Will you now or in the future require sponsorship?</label>
    <div class="form-check"><input type="radio" name="sponsor" id="sp_yes" value="Yes"><label for="sp_yes">Yes</label></div>
    <div class="form-check"><input type="radio" name="sponsor" id="sp_no"  value="No"><label for="sp_no">No</label></div>
  </div>
</form>`;

// Greenhouse/Lever: location field is a combobox-style autocomplete. The
// engine should detect the role=combobox + aria-autocomplete attributes
// and try the autocomplete path; in jsdom there's no real dropdown, so it
// falls through to setNativeValue with the city.
const LOCATION_AUTOCOMPLETE = `
<form>
  <label for="loc">Current Location</label>
  <input id="loc" name="location" type="text"
         role="combobox" aria-autocomplete="list" aria-haspopup="listbox"
         placeholder="Start typing your city..." />
  <label for="email_loc">Email</label>
  <input id="email_loc" name="email" type="email" />
</form>`;

// ── Test harness ───────────────────────────────────────────────────────────

async function runOn(name, html) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    pretendToBeVisual: true,
    runScripts: "dangerously",
  });
  const { window } = dom;

  // jsdom doesn't lay out elements, so `offsetParent` is always null. The
  // autofill engine's visibility check filters those out. In a real browser
  // visible inputs do have an offsetParent — so we stub it for the test.
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
    get() { return this.ownerDocument.body; },
  });

  // ...and no layout also means getBoundingClientRect() is all zeros, which
  // isOffscreen() reads as "off the left edge" — so isFillable() rejected
  // every field and this half of the suite printed "0/0 fields filled" with
  // every input listed as a matcher gap, on an engine the assertion half was
  // passing. runAssertions() has always had this stub; runOn() never did.
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 };
  };

  // jsdom doesn't expose CSS.escape on the global; real Chrome does. Polyfill.
  if (!window.CSS) window.CSS = {};
  if (!window.CSS.escape) {
    window.CSS.escape = (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c);
  }

  // Run the autofill IIFE inside the jsdom window via a real <script> tag so
  // globals (window, document, CSS, MutationObserver) resolve naturally.
  const script = window.document.createElement("script");
  script.textContent = AUTOFILL_SRC;
  window.document.head.appendChild(script);
  const af = window.__jobTrackerAutofill;

  // Pass autocomplete:false + short wait so combobox fields don't burn
  // 3 seconds per fixture waiting on a Google-Places dropdown that jsdom
  // can't render anyway. Real Chrome path is unchanged.
  const result = await af.run(PROFILE, { autocomplete: false });

  // Inspect what actually got filled.
  const fields = [...window.document.querySelectorAll("input, select, textarea")];
  const filled = [];
  const empty  = [];
  for (const el of fields) {
    if (el.type === "file" || el.type === "hidden") continue;
    const tag = el.tagName.toLowerCase();
    const labelText =
      (el.id && window.document.querySelector(`label[for="${el.id}"]`)?.textContent?.trim())
      || el.closest("label")?.textContent?.trim()
      || el.getAttribute("placeholder")
      || el.name || el.id || "?";
    if (el.type === "radio" || el.type === "checkbox") {
      if (el.checked) filled.push(`[radio] ${labelText} = ${el.value}`);
    } else if (el.value) {
      filled.push(`${tag} "${labelText.replace(/\s+/g," ").slice(0,60)}" = ${el.value}`);
    } else {
      empty.push(`${tag} "${labelText.replace(/\s+/g," ").slice(0,60)}"`);
    }
  }

  console.log(`\n══ ${name} ══════════════════════════════════════════`);
  console.log(`Engine reported: ${result.filled}/${result.total} fields filled`);
  console.log(`\n  FILLED:`);
  filled.forEach(f => console.log("   ✓ " + f));
  if (empty.length) {
    console.log(`\n  EMPTY (potential matcher gaps):`);
    empty.forEach(f => console.log("   ✗ " + f));
  }
}

// Assertion-mode run — fails the process (exit code 1) if any expectation
// doesn't hold. `getExpected` is a function that takes the jsdom window and
// returns an object of { description: expected-value, ... } — using a
// function lets each test introspect the DOM (e.g. read .checked on a
// button to verify a click landed).
async function runAssertions(name, html, getExpected) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    pretendToBeVisual: true,
    runScripts: "dangerously",
  });
  const { window } = dom;
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
    get() { return this.ownerDocument.body; },
  });
  // jsdom's getBoundingClientRect returns all-zeros by default, which makes
  // our engine's isVisible() reject the element. Stub a non-zero rect so
  // button-group visibility checks work in tests.
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 };
  };
  if (!window.CSS) window.CSS = {};
  if (!window.CSS.escape) {
    window.CSS.escape = (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c);
  }
  const script = window.document.createElement("script");
  script.textContent = AUTOFILL_SRC;
  window.document.head.appendChild(script);
  await window.__jobTrackerAutofill.run(PROFILE, { autocomplete: false });

  console.log(`\n══ ${name} (strict) ════════════════════════════════════`);
  let failed = 0;
  // getExpected must return entries shaped { description: [actual, expected] }.
  // Using a function instead of a literal lets each test introspect the DOM
  // after autofill (e.g. read .value, check attributes).
  const expected = getExpected(window);
  for (const [desc, want] of Object.entries(expected)) {
    if (!Array.isArray(want) || want.length !== 2) {
      console.error(`  ✗ ${desc}: malformed expectation (must be [actual, expected])`);
      failed++;
      continue;
    }
    const [actual, target] = want;
    const ok = actual === target;
    console.log(`  ${ok ? "✓" : "✗"} ${desc} = ${JSON.stringify(actual)}` +
                (ok ? "" : `   expected ${JSON.stringify(target)}`));
    if (!ok) failed++;
  }
  if (failed) {
    console.error(`\n  ${failed} assertion(s) failed in "${name}"`);
    process.exit(1);
  }
}

// Track button clicks via a global counter set during script init.
// We use this to verify Pass 2 (button-radio) actually clicked the right one.

(async () => {
  await runOn("Greenhouse-style form", GREENHOUSE);
  await runOn("Lever-style form",      LEVER);
  await runOn("Workable-style form",   WORKABLE);
  await runOn("Ashby-style form",      ASHBY);

  // ── Combined-name regression ────────────────────────────────────────────
  await runAssertions("Combined-name regression", COMBINED_NAME, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      "#legal_name fills with full name": [$("legal_name").value, "George Tupayachi"],
      "#preferred":       [$("preferred").value, "George"],
      "#applicant_name":  [$("applicant_name").value, "George Tupayachi"],
      "#full_legal_name": [$("full_legal_name").value, "George Tupayachi"],
      "#email_combined":  [$("email_combined").value, "georgetupayachijobs@outlook.com"],
    };
  });

  // ── Button-radio regression — Ashby Yes/No chips ────────────────────────
  // The "relocate" question + the user's profile.answers.willing_to_relocate
  // ("Yes") should result in the #relo_yes button being clicked. Similarly,
  // "previously employed" + profile ("No") → #prev_no.
  // We instrument clicks by attaching a marker class in a capture-phase listener.
  await runAssertions("Button-radio regression", `
    <script>
      document.addEventListener('click', (e) => {
        if (e.target && e.target.tagName === 'BUTTON') {
          e.target.setAttribute('data-clicked', '1');
        }
      }, true);
    </script>${BUTTON_RADIO}`, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      "#relo_yes clicked":       [$("relo_yes").getAttribute("data-clicked"), "1"],
      "#relo_no NOT clicked":    [$("relo_no").getAttribute("data-clicked"), null],
      "#prev_no clicked":        [$("prev_no").getAttribute("data-clicked"), "1"],
      "#prev_yes NOT clicked":   [$("prev_yes").getAttribute("data-clicked"), null],
      "email also filled":       [$("email_btn").value, "georgetupayachijobs@outlook.com"],
    };
  });

  // ── Autocomplete fallback — combobox without a real dropdown ────────────
  // In jsdom there's no Google Places to render options; the engine should
  // detect the combobox attributes, try autocomplete (fails), then fall
  // through to setNativeValue with the city.
  await runAssertions("Autocomplete fallback (no dropdown)", LOCATION_AUTOCOMPLETE, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      "#loc filled with city":  [$("loc").value, "Egg Harbor Township"],
      "#email_loc filled":      [$("email_loc").value, "georgetupayachijobs@outlook.com"],
    };
  });

  // ── DEI + qa_defaults + country code (v0.9) ──────────────────────
  await runAssertions("Pronouns + country code + built-in QA", DEI_AND_QA, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      "#pronouns":      [$("pronouns").value, "He/Him"],
      "#cc":            [$("cc").value, "+1"],
      "#ph":            [$("ph").value, "+1 (609) 553-6215"],
      "Over-18 = Yes":  [$("o18_yes").checked, true],
      "#src":           [$("src").value, "LinkedIn"],
    };
  });

  // ── Native date inputs (v0.8) — YYYY-MM → YYYY-MM-DD conversion ────
  await runAssertions("Native date inputs", NATIVE_DATES, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      // Employment start = work_experience[0].start_date = "2024-07"
      "type=date gets day appended":   [$("emp_start_date").value, "2024-07-01"],
      "type=month stays YYYY-MM":      [$("emp_start_month").value, "2024-07"],
      // Graduation = education[0].end_date = "2027-01"
      "grad month":                    [$("grad_month").value, "2027-01"],
      "grad date":                     [$("grad_full_date").value, "2027-01-01"],
    };
  });

  // ── "Add Another" button auto-click regression (v0.7) ──────────────
  // Engine should: (1) detect the "+ Add Another Education" button,
  // (2) click it once because profile.education has 2 entries and the
  // form initially shows 1, (3) fill the newly-created row.
  await runAssertions("Add Another button", ADD_ANOTHER, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      "#add_edu0_school":     [$("add_edu0_school")?.value, "Franklin University"],
      "#add_edu1_school exists after Add click": [!!$("add_edu1_school"), true],
      "#add_edu1_school":     [$("add_edu1_school")?.value || "(missing)", "Stockton University"],
    };
  });

  // ── Multi-row education + work history regression ───────────────────
  await runAssertions("Multi-row education + work", MULTI_ROW, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      // Education row 0
      "#edu0_school":   [$("edu0_school").value, "Franklin University"],
      "#edu0_degree":   [$("edu0_degree").value, "Masters"],
      // Education row 1
      "#edu1_school":   [$("edu1_school").value, "Stockton University"],
      "#edu1_degree":   [$("edu1_degree").value, "Bachelors"],
      // Work row 0
      "#we0_company":   [$("we0_company").value, "Harrah's Casino"],
      "#we0_title":     [$("we0_title").value, "Bartender"],
      // Work row 1 — was previously filling from row 0 because unindexed
      // rules don't see the index
      "#we1_company":   [$("we1_company").value, "Ocean Casino Resort"],
      "#we1_title":     [$("we1_title").value, "Casino Shift Manager"],
    };
  });

  // ── Work-experience single-row regression ────────────────────────────
  await runAssertions("Work experience single-row", WORK_EXPERIENCE, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      "#emp_company":   [$("emp_company").value, "Harrah's Casino"],
      "#emp_title":     [$("emp_title").value, "Bartender"],
      "#emp_start":     [$("emp_start").value, "2024-07"],
      "#emp_end (empty for current job)": [$("emp_end").value, ""],
      "current=Yes":    [$("cp_yes").checked, true],
      "current!=No":    [$("cp_no").checked, false],
    };
  });

  // ── Education single-row regression ──────────────────────────────────
  await runAssertions("Education single-row", EDUCATION, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      "#school":    [$("school").value, "Franklin University"],
      "#degree":    [$("degree").value, "Masters"],
      "#major":     [$("major").value, "Cybersecurity"],
      "#grad_year": [$("grad_year").value, "2027-01"],
    };
  });

  // ── Bootstrap-style sibling-label radios + 'no'/'now' substring bug ─────
  // The question is a <label class="d-block"> SIBLING of the form-check
  // div, not a parent. probeText must walk up to find it. AND the
  // sponsorship question contains "now" which would have false-matched
  // the old `text.includes("no")` check — the radio fix requires word
  // boundaries.
  await runAssertions("Bootstrap radios + 'no'/'now' regression", BOOTSTRAP_RADIOS, (w) => {
    const $ = (id) => w.document.getElementById(id);
    return {
      "work_auth = Yes":   [$("wa_yes").checked, true],
      "work_auth not No":  [$("wa_no").checked, false],
      "sponsor = No":      [$("sp_no").checked, true],
      "sponsor not Yes":   [$("sp_yes").checked, false],
    };
  });
})().catch(e => {
  console.error("Test runner failed:", e);
  process.exit(1);
});
