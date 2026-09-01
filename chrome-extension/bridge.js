// Site → extension bridge.
//
// Runs ONLY on the job-tracker web app's own origin (see manifest
// content_scripts). Its whole job is to let the website start an autopilot
// run, so "Run autopilot now" doesn't live exclusively in the extension
// popup — the site is where the user already looks for results.
//
// The page can't call chrome.runtime.sendMessage directly (no
// externally_connectable declared, and we'd rather not hardcode the
// extension id), so the page posts a window message and this relay forwards
// it. Deliberately narrow: the ONLY accepted command is "start a run".
// Nothing here reads or returns profile data.
//
// Protocol (page ↔ bridge), all messages tagged __jtBridge:
//   page  → "ping"          bridge → "present" {version}
//   page  → "runAutopilot"  bridge → "autopilotStarted" {ok}

(function () {
  if (window.__jtBridgeLoaded) return;
  window.__jtBridgeLoaded = true;

  function reply(type, extra) {
    window.postMessage(Object.assign({ __jtBridge: type }, extra || {}), window.location.origin);
  }

  function extensionAlive() {
    try { return !!(chrome.runtime && chrome.runtime.id); } catch (_) { return false; }
  }

  function announce() {
    if (!extensionAlive()) return;
    let version = "";
    try { version = chrome.runtime.getManifest().version; } catch (_) {}
    reply("present", { version });
  }

  window.addEventListener("message", (ev) => {
    // Same-window, same-origin messages only — never act on anything a
    // frame or another origin posted in.
    if (ev.source !== window) return;
    if (ev.origin !== window.location.origin) return;
    const d = ev.data;
    if (!d || typeof d !== "object") return;

    if (d.__jtBridge === "ping") { announce(); return; }

    if (d.__jtBridge === "runAutopilot") {
      if (!extensionAlive()) { reply("autopilotStarted", { ok: false, error: "extension reloading" }); return; }
      try {
        chrome.runtime.sendMessage({ type: "autopilotRunNow" }, (r) => {
          const err = chrome.runtime.lastError;
          reply("autopilotStarted", {
            ok: !err && !!(r && r.started),
            error: err ? err.message : (r && r.started ? "" : "worker did not start"),
          });
        });
      } catch (e) {
        reply("autopilotStarted", { ok: false, error: e.message || String(e) });
      }
    }
  });

  // The page may load its script before or after this content script, so
  // announce immediately AND answer pings.
  announce();
})();
