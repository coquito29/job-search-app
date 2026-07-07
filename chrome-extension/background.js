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
