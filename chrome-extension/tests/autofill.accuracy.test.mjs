// Accuracy regression tests for the v1.0 autofill fixes.
//
// Each fixture below reproduces a failure observed on a REAL application in
// July 2026. Run alongside autofill.test.mjs:
//
//   node autofill.accuracy.test.mjs
//
// Expected: all groups pass, exit 0.

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
  phone: "+1 (609) 553-6215", phone_digits: "6095536215",
  address: {
    street: "1412 Doughty Road", city: "Egg Harbor Township",
    state: "NJ", state_full: "New Jersey",
    zip: "08234", country: "United States", country_code: "US",
  },
  linkedin: "https://www.linkedin.com/in/george-tupayachi",
  answers: { salary_expectation: "$50,000 - $60,000" },
  work_experience: [], education: [],
  qa_defaults: [
    // Saved from an Ashby form; deliberately worded DIFFERENTLY from the
    // fixtures below so the fuzzy matcher is what's under test.
    ["Do you now, or will you in the future, need sponsorship from an employer to work in the United States?", "No"],
    ["Have you worked directly with Shopify? Either at Shopify or at a company building apps or integrations in the Shopify ecosystem?", "Yes"],
    ["Do you have experience working in technical support at a SaaS company?", "Yes"],
    ["What timezone are you located in?", "Eastern Time"],
  ],
};

let failures = 0;
const results = [];

async function withDom(html, fn, opts = {}) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`,
    { runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  // jsdom has no layout: offsetParent is always null and
  // getBoundingClientRect() returns all-zeros, so isVisible() would reject
  // every element and the button-group / dropdown passes would find nothing.
  // Same stubs the main suite uses.
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", {
    get() { return this.parentElement; }, configurable: true,
  });
  w.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: 100, height: 20, top: 0, left: 0, bottom: 20, right: 100, x: 0, y: 0 };
  };
  // Mark every clicked element so assertions can verify which option the
  // engine committed to — the engine only tags <input>s it fills, and a
  // dropdown option is chosen by clicking, not by setting a value.
  w.document.addEventListener("click", (e) => {
    const t = e.target;
    if (t && t.setAttribute) t.setAttribute("data-clicked", "1");
  }, true);
  // Trip a flag if a submit ESCAPES the form, so tests can assert that
  // filling a field never fires a premature submission. Deliberately on the
  // bubble phase: the engine's guard sits in the form's capture phase and
  // calls stopPropagation, so a properly-suppressed submit never reaches
  // here, while an unguarded one does.
  w.__submitted = false;
  w.document.addEventListener("submit", (e) => {
    w.__submitted = true;
    e.preventDefault();
  }, false);
  const { profile, ...runOpts } = opts;
  w.eval(AUTOFILL_SRC);
  await w.__jobTrackerAutofill.run(profile || PROFILE, {
    autocomplete: true, autocompleteWaitMs: 400, ...runOpts,
  });
  return fn(w);
}

async function group(name, html, assertFn, opts) {
  let checks = {};
  try {
    checks = await withDom(html, assertFn, opts);
  } catch (e) {
    failures++;
    results.push(`\n══ ${name} ══\n  ✗ THREW: ${e.message}`);
    return;
  }
  const lines = [`\n══ ${name} ══`];
  for (const [label, [actual, expected]] of Object.entries(checks)) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    if (!ok) failures++;
    lines.push(ok
      ? `  ✓ ${label} = ${JSON.stringify(actual)}`
      : `  ✗ ${label} = ${JSON.stringify(actual)}   expected ${JSON.stringify(expected)}`);
  }
  results.push(lines.join("\n"));
}

// ── 1. Ashby button chips must consult saved answers ────────────────────────
// Observed on Recharge "Support Engineer (Skio)": the Yes/No chips for the
// Shopify and SaaS-support screeners stayed blank even though both answers
// were already saved, because Pass 2 only consulted the rule list.
//
// NOTE ON MARKUP FIDELITY: the first block below is copied VERBATIM from the
// live Recharge form, including the fact that the chips carry no `type`
// attribute. That matters — inside a <form>, `button.type` reports "submit"
// by spec, and an earlier version of findButtonGroups filtered those out. A
// hand-written fixture using `type="button"` passed while the real form still
// failed. Keep at least one group in real captured markup.
const ASHBY_CHIPS = `
<form>
  <div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry"
       data-field-path="12d9301e-9579-4aa9-a276-da20deee50d1">
    <label class="_heading_f7cvd_52 _required_f7cvd_91 ashby-application-form-question-title"
           for="12d9301e-9579-4aa9-a276-da20deee50d1">Have you worked directly with Shopify? Either at Shopify or at a company building apps or integrations in the Shopify ecosystem?</label>
    <div class="_container_1svni_28 _yesno_1e3gg_148 ">
      <button class="_container_pjyt6_1 _option_1svni_32 " id="shopify_yes">Yes</button>
      <button class="_container_pjyt6_1 _option_1svni_32 " id="shopify_no">No</button>
      <input type="checkbox" class="_input_1svni_78" tabindex="-1"
             name="12d9301e-9579-4aa9-a276-da20deee50d1">
    </div>
  </div>
  <div class="field">
    <label>Do you have experience working in technical support at a Saas company?</label>
    <div><button type="button" id="saas_yes">Yes</button><button type="button" id="saas_no">No</button></div>
  </div>
  <div class="field">
    <label>Do you now, or will you in the future, need sponsorship from an
      employer in order to obtain, extend or renew your authorization to work
      in the United States or Canada?</label>
    <div><button type="button" id="spon_yes">Yes</button><button type="button" id="spon_no">No</button></div>
  </div>
</form>`;

// ── 2. Fixed-option combobox must not receive invalid free text ─────────────
// Observed on Dutchie (Greenhouse): "expected compensation range" is a closed
// list. The engine typed "Negotiable", the ATS rejected the submission, and
// the failure only surfaced after clicking Submit.
const COMP_COMBOBOX = `
<form>
  <label for="comp">What is your expected compensation range for this position?</label>
  <input id="comp" name="desired_salary" type="text" role="combobox"
         aria-autocomplete="list" aria-haspopup="listbox" />
  <ul role="listbox">
    <li role="option" id="c1">$30,000 - $39,999</li>
    <li role="option" id="c2">$40,000 - $49,999</li>
    <li role="option" id="c3">$50,000 - $59,999</li>
    <li role="option" id="c4">$60,000 - $69,999</li>
  </ul>
</form>`;

// ── 3. Decoy <select> driven by a custom searchable panel ───────────────────
// Observed on Eventus (BambooHR): the State <select> carries no real options;
// the visible widget is a separate panel. The field was left blank.
const DECOY_SELECT = `
<form>
  <label for="state">State</label>
  <select id="state" name="state"><option value=""></option></select>
  <div id="state_panel">
    <input type="text" placeholder="Search..." id="state_search" />
    <ul role="listbox">
      <li role="option">New Hampshire</li>
      <li role="option">New Jersey</li>
      <li role="option">New Mexico</li>
      <li role="option">New York</li>
      <li role="option">South Carolina</li>
    </ul>
  </div>
</form>`;

(async () => {
  await group("Ashby chips use saved answers", ASHBY_CHIPS, (w) => {
    const clicked = (id) => w.document.getElementById(id).hasAttribute("data-jt-autofilled");
    return {
      // Real captured markup: no type attribute on the chips.
      "Shopify → Yes":       [clicked("shopify_yes"), true],
      "Shopify not No":      [clicked("shopify_no"), false],
      // Clicking a type-less button must not submit the form.
      "form was not submitted": [w.__submitted === true, false],
      "SaaS support → Yes":  [clicked("saas_yes"), true],
      // Saved answer says "United States"; the form says "United States or
      // Canada" — only the fuzzy tier can bridge that wording gap.
      "Sponsorship → No (fuzzy match)": [clicked("spon_no"), true],
      "Sponsorship not Yes": [clicked("spon_yes"), false],
    };
  });

  await group("Closed combobox picks nearest legal option", COMP_COMBOBOX, (w) => {
    const el = w.document.getElementById("comp");
    const clicked = (id) => w.document.getElementById(id).hasAttribute("data-clicked");
    return {
      // $50,000 desired → the $50,000-$59,999 band, not the adjacent ones.
      // (In jsdom the input keeps the typed text because there's no real
      // widget to sync it; on a live form the click sets the field.)
      "picked $50,000 - $59,999": [clicked("c3"), true],
      "did not pick $30,000 band": [clicked("c1"), false],
      "did not pick $60,000 band": [clicked("c4"), false],
    };
  });

  // The exact Dutchie failure: the saved answer has no counterpart in the
  // list at all. The engine must clear the field rather than leave text that
  // the ATS silently rejects at submit time.
  await group("Closed combobox with no legal option clears the field", `
    <form>
      <label for="comp2">What is your expected compensation range for this position?</label>
      <input id="comp2" name="salary" type="text" role="combobox"
             aria-autocomplete="list" aria-haspopup="listbox" />
      <ul role="listbox">
        <li role="option" id="d1">Prefer not to say</li>
        <li role="option" id="d2">Hourly</li>
      </ul>
    </form>`, (w) => {
    const el = w.document.getElementById("comp2");
    const anyClicked = ["d1", "d2"]
      .some(id => w.document.getElementById(id).hasAttribute("data-clicked"));
    return {
      "field left empty, not invalid text": [el.value, ""],
      "no unrelated option committed":      [anyClicked, false],
    };
  }, { profile: { ...PROFILE, answers: { salary_expectation: "Negotiable" } } });

  await group("Decoy select fills via search panel", DECOY_SELECT, (w) => {
    const opts = Array.from(w.document.querySelectorAll('#state_panel li[role=option]'));
    const clicked = opts.filter(o => o.hasAttribute("data-clicked")).map(o => o.textContent);
    return {
      // Exact-match preference: substring matching would have taken
      // "New Hampshire" (first startsWith("New")). The engine must also try
      // the long form "New Jersey" after the profile's short "NJ" misses.
      "clicked exactly one option": [clicked.length, 1],
      "clicked New Jersey":         [clicked[0], "New Jersey"],
    };
  });

  // ── Auto-submit safety gates ──────────────────────────────────────────────
  const COMPLETE_FORM = `
    <form>
      <label for="fn">First Name</label><input id="fn" name="first_name" required />
      <label for="ln">Last Name</label><input id="ln" name="last_name" required />
      <label for="em">Email</label><input id="em" name="email" type="email" required />
      <button type="submit" id="go">Submit Application</button>
    </form>`;

  await group("Auto-submit is off unless requested", COMPLETE_FORM, (w) => ({
    "submit not clicked": [w.document.getElementById("go").hasAttribute("data-clicked"), false],
  }));

  await group("Auto-submit fires when form is complete", COMPLETE_FORM, (w) => ({
    "submit clicked": [w.document.getElementById("go").hasAttribute("data-clicked"), true],
  }), { autoSubmit: true });

  // A required question the profile has no answer for must stop the submit.
  await group("Auto-submit blocked by an unanswered required field", `
    <form>
      <label for="fn2">First Name</label><input id="fn2" name="first_name" required />
      <label for="odd">How many pineapples do you own?</label>
      <input id="odd" name="pineapple_count" required />
      <button type="submit" id="go2">Submit Application</button>
    </form>`, (w) => ({
    "submit NOT clicked": [w.document.getElementById("go2").hasAttribute("data-clicked"), false],
  }), { autoSubmit: true });

  // Never auto-submit past a CAPTCHA.
  await group("Auto-submit blocked by CAPTCHA", `
    <form>
      <label for="fn3">First Name</label><input id="fn3" name="first_name" required />
      <div class="g-recaptcha" data-sitekey="abc"></div>
      <button type="submit" id="go3">Submit Application</button>
    </form>`, (w) => ({
    "submit NOT clicked": [w.document.getElementById("go3").hasAttribute("data-clicked"), false],
  }), { autoSubmit: true });

  console.log(results.join("\n"));
  console.log(failures === 0
    ? "\nAll accuracy assertions passed."
    : `\n${failures} assertion(s) FAILED.`);
  process.exit(failures === 0 ? 0 : 1);
})();
