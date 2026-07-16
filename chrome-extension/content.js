// Content script — runs on ATS domains listed in manifest. Two modes:
//   1) AUTO (default): when an application form is detected, runs the full
//      pipeline automatically — rules + AI Phase 2 + CV upload. Toggle off
//      via the popup's "Auto-fill forms automatically" checkbox.
//   2) Manual: floating "Autofill" button re-runs the same pipeline (useful
//      after expanding hidden sections or revealing more fields).
// Never submits — the engine only highlights the Submit button.

(function () {
  // Avoid duplicate setup if injected twice.
  if (window.__jobTrackerContentLoaded) return;
  window.__jobTrackerContentLoaded = true;

  const ranUrls = new Set();   // URLs auto-run already happened on (SPA-aware)
  let runInFlight = false;
  let obs = null;              // MutationObserver — disconnected when orphaned

  // After the extension is reloaded/updated in chrome://extensions, content
  // scripts already running in open tabs are orphaned: chrome.runtime.id
  // becomes undefined and any chrome.* API call throws "Extension context
  // invalidated". Detect that and shut down quietly instead of erroring.
  function extensionAlive() {
    try { return !!(chrome.runtime && chrome.runtime.id); } catch (_) { return false; }
  }

  function shutdownIfOrphaned() {
    if (extensionAlive()) return false;
    if (obs) { try { obs.disconnect(); } catch (_) {} obs = null; }
    return true;
  }

  function notify(text, kind) {
    const root = document.createElement("div");
    root.style.cssText = `
      position: fixed; bottom: 80px; right: 16px; z-index: 2147483647;
      padding: 10px 14px; border-radius: 8px;
      background: ${kind === "err" ? "#b91c1c" : kind === "warn" ? "#b45309" : "#0d6efd"}; color: #fff;
      font: 500 13px/1.4 system-ui, -apple-system, sans-serif;
      box-shadow: 0 4px 14px rgba(0,0,0,.25); max-width: 320px;
    `;
    root.textContent = text;
    document.body.appendChild(root);
    setTimeout(() => root.remove(), 4000);
  }

  function getStored(keys) {
    if (shutdownIfOrphaned()) return Promise.resolve({});
    return new Promise(res => {
      try { chrome.storage.local.get(keys, res); } catch (_) { res({}); }
    });
  }

  function sendMessage(msg) {
    if (shutdownIfOrphaned()) return Promise.resolve(null);
    return new Promise(res => {
      try { chrome.runtime.sendMessage(msg, r => res(r || null)); } catch (_) { res(null); }
    });
  }

  // Full pipeline: CV globals → Phase 1 rules → Phase 2 AI. Mirrors what the
  // popup's "Autofill this tab" button does, so auto and manual behave the same.
  async function runFullAutofill(trigger) {
    if (runInFlight) return;
    if (!window.__jobTrackerAutofill) {
      if (trigger === "manual") notify("Autofill engine not loaded.", "err");
      return;
    }
    const { profile, appUrl } = await getStored(["profile", "appUrl"]);
    if (!profile) {
      if (trigger === "manual") notify("No profile cached. Open the extension popup to sign in.", "err");
      return;
    }
    runInFlight = true;
    try {
      // CV for the resume-upload pass (Pass 4). Background fetches + caches
      // the default CV; soft-fail keeps the rest of the fill working.
      if (!window.__jt_cv_b64 && appUrl) {
        try {
          const cv = await sendMessage({ type: "getCv", appUrl });
          if (cv && cv.b64) {
            window.__jt_cv_b64      = cv.b64;
            window.__jt_cv_filename = cv.filename;
            window.__jt_cv_mime     = cv.mime;
          }
        } catch (_) { /* CV is opportunistic */ }
      }

      // Phase 1: rule-based fill (async — awaits autocomplete dropdowns etc.)
      const r1 = await window.__jobTrackerAutofill.run(profile) || { filled: 0, total: 0 };

      // Phase 2: AI fill for what the rules couldn't match.
      let aiFilled = 0, aiReason = "";
      try {
        const unfilled = window.__jobTrackerAutofill.collectUnfilledFields(profile) || [];
        if (unfilled.length && appUrl) {
          const page_context = {
            url: location.href, hostname: location.hostname,
            title: (document.title || "").slice(0, 200),
            h1: (document.querySelector("h1")?.innerText || "").slice(0, 200),
          };
          const reply = await sendMessage({ type: "aiFill", appUrl, fields: unfilled, page_context });
          if (reply && reply.status === 200 && Array.isArray(reply.body && reply.body.fills)) {
            const res = await window.__jobTrackerAutofill.applyAiFills(reply.body.fills);
            aiFilled = res.applied || 0;
          } else if (reply && reply.status === 503) {
            aiReason = "AI not configured on server";
          } else if (reply && reply.body && reply.body.error) {
            aiReason = String(reply.body.error).slice(0, 80);
          }
        }
      } catch (e) {
        aiReason = e.message || "AI step failed";
      }

      const combined = (r1.filled || 0) + aiFilled;
      // In iframes (all_frames), stay quiet unless something actually filled —
      // otherwise every empty sub-frame pops a useless toast.
      if (combined > 0) {
        notify(`Auto-filled ${r1.filled}${aiFilled ? ` + ${aiFilled} AI` : ""} fields. Review, then submit.`);
      } else if (trigger === "manual" && window === window.top) {
        notify(`No matching fields found (${r1.total} scanned).${aiReason ? " AI: " + aiReason : ""}`, "warn");
      }
    } finally {
      runInFlight = false;
    }
  }

  // Collect answers the user typed by hand (fields the engine missed) and
  // send them to the server's qa_defaults — next time a form asks the same
  // question, Pass 1 fills it automatically, and the AI sees them as
  // grounding for similar questions.
  async function learnAnswers() {
    if (!window.__jobTrackerAutofill?.collectLearnableAnswers) {
      notify("Autofill engine not loaded.", "err");
      return;
    }
    const { profile, appUrl } = await getStored(["profile", "appUrl"]);
    if (!profile || !appUrl) {
      notify("Sign in via the extension popup first.", "err");
      return;
    }
    const pairs = window.__jobTrackerAutofill.collectLearnableAnswers(profile);
    if (!pairs.length) {
      notify("Nothing new to learn — everything filled is already covered.", "warn");
      return;
    }
    const reply = await sendMessage({ type: "learnAnswers", appUrl, answers: pairs });
    if (reply && reply.status === 200 && reply.body && reply.body.ok) {
      const n = (reply.body.added || 0) + (reply.body.updated || 0);
      notify(`Learned ${n} answer${n === 1 ? "" : "s"} — they'll fill automatically next time.`);
      // Refresh the cached profile so this session already knows them.
      if (Array.isArray(reply.body.qa_defaults)) {
        profile.qa_defaults = reply.body.qa_defaults;
        try { chrome.storage.local.set({ profile }); } catch (_) {}
      }
    } else {
      notify("Couldn't save answers: " + (reply?.body?.error || "network error"), "err");
    }
  }

  function injectLauncher() {
    if (document.getElementById("jt-autofill-launcher")) return;
    if (window !== window.top) return; // one button, top frame only
    const btn = document.createElement("button");
    btn.id = "jt-autofill-launcher";
    btn.type = "button";
    btn.textContent = "Autofill";
    btn.title = "Job Tracker — autofill this application";
    btn.style.cssText = `
      position: fixed; bottom: 20px; right: 16px; z-index: 2147483647;
      padding: 10px 16px; border-radius: 999px; border: none;
      background: #0d6efd; color: #fff; cursor: pointer;
      font: 600 14px/1 system-ui, -apple-system, sans-serif;
      box-shadow: 0 4px 14px rgba(0,0,0,.25);
    `;
    btn.addEventListener("click", () => runFullAutofill("manual"));
    document.body.appendChild(btn);

    const learn = document.createElement("button");
    learn.id = "jt-learn-launcher";
    learn.type = "button";
    learn.textContent = "Learn answers";
    learn.title = "Save the answers you typed by hand so they auto-fill next time";
    learn.style.cssText = `
      position: fixed; bottom: 20px; right: 112px; z-index: 2147483647;
      padding: 10px 14px; border-radius: 999px; border: 1px solid #0d6efd;
      background: #fff; color: #0d6efd; cursor: pointer;
      font: 600 13px/1 system-ui, -apple-system, sans-serif;
      box-shadow: 0 4px 14px rgba(0,0,0,.18);
    `;
    learn.addEventListener("click", learnAnswers);
    document.body.appendChild(learn);
  }

  // Message from the popup ("Autofill current tab" button) — kept for
  // back-compat; the popup path injects and drives the engine itself.
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "autofill" && msg.profile) {
      Promise.resolve(
        window.__jobTrackerAutofill
          ? window.__jobTrackerAutofill.run(msg.profile)
          : { filled: 0, total: 0 }
      ).then(result => {
        if (result.filled > 0) notify(`Autofilled ${result.filled} of ${result.total} fields.`);
        sendResponse(result);
      });
      return true;
    }
  });

  // Auto-run once per URL when a real form is present. Waits a beat after
  // detection so React/Vue finish rendering the full form first.
  async function maybeAutoRun() {
    if (shutdownIfOrphaned()) return;
    const url = location.href.split("#")[0];
    if (ranUrls.has(url)) return;
    const { autoFillEnabled, profile } = await getStored(["autoFillEnabled", "profile"]);
    if (autoFillEnabled === false) return;  // default ON when undefined
    if (!profile) return;                   // signed out — stay silent
    ranUrls.add(url);
    setTimeout(() => runFullAutofill("auto"), 900);
  }

  // Only act once a real application form is on the page — some ATSes render
  // the job listing first and the form behind an "Apply" click. Ashby renders
  // its form WITHOUT a <form> tag (inputs in bare divs), so don't require one;
  // instead look for an application-shaped field set: a resume file input, an
  // email input, or several free-text fields. A lone search box won't trigger.
  const ensureReady = () => {
    const hasFile  = document.querySelector('input[type="file"]');
    const hasEmail = document.querySelector('input[type="email"], input[autocomplete="email"]');
    const textish  = document.querySelectorAll(
      'input[type="text"], input:not([type]), textarea').length;
    if (!hasFile && !hasEmail && textish < 3) return;
    injectLauncher();
    maybeAutoRun();
  };

  ensureReady();
  obs = new MutationObserver(() => ensureReady());
  obs.observe(document.documentElement, { childList: true, subtree: true });
})();
