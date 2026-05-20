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
}

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
  await setStored({ profile: null, lastSync: null });
  renderSignedOut(appUrl);
  msg("Signed out.", "ok");
});

$("autofill").addEventListener("click", async () => {
  clearMsg();
  const { profile } = await getStored();
  if (!profile) { msg("Sign in first.", "err"); return; }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) { msg("No active tab.", "err"); return; }

  // Inject the autofill engine, then run it. Works on any page, not just
  // the manifest-matched ATS domains.
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ["autofill.js"],
    });
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: (p) => window.__jobTrackerAutofill && window.__jobTrackerAutofill.run(p),
      args: [profile],
    });
    const totals = results.reduce((acc, r) => {
      if (r.result) { acc.filled += r.result.filled; acc.total += r.result.total; }
      return acc;
    }, { filled: 0, total: 0 });
    if (totals.filled === 0) {
      msg(`No matching fields found (${totals.total} scanned).`, "err");
    } else {
      msg(`Filled ${totals.filled} of ${totals.total} fields. Review before submit.`, "ok");
    }
  } catch (e) {
    msg(e.message || "Autofill failed (can't inject into this page).", "err");
  }
});

init();
