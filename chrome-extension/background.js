// Job Search App — Auto-fill (background service worker)
//
// Content scripts can't reliably hit /api/autofill directly:
//   - Cross-origin from the page's host (ashbyhq.com, etc.) triggers CORS.
//   - The session cookie has SameSite=Lax, so a content-script fetch is a
//     cross-site request that wouldn't include the cookie even if CORS allowed.
//
// The service worker has the extension's privileges (matches host_permissions
// in manifest.json), so its fetch behaves like the popup's: cookies flow,
// CORS preflight is unnecessary.
//
// Content scripts send {type: 'autofill', server, payload}; we return
// {status, body} synchronously to the listener.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === 'autofill') {
    autofillRequest(msg.server, msg.payload)
      .then(result => sendResponse(result))
      .catch(e => sendResponse({ status: 0, error: e.message || String(e) }));
    return true; // keep the channel open for async sendResponse
  }
});

async function autofillRequest(server, payload) {
  if (!server) return { status: 0, error: 'no server configured' };
  const url = server.replace(/\/+$/, '') + '/api/autofill';
  let res, body = {};
  try {
    res = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    return { status: 0, error: 'network: ' + (e.message || String(e)) };
  }
  try {
    body = await res.json();
  } catch (_) {
    body = {};
  }
  return { status: res.status, body };
}
