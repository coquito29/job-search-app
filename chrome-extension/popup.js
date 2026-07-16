// Popup logic. Three states:
//   1. No profile yet → show the App URL / Passcode form.
//   2. Logged in with cached profile → show the active panel.
//   3. Action buttons run autofill, refresh, or sign out.

const $ = (id) => document.getElementById(id);

function show(el)  { el.hidden = false; }
function hide(el)  { el.hidden = true; }

function msg(text, kind) {
  const m = $("msg");
  m.textContent = text;
  m.className   = "msg" + (kind ? " " + kind : "");
  show(m);
}
function clearMsg() { hide($("msg")); $("msg").textContent = ""; }

async function getStored() {
  return new Promise(res =>
    chrome.storage.local.get(["appUrl", "profile", "lastSync"], res)
  );
}

async function setStored(obj) {
  return new Promise(res => chrome.storage.local.set(obj, res));
}

function trimSlash(u) { return (u || "").replace(/\/+$/, ""); }

async function fetchProfile(appUrl) {
  const r = await fetch(`${trimSlash(appUrl)}/api/profile/full`, {
    method: "GET",
    credentials: "include",
    headers: { "Accept": "application/json" },
  });
  if (!r.ok) throw new Error(`profile fetch failed (${r.status})`);
  return await r.json();
}

async function login(appUrl, passcode) {
  const r = await fetch(`${trimSlash(appUrl)}/api/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode }),
  });
  if (!r.ok) {
    let err;
    try { err = (await r.json()).error; } catch (_) { err = `HTTP ${r.status}`; }
    throw new Error(err);
  }
  return await r.json();
}

async function logout(appUrl) {
  try {
    await fetch(`${trimSlash(appUrl)}/api/auth/logout`, {
      method: "POST",
      credentials: "include",
    });
  } catch (_) { /* offline is fine — we still clear local state */ }
}

function renderActive(profile, lastSync) {
  $("profile-name").textContent = profile.full_name || profile.email || "—";
  const when = lastSync ? new Date(lastSync).toLocaleString() : "—";
  $("last-sync").textContent = `Last sync: ${when}`;
  $("status-dot").className = "dot dot-on";
  $("status-dot").title = "signed in";
  show($("active"));
  hide($("settings"));
}

function renderSignedOut(appUrl) {
  if (appUrl) $("app-url").value = appUrl;
  $("status-dot").className = "dot dot-off";
  $("status-dot").title = "signed out";
  hide($("active"));
  show($("settings"));
}

async function init() {
  const { appUrl, profile, lastSync } = await getStored();
  if (profile) {
    renderActive(profile, lastSync);
  } else {
    renderSignedOut(appUrl);
  }
  // Auto-fill toggle — default ON when the key has never been set.
  const { autoFillEnabled } = await new Promise(res =>
    chrome.storage.local.get("autoFillEnabled", res));
  $("auto-fill-toggle").checked = autoFillEnabled !== false;
}

$("auto-fill-toggle").addEventListener("change", (e) => {
  chrome.storage.local.set({ autoFillEnabled: e.target.checked });
});

$("signin").addEventListener("click", async () => {
  clearMsg();
  const appUrl   = $("app-url").value.trim();
  const passcode = $("passcode").value.trim();
  if (!appUrl)   { msg("App URL is required.", "err"); return; }
  if (!passcode) { msg("Passcode is required.", "err"); return; }
  $("signin").disabled = true;
  try {
    await login(appUrl, passcode);
    const profile = await fetchProfile(appUrl);
    const lastSync = Date.now();
    await setStored({ appUrl, profile, lastSync });
    $("passcode").value = "";
    renderActive(profile, lastSync);
    msg("Signed in. Profile cached locally.", "ok");
  } catch (e) {
    msg(e.message || "Sign-in failed.", "err");
  } finally {
    $("signin").disabled = false;
  }
});

$("refresh").addEventListener("click", async () => {
  clearMsg();
  const { appUrl } = await getStored();
  if (!appUrl) { msg("Sign in again.", "err"); return; }
  $("refresh").disabled = true;
  try {
    const profile  = await fetchProfile(appUrl);
    const lastSync = Date.now();
    await setStored({ profile, lastSync });
    renderActive(profile, lastSync);
    msg("Profile refreshed.", "ok");
  } catch (e) {
    msg("Refresh failed — session may have expired. Sign in again.", "err");
    await setStored({ profile: null });
    renderSignedOut(appUrl);
  } finally {
    $("refresh").disabled = false;
  }
});

$("signout").addEventListener("click", async () => {
  clearMsg();
  const { appUrl } = await getStored();
  if (appUrl) await logout(appUrl);
  await setStored({ profile: null, lastSync: null, cvCache: null });
  renderSignedOut(appUrl);
  msg("Signed out.", "ok");
});

$("autofill").addEventListener("click", async () => {
  clearMsg();
  const { profile, appUrl } = await getStored();
  if (!profile) { msg("Sign in first.", "err"); return; }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) { msg("No active tab.", "err"); return; }

  $("autofill").disabled = true;
  try {
    // Inject the autofill engine into every frame (top-level + iframes).
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ["autofill.js"],
    });

    // ── Phase 1: rule-based fill (sync, returns {filled, total}) ─────────
    const phase1 = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: (p) => window.__jobTrackerAutofill && window.__jobTrackerAutofill.run(p),
      args: [profile],
    });
    const totals = phase1.reduce((acc, r) => {
      if (r.result) { acc.filled += r.result.filled; acc.total += r.result.total; }
      return acc;
    }, { filled: 0, total: 0 });

    // ── Phase 2: AI fill for fields the rules couldn't match ─────────────
    // Run only on the top frame — Claude's prompt size and the typical ATS
    // form-in-iframe pattern aren't worth the extra cost for sub-frames.
    let aiFilled = 0, aiSkipped = 0, aiReason = "";
    try {
      const collectRes = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: (p) => window.__jobTrackerAutofill && window.__jobTrackerAutofill.collectUnfilledFields(p),
        args: [profile],
      });
      const unfilled = (collectRes && collectRes[0] && collectRes[0].result) || [];
      if (unfilled.length && appUrl) {
        const pageCtx = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: () => ({
            url: location.href, hostname: location.hostname,
            title: (document.title || "").slice(0, 200),
            h1: (document.querySelector("h1")?.innerText || "").slice(0, 200),
          }),
        });
        const page_context = (pageCtx && pageCtx[0] && pageCtx[0].result) || {};
        // Relay through the background worker so the round-trip survives the popup closing.
        const reply = await new Promise(res =>
          chrome.runtime.sendMessage(
            { type: "aiFill", appUrl, fields: unfilled, page_context },
            r => res(r || { status: 0, body: { error: "no reply" } })
          )
        );
        if (reply.status === 200 && Array.isArray(reply.body && reply.body.fills)) {
          const applyRes = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: (fills) => window.__jobTrackerAutofill && window.__jobTrackerAutofill.applyAiFills(fills),
            args: [reply.body.fills],
          });
          const r = (applyRes && applyRes[0] && applyRes[0].result) || { applied: 0, skipped: 0 };
          aiFilled  = r.applied;
          aiSkipped = r.skipped;
        } else if (reply.status === 503) {
          aiReason = "AI not configured on server";
        } else if (reply.body && reply.body.error) {
          aiReason = reply.body.error;
        }
      }
    } catch (e) {
      aiReason = e.message || "AI step failed";
    }

    // Build final status line
    const combined = totals.filled + aiFilled;
    let line;
    if (combined === 0) {
      line = `No matching fields found (${totals.total} scanned).`;
    } else {
      line = `Filled ${totals.filled}` +
        (aiFilled ? ` + ${aiFilled} AI` : "") +
        ` of ${totals.total} fields.`;
    }
    if (aiReason) line += ` AI: ${aiReason}.`;
    line += " Review before submit.";
    msg(line, combined > 0 ? "ok" : "err");
  } catch (e) {
    msg(e.message || "Autofill failed (can't inject into this page).", "err");
  } finally {
    $("autofill").disabled = false;
  }
});

init();
