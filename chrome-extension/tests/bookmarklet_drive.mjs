// End-to-end drive of the MOBILE BOOKMARKLET against the captured ATS forms
// (form_fixtures.json) in real headless Chromium — the exact path a phone
// user triggers: a page on a foreign origin injecting
// <script src="<app>/bookmarklet/run.js">, profile inlined by the server.
//
// Needs the Flask app running locally (its /bookmarklet/run.js serves the
// engine + profile):
//
//   flask --app app run --port 5054          # repo root
//   APP=http://127.0.0.1:5054 node tests/bookmarklet_drive.mjs   # this dir's parent
//
// Env:
//   APP       base URL of the Flask app (default http://127.0.0.1:5054)
//   CHROMIUM  explicit Chromium executable path (else Playwright's default)
//
// Diagnostic like fixture_runner.mjs, not pass/fail — read the per-field
// report. Unlike the jsdom runner this exercises visibility checks, chip
// clicks, and the server's profile/CV inlining.

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = process.env.APP || 'http://127.0.0.1:5054';
const FIXTURES = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'form_fixtures.json'), 'utf8'));

const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

// Same reconstruction as fixture_runner.mjs buildForm().
function buildForm(fixture) {
  const parts = ['<form>'];
  let section = null;
  (fixture.fields || []).forEach((f, i) => {
    if (f.section && f.section !== section) {
      section = f.section;
      parts.push(`<h3>${esc(section)}</h3>`);
    }
    const id = f.id || (f.name ? 'n_' + f.name : 'anon_' + i);
    const attrs = [
      `id="${esc(id)}"`,
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

// Serve every fixture from one tiny server so each page is a distinct URL
// on an origin that is NOT the app's — same cross-origin shape as a real tap.
const server = http.createServer((req, res) => {
  const key = req.url.slice(1).split('?')[0];
  const fx = FIXTURES[key];
  if (!fx) { res.writeHead(404); res.end('unknown fixture'); return; }
  res.writeHead(200, { 'Content-Type': 'text/html' });
  res.end(`<!doctype html><html><body><h1>${esc(fx.company || key)}</h1>${buildForm(fx)}</body></html>`);
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

const browser = await chromium.launch(
  process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});

// The bookmarklet URL carries a per-user token (?k=) that run.js requires —
// harvest it from the install page exactly the way a user's bookmark does.
const installHtml = await (await fetch(`${APP}/bookmarklet`)).text();
const token = (installHtml.match(/run\.js\?k=([A-Za-z0-9_-]+)/) || [])[1];
if (!token) { console.error('could not extract bookmarklet token from /bookmarklet'); process.exit(1); }

for (const [key, fx] of Object.entries(FIXTURES)) {
  if (key === '_README') continue;
  if (!(fx.fields || []).length && !(fx.button_chip_questions || []).length) {
    console.log(`\n════ ${key} — SKIPPED (no captured fields)`);
    continue;
  }
  const page = await browser.newPage();
  page.on('pageerror', e => console.log('[pageerror]', e.message));
  await page.goto(`http://127.0.0.1:${port}/${key}`);
  // Record chip clicks so the report can show what the engine chose.
  await page.evaluate(() => {
    document.querySelectorAll('button[data-q]').forEach(b =>
      b.addEventListener('click', () => { b.dataset.jtClicked = 'CLICKED'; }));
  });
  // This is exactly what the bookmarklet's javascript: URL does.
  await page.evaluate(({ app, k }) => {
    const s = document.createElement('script');
    s.src = app + '/bookmarklet/run.js?k=' + k + '&t=' + Date.now();
    s.onerror = () => console.log('BOOKMARKLET_SCRIPT_LOAD_FAILED');
    document.head.appendChild(s);
  }, { app: APP, k: token });
  let toast = '(no toast — engine never finished)';
  try {
    await page.waitForSelector('#__jt_bookmarklet_toast', { timeout: 20000 });
    toast = await page.textContent('#__jt_bookmarklet_toast');
  } catch (_) {}

  const rows = await page.evaluate(() => {
    const out = [];
    for (const e of document.querySelectorAll('input,select,textarea')) {
      if (e.type === 'submit') continue;
      const lbl = (document.querySelector(`label[for="${e.id}"]`) || {}).textContent || '';
      const sib = e.parentElement && e.parentElement.querySelector('span');
      const hint = (lbl || (sib ? sib.textContent : '') || e.placeholder || e.name || e.id || '').trim();
      // Checkboxes report value==="on" even when unticked — read .checked.
      const val = (e.type === 'checkbox' || e.type === 'radio')
        ? (e.checked ? 'CHECKED' : '')
        : (e.value || '');
      out.push({ hint: hint.slice(0, 58), type: e.type, value: val.slice(0, 44), req: e.required });
    }
    document.querySelectorAll('button[data-q]').forEach(b => {
      out.push({ hint: 'CHIP: ' + b.textContent.trim().slice(0, 50), type: 'chip',
                 value: b.dataset.jtClicked || '', req: false });
    });
    return out;
  });

  console.log(`\n════ ${key} (${fx.ats}) ════`);
  console.log('toast:', toast);
  for (const r of rows) {
    const mark = r.value ? '  ✔' : (r.req ? '  ✗REQ' : '  ·');
    console.log(`${mark} [${r.type}] ${r.hint.padEnd(58)} => ${r.value}`);
  }
  await page.close();
}

await browser.close();
server.close();
