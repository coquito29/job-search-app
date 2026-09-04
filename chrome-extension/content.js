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
    if (runInFlight) return null;
    if (!window.__jobTrackerAutofill) {
      if (trigger === "manual") notify("Autofill engine not loaded.", "err");
      return null;
    }
    const { profile, appUrl } = await getStored(["profile", "appUrl"]);
    if (!profile) {
      if (trigger === "manual") notify("No profile cached. Open the extension popup to sign in.", "err");
      return null;
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

      // A run that filled almost nothing is the one worth keeping. Snapshot the
      // form's shape and send it up, so a page that beat the engine becomes an
      // offline fixture instead of closing with the tab -- without this, every
      // attempt to understand a 0/8 costs another real application. Top frame
      // only (all_frames means an ad iframe would otherwise report a 0/0), and
      // only when there was a form to miss.
      const CAPTURE_FILL_RATIO = 0.5;
      const seenTotal = r1.total || 0;
      if (window === window.top && appUrl && seenTotal > 0
          && (combined / seenTotal) < CAPTURE_FILL_RATIO
          && window.__jobTrackerAutofill.captureFormShape) {
        try {
          const capture = window.__jobTrackerAutofill.captureFormShape();
          capture.filled = combined;
          capture.total  = seenTotal;
          // Fire-and-forget: a diagnostic must never delay or break a run.
          sendMessage({ type: "captureForm", appUrl, capture }).catch(() => {});
        } catch (_) { /* ignore */ }
      }
      // In iframes (all_frames), stay quiet unless something actually filled —
      // otherwise every empty sub-frame pops a useless toast.
      if (combined > 0) {
        notify(`Auto-filled ${r1.filled}${aiFilled ? ` + ${aiFilled} AI` : ""} fields. Review, then submit.`);
      } else if (trigger === "manual" && window === window.top) {
        notify(`No matching fields found (${r1.total} scanned).${aiReason ? " AI: " + aiReason : ""}`, "warn");
      }
      return { filled: combined, aiFilled, total: r1.total || 0 };
    } finally {
      runInFlight = false;
    }
  }

  // ── Autopilot (background-tab auto-apply) ─────────────────────────────────
  // Full pipeline + submitIfComplete(). Only the frame that actually holds an
  // application-shaped form answers; frames without one return no response so
  // the background worker's sendMessage resolves from the right frame.
  function hasApplicationForm() {
    if (document.querySelector('input[type="file"]')) return true;
    if (document.querySelector('input[type="email"], input[autocomplete="email"]')) return true;
    return document.querySelectorAll(
      'input[type="text"], input:not([type]), textarea').length >= 3;
  }

  // Many ATSes (BambooHR, Lever, Recruitee…) render the job DESCRIPTION at the
  // posting URL and only reveal the application form after an "Apply" click.
  // Without this, autopilot opened those pages, saw no form and reported
  // no_form — never even attempting jobs it could have filled. Verified on
  // ebq.bamboohr.com 2026-08-31: one click turns 1 field into 21.
  // Anchored to the START, not the whole string. The old exact-match version
  // only recognised a handful of exact phrases, so two of the five no_form
  // results on 2026-09-02 were simply an unrecognised label: SmartRecruiters
  // says "I'm interested" and Breezy says "Apply To Position". Neither was
  // matched, so the page was reported as having no form at all.
  const APPLY_TEXT_RE =
    /^(apply\b|start\s+(your\s+)?application\b|begin\s+application\b|submit\s+an?\s+application\b|i'?m\s+interested\b|application$)/i;
  // ...but never a third-party sign-in route or an unrelated "apply". Breezy
  // renders "Apply Using LinkedIn" right beside the real button, and that
  // hands the application to an OAuth flow instead of the form.
  const APPLY_EXCLUDE_RE =
    /\b(linkedin|indeed|google|facebook|seek|xing|glassdoor|filters?|coupon|promo|discount)\b/i;

  function isVisibleEl(el) {
    if (!el || !el.offsetParent) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function findApplyButton() {
    for (const el of document.querySelectorAll(
        'button, a, [role="button"], input[type="button"]')) {
      const t = (el.innerText || el.value || "").replace(/\s+/g, " ").trim();
      if (!t || t.length > 40) continue;
      if (!APPLY_TEXT_RE.test(t)) continue;
      if (APPLY_EXCLUDE_RE.test(t)) continue;
      if (!isVisibleEl(el)) continue;
      return el;
    }
    return null;
  }

  // Cookie walls hide application forms. On Workable's aggregator listings
  // (jobs.workable.com/view/...) the Apply button opens the application modal
  // UNDERNEATH the consent banner, and the form's inputs are not in the DOM
  // at all until the banner is dealt with — so the page looked formless.
  // Three of the five no_form results on 2026-09-02 were this, not a missing
  // form: dismissing the banner turned 0 inputs into 13, including the resume
  // slot.
  //
  // Only ever the privacy-preserving option. "Accept all" is a consent
  // decision that belongs to the user, so if a banner offers nothing but
  // acceptance we leave it alone and the attempt honestly reports no_form.
  const CONSENT_DECLINE_RE =
    /^(decline all|decline|reject all|reject|refuse all|only necessary|necessary only|essential only|strictly necessary|use necessary cookies only|continue without accepting)$/i;

  function dismissConsentBanner() {
    for (const el of document.querySelectorAll(
        'button, a, [role="button"], input[type="button"]')) {
      const t = (el.innerText || el.value || "").replace(/\s+/g, " ").trim();
      if (!t || t.length > 40) continue;
      if (!CONSENT_DECLINE_RE.test(t)) continue;
      if (!isVisibleEl(el)) continue;
      try { el.click(); } catch (_) { continue; }
      return true;
    }
    return false;
  }

  // Only ever called when NO form is on the page, so there is nothing this
  // click could submit — it can only reveal one.
  async function revealApplicationForm(maxWaitMs) {
    // A banner can be sitting over the form before we click anything.
    if (dismissConsentBanner()) await new Promise(r => setTimeout(r, 600));
    if (hasApplicationForm()) return true;
    const btn = findApplyButton();
    if (!btn) return false;
    try { btn.click(); } catch (_) { return false; }
    // 45s, not 12s. Workable's apply modal opens immediately but loads its
    // form body lazily behind a spinner — on jobs.workable.com the header and
    // Submit button were up in under a second while the fields took well over
    // twenty to arrive, and eventually rendered 63 inputs including both file
    // slots. The old 12s ceiling gave up mid-spinner and reported "apply
    // button did not reveal a form" on a page that had one coming. Matches
    // the 45s already allowed for a tab load; the per-job fill timeout (180s)
    // still bounds the whole attempt.
    const start = Date.now();
    let dismissed = false;
    while (Date.now() - start < (maxWaitMs || 45000)) {
      await new Promise(r => setTimeout(r, 400));
      if (hasApplicationForm()) return true;
      // ...and one can appear WITH the modal, which is the Workable case.
      if (!dismissed && dismissConsentBanner()) {
        dismissed = true;
        await new Promise(r => setTimeout(r, 600));
        if (hasApplicationForm()) return true;
      }
    }
    return false;
  }

  async function runAutopilot(job) {
    // The per-URL auto-run may already be mid-flight (it fires ~900ms after
    // form detection). Let it finish, then run again — the engine skips
    // fields that already hold values, so the second pass is cheap.
    for (let waited = 0; runInFlight && waited < 60_000; waited += 500) {
      await new Promise(r => setTimeout(r, 500));
    }
    const stats = await runFullAutofill("autopilot");
    if (!stats) {
      return { result: "error", detail: "engine not loaded or signed out", filled: 0, total: 0 };
    }
    let sub = { submitted: false, blockers: ["engine has no submit support"] };
    if (window.__jobTrackerAutofill && window.__jobTrackerAutofill.submitIfComplete) {
      try { sub = await window.__jobTrackerAutofill.submitIfComplete(); }
      catch (e) { sub = { submitted: false, blockers: ["submit error: " + (e.message || e)] }; }
    }

    // Blocked ONLY by a CAPTCHA? Then the application is otherwise complete
    // and the user's whole job is ticking one box. Watch for that tick and
    // submit the moment it lands, so they never have to hunt for the Submit
    // button — one interaction per job instead of two plus a scroll.
    if (!sub.submitted && (sub.blockers || []).length) {
      armCaptchaAutoSubmit(job, sub.blockers);
      return {
        result: "needs_review",
        detail: "filled; waiting on you — " +
                (sub.blockers || []).slice(0, 4).join("; ").slice(0, 300),
        filled: stats.filled || 0,
        total:  stats.total  || 0,
      };
    }

    return {
      result: sub.submitted ? "submitted" : "needs_review",
      detail: (sub.blockers || []).slice(0, 6).join("; ").slice(0, 400),
      filled: stats.filled || 0,
      total:  stats.total  || 0,
    };
  }

  // ── CAPTCHA-tick → auto-submit ─────────────────────────────────────────────
  // Every ATS form tested carries a CAPTCHA, so this is the normal endgame:
  // everything else is filled, and a human tick is the only missing input.
  // We never solve or bypass the challenge — we just stop making the user
  // click Submit afterwards.
  function isCaptchaOnly(blockers) {
    const b = blockers || [];
    return b.length > 0 && b.every(x => /captcha/i.test(x));
  }

  function captchaToken() {
    for (const sel of ['#g-recaptcha-response',
                       'textarea[name="g-recaptcha-response"]',
                       'textarea[name="h-captcha-response"]',
                       'input[name="cf-turnstile-response"]',
                       'input[name="cf-chl-widget-response"]']) {
      for (const el of document.querySelectorAll(sel)) {
        if (el && String(el.value || "").trim()) return true;
      }
    }
    return false;
  }

  function readyBanner(text, tone) {
    let el = document.getElementById("jt-ready-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "jt-ready-banner";
      el.style.cssText = `
        position: fixed; top: 0; left: 0; right: 0; z-index: 2147483647;
        padding: 11px 16px; text-align: center;
        font: 600 14px/1.4 system-ui, -apple-system, sans-serif;
        box-shadow: 0 2px 10px rgba(0,0,0,.2);`;
      document.documentElement.appendChild(el);
    }
    el.style.background = tone === "done" ? "#198754" : "#0d6efd";
    el.style.color = "#fff";
    el.textContent = text;
    return el;
  }

  // Tells the user exactly what is left, then submits the instant they do it.
  // Not captcha-only: a form can also be missing something the engine can't
  // fill (BambooHR's State widget refuses synthetic events), and making the
  // user hunt for Submit after fixing it is the same wasted interaction.
  function armCaptchaAutoSubmit(job, blockers) {
    if (window.__jtCaptchaWatch) return;
    window.__jtCaptchaWatch = true;

    const others = (blockers || []).filter(b => !/captcha/i.test(b))
      .map(b => b.replace(/^required:\s*/i, "").trim());
    const needsCaptcha = (blockers || []).some(b => /captcha/i.test(b));
    const parts = [];
    if (others.length) parts.push(others.slice(0, 3).join(", "));
    if (needsCaptcha)  parts.push("tick the “I’m not a robot” box");
    readyBanner("✅ Application filled — just " +
                (parts.join(" and ") || "review it") + ". It submits itself.");

    // Bring whatever is still needed into view.
    setTimeout(() => {
      const cap = document.querySelector(
        'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], .g-recaptcha, .h-captcha');
      const target = others.length ? null : cap;
      if (target) try { target.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (_) {}
    }, 600);

    const started = Date.now();
    const timer = setInterval(async () => {
      if (shutdownIfOrphaned()) { clearInterval(timer); return; }
      if (Date.now() - started > 45 * 60_000) { clearInterval(timer); return; }  // give up after 45m
      // Re-validate rather than watching only the token: the user may also
      // have just filled the field the engine couldn't.
      let ready = false;
      try { ready = window.__jobTrackerAutofill.validateBeforeSubmit().ok; } catch (_) {}
      if (!ready) return;

      clearInterval(timer);
      readyBanner("Submitting your application…");
      let ok = false, why = "";
      try {
        const res = await window.__jobTrackerAutofill.submitIfComplete();
        ok = !!(res && res.submitted);
        why = (res && (res.blockers || []).join("; ")) || "";
      } catch (e) { why = e.message || String(e); }

      if (ok) {
        readyBanner("✅ Application submitted. You can close this tab.", "done");
        // The background worker already reported needs_review, so tell the
        // server this one actually went out or the tracker stays wrong.
        sendMessage({
          type: "autopilotLateSubmit",
          url: location.href,
          title: (job && job.title) || (document.querySelector("h1")?.innerText || "").slice(0, 120),
          company: (job && job.company) || "",
        });
      } else {
        readyBanner("Couldn't submit automatically — please click Submit yourself. " + why.slice(0, 90));
        try { window.__jobTrackerAutofill.findSubmitButton()?.scrollIntoView({ block: "center" }); } catch (_) {}
      }
    }, 1000);
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
    if (msg && msg.type === "autopilotGo") {
      if (hasApplicationForm()) {
        runAutopilot(msg.job)
          .then(sendResponse)
          .catch(e => sendResponse({ result: "error", detail: e.message || String(e) }));
        return true;  // async response
      }
      // No form here. In the TOP frame, try revealing one behind an "Apply"
      // button before giving up; sub-frames stay quiet so the frame that
      // actually holds the form is the one that answers.
      if (window !== window.top || !findApplyButton()) return false;
      revealApplicationForm()
        .then(ok => ok
          ? runAutopilot(msg.job)
          : { result: "no_form", detail: "apply button did not reveal a form", filled: 0, total: 0 })
        .then(sendResponse)
        .catch(e => sendResponse({ result: "error", detail: e.message || String(e) }));
      return true;
    }
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
    try { dismissConsentBanner(); } catch (_) {}
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
