// Runs the real autofill engine against the forms captured from live ATS pages
// on 2026-08-04 (chrome-extension/form_fixtures.json). Builds each captured
// form as a jsdom document, runs window.__jobTrackerAutofill.run() with
// George's actual profile, then reports what each field ended up holding.
//
//   node fixture_runner.mjs
//
// This is diagnostic, not a pass/fail suite — the point is to see what the
// engine really does on markup it has never met.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

// Defaults resolve against this file, not the shell's cwd. The README says to
// run this from chrome-extension/tests, where './autofill.js' does not exist —
// so every documented invocation died on ENOENT before reaching a fixture.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENGINE = fs.readFileSync(
  process.env.ENGINE || path.join(HERE, '..', 'autofill.js'), 'utf8');

// George's real profile, as /api/profile/full returns it.
const PROFILE = {
  first_name: 'George', last_name: 'Tupayachi', full_name: 'George Tupayachi',
  preferred_name: 'George', pronouns: 'He/Him',
  email: 'georgetupayachijobs@outlook.com',
  phone: '+1 (609) 553-6215', phone_digits: '6095536215', phone_e164: '+16095536215',
  linkedin: 'https://www.linkedin.com/in/george-tupayachi', portfolio: '',
  address: {
    street: '1412 Doughty Road', city: 'Egg Harbor Township', state: 'NJ',
    state_full: 'New Jersey', zip: '08234', country: 'United States', country_code: 'US',
  },
  answers: {
    work_authorized_us: 'Yes', sponsorship_needed: 'No',
    willing_to_relocate: 'No - seeking remote roles',
    salary_expectation: '$50,000 - $60,000',
    notice_period: 'None - available immediately',
    race: 'Hispanic or Latino', hispanic_latino: 'Yes', gender: 'Male',
    veteran_status: 'No', disability: 'No', esignature: 'George Tupayachi',
    previously_employed: 'No', active_security_clearance: 'No', us_gov_employment: 'Never',
  },
  work_experience: [
    { company: "Harrah's Casino", title: 'Bartender', start_date: '2024-07', end_date: '',
      current: true, location: 'Atlantic City, NJ' },
    { company: 'Cholo Tech', title: 'Software Developer / Founder', start_date: '2022-07',
      end_date: '', current: false, location: '' },
    { company: 'Ocean Casino Resort', title: 'Casino Shift Manager', start_date: '2021-06',
      end_date: '2022-07', current: false, location: 'Atlantic City, NJ' },
    { company: 'Ocean Casino Resort', title: 'Bartender', start_date: '2019-06',
      end_date: '2020-08', current: false, location: 'Atlantic City, NJ' },
  ],
  education: [
    { school: 'Franklin University', degree: 'Masters', field: 'Cybersecurity',
      start_date: '2024-09', end_date: '2027-01', graduated: false },
    { school: 'Stockton University', degree: 'Bachelors', field: 'Business Management Studies',
      start_date: '2017-01', end_date: '2021-05', graduated: true },
  ],
  qa_defaults: [
    ['Are you legally authorized to work in the United States', 'Yes'],
    ['Are you able to work in the United States for any employer without sponsorship', 'Yes'],
    ['Do you now, or will you in the future, need sponsorship from an employer in order to work in the United States?', 'No'],
    ['Will you require sponsorship for employment visa status', 'No'],
    ['Desired pay / salary expectation', '$50,000 - $60,000 (negotiable)'],
    ['What is your current location', 'Egg Harbor Township, New Jersey, United States'],
    ['How did you hear about this position', 'LinkedIn'],
    ['Are you currently employed?', 'Yes'],
    ['What timezone are you located in', 'Eastern Time'],
    ['Are you at least 18 years of age', 'Yes'],
  ],
};

// ── Build a captured form as real DOM ───────────────────────────────────────
function buildForm(fixture) {
  const parts = ['<form>'];
  let section = null;
  (fixture.fields || []).forEach((f, i) => {
    if (f.section && f.section !== section) {
      section = f.section;
      parts.push(`<h3>${esc(section)}</h3>`);
    }
    // Fields captured with no id/name/label get NO synthetic id — Greenhouse's
    // value-holder twins are only recognizable by their total lack of identity,
    // and an invented anon_<i> id would hide them from the engine's Pass 1c.
    // Radio-group members SHARE a name but must not share an id: the old
    // 'n_' + name rule gave every member of a group the same id, so each
    // <label for> resolved to the first member and the engine read the "No"
    // radio's label as "Yes". Workable's two screener groups came back 1-of-4
    // answered here while the same markup with distinct ids answers 2-of-2 —
    // a harness artifact that reads exactly like an engine bug.
    const grouped = f.type === 'radio' || f.type === 'checkbox';
    const id = f.id || (f.name ? 'n_' + f.name + (grouped ? '_' + i : '')
                               : (f.label ? 'anon_' + i : ''));
    const attrs = [
      id ? `id="${esc(id)}"` : '',
      f.name ? `name="${esc(f.name)}"` : '',
      f.placeholder ? `placeholder="${esc(f.placeholder)}"` : '',
      f.aria ? `aria-label="${esc(f.aria)}"` : '',
      f.required ? 'required' : '',
    ].filter(Boolean).join(' ');

    parts.push('<div class="field">');
    if (f.prev_sibling) parts.push(`<span>${esc(f.prev_sibling)}</span>`);
    if (f.label) parts.push(`<label for="${esc(id)}">${esc(f.label)}</label>`);

    if (f.type === 'textarea') parts.push(`<textarea ${attrs}></textarea>`);
    else if (f.type === 'select-one') {
      const opts = (f.options || ['', 'Yes', 'No']).map(o => `<option>${esc(o)}</option>`).join('');
      parts.push(`<select ${attrs}>${opts}</select>`);
    } else parts.push(`<input type="${esc(f.type || 'text')}" ${attrs} />`);
    parts.push('</div>');
  });

  (fixture.button_chip_questions || []).forEach((q, i) => {
    parts.push(`<div class="field"><div>${esc(q.question)}</div>` +
      q.chips.map(c => `<button type="button" data-q="${i}">${esc(c)}</button>`).join('') +
      '</div>');
  });

  parts.push('<button type="submit">Submit Application</button></form>');
  return parts.join('\n');
}

const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

// ── Run one fixture ─────────────────────────────────────────────────────────
async function runFixture(key, fixture) {
  const dom = new JSDOM(`<!doctype html><html><body>${buildForm(fixture)}</body></html>`, {
    url: fixture.url || 'https://example.com/apply',
    pretendToBeVisual: true,
    runScripts: 'outside-only',
  });
  const w = dom.window;
  // jsdom has no layout, so offsetParent is null for everything; the engine
  // uses it as a visibility test. Make every element report as visible.
  Object.defineProperty(w.HTMLElement.prototype, 'offsetParent', {
    get() { return this.parentNode; }, configurable: true,
  });
  // ...and no layout means getBoundingClientRect() is all zeros, which the
  // engine's isOffscreen() reads as "right <= 0 && bottom <= 0" — i.e. every
  // field is off the left edge of the screen. isFillable() then rejected the
  // whole form, so this runner reported "filled 0 of 0" and every field blank
  // for all eleven fixtures, on an engine whose unit suite is green. That is
  // worse than no diagnostic: it says the engine is dead on every ATS. Same
  // stub the unit suite has always used.
  w.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 };
  };
  if (!w.CSS) w.CSS = {};
  if (!w.CSS.escape) {
    w.CSS.escape = (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, c => '\\' + c);
  }
  w.eval(ENGINE);

  let result = null, error = null;
  try {
    result = await w.__jobTrackerAutofill.run(PROFILE, { autoSubmit: false });
  } catch (e) { error = String(e && e.message || e); }

  const rows = [...w.document.querySelectorAll('input,select,textarea')]
    .filter(e => e.type !== 'submit')
    .map(e => {
      const lbl = (w.document.querySelector(`label[for="${e.id}"]`) || {}).textContent || '';
      const sib = e.parentElement && e.parentElement.querySelector('span');
      const hint = (lbl || (sib ? sib.textContent : '') || e.placeholder || e.name || e.id || '').trim();
      // A checkbox reports value==="on" even when unchecked — reading .value
      // here once produced a false 'the engine ticked your consent box' report.
      const val = (e.type === 'checkbox' || e.type === 'radio')
        ? (e.checked ? 'CHECKED' : '')
        : (e.value || '');
      return { hint: hint.slice(0, 58), type: e.type, value: val.slice(0, 40) };
    });

  const chipsClicked = [...w.document.querySelectorAll('button[data-q]')]
    .filter(b => b.getAttribute('aria-pressed') === 'true' || b.dataset.jtClicked === '1' ||
                 /selected|active|checked/i.test(b.className))
    .map(b => b.textContent);

  return { key, ats: fixture.ats, result, error, rows, chipsClicked };
}

// ── Report ──────────────────────────────────────────────────────────────────
const fixtures = JSON.parse(fs.readFileSync(
  process.env.FIXTURES || path.join(HERE, '..', 'form_fixtures.json'), 'utf8'));

for (const [key, fx] of Object.entries(fixtures)) {
  if (key.startsWith('_') || !fx.fields || !fx.fields.length) continue;
  const out = await runFixture(key, fx);
  console.log('\n' + '='.repeat(74));
  console.log(`${out.ats} — ${fx.company}`);
  console.log('='.repeat(74));
  if (out.error) console.log('ENGINE ERROR:', out.error);
  if (out.result) console.log(`engine reports: filled ${out.result.filled} of ${out.result.total}`);
  console.log('');
  let blank = 0;
  for (const r of out.rows) {
    const mark = r.value ? '  ' : '??';
    if (!r.value) blank++;
    console.log(`${mark} ${r.hint.padEnd(58)} ${r.type.padEnd(10)} ${r.value ? '→ ' + r.value : '(blank)'}`);
  }
  console.log(`\n${out.rows.length - blank} filled, ${blank} blank`);
  if (fx.button_chip_questions) {
    console.log('chips clicked:', out.chipsClicked.length ? out.chipsClicked.join(', ') : 'NONE');
  }
}
