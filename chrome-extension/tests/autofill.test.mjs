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
  email: "georgetupayachijobs@outlook.com",
  phone: "+1 (609) 553-6215",
  address: {
    street: "1412 Doughty Road", city: "Egg Harbor Township",
    state: "NJ", state_full: "New Jersey",
    zip: "08234", country: "United States", country_code: "US",
  },
  linkedin: "https://www.linkedin.com/in/george-tupayachi",
  portfolio: "",
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
