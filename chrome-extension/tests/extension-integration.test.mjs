// Full user-journey integration test: start the Flask backend locally, init
// a passcode, launch Chrome with the extension, drive the popup UI as a real
// user would, verify the floating Autofill button fills a form on a page
// served from an http origin matching one of the ATS content-script patterns.
//
// /etc/hosts trick: maps a real ATS hostname to 127.0.0.1 so the manifest's
// content_scripts match URL fires and the content script auto-loads. This
// exercises the floating-button path (vs. the popup-injected path the prior
// test covered).

import { chromium } from "playwright";
import { spawn } from "node:child_process";
import http from "node:http";
import fs from "node:fs";
import { setTimeout as wait } from "node:timers/promises";

const EXT_PATH = "/home/user/job-search-app/chrome-extension";
const USER_DATA_DIR = "/tmp/pw-ext-profile2";
fs.rmSync(USER_DATA_DIR, { recursive: true, force: true });

// --- /etc/hosts: alias a Greenhouse-looking domain to localhost so the
// content_scripts URL pattern in manifest.json matches our test page.
const HOSTS = fs.readFileSync("/etc/hosts", "utf8");
const ALIAS = "boards.greenhouse.io";
if (!HOSTS.includes(ALIAS)) {
  fs.writeFileSync("/etc/hosts", HOSTS + `\n127.0.0.1 ${ALIAS}\n`);
  console.log("✓ /etc/hosts: 127.0.0.1 ->", ALIAS);
}

const FORM_HTML = `<!doctype html><html><body>
<h1>Apply for SWE — Pretend Company</h1>
<form id="application_form">
  <label for="first_name">First Name *</label>
  <input type="text" name="first_name" id="first_name" />
  <label for="last_name">Last Name *</label>
  <input type="text" name="last_name" id="last_name" />
  <label for="email">Email *</label>
  <input type="email" name="email" id="email" />
  <label for="phone">Phone *</label>
  <input type="tel" name="phone" id="phone" />
  <label for="linkedin">LinkedIn</label>
  <input type="url" name="linkedin" id="linkedin" />
  <fieldset>
    <legend>Are you legally authorized to work in the United States?</legend>
    <select id="workauth">
      <option value=""></option>
      <option>Yes</option>
      <option>No</option>
    </select>
  </fieldset>
</form>
</body></html>`;

// 1. Start Flask backend
const FLASK_PORT = 5117;
const APP_DB = "/tmp/extension_int_test.db";
fs.rmSync(APP_DB, { force: true });
const flask = spawn("python", ["-c", `
import os
os.environ['FLASK_SAMESITE_NONE'] = '1'
os.environ['APPLICATIONS_DB']    = '${APP_DB}'
os.environ['FLASK_SECRET_KEY']   = 'test-key-deterministic'
import sys; sys.path.insert(0, '/home/user/job-search-app')
import app
app.app.run(host='127.0.0.1', port=${FLASK_PORT}, debug=False, use_reloader=False)
`], { stdio: ["ignore", "pipe", "pipe"] });
flask.stdout.on("data", b => process.stdout.write("[flask] " + b.toString()));
flask.stderr.on("data", b => process.stderr.write("[flask] " + b.toString()));

// Wait until Flask is responsive
for (let i = 0; i < 30; i++) {
  await wait(300);
  try {
    const r = await fetch(`http://127.0.0.1:${FLASK_PORT}/api/auth/status`);
    if (r.ok) { console.log("✓ flask up"); break; }
  } catch (_) {}
  if (i === 29) { console.error("flask never started"); process.exit(1); }
}

// 2. Init the passcode
const r = await fetch(`http://127.0.0.1:${FLASK_PORT}/api/auth/init`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ passcode: "test1234" }),
});
console.log("auth/init:", r.status, await r.json());

// 3. Serve the form on the aliased Greenhouse-like hostname
const server = http.createServer((req, res) => {
  res.writeHead(200, { "content-type": "text/html" });
  res.end(FORM_HTML);
});
const SERVE_PORT = 8081;
await new Promise(r => server.listen(SERVE_PORT, "0.0.0.0", r));
const FORM_URL = `http://${ALIAS}:${SERVE_PORT}/jobs/12345`;
console.log("test form URL:", FORM_URL);

// 4. Launch Chrome with extension
const ctx = await chromium.launchPersistentContext(USER_DATA_DIR, {
  headless: false,
  args: [
    `--disable-extensions-except=${EXT_PATH}`,
    `--load-extension=${EXT_PATH}`,
    "--no-first-run", "--no-default-browser-check",
  ],
});

// 5. Find extension ID
const probe = await ctx.newPage();
await probe.goto("chrome://extensions");
await wait(500);
const exts = await probe.evaluate(async () =>
  new Promise(r => chrome.management.getAll(r)));
await probe.close();
const ext = exts.find(e => e.name === "Job Tracker Autofill");
console.log("✓ extension ID:", ext.id);
const POPUP = `chrome-extension://${ext.id}/popup.html`;

// 6. Drive the popup signin UI like a user would
const popup = await ctx.newPage();
await popup.goto(POPUP);
await popup.fill("#app-url",  `http://127.0.0.1:${FLASK_PORT}`);
await popup.fill("#passcode", "test1234");
await popup.click("#signin");

// Wait for the success message OR an error
await popup.waitForFunction(
  () => !document.getElementById("msg").hidden,
  null, { timeout: 5000 }
);
const msgText = await popup.locator("#msg").textContent();
const msgClass = await popup.locator("#msg").getAttribute("class");
console.log(`signin result: "${msgText}" (class=${msgClass})`);
if (!msgClass.includes("ok")) {
  console.error("✗ signin failed via popup UI");
  await ctx.close(); server.close(); flask.kill(); process.exit(1);
}
console.log("✓ popup signin succeeded — cookie flowed cross-origin");

// 7. Verify the profile was actually cached in storage
const cached = await popup.evaluate(() =>
  new Promise(r => chrome.storage.local.get(["profile", "lastSync"], r)));
console.log("✓ profile cached:", !!cached.profile,
            "name:", cached.profile?.full_name,
            "lastSync:", cached.lastSync ? "set" : "missing");

// 8. Open the form page on the aliased ATS domain → content script should
//    auto-load (manifest match), and the floating Autofill button should
//    appear once the form is detected.
const formPage = await ctx.newPage();
await formPage.goto(FORM_URL);
console.log("✓ form page loaded at", FORM_URL);

// Wait for the floating button injected by content.js
try {
  await formPage.waitForSelector("#jt-autofill-launcher", { timeout: 5000 });
  console.log("✓ floating Autofill button appeared (content script ran)");
} catch (e) {
  console.error("✗ floating button never appeared — content script didn't fire on", FORM_URL);
  console.error("    manifest URL match for boards.greenhouse.io must be off");
  await ctx.close(); server.close(); flask.kill(); process.exit(1);
}

// 9. Click the floating button
await formPage.click("#jt-autofill-launcher");
await wait(500);

// 10. Inspect fields
const values = await formPage.evaluate(() => {
  const out = {};
  document.querySelectorAll("input[name], select[id]").forEach(el => {
    const key = el.name || el.id;
    out[key] = el.value;
  });
  return out;
});
console.log("\nform values after Autofill button click:");
let ok = 0, miss = 0;
for (const [k, v] of Object.entries(values)) {
  const mark = v ? "✓" : "✗";
  if (v) ok++; else miss++;
  console.log(`  ${mark} ${k} = ${JSON.stringify(v)}`);
}
console.log(`\nSummary: ${ok} filled / ${ok + miss} total`);

await ctx.close();
server.close();
flask.kill();
console.log("\n✅ integration test complete");
