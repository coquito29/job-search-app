// Background service worker.
//
// One job: relay POSTs to /api/autofill on behalf of the popup so the AI call
// survives the popup closing during the round-trip (popups die the moment the
// user clicks anywhere else). The popup could fetch directly, but a 3-5s
// Claude call routinely outlives the popup's lifetime.
//
// Session cookies for the configured appUrl flow with `credentials: 'include'`
// because the extension origin is whitelisted in host_permissions
// (`<all_urls>` in manifest.json covers any user-chosen appUrl).
//
// Message shape:
//   { type: 'aiFill', appUrl, fields, page_context, cv_id }
// Reply shape:
//   { status: <http-status-or-0>, body: { ai_used, fills, ... } | { error } }

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "aiFill") return;
  aiFillRequest(msg)
    .then(r => sendResponse(r))
    .catch(e => sendResponse({ status: 0, body: { error: e.message || String(e) } }));
  return true; // keep channel open for async sendResponse
});

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
