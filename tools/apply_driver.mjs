// Remote apply driver — drives REAL ATS application forms in headless
// Chromium using the app's own bookmarklet autofill engine, with a
// screenshot-approval gate between "filled" and "submitted".
//
// The flow this enables (agent or human at a terminal, user on a phone):
//
//   1. `list`   — show today's digest matches with numbers + URLs
//   2. `prep`   — open a job's form, run <app>/bookmarklet/run.js in the
//                 page (the exact engine + profile the phone bookmarklet
//                 gets, AI answers included), print a per-field report,
//                 and save a full-page screenshot for the user to approve.
//                 NEVER submits.
//   3. `submit` — after the user approves: re-fill fresh (fills are
//                 deterministic for the same profile), screenshot the
//                 exact pre-submit state, click the submit button, then
//                 screenshot the confirmation page. The engine's autolog
//                 hook records the application in the tracker; we also
//                 POST /api/applications/autolog as a fallback (the
//                 endpoint dedupes by URL, so double-logging is safe).
//
// Usage:
//   cd tools && npm install playwright        # once (package.json ignored)
//   APP=https://job-search-app-9pnx.onrender.com PASSCODE=... \
//     node apply_driver.mjs list
//     node apply_driver.mjs prep   <job_url>
//     node apply_driver.mjs submit <job_url> [--force-captcha]
//
// Env:
//   APP        base URL of the deployed app (default the Render deploy)
//   PASSCODE   the app's sign-in passcode (required)
//   CHROMIUM   explicit Chromium executable (else Playwright's default;
//              in the remote sandbox use /opt/pw-browsers/chromium)
//   SHOT_DIR   where screenshots land (default ./screenshots)
//
// Guard rails, deliberately:
//   - `prep` cannot submit, no flag unlocks it.
//   - `submit` refuses when a CAPTCHA is present unless --force-captcha
//     (and even then it only clicks — a live CAPTCHA will still block
//     server-side; the flag exists for false-positive detection).
//   - Every submit saves a pre-click screenshot, so what was actually
//     sent is always on disk next to the approval shot.

import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const APP      = (process.env.APP || 'https://job-search-app-9pnx.onrender.com').replace(/\/+$/, '');
const PASSCODE = process.env.PASSCODE || '';
const SHOT_DIR = process.env.SHOT_DIR || path.join(process.cwd(), 'screenshots');

const [cmd, jobUrl, ...rest] = process.argv.slice(2);
const FORCE_CAPTCHA = rest.includes('--force-captcha');

function die(msg) { console.error('ERROR: ' + msg); process.exit(1); }

if (!['list', 'prep', 'submit'].includes(cmd)) {
  die('usage: apply_driver.mjs list | prep <job_url> | submit <job_url> [--force-captcha]');
}
if (!PASSCODE) die('PASSCODE env var is required (the app sign-in passcode).');
if (cmd !== 'list' && !jobUrl) die(`${cmd} needs a job URL.`);

function slugFor(url) {
  return url.replace(/^https?:\/\//, '').replace(/[^a-z0-9]+/gi, '-')
            .replace(/^-+|-+$/g, '').slice(0, 80).toLowerCase();
}

async function newSession() {
  const browser = await chromium.launch(
    process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const context = await browser.newContext({
    viewport: { width: 1280, height: 1600 },
    // Real-browser UA: some ATSes serve degraded/blocked pages to headless UAs.
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             + '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  });
  const login = await context.request.post(APP + '/api/auth/login', {
    data: { passcode: PASSCODE },
  });
  if (!login.ok()) {
    const body = await login.text().catch(() => '');
    await browser.close();
    die(`login to ${APP} failed (${login.status()}): ${body.slice(0, 200)}`);
  }
  return { browser, context };
}

async function cmdList() {
  const { browser, context } = await newSession();
  const res = await context.request.get(APP + '/api/daily-results');
  if (!res.ok()) die(`/api/daily-results failed (${res.status()})`);
  const data = await res.json();
  const jobs = data.jobs || [];
  console.log(`Daily run: ${data.run_at || '(none yet)'} — ${jobs.length} jobs\n`);
  jobs.forEach((j, i) => {
    const pct = j.match_pct != null ? `${j.match_pct}%`.padStart(4) : '  ? ';
    console.log(`${String(i + 1).padStart(2)}. [${pct}] ${j.title || '(no title)'} — ${j.company_name || '?'}`
      + (j.salary_clean ? ` — ${j.salary_clean}` : ''));
    console.log(`      ${j.url}`);
  });
  await browser.close();
}

const FIELD_MIN = 3; // fewer visible fields than this = "no form here yet"

async function countFormFields(page) {
  return page.evaluate(() => {
    let n = 0;
    for (const e of document.querySelectorAll('input, select, textarea')) {
      if (['hidden', 'submit', 'button', 'image'].includes(e.type)) continue;
      const r = e.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) n++;
    }
    return n;
  });
}

// Job-posting pages (Workable, Lever postings, company career pages) often
// mount the form only after the site's own Apply button. Click it once.
async function ensureFormOpen(page) {
  if (await countFormFields(page) >= FIELD_MIN) return true;
  const clicked = await page.evaluate(() => {
    const els = [...document.querySelectorAll('button, a, [role="button"]')];
    const btn = els.find(b => {
      const t = (b.innerText || b.textContent || '').trim();
      return t.length < 40 && /\bapply\b/i.test(t) && !/appl(ied|ication status)/i.test(t);
    });
    if (!btn) return false;
    btn.click();
    return true;
  });
  if (!clicked) return (await countFormFields(page)) >= FIELD_MIN;
  // The click may navigate (posting page → application page) or mount in place.
  await page.waitForLoadState('domcontentloaded').catch(() => {});
  try {
    await page.waitForFunction((min) => {
      let n = 0;
      for (const e of document.querySelectorAll('input, select, textarea')) {
        if (['hidden', 'submit', 'button', 'image'].includes(e.type)) continue;
        const r = e.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) n++;
      }
      return n >= min;
    }, FIELD_MIN, { timeout: 15000 });
  } catch { /* report whatever is there */ }
  return (await countFormFields(page)) >= FIELD_MIN;
}

async function runEngine(page) {
  await page.evaluate((app) => {
    const s = document.createElement('script');
    s.src = app + '/bookmarklet/run.js?t=' + Date.now();
    s.onerror = () => console.log('BOOKMARKLET_SCRIPT_LOAD_FAILED');
    document.head.appendChild(s);
  }, APP);
  // The AI pass (Phase 2) round-trips to the server — allow up to 60s.
  let toast = '(no toast — engine never finished)';
  try {
    await page.waitForSelector('#__jt_bookmarklet_toast', { timeout: 60000 });
    toast = (await page.textContent('#__jt_bookmarklet_toast')) || toast;
  } catch { /* fall through with the default */ }
  return toast;
}

async function fieldReport(page) {
  const rows = await page.evaluate(() => {
    const out = [];
    for (const e of document.querySelectorAll('input, select, textarea')) {
      if (['hidden', 'submit', 'button', 'image'].includes(e.type)) continue;
      const r = e.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const lbl = (e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`)?.textContent) || '';
      const hint = (lbl || e.getAttribute('aria-label') || e.placeholder || e.name || e.id || '').trim();
      const val = (e.type === 'checkbox' || e.type === 'radio')
        ? (e.checked ? 'CHECKED' : '')
        : (e.value || '');
      out.push({ hint: hint.slice(0, 60), type: e.type, value: String(val).slice(0, 60), req: e.required });
    }
    return out;
  });
  let unfilledRequired = 0;
  for (const r of rows) {
    // Radio groups: one CHECKED member satisfies the group; per-element
    // "required" would over-count, so radios are reported but not tallied.
    const missing = r.req && !r.value && r.type !== 'radio';
    if (missing) unfilledRequired++;
    console.log(`${r.value ? '  ✔' : (missing ? '  ✗REQ' : '  ·')} [${r.type}] ${r.hint.padEnd(60)} => ${r.value}`);
  }
  return unfilledRequired;
}

async function hasCaptcha(page) {
  return page.evaluate(() =>
    !!document.querySelector(
      'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], '
      + '.g-recaptcha, .h-captcha, [data-sitekey]'));
}

async function shot(page, name) {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const file = path.join(SHOT_DIR, name);
  await page.screenshot({ path: file, fullPage: true });
  console.log('screenshot: ' + file);
  return file;
}

async function openAndFill(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(2500); // let SPA ATSes hydrate
  const hasForm = await ensureFormOpen(page);
  if (!hasForm) {
    console.log('WARNING: no application form found on this page (even after '
      + 'trying the Apply button). Screenshot saved for a human look.');
  }
  const toast = await runEngine(page);
  console.log('engine: ' + toast);
  await page.waitForTimeout(1000); // let async chip clicks / uploads settle
  const unfilledRequired = await fieldReport(page);
  if (unfilledRequired > 0) {
    console.log(`\n${unfilledRequired} REQUIRED field(s) still empty — fill manually or expect rejection on submit.`);
  }
  return { hasForm, unfilledRequired };
}

async function cmdPrep() {
  const { browser } = await newSession();
  const page = await (await browser.contexts())[0].newPage();
  page.on('pageerror', e => console.log('[pageerror]', e.message));
  await openAndFill(page, jobUrl);
  if (await hasCaptcha(page)) {
    console.log('NOTE: this form has a CAPTCHA — submit will need --force-captcha and may still be blocked.');
  }
  await shot(page, slugFor(jobUrl) + '.prep.png');
  console.log('\nPrep complete. Review the screenshot; nothing was submitted.');
  await browser.close();
}

const SUBMIT_RE = /^(submit( application)?|apply( now)?|send( application)?|finish|complete application)$/i;

async function cmdSubmit() {
  const { browser } = await newSession();
  const page = await (await browser.contexts())[0].newPage();
  const { unfilledRequired } = await openAndFill(page, jobUrl);

  if (await hasCaptcha(page) && !FORCE_CAPTCHA) {
    await shot(page, slugFor(jobUrl) + '.captcha.png');
    await browser.close();
    die('CAPTCHA detected — not submitting. Do this one by hand, or re-run with --force-captcha if the detection is wrong.');
  }
  if (unfilledRequired > 0) {
    console.log('Proceeding despite empty required fields — the ATS will reject if they truly are required.');
  }

  await shot(page, slugFor(jobUrl) + '.presubmit.png'); // exact state being sent

  const clicked = await page.evaluate((reSrc) => {
    const re = new RegExp(reSrc, 'i');
    const els = [...document.querySelectorAll('button, input[type="submit"], [role="button"]')];
    const btn = els.find(b => {
      const t = (b.innerText || b.value || b.textContent || '').trim();
      return b.type === 'submit' ? t.length < 40 : re.test(t);
    });
    if (!btn) return false;
    btn.scrollIntoView({ block: 'center' });
    btn.click();
    return true;
  }, SUBMIT_RE.source);

  if (!clicked) {
    await browser.close();
    die('No submit button found — check the prep screenshot; this form may be multi-step.');
  }

  // Confirmation may be a navigation or an in-place thank-you render.
  await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(3000);
  await shot(page, slugFor(jobUrl) + '.submitted.png');

  // Fallback autolog — server dedupes by URL, so this is safe even when the
  // engine's own submit hook already fired.
  const ctx = (await browser.contexts())[0];
  const logRes = await ctx.request.post(APP + '/api/applications/autolog', {
    data: {
      url: jobUrl,
      page_title: await page.title().catch(() => ''),
      hostname: new URL(jobUrl).hostname,
      source: 'apply_driver',
    },
  }).catch(() => null);
  if (logRes && logRes.ok()) {
    const j = await logRes.json();
    console.log(`tracker: logged as "${j.title}" at ${j.company}${j.deduped ? ' (already logged)' : ''}`);
  } else {
    console.log('tracker: autolog failed — add this application manually.');
  }

  console.log('\nSubmitted. Check the .submitted.png screenshot to confirm the thank-you page.');
  await browser.close();
}

try {
  if (cmd === 'list') await cmdList();
  else if (cmd === 'prep') await cmdPrep();
  else await cmdSubmit();
} catch (e) {
  die(e.stack || String(e));
}
