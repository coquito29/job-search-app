// Autofill engine. Reads a profile (the shape returned by /api/profile/full)
// and walks the page's inputs/selects/textareas, filling each field whose
// label/name/placeholder matches a known signal.
//
// Designed to run in two contexts:
//   1. As a content script on known ATS pages (loaded by manifest).
//   2. Injected on-demand into any tab via chrome.scripting.executeScript.
// Both contexts call `window.__jobTrackerAutofill.run(profile, opts)`.

(function () {
  if (window.__jobTrackerAutofill) return; // idempotent — content script + injection can double-load

  // ── Field signals ──────────────────────────────────────────────────────────
  // Each entry: regex(es) that, when matched against a field's label/name/id/
  // placeholder/aria-label, indicates we should fill it with the given value.
  // Order matters — more specific patterns come first.
  const RULES = (p) => {
    const addr = p.address || {};
    const ans  = p.answers || {};
    return [
      // Most-specific first so 'first name' doesn't fall into 'name'.
      { value: p.first_name,       patterns: [/\b(first|given|forename)[\s_-]*name\b/i, /\bfname\b/i] },
      { value: p.last_name,        patterns: [/\b(last|family|sur)[\s_-]*name\b/i, /\blname\b/i, /\bsurname\b/i] },
      { value: p.preferred_name,   patterns: [/\b(preferred|nick)[\s_-]*name\b/i, /\bgoes[\s_-]*by\b/i] },
      { value: p.full_name,        patterns: [/\bfull[\s_-]*name\b/i, /^name$/i, /\byour[\s_-]*name\b/i, /\blegal[\s_-]*name\b/i] },

      { value: p.email,            patterns: [/\bemail\b/i, /\be-?mail[\s_-]*address\b/i] },
      { value: p.phone,            patterns: [/\bphone\b/i, /\bmobile\b/i, /\btelephone\b/i, /\bcontact[\s_-]*number\b/i] },

      { value: addr.street,        patterns: [/\b(street|address)[\s_-]*(line)?[\s_-]*1?\b/i, /\bstreet[\s_-]*address\b/i, /^address$/i] },
      { value: addr.city,          patterns: [/\bcity\b/i, /\btown\b/i, /\blocality\b/i] },
      { value: addr.state,         patterns: [/\bstate\b/i, /\bprovince\b/i, /\bregion\b/i] },
      { value: addr.zip,           patterns: [/\bzip\b/i, /\bpostal[\s_-]*code\b/i, /\bpostcode\b/i] },
      { value: addr.country,       patterns: [/\bcountry\b/i] },

      { value: p.linkedin,         patterns: [/\blinkedin\b/i] },
      { value: p.portfolio,        patterns: [/\b(portfolio|website|personal[\s_-]*site|homepage|github)\b/i, /\burl\b/i] },

      // Demographic / EEO answers — these are usually selects/radios; the
      // dropdown matcher below tries to find an <option> whose text contains
      // this value.
      { value: ans.work_authorized_us,       patterns: [/\bauthor(i[sz]ed|i[sz]ation)\b.*\b(work|us|u\.s\.)\b/i, /\beligible\b.*\bwork\b.*\bus\b/i, /\bus[\s_-]*work[\s_-]*author/i] },
      { value: ans.sponsorship_needed,       patterns: [/\bsponsor(ship)?\b/i, /\bvisa\b.*\bsponsor/i, /\brequire[\s_-]*sponsor/i] },
      { value: ans.veteran_status,           patterns: [/\bveteran\b/i] },
      { value: ans.disability,               patterns: [/\bdisab(il)ity\b/i, /\bdisabled\b/i] },
      { value: ans.gender,                   patterns: [/\bgender\b/i, /\bsex\b/i] },
      { value: ans.hispanic_latino,          patterns: [/\bhispanic\b/i, /\blatino\b/i, /\blatinx\b/i] },
      { value: ans.race,                     patterns: [/\brace\b/i, /\bethnicity\b/i, /\bethnic\b/i] },
      { value: ans.salary_expectation,       patterns: [/\b(salary|compensation|pay)[\s_-]*(expectation|requirement|range|desired)\b/i, /\bdesired[\s_-]*salary\b/i, /\bexpected[\s_-]*salary\b/i] },
      { value: ans.notice_period,            patterns: [/\b(notice|start)[\s_-]*period\b/i, /\bwhen[\s_-]*can[\s_-]*you[\s_-]*start\b/i, /\bavailability\b/i] },
      { value: ans.esignature,               patterns: [/\b(e[\s_-]*signature|signature|sign[\s_-]*here)\b/i, /\btype[\s_-]*your[\s_-]*name\b/i] },
      { value: ans.willing_to_relocate,      patterns: [/\brelocat/i] },
      { value: ans.previously_employed,      patterns: [/\bpreviously[\s_-]*employed\b/i, /\bever[\s_-]*worked[\s_-]*(here|with[\s_-]*us|for[\s_-]*(us|the[\s_-]*company))\b/i] },
      { value: ans.active_security_clearance,patterns: [/\b(security)?[\s_-]*clearance\b/i] },
      { value: ans.us_gov_employment,        patterns: [/\bgov(ernment)?[\s_-]*employ(ee|ment)\b/i, /\bfederal[\s_-]*employ/i] },
    ].filter(r => r.value !== undefined && r.value !== null && r.value !== "");
  };

  // ── Field probing ──────────────────────────────────────────────────────────
  // The signal we match against: label text + name + id + placeholder + aria.
  // We concatenate so a single regex match can fire against any of them.
  function probeText(el) {
    const parts = [];
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && lab.textContent) parts.push(lab.textContent);
    }
    // <label> wrapping the input
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      if (p.tagName === "LABEL" && p.textContent) { parts.push(p.textContent); break; }
    }
    // ARIA labelling
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      labelledby.split(/\s+/).forEach(id => {
        const node = document.getElementById(id);
        if (node && node.textContent) parts.push(node.textContent);
      });
    }
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) parts.push(ariaLabel);
    if (el.placeholder)  parts.push(el.placeholder);
    if (el.name)         parts.push(el.name);
    if (el.id)           parts.push(el.id);
    if (el.dataset && el.dataset.qa) parts.push(el.dataset.qa);
    return parts.join(" | ");
  }

  function isFillable(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.type === "hidden" || el.type === "file" || el.type === "submit"
        || el.type === "button" || el.type === "reset" || el.type === "image") return false;
    if (!el.offsetParent && el.type !== "radio" && el.type !== "checkbox") return false; // visibility check
    return true;
  }

  // Fire the events React/Vue/Angular listen for so framework state updates.
  function setNativeValue(el, value) {
    const proto = Object.getPrototypeOf(el);
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input",  { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // For <select> we look for an option whose text contains the value.
  function fillSelect(el, value) {
    const want = String(value).trim().toLowerCase();
    const opts = Array.from(el.options || []);
    // Exact match on label first, then on value, then substring on label.
    let opt = opts.find(o => o.textContent.trim().toLowerCase() === want)
           || opts.find(o => String(o.value).trim().toLowerCase() === want)
           || opts.find(o => o.textContent.toLowerCase().includes(want));
    // Yes/No heuristic — many ATSes use "Yes, I am authorized..." etc.
    if (!opt && (want === "yes" || want === "no")) {
      opt = opts.find(o => new RegExp(`\\b${want}\\b`, "i").test(o.textContent));
    }
    if (!opt) return false;
    el.value = opt.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("input",  { bubbles: true }));
    return true;
  }

  // For radio groups + standalone checkboxes (Yes/No, agreement).
  function fillRadioOrCheckbox(el, value) {
    if (el.type !== "radio" && el.type !== "checkbox") return false;
    const want = String(value).trim().toLowerCase();
    // Find the group (same name) and pick the one whose label matches.
    const group = el.form
      ? Array.from(el.form.querySelectorAll(`input[type="${el.type}"][name="${CSS.escape(el.name || "")}"]`))
      : [el];
    for (const r of group) {
      const text = probeText(r).toLowerCase();
      const valAttr = String(r.value || "").toLowerCase();
      if (text.includes(want) || valAttr === want
          || (want === "yes" && (valAttr === "true"  || valAttr === "1"))
          || (want === "no"  && (valAttr === "false" || valAttr === "0"))) {
        if (!r.checked) {
          r.click();
        }
        return true;
      }
    }
    return false;
  }

  function fillTextLike(el, value) {
    if (el.value && el.value.trim()) return false; // don't overwrite user input
    setNativeValue(el, String(value));
    return true;
  }

  function matchRule(rules, text) {
    for (const rule of rules) {
      for (const re of rule.patterns) {
        if (re.test(text)) return rule;
      }
    }
    return null;
  }

  // ── Q&A fuzzy fallback ─────────────────────────────────────────────────────
  // After the structured rules run, walk remaining fields and try the user's
  // raw qa_defaults list (label substring → answer).
  function tryQADefaults(el, qaDefaults) {
    if (!qaDefaults || !qaDefaults.length) return false;
    const text = probeText(el).toLowerCase();
    for (const entry of qaDefaults) {
      if (!Array.isArray(entry) || entry.length < 2) continue;
      const needle = String(entry[0]).toLowerCase().trim();
      if (!needle || needle.length < 3) continue;
      if (text.includes(needle)) {
        return applyValue(el, entry[1]);
      }
    }
    return false;
  }

  function applyValue(el, value) {
    if (value === undefined || value === null) return false;
    const v = String(value);
    if (el.tagName === "SELECT")   return fillSelect(el, v);
    if (el.type === "radio" || el.type === "checkbox") return fillRadioOrCheckbox(el, v);
    return fillTextLike(el, v);
  }

  // ── Public entry point ─────────────────────────────────────────────────────
  function run(profile, opts) {
    opts = opts || {};
    const rules = RULES(profile);
    const fields = Array.from(document.querySelectorAll("input, select, textarea"))
      .filter(isFillable);

    let filled = 0;
    const skipNames = new Set();

    for (const el of fields) {
      // Radio groups: only act on the group once (keyed by name).
      if (el.type === "radio" && el.name) {
        if (skipNames.has(el.name)) continue;
        skipNames.add(el.name);
      }
      const text = probeText(el);
      const rule = matchRule(rules, text);
      if (rule) {
        if (applyValue(el, rule.value)) filled++;
        continue;
      }
      if (profile.qa_defaults && tryQADefaults(el, profile.qa_defaults)) {
        filled++;
      }
    }

    return { filled, total: fields.length };
  }

  window.__jobTrackerAutofill = { run };
})();
