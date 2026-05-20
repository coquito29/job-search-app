// Real-Chrome smoke test for the extension. Launches headed Chromium under
// xvfb with the extension loaded, opens a synthetic Greenhouse-style form
// served from a local HTTP origin, seeds chrome.storage.local with a profile
// via the extension's popup page (so we skip the OAuth/login dance), then
// triggers autofill via chrome.scripting.executeScript and inspects the form.
//
// This exercises code paths jsdom can't:
//   - manifest parses and loads without errors
//   - the autofill engine runs under real Chrome's V8
//   - chrome.scripting.executeScript actually injects into the target tab
//   - input/change events fire the way real ATS frameworks expect

import { chromium } from "playwright";
import path from "node:path";
import http from "node:http";
import fs from "node:fs";

const EXT_PATH = "/home/user/job-search-app/chrome-extension";
const USER_DATA_DIR = "/tmp/pw-ext-profile";
fs.rmSync(USER_DATA_DIR, { recursive: true, force: true });

const FORM_HTML = `<!doctype html><html><body>
<form id="application_form">
  <label for="first_name">First Name *</label>
  <input type="text" name="first_name" id="first_name" />
  <label for="last_name">Last Name *</label>
  <input type="text" name="last_name" id="last_name" />
  <label for="email">Email *</label>
  <input type="email" name="email" id="email" />
  <label for="phone">Phone *</label>
  <input type="tel" name="phone" id="phone" />
  <label for="linkedin_url">LinkedIn URL</label>
  <input type="url" name="linkedin_url" id="linkedin_url" />
  <label for="resume">Resume *</label>
  <input type="file" name="resume" id="resume" />
</form>
</body></html>`;

const PROFILE = {
  first_name: "George", last_name: "Tupayachi",
  full_name: "George Tupayachi",
  email: "georgetupayachijobs@outlook.com",
  phone: "+1 (609) 553-6215",
  address: { city: "Egg Harbor Township", state: "NJ", zip: "08234" },
  linkedin: "https://www.linkedin.com/in/george-tupayachi",
  portfolio: "",
  answers: {},
  qa_defaults: [],
};

// Serve the form on a real http origin so chrome.scripting can inject.
const server = http.createServer((req, res) => {
  res.writeHead(200, { "content-type": "text/html" });
  res.end(FORM_HTML);
});
await new Promise(r => server.listen(0, "127.0.0.1", r));
const PORT = server.address().port;
const FORM_URL = `http://127.0.0.1:${PORT}/apply`;
console.log("test form URL:", FORM_URL);

const ctx = await chromium.launchPersistentContext(USER_DATA_DIR, {
  headless: false,
  args: [
    `--disable-extensions-except=${EXT_PATH}`,
    `--load-extension=${EXT_PATH}`,
    "--no-first-run",
    "--no-default-browser-check",
  ],
});

// Wait for the extension's service worker to register so we know it loaded.
let sw = ctx.serviceWorkers()[0];
if (!sw) sw = await ctx.waitForEvent("serviceworker", { timeout: 10_000 }).catch(() => null);

let extensionId = null;
if (sw) {
  extensionId = new URL(sw.url()).host;
  console.log("✓ extension service worker URL:", sw.url());
} else {
  // MV3 with no background service_worker registers on first popup open.
  // Discover the extension ID from the loaded extensions API by opening
  // any extension URL pattern — we'll find it via the chrome internals page.
  const probe = await ctx.newPage();
  await probe.goto("chrome://extensions");
  await probe.waitForTimeout(500);
  // Pull extension IDs from the management API via DevTools protocol
  const ids = await probe.evaluate(async () => {
    const items = await new Promise(r => chrome.management.getAll(r));
    return items.filter(i => i.type === "extension").map(i => ({ id: i.id, name: i.name }));
  }).catch(() => null);
  if (ids) console.log("management API extensions:", ids);
  await probe.close();
  if (ids && ids.length) extensionId = ids.find(i => i.name.includes("Job Tracker"))?.id;
}

if (!extensionId) {
  console.error("✗ could not determine extension ID — extension may have failed to load");
  await ctx.close(); server.close(); process.exit(1);
}
console.log("✓ extension ID:", extensionId);

// 1. Seed chrome.storage.local with the profile via the popup page (which has
//    chrome.storage access). This skips the signin/network flow.
const popup = await ctx.newPage();
await popup.goto(`chrome-extension://${extensionId}/popup.html`);
await popup.evaluate((profile) => {
  return new Promise(r => chrome.storage.local.set(
    { appUrl: "http://example.invalid", profile, lastSync: Date.now() }, r));
}, PROFILE);
console.log("✓ profile seeded into chrome.storage.local");

// 2. Open the synthetic form page.
const formPage = await ctx.newPage();
await formPage.goto(FORM_URL);
console.log("✓ form page loaded");

// 3. Reload the popup so it reads the cached profile state.
await popup.reload();
await popup.waitForSelector("#autofill", { state: "visible", timeout: 5000 });

// 4. The popup's "Autofill this tab" button uses chrome.scripting.executeScript
//    to inject autofill.js into the active tab and run it. We can't easily
//    activate the form tab from the popup's POV, so we replicate the popup's
//    logic from the popup page (which has chrome.scripting privileges).
const result = await popup.evaluate(async (formUrl) => {
  const tabs = await chrome.tabs.query({});
  const tab = tabs.find(t => t.url === formUrl);
  if (!tab) return { error: "form tab not found", tabs: tabs.map(t => t.url) };
  await chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: true },
    files: ["autofill.js"],
  });
  const { profile } = await new Promise(r => chrome.storage.local.get("profile", r));
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: true },
    func: (p) => window.__jobTrackerAutofill && window.__jobTrackerAutofill.run(p),
    args: [profile],
  });
  return results.map(r => r.result);
}, FORM_URL);

console.log("autofill result from popup:", JSON.stringify(result, null, 2));

// 5. Inspect the form page directly to confirm fields actually changed.
await formPage.bringToFront();
const values = await formPage.evaluate(() => {
  const out = {};
  document.querySelectorAll("input[name]").forEach(el => { out[el.name] = el.value; });
  return out;
});
console.log("\nform values after autofill:");
for (const [k, v] of Object.entries(values)) {
  const mark = v ? "✓" : "✗";
  console.log(`  ${mark} ${k} = ${JSON.stringify(v)}`);
}

await ctx.close();
server.close();
