// Autofill engine. Reads a profile (the shape returned by /api/profile/full)
// and walks the page's inputs/selects/textareas, filling each field whose
// label/name/placeholder matches a known signal.
//
// Designed to run in two contexts:
//   1. As a content script on known ATS pages (loaded by manifest).
//   2. Injected on-demand into any tab via chrome.scripting.executeScript.
// Both contexts call `window.__jobTrackerAutofill.run(profile, opts)`.
//
// run() is async because two field types require awaits:
//   - Custom button-radio groups (Ashby Yes/No chips): synchronous click, but
//     the rest of the pipeline await-s for symmetry.
//   - Google-Places-backed location autocompletes: focus + type + wait up to
//     3s for a dropdown option to appear, then click it.

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
      // ── Combined-name labels MUST win before the first_name/last_name
      // singletons. "Legal First & Last Name" contains both "First" and
      // "Last Name", and without this rule it falls through to the
      // last_name pattern and fills only the surname.
      { value: p.full_name,        patterns: [
          /\bfirst[\s_-]*(?:&|and|\/|,)[\s_-]*last[\s_-]*name\b/i,
          /\b(?:full|legal|applicant)(?:[\s_-]+(?:full|legal))?[\s_-]*name\b/i,
      ] },
      // Most-specific first so 'first name' doesn't fall into 'name'.
      { value: p.first_name,       patterns: [/\b(first|given|forename)[\s_-]*name\b/i, /\bfname\b/i] },
      { value: p.last_name,        patterns: [/\b(last|family|sur)[\s_-]*name\b/i, /\blname\b/i, /\bsurname\b/i] },
      { value: p.preferred_name,   patterns: [/\b(preferred|nick)[\s_-]*name\b/i, /\bgoes[\s_-]*by\b/i] },
      // Bare "name" is the lowest-priority full-name match — fires only after
      // first/last/preferred have had their shot above. Ashby uses this shape.
      { value: p.full_name,        patterns: [/^name$/i, /\byour[\s_-]*name\b/i, /\bname\b/i] },

      { value: p.email,            patterns: [/\bemail\b/i, /\be-?mail[\s_-]*address\b/i] },
      { value: p.phone,            patterns: [/\bphone\b/i, /\bmobile\b/i, /\btelephone\b/i, /\bcontact[\s_-]*number\b/i] },

      // City/state/zip evaluate BEFORE street so that a Workable-style
      // "Current location (city)" field with name="address" still resolves
      // to the city, not the street.
      { value: addr.city,          patterns: [/\bcity\b/i, /\btown\b/i, /\blocality\b/i] },
      { value: addr.state,         patterns: [/\bstate\b/i, /\bprovince\b/i, /\bregion\b/i] },
      { value: addr.zip,           patterns: [/\bzip\b/i, /\bpostal[\s_-]*code\b/i, /\bpostcode\b/i] },
      { value: addr.country,       patterns: [/\bcountry\b/i] },
      { value: addr.street,        patterns: [/\b(street|address)[\s_-]*(line)?[\s_-]*1?\b/i, /\bstreet[\s_-]*address\b/i, /^address$/i] },

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
      { value: ans.previously_employed,      patterns: [
          /\bpreviously[\s_-]*(employed|worked)\b/i,
          /\bever[\s_-]*worked[\s_-]*(here|with[\s_-]*us|for[\s_-]*(us|the[\s_-]*company))\b/i,
      ] },
      { value: ans.active_security_clearance,patterns: [/\b(security)?[\s_-]*clearance\b/i] },
      { value: ans.us_gov_employment,        patterns: [/\bgov(ernment)?[\s_-]*employ(ee|ment)\b/i, /\bfederal[\s_-]*employ/i] },

      // ── Work experience (single-row, fills from work_experience[0]) ────
      // Patterns lean on explicit "job/employment/work" qualifiers because
      // generic words like "start date" or "title" overlap with education
      // fields and personal-info fields. Multi-row support is the next
      // phase (parse name="work[0][company]" indexes, etc.).
      //
      // ORDER MATTERS: the "currently employed" Yes/No rule must come
      // BEFORE the title rule. Otherwise the title pattern's "position"
      // word matches "Is this your current position?" first, fillRadio
      // searches for the title text against Yes/No options, finds nothing,
      // and the radio never gets clicked.
      { value: p.work_experience?.[0]?.current ? "Yes" : "No", patterns: [
          /\bcurrent(ly)?[\s_-]*(employed|working)\b/i,
          /\bis[\s_-]*this[\s_-]*your[\s_-]*current[\s_-]*(job|role|position)\b/i,
          /\bcurrent[\s_-]*position\?\b/i,
      ] },
      { value: p.work_experience?.[0]?.company,    patterns: [
          /\b(company|employer)\b/i,
      ] },
      { value: p.work_experience?.[0]?.title,      patterns: [
          /\bjob[\s_-]*title\b/i,
          /\b(position|role)\b/i,
      ] },
      { value: p.work_experience?.[0]?.start_date, patterns: [
          /\b(employment|job|work)[\s_-]*(start|begin)[\s_-]*(date|year)?\b/i,
          /\b(start|begin)[\s_-]*date\b.*\b(employ|job|work|position)\b/i,
      ] },
      { value: p.work_experience?.[0]?.end_date,   patterns: [
          /\b(employment|job|work)[\s_-]*end[\s_-]*(date|year)?\b/i,
          /\bend[\s_-]*date\b.*\b(employ|job|work|position)\b/i,
          /\blast[\s_-]*day\b/i,
      ] },

      // ── Education (single-row, fills from profile.education[0]) ─────────
      // Multi-row repeating education sections will get their own pass
      // later. For now we cover the common case of the first/most-recent
      // school the user is asked about. The graduation-year rule is
      // intentionally specific so it doesn't false-match employment date
      // fields that just say "year".
      { value: p.education?.[0]?.school,    patterns: [
          /\b(school|university|college|institution)\b/i,
          /\b(name[\s_-]*of[\s_-]*)?(school|university|college|institution)\b/i,
      ] },
      { value: p.education?.[0]?.degree,    patterns: [/\bdegree\b/i, /\blevel[\s_-]*of[\s_-]*education\b/i] },
      { value: p.education?.[0]?.field,     patterns: [
          /\bfield[\s_-]*of[\s_-]*study\b/i,
          /\b(major|concentration|discipline|area[\s_-]*of[\s_-]*study)\b/i,
      ] },
      { value: p.education?.[0]?.end_date,  patterns: [
          /\bgraduation[\s_-]*(year|date|month)\b/i,
          /\bexpected[\s_-]*graduation/i,
          /\bcompletion[\s_-]*(year|date)\b/i,
          /\b(end|finish)[\s_-]*(year|date)\b.*\b(school|education|degree|college)\b/i,
      ] },
    ].filter(r => r.value !== undefined && r.value !== null && r.value !== "");
  };

  // ── Text utilities ─────────────────────────────────────────────────────────
  function cleanText(s) {
    return (s || "")
      .replace(/\s+/g, " ")
      .replace(/\s*\*\s*$/, "")                   // trailing required-asterisk
      .replace(/\s*\(required\)\s*$/i, "")
      .replace(/\s*\(optional\)\s*$/i, "")
      .trim()
      .slice(0, 200);
  }

  function normalizeYesNo(v) {
    const s = String(v).toLowerCase().trim();
    if (/^(yes|y|true|1)$/.test(s)) return "yes";
    if (/^(no|n|false|0)$/.test(s)) return "no";
    return s;
  }

  // ── Field probing ──────────────────────────────────────────────────────────
  function probeText(el) {
    const parts = [];
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && lab.textContent) parts.push(lab.textContent);
    }
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      if (p.tagName === "LABEL" && p.textContent) { parts.push(p.textContent); break; }
    }
    const fieldset = el.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector(":scope > legend");
      if (legend && legend.textContent) parts.push(legend.textContent);
    }
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

    // Walk up looking for label-like siblings of ancestors. Catches the
    // Bootstrap pattern where the question is a <label class="d-block">
    // BEFORE the form-check divs holding the actual radio inputs — neither
    // a parent <label> nor a [for] attribute reach that text. Without this,
    // radio groups labeled "Are you authorized to work in the US?" never
    // fill because probeText only sees the per-option "Yes" / "No" text.
    //
    // CRITICAL: skip siblings that contain ANY input/select/textarea — their
    // labels belong to other fields. Without this guard, a Phone Number
    // field's walk-up would pick up "First Name" from a sibling form-group
    // and the first_name rule would fire on the phone input.
    let container = el.parentElement;
    for (let depth = 0; container && container !== document.body && depth < 5; depth++) {
      for (const child of container.children) {
        if (child === el || child.contains(el)) continue;
        if (child.querySelector && child.querySelector("input, select, textarea")) continue;
        const tag = child.tagName;
        const isLabelLike = tag === "LABEL"
                         || /^H[1-6]$/.test(tag)
                         || /label/i.test(child.className || "");
        if (!isLabelLike) continue;
        const t = (child.textContent || "").trim();
        if (t && t.length < 300) parts.push(t);
      }
      container = container.parentElement;
    }

    return parts.join(" | ");
  }

  function isFillable(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.type === "hidden" || el.type === "file" || el.type === "submit"
        || el.type === "button" || el.type === "reset" || el.type === "image") return false;
    if (!el.offsetParent && el.type !== "radio" && el.type !== "checkbox") return false;
    return true;
  }

  function isVisible(el) {
    if (!el) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== "fixed") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  // True for inputs that pop a Google-Places-style dropdown — Ashby, Lever,
  // and Greenhouse all use these for location. We detect by ARIA role rather
  // than label text so it works even when our regex misses the label.
  function isComboboxLike(el) {
    if (el.tagName !== "INPUT") return false;
    return el.getAttribute("role") === "combobox"
        || el.hasAttribute("aria-autocomplete")
        || el.getAttribute("aria-haspopup") === "listbox"
        || /pac-target-input/.test(el.className || "");
  }

  // ── Fillers ────────────────────────────────────────────────────────────────
  function setNativeValue(el, value) {
    const proto = Object.getPrototypeOf(el);
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input",  { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fillSelect(el, value) {
    const want = String(value).trim().toLowerCase();
    const opts = Array.from(el.options || []);
    let opt = opts.find(o => o.textContent.trim().toLowerCase() === want)
           || opts.find(o => String(o.value).trim().toLowerCase() === want)
           || opts.find(o => o.textContent.toLowerCase().includes(want));
    if (!opt && (want === "yes" || want === "no")) {
      opt = opts.find(o => new RegExp(`\\b${want}\\b`, "i").test(o.textContent));
    }
    if (!opt) return false;
    el.value = opt.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("input",  { bubbles: true }));
    return true;
  }

  function fillRadioOrCheckbox(el, value) {
    if (el.type !== "radio" && el.type !== "checkbox") return false;
    const want = String(value).trim().toLowerCase();
    const group = el.form
      ? Array.from(el.form.querySelectorAll(`input[type="${el.type}"][name="${CSS.escape(el.name || "")}"]`))
      : [el];

    // Priority order for matching — we want the most authoritative signal
    // (value attr) to win before falling back to fuzzy text matching. The
    // text check uses WORD BOUNDARIES because plain .includes("no") false-
    // matches against the substring inside "Will you now or in the future
    // require sponsorship?" — "now" contains "no", so Pass-1 of the old
    // code clicked the wrong radio for any question whose label has "now".
    const wantTokens = want.replace(/[^a-z0-9]+/g, " ").trim().split(/\s+/).filter(Boolean);
    const wordBoundaryRe = wantTokens.length
      ? new RegExp(`\\b(?:${wantTokens.join("|")})\\b`, "i")
      : null;

    // 1. Exact value-attribute match
    for (const r of group) {
      const valAttr = String(r.value || "").toLowerCase();
      if (valAttr === want) { if (!r.checked) r.click(); return true; }
    }
    // 2. Yes/No → true/false/1/0 value-attribute aliases
    if (want === "yes" || want === "no") {
      for (const r of group) {
        const va = String(r.value || "").toLowerCase();
        if ((want === "yes" && (va === "true"  || va === "1")) ||
            (want === "no"  && (va === "false" || va === "0"))) {
          if (!r.checked) r.click(); return true;
        }
      }
    }
    // 3. Word-boundary regex on the radio's own label text. The per-radio
    // probeText catches "Yes" / "No" labels reliably without leaking the
    // GROUP'S question label into the match.
    if (wordBoundaryRe) {
      for (const r of group) {
        // Use only the radio's [for] label + parent <label> wrapper — NOT
        // the full walk-up. The walk-up includes the question label which
        // can contain misleading substrings (e.g. "Will you NOW or...").
        const ownLabel = ((r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`)?.textContent) || "")
          + " " + (r.closest("label")?.textContent || "")
          + " " + (r.value || "");
        if (wordBoundaryRe.test(ownLabel)) {
          if (!r.checked) r.click(); return true;
        }
      }
    }
    return false;
  }

  function fillTextLike(el, value) {
    if (el.value && el.value.trim()) return false;
    setNativeValue(el, String(value));
    return true;
  }

  // ── Autocomplete (Google Places / React combobox) ──────────────────────────
  function simulateClick(el) {
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, view: window }));
    }
  }

  async function waitForOption(maxWaitMs) {
    const start = Date.now();
    const selector = [
      '[role="option"]:not([aria-disabled="true"]):not([aria-selected="true"])',
      '[role="listbox"] li:not([aria-disabled="true"])',
      "li[data-option-index]",
      ".ashby-job-posting-form-field-entry__option",
      ".pac-item",
      '[class*="autocomplete-option"]:not([class*="disabled"])',
      '[class*="MenuItem"]:not([aria-disabled="true"])',
    ].join(",");
    while (Date.now() - start < maxWaitMs) {
      const candidates = Array.from(document.querySelectorAll(selector)).filter(isVisible);
      if (candidates.length) return candidates[0];
      await new Promise(r => setTimeout(r, 100));
    }
    return null;
  }

  async function fillAutocomplete(el, value, waitMs = 3000) {
    el.focus();
    el.click();
    // Set the value WITHOUT firing 'blur' — blur closes the dropdown before
    // it has a chance to render. So we bypass setNativeValue here.
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    // Nudge typeahead APIs (Google Places) that listen for keyup too.
    const lastChar = value.slice(-1) || "a";
    el.dispatchEvent(new KeyboardEvent("keydown", { key: lastChar, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup",   { key: lastChar, bubbles: true }));

    const option = await waitForOption(waitMs);
    if (option) {
      simulateClick(option);
      await new Promise(r => setTimeout(r, 220));
      return true;
    }
    return false;
  }

  async function applyValue(el, value, opts) {
    if (value === undefined || value === null) return false;
    const v = String(value);
    if (el.tagName === "SELECT") return fillSelect(el, v);
    if (el.type === "radio" || el.type === "checkbox") return fillRadioOrCheckbox(el, v);
    // Autocomplete-aware path for combobox-like inputs. Defaults on; opts can
    // disable for fast jsdom tests.
    if ((opts?.autocomplete ?? true) && isComboboxLike(el)) {
      const ok = await fillAutocomplete(el, v, opts?.autocompleteWaitMs ?? 3000);
      if (ok) return true;
      // Fall through to plain text-set if dropdown didn't surface.
    }
    return fillTextLike(el, v);
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
  async function tryQADefaults(el, qaDefaults, opts) {
    if (!qaDefaults || !qaDefaults.length) return false;
    const text = probeText(el).toLowerCase();
    for (const entry of qaDefaults) {
      if (!Array.isArray(entry) || entry.length < 2) continue;
      const needle = String(entry[0]).toLowerCase().trim();
      if (!needle || needle.length < 3) continue;
      if (text.includes(needle)) {
        return await applyValue(el, entry[1], opts);
      }
    }
    return false;
  }

  // ── Button-radio groups (Ashby Yes/No chips, Lever custom radios) ──────────
  // Ashby renders Yes/No questions as plain <button> elements inside a <div>
  // — no role="radiogroup", no <fieldset>. We look for visible buttons whose
  // text is short and option-shaped, group them by parent, and pair them
  // with the nearest ancestor's question-style label.
  const NAV_RE = /^(submit|apply|next|continue|save|upload|browse|cancel|back|done|sign|log|reset|clear|close|edit|delete|remove|add)\b/i;

  function findButtonGroupLabel(parent, btns) {
    let best = "";
    let cur = parent;
    for (let depth = 0; depth < 6 && cur; depth++, cur = cur.parentElement) {
      for (const child of cur.children) {
        if (child === parent || child.contains(parent)) continue;
        if (btns.some(b => child.contains(b))) continue;
        const tag = child.tagName.toLowerCase();
        const isLabelLike = tag === "label" || tag === "legend"
                         || /^h[1-6]$/.test(tag)
                         || /label/i.test(child.className || "");
        if (!isLabelLike) continue;
        const t = cleanText(child.innerText || child.textContent || "");
        if (!t || t.length > 300) continue;
        if (/\?/.test(t)) return t;
        if (!best) best = t;
      }
      if (best) return best;
    }
    return best;
  }

  function clickMatchingButton(buttons, value) {
    const want = normalizeYesNo(value);
    for (const btn of buttons) {
      const txt = cleanText(btn.innerText || btn.textContent || "").toLowerCase();
      if (txt === want) { btn.click(); return true; }
    }
    for (const btn of buttons) {
      const txt = cleanText(btn.innerText || btn.textContent || "").toLowerCase();
      if (!txt) continue;
      if (txt.includes(want) || want.includes(txt)) { btn.click(); return true; }
    }
    return false;
  }

  function findButtonGroups() {
    const candidates = Array.from(document.querySelectorAll('button, [role="radio"], [role="option"]'))
      .filter(isVisible)
      .filter(b => {
        // Buttons inside a form's submit row, nav bars, etc. don't qualify.
        const txt = cleanText(b.innerText || b.textContent || "");
        if (!txt || txt.length > 30) return false;
        if (NAV_RE.test(txt)) return false;
        // Skip type="submit" or "reset" buttons.
        const t = (b.type || "").toLowerCase();
        if (t === "submit" || t === "reset") return false;
        return true;
      });
    const byParent = new Map();
    for (const b of candidates) {
      const key = b.parentElement;
      if (!key) continue;
      if (!byParent.has(key)) byParent.set(key, []);
      byParent.get(key).push(b);
    }
    const groups = [];
    const seen = new Set();
    for (const [parent, btns] of byParent) {
      if (btns.length < 2 || btns.length > 5) continue;
      if (parent.querySelector('input[type="radio"]')) continue; // handled by Pass 1
      if (seen.has(parent)) continue;
      seen.add(parent);
      groups.push({ parent, btns });
    }
    return groups;
  }

  // ── Public entry point ─────────────────────────────────────────────────────
  // ── Repeating-row dispatch ─────────────────────────────────────────────────
  // Matches name="education[0][school]", name="work_experience[1][company]",
  // name="job_application[education_attributes][0][school]" and the dotted
  // / dashed / underscored variants. Returns { section, index, field } or
  // null. The section keys we recognize: education / work / employment /
  // experience / job.
  function parseRepeatingFieldName(haystack) {
    const sectionRe = /(education|work[\s_-]*experience|work_experience|employment|experience)\b/i;
    const s = haystack.match(sectionRe);
    if (!s) return null;
    // Find the FIRST digit token after the section word — that's the row index
    const tail = haystack.slice(s.index + s[0].length, s.index + s[0].length + 80);
    const idxMatch = tail.match(/(\d+)/);
    if (!idxMatch) return null;
    const section = /educat/i.test(s[1]) ? "education" : "work";
    const index   = parseInt(idxMatch[1], 10);
    return { section, index };
  }

  function pickRepeatingValue(section, row, haystack) {
    if (!row) return undefined;
    const h = haystack.toLowerCase();
    if (section === "education") {
      if (/\b(school|university|college|institution)\b/.test(h)) return row.school;
      if (/\bdegree\b/.test(h)) return row.degree;
      if (/\b(major|field[\s_-]*of[\s_-]*study|concentration|discipline)\b/.test(h)) return row.field;
      if (/\b(end|graduation|completion)[\s_-]*(year|date|month)?\b/.test(h)) return row.end_date;
      if (/\b(start|begin)[\s_-]*(year|date|month)?\b/.test(h)) return row.start_date;
    } else {
      if (/\b(company|employer|organization|org)\b/.test(h)) return row.company;
      if (/\b(title|position|role)\b/.test(h)) return row.title;
      if (/\b(start|begin)[\s_-]*(date|year|month)?\b/.test(h)) return row.start_date;
      if (/\b(end|finish|last[\s_-]*day|left)[\s_-]*(date|year|month)?\b/.test(h)) return row.end_date;
      if (/\b(current|currently|present)\b/.test(h)) return row.current ? "Yes" : "No";
    }
    return undefined;
  }

  async function run(profile, opts) {
    opts = opts || {};
    const rules = RULES(profile);
    const fields = Array.from(document.querySelectorAll("input, select, textarea"))
      .filter(isFillable);

    let filled = 0;
    const skipNames = new Set();
    const handledByRow = new WeakSet();   // elements filled by Pass 0 — Pass 1 skips them

    // ── Pass 0: repeating rows (education[N], work_experience[N]) ────────────
    // Runs FIRST so that single-row rules in Pass 1 don't fill row 1+ with
    // row 0's data via the unindexed /\bschool\b/ pattern.
    for (const el of fields) {
      const haystack = `${el.name || ""} ${el.id || ""}`;
      const r = parseRepeatingFieldName(haystack);
      if (!r) continue;
      const row = r.section === "education"
        ? (profile.education || [])[r.index]
        : (profile.work_experience || [])[r.index];
      if (!row) continue;
      const value = pickRepeatingValue(r.section, row, haystack);
      if (value === undefined || value === null || value === "") continue;
      if (await applyValue(el, value, opts)) {
        filled++;
        handledByRow.add(el);
      }
    }

    // ── Pass 1: native inputs ────────────────────────────────────────────────
    for (const el of fields) {
      if (handledByRow.has(el)) continue;
      if (el.type === "radio" && el.name) {
        if (skipNames.has(el.name)) continue;
        skipNames.add(el.name);
      }
      const text = probeText(el);
      const rule = matchRule(rules, text);
      if (rule) {
        if (await applyValue(el, rule.value, opts)) filled++;
        continue;
      }
      if (profile.qa_defaults && await tryQADefaults(el, profile.qa_defaults, opts)) {
        filled++;
      }
    }

    // ── Pass 2: custom button-radio groups (Ashby) ───────────────────────────
    let buttonGroupTotal = 0;
    const groups = findButtonGroups();
    for (const { parent, btns } of groups) {
      buttonGroupTotal++;
      const label = findButtonGroupLabel(parent, btns);
      if (!label) continue;
      const rule = matchRule(rules, label);
      if (!rule) continue;
      const value = rule.value;
      if (value === undefined || value === null || value === "") continue;
      if (clickMatchingButton(btns, value)) filled++;
    }

    return { filled, total: fields.length + buttonGroupTotal };
  }

  // ── AI Phase 2 helpers ─────────────────────────────────────────────────────
  // After run() handles what the rules can match, the popup can call
  // collectUnfilledFields() to gather metadata for the remaining fields, send
  // them to /api/autofill (Claude), then call applyAiFills() with the response.
  //
  // collectUnfilledFields tags each candidate with a data-jt-id attribute so
  // applyAiFills can re-find them by querySelector without re-walking the DOM.

  function collectUnfilledFields(profile) {
    const skipNames = new Set();
    const out = [];
    let counter = 0;
    const fields = Array.from(document.querySelectorAll("input, select, textarea"))
      .filter(isFillable);
    for (const el of fields) {
      if (el.type === "radio" && el.name) {
        if (skipNames.has(el.name)) continue;
        skipNames.add(el.name);
      }
      if (el.type === "checkbox" || el.type === "radio") {
        const group = el.form && el.name
          ? Array.from(el.form.querySelectorAll(
              `input[type="${el.type}"][name="${CSS.escape(el.name)}"]`))
          : [el];
        if (group.some(g => g.checked)) continue;
      } else if ((el.value || "").trim() !== "") {
        continue;
      }
      const label = probeText(el).trim();
      if (!label || label.length < 3) continue;
      const id = "jt-" + (++counter);
      try { el.setAttribute("data-jt-id", id); } catch (_) { continue; }
      const type = el.tagName === "SELECT" ? "select"
                 : el.tagName === "TEXTAREA" ? "textarea"
                 : el.type === "radio" ? "radio"
                 : el.type === "checkbox" ? "checkbox" : "text";
      let options = [];
      if (type === "select") {
        options = Array.from(el.options || [])
          .map(o => (o.text || o.value || "").trim())
          .filter(Boolean);
      } else if (type === "radio" && el.name && el.form) {
        options = Array.from(el.form.querySelectorAll(
          `input[type="radio"][name="${CSS.escape(el.name)}"]`))
          .map(r => probeText(r).split("|").pop().trim() || r.value)
          .filter(Boolean);
      }
      out.push({
        id, type, options,
        label:       label.slice(0, 200),
        name:        el.name || "",
        placeholder: (el.placeholder || "").slice(0, 120),
        maxlength:   el.maxLength > 0 ? el.maxLength : null,
      });
    }
    // Also include any button-groups whose label didn't hit a rule. Tag the
    // group with a data-jt-id on the first button so applyAiFills can re-find.
    for (const { parent, btns } of findButtonGroups()) {
      // Skip if Pass 2 in run() already clicked one (Ashby applies a
      // 'selected' class or aria-pressed)
      if (btns.some(b => b.getAttribute("aria-pressed") === "true"
                      || /selected|active/.test(b.className || ""))) continue;
      const label = findButtonGroupLabel(parent, btns);
      if (!label || label.length < 3) continue;
      const id = "jt-" + (++counter);
      try { btns[0].setAttribute("data-jt-id", id); } catch (_) { continue; }
      out.push({
        id, type: "button-group",
        options: btns.map(b => cleanText(b.innerText || b.textContent || "")).filter(Boolean),
        label: label.slice(0, 200),
        name: "", placeholder: "", maxlength: null,
      });
    }
    return out;
  }

  async function applyAiFills(fills, opts) {
    if (!Array.isArray(fills)) return { applied: 0, skipped: 0 };
    let applied = 0, skipped = 0;
    for (const f of fills) {
      if (!f || !f.id) { skipped++; continue; }
      if (f.skip || f.value === "" || f.value == null) { skipped++; continue; }
      const tagged = document.querySelector(`[data-jt-id="${CSS.escape(f.id)}"]`);
      if (!tagged) { skipped++; continue; }
      let ok = false;
      if (f.type === "button-group" || tagged.tagName === "BUTTON") {
        // The tag is on the first button of the group — re-find siblings.
        const group = Array.from(tagged.parentElement?.children || [])
          .filter(c => c.tagName === "BUTTON" && isVisible(c));
        ok = clickMatchingButton(group, f.value);
      } else {
        ok = await applyValue(tagged, f.value, opts);
      }
      if (ok) {
        applied++;
        try { tagged.style.outline = "2px solid #6f42c1"; setTimeout(() => tagged.style.outline = "", 1500); } catch (_) {}
      } else {
        skipped++;
      }
    }
    return { applied, skipped };
  }

  window.__jobTrackerAutofill = { run, collectUnfilledFields, applyAiFills };
})();
