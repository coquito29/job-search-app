// Content script — runs on ATS domains listed in manifest. Loads the cached
// profile from chrome.storage and either:
//   1) Responds to a message from the popup ("Autofill this page" button), or
//   2) Shows a small floating launcher so the user can fire autofill in-page.

(function () {
  // Avoid duplicate setup if injected twice.
  if (window.__jobTrackerContentLoaded) return;
  window.__jobTrackerContentLoaded = true;

  function notify(text, kind) {
    const root = document.createElement("div");
    root.style.cssText = `
      position: fixed; bottom: 80px; right: 16px; z-index: 2147483647;
      padding: 10px 14px; border-radius: 8px;
      background: ${kind === "err" ? "#b91c1c" : "#0d6efd"}; color: #fff;
      font: 500 13px/1.4 system-ui, -apple-system, sans-serif;
      box-shadow: 0 4px 14px rgba(0,0,0,.25); max-width: 320px;
    `;
    root.textContent = text;
    document.body.appendChild(root);
    setTimeout(() => root.remove(), 3000);
  }

  function runAutofill(profile) {
    if (!profile) {
      notify("No profile cached. Open the extension popup to sign in.", "err");
      return;
    }
    if (!window.__jobTrackerAutofill) {
      notify("Autofill engine not loaded.", "err");
      return;
    }
    const { filled, total } = window.__jobTrackerAutofill.run(profile);
    if (filled === 0) {
      notify(`No fields matched (${total} scanned).`, "err");
    } else {
      notify(`Autofilled ${filled} of ${total} fields. Review before submit.`);
    }
  }

  function injectLauncher() {
    if (document.getElementById("jt-autofill-launcher")) return;
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
    btn.addEventListener("click", () => {
      chrome.storage.local.get("profile", ({ profile }) => runAutofill(profile));
    });
    document.body.appendChild(btn);
  }

  // Message from the popup ("Autofill current tab" button).
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "autofill" && msg.profile) {
      const result = window.__jobTrackerAutofill
        ? window.__jobTrackerAutofill.run(msg.profile)
        : { filled: 0, total: 0 };
      if (result.filled === 0) {
        notify(`No fields matched (${result.total} scanned).`, "err");
      } else {
        notify(`Autofilled ${result.filled} of ${result.total} fields.`);
      }
      sendResponse(result);
      return true;
    }
  });

  // Only show the launcher once a real form is on the page — some ATSes
  // render the job listing first and the form behind an "Apply" click.
  const ensureLauncher = () => {
    const hasForm = document.querySelector("form input, form select, form textarea");
    if (hasForm) injectLauncher();
  };

  ensureLauncher();
  const obs = new MutationObserver(() => ensureLauncher());
  obs.observe(document.documentElement, { childList: true, subtree: true });
})();
