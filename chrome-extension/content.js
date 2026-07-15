// Job Search App — Auto-fill (content script) v0.4
//
// Listens for fill messages from the popup, walks the page's form fields,
// and fills the ones it can match against the user's profile. Then runs a
// third "AI pass" against /api/autofill for any fields the rules couldn't
// match (custom free-text questions, etc.).
//
// v0.2 changes:
//  - Reordered rules so "Legal First & Last Name" wins on the full-name rule
//    before the last_name singleton sees it.
//  - Added an autocomplete handler for combobox/Google-Places-style location
//    fields (Ashby, Lever, Greenhouse): focuses, types, waits for dropdown,
//    clicks first option.
//  - Added a pass for custom button-based "radio" groups (Ashby yes/no chips).
//  - State / country selects now try both abbreviation and full name.
//  - cleanText strips required/optional markers so labels normalize cleanly.
//
// v0.4 changes:
//  - Pass 3: gather fields rules didn't match, POST to /api/autofill, apply.
//    Silently no-ops when the server returns 503 (no ANTHROPIC_API_KEY) — the
//    flag is cached in chrome.storage.local so we don't spam the endpoint.
//  - Toast shows "+N AI" when Pass 3 contributed.
//
// v0.5 changes:
//  - Field matching now includes semantic QA attributes (data-zcqa on Zoho
//    Recruit, data-automation-id on Workday, data-testid, etc.) from the
//    element or its closest wrapper — fixes ATSes whose inputs carry no
//    usable label/name/placeholder (Zoho's rec-form_<id> names).
//  - Auto-fill re-triggers on SPA content changes (MutationObserver +
//    history hooks): fixes Zoho's click-to-reveal form and Workday wizards.
//  - Skips captcha fields and text inputs that already hold a value.
//  - manifest: all_frames=true (embedded ATS iframes), zohorecruit.com added
//    to auto-fire hosts.
//
// Never clicks Submit. Highlights filled fields briefly with a green outline.

(() => {
  if (window.__jsAutofillLoaded) return;
  window.__jsAutofillLoaded = true;

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === 'fill' && msg.profile) {
      fillForm(msg.profile)
        .then(result => sendResponse(result))
        .catch(e => sendResponse({ error: e.message || String(e) }));
      return true; // async response
    }
  });

  // ── Field-matching rules ────────────────────────────────────────────────
  // Each rule = [regex, value (or array of fallbacks), type-hint]. First match wins.
  function ruleSet(p) {
    const cityState     = `${p.address.city}, ${p.address.state}`;
    const cityStateUsa  = `${p.address.city}, ${p.address.state}, USA`;
    return [
      // ── Preferred name FIRST (so "Preferred First Name" doesn't get swallowed
      //    by the full-name combined rule below) ──
      [/preferred[\s_-]*(first[\s_-]*)?name|nickname|known[\s_-]*as/i,      p.preferred_name,   'text'],

      // ── Full-name combos (MUST come before first_name/last_name singletons) ──
      // Catches "Legal First & Last Name", "Full Name", "Full Legal Name",
      // "Legal Name", "Applicant Name". Bare "name" is intentionally NOT a
      // catch-all — too greedy (would swallow "Preferred First Name", etc.).
      [/(first[\s_-]*(&|and|\/|,)[\s_-]*last[\s_-]*name)|(\b(full|legal|applicant)([\s_-]+(legal|full))?[\s_-]+name)/i,
                                                p.full_name,                                    'text'],

      // Singletons — only match isolated "First Name" / "Last Name"
      [/(^|[^a-z])(first[\s_-]*name|fname|given[\s_-]*name)([^a-z]|$)/i,    p.first_name,        'text'],
      [/(^|[^a-z])(last[\s_-]*name|lname|family[\s_-]*name|surname)([^a-z]|$)/i, p.last_name,    'text'],

      // Contact
      [/e[\s\-_]?mail/i,                          p.email,                  'email'],
      [/(phone|mobile|cell|telephone)/i,          p.phone,                  'tel'],

      // Location — autocomplete on Ashby / Greenhouse / Lever. Word-boundary
      // match (not ^...$) because the haystack also includes placeholder text
      // like "Start typing..." that breaks anchored regexes.
      //
      // Try the shortest form first — typing "Egg Harbor Township" gets the
      // best Google-Places match. The longer forms with state/USA suffixes
      // sometimes confuse the picker and surface no usable option.
      [/(^|[^a-z])location([^a-z]|$)|current[\s_-]*location|where[\s_-]*do[\s_-]*you[\s_-]*live|(^|[^a-z])city[\s_-]*and[\s_-]*state([^a-z]|$)/i,
                                                  [p.address.city, cityState, cityStateUsa],     'autocomplete'],

      // Address — word-boundary instead of ^...$ for the same haystack reason
      [/(street[\s_-]*address|address[\s_-]*line[\s_-]*1|(^|[^a-z])(address|street)([^a-z]|$))/i, p.address.street, 'text'],
      [/address[\s_-]*line[\s_-]*2|(^|[^a-z])(apt|suite|unit)([^a-z]|$)/i,      '',               'text'],
      [/(^|[^a-z])(city|town)([^a-z]|$)/i,                                      p.address.city,   'text'],
      [/(^|[^a-z])(state|province|region)([^a-z]|$)/i,
                                                  [p.address.state_full, p.address.state],         'select-or-text'],
      [/zip|postal[\s_-]*code|postcode/i,                                       p.address.zip,    'text'],
      [/(^|[^a-z])country([^a-z]|$)/i,
                                                  [p.address.country, p.address.country_code],     'select-or-text'],

      // Links
      [/linkedin/i,                               p.linkedin,               'url'],
      [/(portfolio|personal[\s_-]*(site|website|url)|github)/i, p.portfolio, 'url'],

      // EEO + demographics  (radios / selects)
      [/(authoriz|legally[\s\-_]*work|right[\s\-_]*to[\s\-_]*work|eligible[\s\-_]*to[\s\-_]*work).*?(work|u\.?s\.?|united states|usa)/i,
                                                  p.answers.work_authorized_us, 'yesno'],
      [/(sponsor|visa).*?(require|need)|require.*?(sponsor|visa)|need.*?sponsor/i,
                                                  p.answers.sponsorship_needed, 'yesno'],
      [/(^|[^a-z])veteran([^a-z]|$)/i,            p.answers.veteran_status,     'select'],
      [/disab(le|ility)/i,                        p.answers.disability,         'select'],
      [/(^|[^a-z])gender([^a-z]|$)/i,             p.answers.gender,             'select'],
      [/hispanic|latino/i,                        p.answers.hispanic_latino,    'yesno'],
      [/(^|[^a-z])(race|ethnicity)([^a-z]|$)/i,   p.answers.race,               'select'],

      // Salary / availability / signature
      [/salary|compensation|expected.*?(pay|rate)/i,                            p.answers.salary_expectation, 'text'],
      [/notice|available.*?(start|begin)|start.*?date/i,                         p.answers.notice_period,      'text'],
      [/(e[\s\-_]?signature|sign.*?(your|here)|electronic[\s_-]*signature)/i,   p.answers.esignature,         'text'],

      // Catch-alls that some ATSes ask
      [/previously.+(work|employ).+(this|here|company)/i,                       p.answers.previously_employed, 'yesno'],
      [/willing.+relocate|open.+relocation/i,                                    p.answers.willing_to_relocate, 'yesno'],
      [/security.+clearance|active.+clearance/i,                                 p.answers.active_security_clearance, 'text'],
    ];
  }

  async function fillForm(profile) {
    const rules = ruleSet(profile);
    const radioGroupsSeen = new Set();
    let found = 0, filled = 0;
    const matches = [], skipped = [];
    const undoers = [];   // for the toast's "Undo" button
    // Fields rules couldn't match — collected for Pass 3 (AI fill).
    // Each entry keeps a live DOM reference so we can apply the AI value
    // without re-querying the DOM after the network round-trip.
    const aiCandidates = [];
    let aiCounter = 0;

    // ── Pass 1: native inputs, textareas, selects ─────────────────────────
    const inputs = Array.from(document.querySelectorAll('input, textarea, select'));
    for (const el of inputs) {
      // Skip hidden / disabled / readonly / submit-type
      if (!el || el.disabled || el.readOnly) continue;
      const t = (el.type || '').toLowerCase();
      if (t === 'hidden' || t === 'submit' || t === 'button' || t === 'reset' || t === 'file') continue;
      if (!isVisible(el)) continue;

      // Radios: process one per group name
      if (t === 'radio') {
        const groupKey = el.name || el.getAttribute('aria-labelledby') || '';
        if (groupKey && radioGroupsSeen.has(groupKey)) continue;
        radioGroupsSeen.add(groupKey);
      }

      found++;
      const label = getLabelText(el);

      // Never touch captcha fields (Zoho Recruit renders one inline on the
      // application form) — filling them with garbage blocks the submit.
      if (/captcha/i.test([label, el.name, el.id, semanticAttrs(el)].join(' '))) {
        skipped.push({ label, name: el.name, reason: 'captcha — left for user' });
        continue;
      }

      // Don't overwrite text a previous pass (or the user) already entered.
      // Matters now that autofill can re-run on SPA page changes.
      const isTextLike = ['text', 'email', 'tel', 'url', 'search', 'textarea', ''].includes(t);
      if (isTextLike && el.value && el.value.trim()) {
        skipped.push({ label, name: el.name, reason: 'already has a value' });
        continue;
      }

      const matchInfo = findMatchForElement(el, label, rules);
      if (!matchInfo) {
        skipped.push({ label, name: el.name, reason: 'no rule match' });
        pushAiCandidate({ el, label, kind: 'native' });
        continue;
      }
      const { value, type } = matchInfo;
      const resolvedValues = normalizeValues(value);
      if (!resolvedValues.length) {
        skipped.push({ label, name: el.name, reason: 'rule matched but profile value empty' });
        pushAiCandidate({ el, label, kind: 'native' });
        continue;
      }

      // Snapshot so the toast's Undo can restore.
      const prevValue   = el.value;
      const prevChecked = (elType => elType === 'checkbox' || elType === 'radio'
        ? el.checked : null)((el.type || '').toLowerCase());

      const ok = await applyValue(el, resolvedValues, type, label);
      if (ok) {
        filled++;
        matches.push({ label, name: el.name, value: resolvedValues[0] });
        flash(el);
        undoers.push(() => {
          const tp = (el.type || '').toLowerCase();
          if (tp === 'checkbox' || tp === 'radio') {
            if (el.checked !== prevChecked) el.click();
          } else {
            setNativeValue(el, prevValue);
          }
        });
      } else {
        skipped.push({ label, name: el.name, reason: 'could not apply value' });
      }
    }

    // ── Pass 2: button-based "radio" groups (Ashby/Lever/custom React) ────
    // Ashby renders Yes/No questions as plain <button> elements inside a
    // <div> — no role="radiogroup", no <fieldset>. So we look globally for
    // visible buttons whose text is short and clearly an option (Yes/No/
    // Maybe/Other/etc.), group them by parent, and pair them with the
    // nearest ancestor's question-style label.
    const NAV_RE = /^(submit|apply|next|continue|save|upload|browse|cancel|back|done|sign|log|reset|clear|close)\b/i;
    const candidateButtons = Array.from(document.querySelectorAll('button, [role="radio"], [role="option"]'))
      .filter(isVisible)
      .filter(b => {
        const txt = cleanText(b.innerText || b.textContent || '');
        if (!txt || txt.length > 30) return false;
        if (NAV_RE.test(txt)) return false;
        return true;
      });
    const byParent = new Map();
    for (const b of candidateButtons) {
      const key = b.parentElement;
      if (!key) continue;
      if (!byParent.has(key)) byParent.set(key, []);
      byParent.get(key).push(b);
    }
    const seenGroups = new Set();
    for (const [parent, btns] of byParent) {
      if (btns.length < 2 || btns.length > 5) continue;
      if (parent.querySelector('input[type="radio"]')) continue; // handled in pass 1
      // De-dupe via parent
      if (seenGroups.has(parent)) continue;
      seenGroups.add(parent);

      found++;
      const label = findButtonGroupLabel(parent, btns);
      if (!label) {
        skipped.push({ label: '(button-group)', reason: 'no label found' });
        continue;
      }
      const matchInfo = findMatchForHaystack(label, rules);
      if (!matchInfo) {
        skipped.push({ label, reason: 'no rule match (button-group)' });
        pushAiCandidate({ buttons: btns, label, kind: 'button-group' });
        continue;
      }
      const resolvedValues = normalizeValues(matchInfo.value);
      if (!resolvedValues.length) {
        skipped.push({ label, reason: 'rule matched but profile value empty (button-group)' });
        pushAiCandidate({ buttons: btns, label, kind: 'button-group' });
        continue;
      }

      const ok = clickMatchingButton(btns, resolvedValues[0]);
      if (ok) {
        filled++;
        matches.push({ label, value: resolvedValues[0] });
      } else {
        skipped.push({ label, reason: 'no matching button (button-group)' });
      }
    }

    // ── Pass 3: AI fill for anything rules couldn't match ─────────────────
    let ai = { tried: false, filled: 0, applied: [], skipped: [] };
    if (aiCandidates.length) {
      try {
        ai = await runAiPass(aiCandidates);
        filled += ai.filled;
        for (const a of ai.applied) {
          matches.push({ label: a.label, value: a.value, source: 'ai' });
        }
      } catch (e) {
        console.warn('[autofill] AI pass error:', e);
      }
    }

    return { found, filled, matches, skipped, undoers, ai };

    // ─── inner helpers (closures over fillForm-locals) ────────────────────
    function pushAiCandidate({ el, buttons, label, kind }) {
      // Only worth sending to the server if the field has a useful label.
      const cleanLabel = cleanText(label || '');
      if (!cleanLabel || cleanLabel.length < 3) return;
      const id = 'jsa-' + (++aiCounter);
      if (kind === 'native') {
        aiCandidates.push({
          id, kind, el, label: cleanLabel,
          type:        inferAiType(el),
          options:     extractAiOptions(el),
          name:        el.name || '',
          placeholder: el.placeholder || '',
          maxlength:   (el.maxLength > 0 ? el.maxLength : null),
        });
      } else if (kind === 'button-group') {
        aiCandidates.push({
          id, kind, buttons, label: cleanLabel,
          type: 'button-group',
          options: buttons.map(b => cleanText(b.innerText || b.textContent || '')).filter(Boolean),
          name: '', placeholder: '', maxlength: null,
        });
      }
    }
  }

  // ── Pass 3: AI fill ────────────────────────────────────────────────────
  async function runAiPass(candidates) {
    const out = { tried: false, filled: 0, applied: [], skipped: [],
                  reason: '', error: '' };
    if (!candidates || !candidates.length) return out;

    const { server, aiEnabled } = await loadAiSettings();
    if (aiEnabled === false) {
      out.reason = 'AI disabled (previous 503)';
      return out;
    }
    if (!server) {
      out.reason = 'no server cached';
      return out;
    }

    // Strip DOM references for the wire format; keep them on a parallel map.
    const byId = new Map();
    const wire = candidates.map(c => {
      byId.set(c.id, c);
      return {
        id: c.id,
        label: c.label,
        type: c.type,
        options: c.options || [],
        name: c.name || '',
        placeholder: c.placeholder || '',
        maxlength: c.maxlength,
      };
    });
    const payload = {
      fields: wire.slice(0, 25),  // server caps too; matches it
      page_context: gatherPageContext(),
    };

    out.tried = true;
    // Relay through the service worker — content-script fetches face CORS +
    // SameSite=Lax cookie issues; the worker runs with the extension's
    // privileges and matches host_permissions so cookies + CORS just work.
    const reply = await new Promise(resolve => {
      try {
        chrome.runtime.sendMessage(
          { type: 'autofill', server, payload },
          r => resolve(r || { status: 0, error: 'no reply' })
        );
      } catch (e) { resolve({ status: 0, error: 'sendMessage: ' + e.message }); }
    });
    const status = reply.status;
    const body   = reply.body || {};
    if (reply.error) {
      out.error = reply.error;
      return out;
    }
    if (status === 503) {
      chrome.storage.local.set({ aiEnabled: false });
      out.reason = 'AI not configured';
      return out;
    }
    if (status === 401) {
      out.reason = 'not logged in — open popup to refresh session';
      return out;
    }
    if (status < 200 || status >= 300) {
      out.error = body && body.error ? body.error : ('HTTP ' + status);
      return out;
    }

    // 200: clear any stale aiEnabled=false flag.
    chrome.storage.local.set({ aiEnabled: true });

    const fills = Array.isArray(body.fills) ? body.fills : [];
    for (const f of fills) {
      const c = byId.get(f.id);
      if (!c) { out.skipped.push({ id: f.id, reason: 'unknown id' }); continue; }
      if (f.skip || !f.value) {
        out.skipped.push({ id: f.id, label: c.label, reason: f.reason || 'AI skipped' });
        continue;
      }
      const ok = await applyAiFill(c, f.value);
      if (ok) {
        out.filled++;
        out.applied.push({ id: f.id, label: c.label, value: f.value });
        flashTarget(c);
      } else {
        out.skipped.push({ id: f.id, label: c.label, reason: 'could not apply AI value' });
      }
    }
    console.log(`[autofill] AI pass: ${out.filled} fills applied, ${out.skipped.length} skipped`);
    return out;
  }

  function loadAiSettings() {
    return new Promise(resolve => {
      try {
        chrome.storage.local.get(['server', 'aiEnabled'], data => resolve(data || {}));
      } catch (_) { resolve({}); }
    });
  }

  function inferAiType(el) {
    const tag = el.tagName.toLowerCase();
    const t   = (el.type || '').toLowerCase();
    if (tag === 'textarea') return 'textarea';
    if (tag === 'select')   return 'select';
    if (t === 'radio')      return 'radio';
    if (t === 'checkbox')   return 'checkbox';
    return 'text';
  }

  function extractAiOptions(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'select') {
      return Array.from(el.options || [])
        .map(o => (o.text || o.value || '').trim())
        .filter(Boolean);
    }
    const t = (el.type || '').toLowerCase();
    if (t === 'radio' && el.name) {
      const radios = document.querySelectorAll(
        `input[type="radio"][name="${CSS.escape(el.name)}"]`
      );
      return Array.from(radios).map(r => {
        const lbl = (getLabelText(r) || r.value || '').trim();
        return lbl;
      }).filter(Boolean);
    }
    return [];
  }

  async function applyAiFill(candidate, value) {
    if (candidate.kind === 'button-group') {
      return clickMatchingButton(candidate.buttons, value);
    }
    // native
    const el  = candidate.el;
    if (!el || !el.isConnected) return false;
    const tag = el.tagName.toLowerCase();
    const t   = (el.type || '').toLowerCase();
    if (tag === 'select') return selectOption(el, value);
    if (t === 'radio')    return clickMatchingRadio(el, value);
    if (t === 'checkbox') {
      const yesy = /^(yes|true|1|on)$/i.test(String(value));
      if (el.checked !== yesy) el.click();
      return true;
    }
    // text / textarea / others
    setNativeValue(el, String(value));
    return true;
  }

  function flashTarget(candidate) {
    if (candidate.kind === 'button-group') {
      // already flashed by clickMatchingButton
      return;
    }
    if (candidate.el) flash(candidate.el);
  }

  function gatherPageContext() {
    const h1 = document.querySelector('h1');
    return {
      url:          location.href,
      hostname:     location.hostname,
      title:        (document.title || '').slice(0, 200),
      h1:           ((h1 && h1.innerText) || '').slice(0, 200),
      company_hint: detectCompanyHint(),
    };
  }

  function detectCompanyHint() {
    // Meta tags first
    const og = document.querySelector('meta[property="og:site_name"]');
    if (og && og.content) return og.content.slice(0, 80);
    // Ashby: hostname has the slug "jobs.ashbyhq.com/<company>"
    const ashby = location.pathname.match(/^\/([^\/]+)\//);
    if (location.hostname.includes('ashbyhq.com') && ashby) return ashby[1];
    // Greenhouse: boards.greenhouse.io/<company>/jobs/<id>
    if (location.hostname.includes('greenhouse.io') && ashby) return ashby[1];
    // Lever: jobs.lever.co/<company>/<id>
    if (location.hostname.includes('lever.co') && ashby) return ashby[1];
    return '';
  }

  // Walk up from the buttons' parent container; at each level, look for a
  // question-style label sibling (label / heading / div with "label" class).
  // Prefers labels with a "?" to disambiguate from neighboring field labels.
  function findButtonGroupLabel(parent, btns) {
    let best = '';
    let cur = parent;
    for (let depth = 0; depth < 6 && cur; depth++, cur = cur.parentElement) {
      for (const child of cur.children) {
        if (child === parent || child.contains(parent)) continue;
        // Avoid scanning into other button groups
        if (btns.some(b => child.contains(b))) continue;
        const tag = child.tagName.toLowerCase();
        const isLabelLike = tag === 'label' || tag === 'legend'
                         || /^h[1-6]$/.test(tag)
                         || /label/i.test(child.className || '');
        if (!isLabelLike) continue;
        const t = cleanText(child.innerText);
        if (!t || t.length > 300) continue;
        // Question-shaped text wins; otherwise keep the first short label
        if (/\?/.test(t)) return t;
        if (!best) best = t;
      }
      if (best) return best;
    }
    return best;
  }

  // ── Field-matching ──────────────────────────────────────────────────────
  function findMatchForElement(el, label, rules) {
    const haystack = [
      label,
      el.name || '',
      el.id || '',
      el.placeholder || '',
      el.getAttribute('aria-label') || '',
      semanticAttrs(el),
    ].filter(Boolean).join(' | ');
    return findMatchForHaystack(haystack, rules);
  }

  // QA/test hooks many ATSes stamp on fields or their wrappers — often the
  // ONLY place the field's semantic name appears in the DOM. Zoho Recruit:
  // data-zcqa="manual_First_Name" on the <lyte-input> wrapper (the input
  // itself has a meaningless rec-form_<id> name). Workday: data-automation-id.
  const SEMANTIC_ATTRS = [
    'data-zcqa', 'data-automation-id', 'data-testid',
    'data-test', 'data-qa', 'data-field',
  ];
  function semanticAttrs(el) {
    const parts = [];
    for (const attr of SEMANTIC_ATTRS) {
      const own = el.getAttribute(attr);
      if (own) parts.push(own);
      const wrap = el.closest('[' + attr + ']');
      if (wrap && wrap !== el) {
        const v = wrap.getAttribute(attr);
        if (v) parts.push(v);
      }
    }
    return parts.join(' ');
  }

  function findMatchForHaystack(haystack, rules) {
    if (!haystack || !haystack.trim()) return null;
    for (const [re, value, type] of rules) {
      if (re.test(haystack)) {
        return { value, type };
      }
    }
    return null;
  }

  function normalizeValues(value) {
    const arr = Array.isArray(value) ? value : [value];
    return arr.filter(v => v !== undefined && v !== null && v !== '');
  }

  // ── Label extraction ────────────────────────────────────────────────────
  function getLabelText(el) {
    // 1. native HTMLInputElement.labels
    if (el.labels && el.labels.length) {
      return cleanText(el.labels[0].innerText);
    }
    // 2. aria-labelledby
    const lbId = el.getAttribute('aria-labelledby');
    if (lbId) {
      const target = document.getElementById(lbId);
      if (target) return cleanText(target.innerText);
    }
    // 3. aria-label
    const aria = el.getAttribute('aria-label');
    if (aria) return cleanText(aria);
    // 4. parent <label>
    let parent = el.parentElement;
    while (parent && parent !== document.body) {
      if (parent.tagName === 'LABEL') return cleanText(parent.innerText);
      parent = parent.parentElement;
    }
    // 5. nearest preceding label-ish element
    const prev = el.previousElementSibling;
    if (prev && (prev.tagName === 'LABEL' || /label|legend|h\d/i.test(prev.tagName))) {
      return cleanText(prev.innerText);
    }
    // 6. fieldset/legend (for radio groups)
    let fs = el.closest('fieldset');
    if (fs) {
      const legend = fs.querySelector('legend');
      if (legend) return cleanText(legend.innerText);
    }
    // 7. Walk up the parent chain; at each level, look for a <label> or heading
    //    sibling-of-an-ancestor. Catches Ashby's pattern where the <label> is
    //    a sibling of the input's container, not an ancestor of the input.
    let container = el.parentElement;
    for (let depth = 0; container && container !== document.body && depth < 4; depth++) {
      for (const child of container.children) {
        if (child.contains(el)) continue;
        if (child.tagName === 'LABEL' || /^h[1-6]$/i.test(child.tagName)) {
          const t = cleanText(child.innerText);
          if (t && t.length < 200) return t;
        }
      }
      container = container.parentElement;
    }
    return '';
  }

  function getGroupLabel(group) {
    // 1. aria-labelledby
    const lbId = group.getAttribute('aria-labelledby');
    if (lbId) {
      const target = document.getElementById(lbId);
      if (target) return cleanText(target.innerText);
    }
    // 2. aria-label
    const aria = group.getAttribute('aria-label');
    if (aria) return cleanText(aria);
    // 3. <legend>
    const legend = group.querySelector('legend');
    if (legend) return cleanText(legend.innerText);
    // 4. Preceding sibling that looks like a label/heading/paragraph
    let prev = group.previousElementSibling;
    while (prev) {
      if (/^(label|legend|p|h[1-6]|div|span)$/i.test(prev.tagName)) {
        const txt = cleanText(prev.innerText);
        if (txt && txt.length < 200) return txt;
      }
      prev = prev.previousElementSibling;
    }
    // 5. Look inside parent for a label-like element that isn't a descendant
    const parent = group.parentElement;
    if (parent) {
      for (const child of parent.children) {
        if (child === group || child.contains(group)) continue;
        if (/^(label|legend|p)$/i.test(child.tagName)
            || /label/i.test(child.className || '')) {
          const txt = cleanText(child.innerText);
          if (txt && txt.length < 200) return txt;
        }
      }
    }
    return '';
  }

  function cleanText(s) {
    return (s || '')
      .replace(/\s+/g, ' ')
      .replace(/\s*\*\s*$/, '')                  // trailing required-asterisk
      .replace(/\s*\(required\)\s*$/i, '')
      .replace(/\s*\(optional\)\s*$/i, '')
      .trim()
      .slice(0, 200);
  }

  // ── Apply value to element ──────────────────────────────────────────────
  async function applyValue(el, values, type, label) {
    const elType = (el.type || '').toLowerCase();
    const tag    = el.tagName.toLowerCase();
    const isCombobox = el.getAttribute('role') === 'combobox'
                    || el.hasAttribute('aria-autocomplete')
                    || (el.getAttribute('aria-haspopup') === 'listbox');

    // Autocomplete (React/Google Places/Ashby-style)
    if (type === 'autocomplete' || isCombobox) {
      for (const v of values) {
        const ok = await fillAutocomplete(el, v);
        if (ok) return true;
      }
      // Fall back to plain text-set if autocomplete didn't surface options
      setNativeValue(el, String(values[0]));
      return true;
    }

    // <select>
    if (tag === 'select') {
      for (const v of values) {
        if (selectOption(el, v)) return true;
      }
      return false;
    }

    // Radio group → click the radio whose label matches `value`
    if (elType === 'radio') {
      return clickMatchingRadio(el, values[0]);
    }

    // Checkbox: if value is Yes/true, check it; if No/false, uncheck
    if (elType === 'checkbox') {
      const yesy = /^(yes|true|1)$/i.test(String(values[0]));
      if (el.checked !== yesy) el.click();
      return true;
    }

    // Text-like inputs
    setNativeValue(el, String(values[0]));
    return true;
  }

  function setNativeValue(el, value) {
    const tag = el.tagName.toLowerCase();
    const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur',   { bubbles: true }));
  }

  function selectOption(el, value) {
    const want = String(value).toLowerCase().trim();
    if (!want) return false;
    // Direct match
    for (const opt of el.options) {
      const optText  = (opt.text || '').toLowerCase().trim();
      const optValue = (opt.value || '').toLowerCase().trim();
      if (optText === want || optValue === want) {
        el.value = opt.value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
    // Substring match
    for (const opt of el.options) {
      const optText = (opt.text || '').toLowerCase().trim();
      if (!optText) continue;
      if (optText.includes(want) || want.includes(optText)) {
        el.value = opt.value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
    return false;
  }

  function clickMatchingRadio(anyRadioInGroup, value) {
    const name = anyRadioInGroup.name;
    if (!name) return false;
    const radios = document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`);
    const want = normalizeAnswer(value);

    for (const r of radios) {
      const rLabel = (getLabelText(r) || r.value || '').toLowerCase().trim();
      const rValue = (r.value || '').toLowerCase().trim();
      if (rLabel === want || rValue === want) {
        if (!r.checked) r.click();
        return true;
      }
    }
    // Substring fallback
    for (const r of radios) {
      const rLabel = (getLabelText(r) || r.value || '').toLowerCase().trim();
      if (rLabel.includes(want) || want.includes(rLabel)) {
        if (!r.checked) r.click();
        return true;
      }
    }
    return false;
  }

  function clickMatchingButton(buttons, value) {
    const want = normalizeAnswer(value);
    // Exact text match
    for (const btn of buttons) {
      const txt = cleanText(btn.innerText || btn.textContent || '').toLowerCase();
      if (txt === want) {
        btn.click();
        flash(btn);
        return true;
      }
    }
    // Substring match
    for (const btn of buttons) {
      const txt = cleanText(btn.innerText || btn.textContent || '').toLowerCase();
      if (!txt) continue;
      if (txt.includes(want) || want.includes(txt)) {
        btn.click();
        flash(btn);
        return true;
      }
    }
    return false;
  }

  function normalizeAnswer(v) {
    const s = String(v).toLowerCase().trim();
    if (/^(yes|y|true)$/.test(s)) return 'yes';
    if (/^(no|n|false)$/.test(s)) return 'no';
    return s;
  }

  // ── Autocomplete (combobox) handler ─────────────────────────────────────
  // Focuses, types value, waits up to 1.5s for a dropdown option to appear,
  // then clicks it. Used for Ashby location, Greenhouse location autocomplete,
  // Google-Places-backed pickers (.pac-item), etc.
  async function fillAutocomplete(el, value) {
    el.focus();
    el.click();
    // Set the value without firing 'blur' — the standard setNativeValue ends
    // with a blur which closes the dropdown before it has a chance to render.
    const proto = HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));

    // Nudge typeahead APIs (Google Places, etc.) that listen for keydown too.
    const lastChar = value.slice(-1) || 'a';
    el.dispatchEvent(new KeyboardEvent('keydown', { key: lastChar, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup',   { key: lastChar, bubbles: true }));

    const option = await waitForOption();
    if (option) {
      // Full pointer sequence — React pickers (Ashby uses one backed by Google
      // Places) listen on mousedown/mouseup, not synthetic 'click'.
      simulateClick(option);
      // React needs a moment to process the synthetic click and commit the
      // selected option's text into the input's value.
      await new Promise(r => setTimeout(r, 220));
      return true;
    }
    return false;
  }

  function simulateClick(el) {
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      el.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, button: 0, view: window,
      }));
    }
  }

  async function waitForOption() {
    // 3s gives Google-Places-backed autocompletes time to round-trip the API
    // call; 1.5s was too tight on slow networks.
    const maxWaitMs = 3000;
    const start = Date.now();
    const selector = [
      '[role="option"]:not([aria-disabled="true"]):not([aria-selected="true"])',
      '[role="listbox"] li:not([aria-disabled="true"])',
      'li[data-option-index]',
      '.ashby-job-posting-form-field-entry__option',
      '.pac-item',
      '[class*="autocomplete-option"]:not([class*="disabled"])',
      '[class*="MenuItem"]:not([aria-disabled="true"])',
    ].join(',');
    while (Date.now() - start < maxWaitMs) {
      const candidates = Array.from(document.querySelectorAll(selector))
        .filter(isVisible);
      if (candidates.length) return candidates[0];
      await new Promise(r => setTimeout(r, 100));
    }
    return null;
  }

  // ── Visual feedback ─────────────────────────────────────────────────────
  function flash(el) {
    const prev = el.style.outline;
    el.style.outline = '2px solid #198754';
    el.style.outlineOffset = '1px';
    setTimeout(() => { el.style.outline = prev; }, 1200);
  }

  function isVisible(el) {
    if (!el) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  // ── Auto-fill on supported ATS hosts ───────────────────────────────────
  // Triggers automatically once per page load, no popup click needed.
  // Reads the profile from chrome.storage.local (populated by popup.js the
  // last time the user opened the extension and the API responded). Shows a
  // toast in the bottom-right with an Undo button.

  const SUPPORTED_ATS_HOSTS = [
    'ashbyhq.com', 'greenhouse.io', 'workable.com', 'lever.co',
    'smartrecruiters.com', 'bamboohr.com', 'applytojob.com',
    'rippling.com', 'rippling-ats.com', 'breezy.hr', 'jazzhr.com',
    'icims.com', 'myworkdayjobs.com', 'workday.com',
    'workforcenow.adp.com', 'adp.com',
    'dayforcehcm.com', 'dayforce.com',
    'successfactors.com', 'taleo.net',
    'oraclecloud.com', 'oracle.com',
    'ukgpro.com', 'pro.ukg.net',
    'polymerhr.com', 'recruitee.com',
    'zohorecruit.com', 'zohorecruit.eu',
  ];

  function isSupportedAtsHost(hostname) {
    return SUPPORTED_ATS_HOSTS.some(s => hostname.includes(s));
  }

  function loadCachedProfile() {
    return new Promise(resolve => {
      try {
        chrome.storage.local.get(['profile', 'fetchedAt'], data => {
          resolve(data || {});
        });
      } catch (_) {
        resolve({});
      }
    });
  }

  function countVisibleInputs() {
    return Array.from(document.querySelectorAll(
      'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"]), textarea'
    )).filter(isVisible).length;
  }

  let fillRunning = false;
  let autoFillRuns = 0;
  let lastFilledInputCount = -1;

  async function autoFillIfApplicable() {
    if (!isSupportedAtsHost(location.hostname)) return;
    if (fillRunning || autoFillRuns >= 12) return;
    fillRunning = true;
    try {
      // Wait briefly for the React-rendered form to appear (up to ~3s).
      for (let i = 0; i < 15; i++) {
        if (countVisibleInputs() >= 3) break;
        await new Promise(r => setTimeout(r, 200));
      }
      const cached = await loadCachedProfile();
      if (!cached.profile) return; // user hasn't opened the popup yet — skip silently
      let result;
      try {
        result = await fillForm(cached.profile);
      } catch (e) {
        console.warn('Auto-fill error:', e);
        return;
      }
      autoFillRuns++;
      lastFilledInputCount = countVisibleInputs();
      if (result && result.filled > 0) showToast(result);
    } finally {
      fillRunning = false;
    }
  }

  // Re-run when the page swaps content without a reload — SPA wizards
  // (Workday steps) and forms that only render after a click (Zoho Recruit's
  // "I'm interested"). Triggers only when the visible-input count changes,
  // so our own fills / toast DOM edits don't loop it.
  function scheduleRefill() {
    clearTimeout(scheduleRefill._t);
    scheduleRefill._t = setTimeout(() => {
      if (fillRunning) return;
      const n = countVisibleInputs();
      if (n >= 3 && n !== lastFilledInputCount) autoFillIfApplicable();
    }, 700);
  }

  if (isSupportedAtsHost(location.hostname)) {
    new MutationObserver(scheduleRefill)
      .observe(document.documentElement, { childList: true, subtree: true });
    window.addEventListener('popstate', scheduleRefill);
    for (const m of ['pushState', 'replaceState']) {
      const orig = history[m];
      history[m] = function (...args) {
        const r = orig.apply(this, args);
        scheduleRefill();
        return r;
      };
    }
  }

  function showToast(result) {
    // Tear down any previous toast (e.g., from a hot reload)
    const existing = document.getElementById('__jsAutofillToast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.id = '__jsAutofillToast';
    toast.style.cssText = [
      'position: fixed', 'bottom: 16px', 'right: 16px', 'z-index: 2147483647',
      'background: #198754', 'color: white',
      'padding: 12px 14px', 'border-radius: 8px',
      'font: 13px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif',
      'box-shadow: 0 6px 20px rgba(0,0,0,0.25)',
      'max-width: 320px', 'line-height: 1.4',
    ].join(';');

    const msg = document.createElement('div');
    msg.style.marginBottom = '8px';
    const strong = document.createElement('strong');
    const aiFilled = (result.ai && result.ai.filled) ? result.ai.filled : 0;
    const ruleFilled = result.filled - aiFilled;
    let label = 'Auto-filled ' + ruleFilled + ' field' + (ruleFilled === 1 ? '' : 's');
    if (aiFilled) label += ' + ' + aiFilled + ' AI';
    strong.textContent = label;
    msg.appendChild(strong);
    msg.appendChild(document.createTextNode(' — review before submitting'));
    toast.appendChild(msg);

    const undoBtn = document.createElement('button');
    undoBtn.textContent = '↩ Undo';
    undoBtn.style.cssText = [
      'background: white', 'color: #198754', 'border: none',
      'padding: 5px 10px', 'border-radius: 4px',
      'font-weight: 600', 'cursor: pointer', 'margin-right: 6px',
      'font-size: 12px',
    ].join(';');
    toast.appendChild(undoBtn);

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '×';
    closeBtn.style.cssText = [
      'background: transparent', 'color: white',
      'border: 1px solid rgba(255,255,255,0.5)',
      'padding: 5px 10px', 'border-radius: 4px',
      'cursor: pointer', 'font-size: 12px',
    ].join(';');
    toast.appendChild(closeBtn);

    // Attach to <html>, not <body>: React-controlled ATS pages (Ashby, Lever)
    // re-render their <body> subtree after our button clicks and detach any
    // unknown child nodes, which would otherwise eat the toast.
    document.documentElement.appendChild(toast);

    const timer = setTimeout(() => toast.remove(), 12000);
    undoBtn.addEventListener('click', () => {
      for (const undo of (result.undoers || [])) {
        try { undo(); } catch (_) {}
      }
      toast.remove();
      clearTimeout(timer);
    });
    closeBtn.addEventListener('click', () => {
      toast.remove();
      clearTimeout(timer);
    });
  }

  // Run once at content-script load (manifest's run_at: document_idle means
  // the DOM is parsed but React may still be mounting; we poll for inputs).
  autoFillIfApplicable();
})();
