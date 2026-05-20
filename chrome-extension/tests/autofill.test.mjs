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

// ── Test harness ───────────────────────────────────────────────────────────

function runOn(name, html) {
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

  const result = af.run(PROFILE);

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

runOn("Greenhouse-style form", GREENHOUSE);
runOn("Lever-style form",      LEVER);
runOn("Workable-style form",   WORKABLE);
runOn("Ashby-style form",      ASHBY);
