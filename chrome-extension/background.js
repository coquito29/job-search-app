// Background service worker.
//
// Two jobs:
//   1. Relay POSTs to /api/autofill so the AI call survives the popup closing
//      mid-round-trip (popups die the moment the user clicks elsewhere).
//      Content scripts also use this relay for the auto-fill path.
//   2. Fetch + cache the user's default CV as base64 so the content script
//      can auto-attach it to resume file inputs (engine Pass 4).
//
// Session cookies for the configured appUrl flow with `credentials: 'include'`
// because the extension origin is whitelisted in host_permissions
// (`<all_urls>` in manifest.json covers any user-chosen appUrl).
//
// Message shapes:
//   { type: 'aiFill', appUrl, fields, page_context, cv_id }
//     → { status: <http-status-or-0>, body: { ai_used, fills, ... } | { error } }
//   { type: 'getCv', appUrl }
//     → { b64, filename, mime } | { error }

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "autopilotRunNow") {
    autopilotRun("manual");
    sendResponse({ started: true });
    return;
  }
  if (msg.type === "aiFill") {
    aiFillRequest(msg)
      .then(r => sendResponse(r))
      .catch(e => sendResponse({ status: 0, body: { error: e.message || String(e) } }));
    return true; // keep channel open for async sendResponse
  }
  if (msg.type === "getCv") {
    getCvRequest(msg)
      .then(r => sendResponse(r))
      .catch(e => sendResponse({ error: e.message || String(e) }));
    return true;
  }
  if (msg.type === "learnAnswers") {
    learnAnswersRequest(msg)
      .then(r => sendResponse(r))
      .catch(e => sendResponse({ status: 0, body: { error: e.message || String(e) } }));
    return true;
  }
});

// Relay manually-typed answers to the server's qa_defaults ("Learn answers"
// button). Same cookie-credentialed pattern as aiFill.
async function learnAnswersRequest({ appUrl, answers }) {
  if (!appUrl) return { status: 0, body: { error: "no appUrl configured" } };
  const url = appUrl.replace(/\/+$/, "") + "/api/qa/learn";
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ answers: answers || [] }),
    });
  } catch (e) {
    return { status: 0, body: { error: "network: " + (e.message || String(e)) } };
  }
  let body = {};
  try { body = await res.json(); } catch (_) { body = {}; }
  return { status: res.status, body };
}

async function aiFillRequest({ appUrl, fields, page_context, cv_id }) {
  if (!appUrl) return { status: 0, body: { error: "no appUrl configured" } };
  const url = appUrl.replace(/\/+$/, "") + "/api/autofill";
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({
        fields:       fields || [],
        page_context: page_context || {},
        cv_id:        cv_id || null,
      }),
    });
  } catch (e) {
    return { status: 0, body: { error: "network: " + (e.message || String(e)) } };
  }
  let body = {};
  try { body = await res.json(); } catch (_) { body = {}; }
  return { status: res.status, body };
}

// ── Default-CV fetch + cache ────────────────────────────────────────────────
// The CV rarely changes, so cache for 24h in chrome.storage.local (the
// service worker itself is ephemeral). ~330 KB PDF → ~440 KB base64; well
// under the storage quota.
const CV_CACHE_MS = 24 * 60 * 60 * 1000;

async function getCvRequest({ appUrl }) {
  if (!appUrl) return { error: "no appUrl configured" };
  const base = appUrl.replace(/\/+$/, "");

  const { cvCache } = await chrome.storage.local.get("cvCache");
  if (cvCache && cvCache.b64 && Date.now() - (cvCache.fetchedAt || 0) < CV_CACHE_MS) {
    return { b64: cvCache.b64, filename: cvCache.filename, mime: cvCache.mime };
  }

  // Find the default CV (list is ordered is_default DESC, id DESC).
  const listRes = await fetch(base + "/api/cvs", {
    credentials: "include", headers: { "Accept": "application/json" },
  });
  if (!listRes.ok) return { error: `cv list failed (${listRes.status})` };
  const { cvs } = await listRes.json();
  if (!Array.isArray(cvs) || !cvs.length) return { error: "no CVs in library" };
  const cv = cvs.find(c => c.is_default) || cvs[0];

  const fileRes = await fetch(`${base}/api/cvs/${cv.id}`, { credentials: "include" });
  if (!fileRes.ok) return { error: `cv download failed (${fileRes.status})` };
  const buf = await fileRes.arrayBuffer();

  // Chunked conversion — String.fromCharCode(...wholeArray) overflows the
  // argument limit on large files.
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  const b64 = btoa(bin);

  const entry = {
    b64,
    filename:  cv.filename || "cv.pdf",
    mime:      cv.mime_type || "application/pdf",
    fetchedAt: Date.now(),
  };
  await chrome.storage.local.set({ cvCache: entry });
  return { b64: entry.b64, filename: entry.filename, mime: entry.mime };
}

// ── Autopilot: daily auto-apply ─────────────────────────────────────────────
// A daily alarm pulls the day's queue from the server (/api/autopilot/queue —
// only safe, fast-ATS, unseen jobs come back), opens each in a background
// tab, asks the content script to fill + submitIfComplete(), reports every
// outcome to /api/autopilot/report, and posts a summary notification.
// Runs whenever Chrome is open at alarm time; a missed alarm fires on the
// next browser start. Toggle + "Run now" live in the popup.

const AUTOPILOT_ALARM   = "autopilot-daily";
const AUTOPILOT_HOUR    = 9;    // fire at 09:30 local
const AUTOPILOT_MINUTE  = 30;
const TAB_LOAD_TIMEOUT  = 45_000;   // page load wait
const TAB_SETTLE_MS     = 5_000;    // extra beat for SPA form render
const FILL_TIMEOUT      = 180_000;  // fill + AI + submit, per job
const MAX_REVIEW_TABS   = 5;        // needs-review tabs kept open for the user

function scheduleAutopilotAlarm() {
  const next = new Date();
  next.setHours(AUTOPILOT_HOUR, AUTOPILOT_MINUTE, 0, 0);
  if (next.getTime() <= Date.now()) next.setDate(next.getDate() + 1);
  chrome.alarms.create(AUTOPILOT_ALARM, {
    when: next.getTime(),
    periodInMinutes: 24 * 60,
  });
}
chrome.runtime.onInstalled.addListener(scheduleAutopilotAlarm);
chrome.runtime.onStartup.addListener(scheduleAutopilotAlarm);

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === AUTOPILOT_ALARM) autopilotRun("scheduled");
});

let apRunning = false;

async function autopilotRun(trigger) {
  if (apRunning) return;
  apRunning = true;
  // MV3 service workers idle out after ~30s; a periodic no-op API call keeps
  // this worker alive across the multi-minute run.
  const keepalive = setInterval(() => chrome.runtime.getPlatformInfo(() => {}), 20_000);
  try {
    const { appUrl, profile, autopilotEnabled } =
      await chrome.storage.local.get(["appUrl", "profile", "autopilotEnabled"]);
    if (trigger === "scheduled" && autopilotEnabled === false) return; // default ON
    if (!appUrl || !profile) {
      notifySummary("Autopilot can't run", "Open the extension popup and sign in to your job app first.");
      return;
    }
    const base = appUrl.replace(/\/+$/, "");

    let queue = [];
    try {
      const res = await fetch(base + "/api/autopilot/queue?cap=20", {
        credentials: "include", headers: { "Accept": "application/json" },
      });
      if (res.status === 401) {
        notifySummary("Autopilot: signed out", "Your app session expired — open the app and enter your passcode, then run autopilot again.");
        return;
      }
      const body = await res.json();
      queue = Array.isArray(body.jobs) ? body.jobs : [];
    } catch (e) {
      notifySummary("Autopilot: queue fetch failed", String(e.message || e).slice(0, 120));
      return;
    }
    if (!queue.length) {
      if (trigger === "manual") notifySummary("Autopilot", "No eligible jobs in today's queue — everything safe is already applied or attempted.");
      await chrome.storage.local.set({ lastAutopilotRun: {
        at: Date.now(), submitted: 0, review: 0, failed: 0, empty: true } });
      return;
    }

    let submitted = 0, review = 0, failed = 0, reviewTabs = 0;
    for (const job of queue) {
      const outcome = await attemptJob(job);
      if (outcome.result === "submitted") submitted++;
      else if (outcome.result === "needs_review") review++;
      else failed++;

      // Keep a few needs-review tabs open so the morning starts with the
      // forms already filled; close everything else.
      const keepOpen = outcome.result === "needs_review" && reviewTabs < MAX_REVIEW_TABS;
      if (keepOpen) reviewTabs++;
      else if (outcome.tabId != null) {
        try { await chrome.tabs.remove(outcome.tabId); } catch (_) {}
      }

      try {
        await fetch(base + "/api/autopilot/report", {
          method: "POST", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: job.url, title: job.title, company: job.company,
            result: outcome.result, detail: outcome.detail || "",
            filled: outcome.filled || 0, total: outcome.total || 0,
          }),
        });
      } catch (_) { /* report is best-effort; attempt table catches up next run */ }
    }

    await chrome.storage.local.set({ lastAutopilotRun: {
      at: Date.now(), submitted, review, failed } });
    notifySummary(
      `Autopilot: ${submitted} submitted`,
      `${review} left open for review, ${failed} failed. Details in the app's tracker.`);
  } finally {
    clearInterval(keepalive);
    apRunning = false;
  }
}

// Open one job in a background tab, drive the content script, classify the
// outcome. Always resolves — never throws.
async function attemptJob(job) {
  let tab = null;
  try {
    tab = await chrome.tabs.create({ url: job.url, active: false });
  } catch (e) {
    return { result: "error", detail: "tab open failed: " + (e.message || e), tabId: null };
  }
  const tabId = tab.id;

  const loaded = await new Promise(resolve => {
    const timer = setTimeout(() => { cleanup(); resolve(false); }, TAB_LOAD_TIMEOUT);
    function onUpdated(id, info) {
      if (id === tabId && info.status === "complete") { cleanup(); resolve(true); }
    }
    function onRemoved(id) {
      if (id === tabId) { cleanup(); resolve(false); }
    }
    function cleanup() {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.tabs.onRemoved.removeListener(onRemoved);
    }
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.onRemoved.addListener(onRemoved);
    // The tab may already be complete by the time listeners attach.
    chrome.tabs.get(tabId, t => {
      if (!chrome.runtime.lastError && t && t.status === "complete") { cleanup(); resolve(true); }
    });
  });
  if (!loaded) return { result: "timeout", detail: "page did not finish loading", tabId };

  await new Promise(r => setTimeout(r, TAB_SETTLE_MS));

  const reply = await new Promise(resolve => {
    const timer = setTimeout(() => resolve(null), FILL_TIMEOUT);
    try {
      chrome.tabs.sendMessage(tabId, { type: "autopilotGo" }, r => {
        clearTimeout(timer);
        // lastError fires when no frame answered (no form on page).
        if (chrome.runtime.lastError) resolve(undefined);
        else resolve(r);
      });
    } catch (_) { clearTimeout(timer); resolve(undefined); }
  });

  if (reply === null)      return { result: "timeout", detail: "fill/submit timed out", tabId };
  if (!reply)              return { result: "no_form", detail: "no application form detected", tabId };
  return {
    result: reply.result || "error",
    detail: reply.detail || "",
    filled: reply.filled || 0,
    total:  reply.total  || 0,
    tabId,
  };
}

function notifySummary(title, message) {
  try {
    chrome.notifications.create({
      type: "basic", iconUrl: "icons/icon-128.png",
      title, message,
    });
  } catch (_) {}
}
