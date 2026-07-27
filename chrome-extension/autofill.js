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
      // Bare "name" must not claim fields asking for SOMEONE ELSE's name —
      // "Referred by: provide the name of the person who referred you" was
      // being filled with the applicant's own name, which reads as a fake
      // self-referral. Same for emergency contacts and manager/supervisor.
      { value: p.full_name,
        skipIf: /\b(referr?(ed|al)|referred[\s_-]*by|emergency[\s_-]*contact|supervisor|manager'?s?[\s_-]*name|reference'?s?[\s_-]*name|next[\s_-]*of[\s_-]*kin|spouse|guardian)\b/i,
        patterns: [/^name$/i, /\byour[\s_-]*name\b/i, /\bname\b/i] },

      { value: p.email,            patterns: [/\bemail\b/i, /\be-?mail[\s_-]*address\b/i] },

      // Country code first — explicit country_code/dial_code labels get "+1"
      { value: "+1",               patterns: [
          /\bcountry[\s_-]*(code|calling[\s_-]*code|dial[\s_-]*code)\b/i,
          /\b(dial|calling)[\s_-]*code\b/i,
      ] },
      // Generic phone rule — fills the FORMATTED phone always. On forms
      // with a separate country-code field, both fields get filled and the
      // formatted phone has a redundant "+1" prefix. Most ATSes accept it
      // (they parse the country code from the front of the number anyway).
      // Single-input forms (the common case) get the proper formatted value.
      { value: p.phone,
        patterns: [/\bphone\b/i, /\bmobile\b/i, /\btelephone\b/i, /\bcontact[\s_-]*number\b/i] },

      // Pronouns — increasingly common DEI field. Selects usually have
      // options like "He/Him", "She/Her", "They/Them", "Prefer not to say".
      { value: p.pronouns,         patterns: [/\bpronoun(s)?\b/i] },

      // City/state/zip evaluate BEFORE street so that a Workable-style
      // "Current location (city)" field with name="address" still resolves
      // to the city, not the street.
      { value: addr.city,          patterns: [/\bcity\b/i, /\btown\b/i, /\blocality\b/i] },
      { value: addr.state,         patterns: [/\bstate\b/i, /\bprovince\b/i, /\bregion\b/i] },
      // Bare "postal" (JazzHR placeholder style) must resolve to zip — but not
      // "postal address", which is a full-address label.
      { value: addr.zip,           patterns: [/\bzip\b/i, /\bpostal[\s_-]*code\b/i, /\bpostcode\b/i, /\bpostal\b(?![\s_-]*address)/i] },
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
      // Company pattern is qualified to avoid greedy-matching prose like
      // "How would you decide whether to support MacOS or Windows for a
      // large company?" or "Tell us about a company you respect". Bare
      // /\b(company|employer)\b/ used to fire on those textareas and fill
      // them with the most recent employer (e.g. "Harrah's Casino"), then
      // Phase 2 AI saw the field as "filled" and skipped it.
      //
      // probeText() joins label / name / id / placeholder etc. with " | "
      // — pattern 3 below uses (?:^|\|) so it only matches when an ENTIRE
      // segment is "Company"/"Employer"/etc., not when the word appears
      // mid-sentence in a question label.
      { value: p.work_experience?.[0]?.company,    patterns: [
          /\b(current|present|most[\s_-]*recent|recent|previous|former|past|prior)[\s_-]*(company|employer|organization|org)\b/i,
          /\b(company|employer|organization)[\s_-]*name\b/i,
          /(?:^|\|)\s*(company|employer|organization|org)\s*\*?\s*(?:\||$)/i,
      ] },
      // Title pattern explicitly qualifies "position" / "role" to avoid
      // greedy-matching prose like "Why are you interested in this role?"
      // or "Tell us about the position". Standalone /\brole\b/ used to
      // fire on those textareas and fill them with "Bartender".
      { value: p.work_experience?.[0]?.title,      patterns: [
          /\bjob[\s_-]*(title|role)\b/i,
          /\b(current|recent|most[\s_-]*recent|present)[\s_-]*(role|position)\b/i,
          /\bprevious[\s_-]*(role|position|title)\b/i,
          /\bposition[\s_-]*title\b/i,
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
      // Strip SVG fallback noise ("SVGs not supported by this browser.") that
      // Workable/others inject as text inside custom radio/checkbox icons. It
      // pollutes both labels and option text otherwise.
      .replace(/SVGs?\s+not\s+supported[^.]*\.?/gi, " ")
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
  // Chain of shadow hosts from the element outward (innermost first). Empty
  // for light-DOM elements. Used to read component-host attributes and to
  // scope dropdown searches to the owning web component.
  function shadowHostChain(el) {
    const hosts = [];
    let root = el.getRootNode && el.getRootNode();
    while (root && root.host) {
      hosts.push(root.host);
      root = root.host.getRootNode && root.host.getRootNode();
    }
    return hosts;
  }

  // <label for> lookup scoped to the element's own tree — for fields inside
  // a shadow root, document.querySelector can't see their labels.
  function labelForText(el) {
    if (!el.id) return "";
    const rootNode = (el.getRootNode && el.getRootNode()) || document;
    const lab = rootNode.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    return (lab && lab.textContent) || "";
  }

  function probeText(el) {
    const parts = [];
    const rootNode = (el.getRootNode && el.getRootNode()) || document;
    // Does this field carry a label of its OWN (as opposed to ambient text
    // that happens to sit nearby)? Decides whether the sibling walk-up below
    // is allowed to contribute — see the comment there.
    let hasOwnLabel = false;
    const forLabel = labelForText(el);
    if (forLabel) { parts.push(forLabel); hasOwnLabel = true; }
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      if (p.tagName === "LABEL" && p.textContent) {
        parts.push(p.textContent); hasOwnLabel = true; break;
      }
    }
    const fieldset = el.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector(":scope > legend");
      if (legend && legend.textContent) parts.push(legend.textContent);
    }
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      labelledby.split(/\s+/).forEach(id => {
        const node = (rootNode.getElementById && rootNode.getElementById(id))
          || document.getElementById(id);
        if (node && node.textContent) { parts.push(node.textContent); hasOwnLabel = true; }
      });
    }
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) { parts.push(ariaLabel); hasOwnLabel = true; }
    if (el.placeholder)  { parts.push(el.placeholder); hasOwnLabel = true; }
    if (el.name)         parts.push(el.name);
    if (el.id)           parts.push(el.id);
    if (el.dataset && el.dataset.qa) parts.push(el.dataset.qa);

    // Semantic QA attributes on the element or its nearest wrapper — Zoho
    // Recruit puts data-zcqa="manual_First_Name" on the <lyte-input> wrapper
    // (the input's own name is a meaningless rec-form_<id>), Workday uses
    // data-automation-id, others data-testid.
    for (const attr of ["data-zcqa", "data-automation-id", "data-testid"]) {
      const holder = el.closest && el.closest(`[${attr}]`);
      if (holder) parts.push(holder.getAttribute(attr));
    }

    // Shadow-DOM web components (SmartRecruiters SPL-*) carry the human
    // label as an ATTRIBUTE on the component host — <spl-input
    // label="Institution"> — with no <label> element anywhere in the tree.
    // Collect label-ish attributes from every host on the way out.
    for (const host of shadowHostChain(el)) {
      if (!host.getAttribute) continue;
      for (const attr of ["label", "aria-label", "placeholder", "name",
                          "data-label", "data-field", "formcontrolname"]) {
        const v = host.getAttribute(attr);
        if (v) parts.push(v);
      }
    }

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
    //
    // CRITICAL #2: only walk when the field has NO label of its own. Sibling
    // <label for="...">s belong to OTHER inputs, and collecting them made
    // every field in a flat form see every other field's label — so "Legal
    // First & Last Name" bled onto the Preferred Name and Email inputs and
    // the full-name rule (which is earlier in the list) won, writing the
    // applicant's name into the email box. Radios/checkboxes still walk
    // unconditionally: their own text is just "Yes"/"No" and the question
    // genuinely lives in ambient markup.
    const isRadioOrCheck = el.type === "radio" || el.type === "checkbox";
    let container = (isRadioOrCheck || !hasOwnLabel) ? el.parentElement : null;
    for (let depth = 0; container && container !== document.body && depth < 5; depth++) {
      for (const child of container.children) {
        if (child === el || child.contains(el)) continue;
        if (child.querySelector && child.querySelector("input, select, textarea")) continue;
        // A <label for="other-field"> is owned by that field, not this one.
        // In flat forms (label and input as siblings) the input-containing
        // guard above never fires, so without this check an over-18 radio
        // also saw "Country Code" / "Phone Number" from neighbouring rows —
        // and the country-code rule, being earlier in the list, hijacked it.
        if (child.tagName === "LABEL") {
          const owns = child.getAttribute && child.getAttribute("for");
          if (owns && owns !== el.id) continue;
        }
        const tag = child.tagName;
        const isLabelLike = tag === "LABEL"
                         || tag === "LEGEND"
                         || /^H[1-6]$/.test(tag)
                         || /label|question|prompt/i.test(child.className || "")
                         // Radio/checkbox question text is frequently a bare
                         // <span>/<p> sibling of a legend-less <fieldset>
                         // (Workable screening-question pattern). Restrict to
                         // radios/checkboxes so text inputs don't pull in noise.
                         || (isRadioOrCheck && (tag === "SPAN" || tag === "P"));
        if (!isLabelLike) continue;
        const t = (child.textContent || "")
          .replace(/SVGs?\s+not\s+supported[^.]*\.?/gi, " ")
          .replace(/\s+/g, " ").trim();
        if (t && t.length < 300) parts.push(t);
      }
      // Pierce shadow boundaries on the way up: when the walk tops out inside
      // a shadow root, continue from the host element. Without this, fields
      // inside web components (SmartRecruiters spl-*) never see the question
      // text that lives outside their shadow root.
      container = container.parentElement
        || (container.getRootNode && container.getRootNode().host) || null;
    }

    // Strip SVG fallback noise uniformly (parent-label pushes above don't run
    // through cleanText) so it never reaches rules or the Phase 2 AI label.
    return parts.join(" | ")
      .replace(/SVGs?\s+not\s+supported[^.]*\.?/gi, " ")
      .replace(/\s{2,}/g, " ")
      .trim();
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
    if (el.getAttribute("role") === "combobox"
        || el.hasAttribute("aria-autocomplete")
        || el.getAttribute("aria-haspopup") === "listbox"
        || /pac-target-input/.test(el.className || "")) return true;
    // Shadow-component autocompletes (SmartRecruiters <spl-autocomplete>)
    // put no combobox ARIA on the inner native input — recognize them by
    // the host tag name instead. Critical: these fields REVERT on blur
    // unless a dropdown option is clicked, so the plain text path is
    // guaranteed to silently lose the value.
    return shadowHostChain(el).some(h => /autocomplete|combobox|typeahead/i.test(h.tagName || ""));
  }

  // ── Fillers ────────────────────────────────────────────────────────────────
  function setNativeValue(el, value) {
    const proto = Object.getPrototypeOf(el);
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;
    // composed: true so the events escape shadow roots — web-component
    // wrappers (spl-input) often listen at their shadow root or host.
    el.dispatchEvent(new Event("input",  { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    // React 17+ uses a root-delegated listener that requires a real InputEvent
    // with inputType so its synthetic event system recognises the change.
    try {
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true, cancelable: true, composed: true,
        inputType: "insertText", data: String(value),
      }));
    } catch (_) {}
  }

  // ── Shadow DOM helpers ────────────────────────────────────────────────────
  // querySelectorAllDeep pierces open shadow roots so we find form fields
  // inside web-component wrappers (Workable, some iCIMS widgets).
  function querySelectorAllDeep(selector, root) {
    root = root || document;
    const results = Array.from(root.querySelectorAll(selector));
    // When the root is itself a shadow host (an Element, not a Document),
    // its own shadow tree isn't reachable via querySelectorAll — descend
    // into it explicitly so scoped searches see the component's internals.
    if (root.shadowRoot) results.push(...querySelectorAllDeep(selector, root.shadowRoot));
    for (const host of root.querySelectorAll("*")) {
      if (host.shadowRoot) results.push(...querySelectorAllDeep(selector, host.shadowRoot));
    }
    return results;
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

  // ── Select2 / custom-select bridge (iCIMS, older ATS widgets) ────────────
  // iCIMS and some other ATSes hide the real <select> and wrap it with a
  // Select2 jQuery plugin. fillSelect on the hidden element sets the value
  // but Select2 won't update its display unless we trigger its events.
  // This function handles both: native select + Select2 notification.
  function fillSelect2(el, value) {
    if (el.tagName !== "SELECT") return false;
    // First try the standard fill; if it fails, bail early.
    if (!fillSelect(el, value)) return false;
    // Notify Select2 to sync its display widget.
    if (window.jQuery) {
      try {
        const $el = window.jQuery(el);
        if ($el.data("select2") || el.classList.contains("select2-hidden-accessible")) {
          $el.trigger("change.select2").trigger("change");
        }
      } catch (_) {}
    }
    // React-Select sets a `__reactFiber` key on the container's input.
    // Dispatching the synthetic InputEvent from setNativeValue already covers it.
    return true;
  }

  // ── Decoy <select> / searchable dropdown bridge (BambooHR, Fabric UI) ─────
  // BambooHR renders State/Country as a <select> that contains NO real
  // options — the visible widget is a custom panel with its own search box.
  // fillSelect can never succeed on those (there is nothing to select), so
  // the field silently stayed blank and the ATS blocked submit on it.
  // Here we drive the real widget: click the trigger, type into whatever
  // search box appears, then click the exact option.
  const OPTION_SELECTOR = [
    '[role="option"]:not([aria-disabled="true"])',
    '[role="listbox"] li:not([aria-disabled="true"])',
    "li[data-option-index]",
    '[class*="option"]:not([class*="disabled"]):not([class*="options"])',
    '[class*="MenuItem"]:not([aria-disabled="true"])',
  ].join(",");

  // Equivalent spellings of a value, tried in order when the first form
  // finds no matching option. Custom widgets are inconsistent about whether
  // they list "NJ" or "New Jersey" / "US" or "United States", and a native
  // <select> hides the difference (option value vs text) while a custom
  // panel does not.
  let CURRENT_PROFILE = null;
  function valueAlternates(value) {
    const v = String(value == null ? "" : value).trim();
    const out = v ? [v] : [];
    const addr = (CURRENT_PROFILE && CURRENT_PROFILE.address) || {};
    const pairs = [
      [addr.state, addr.state_full],
      [addr.country_code, addr.country],
    ];
    for (const [short, long] of pairs) {
      if (!short || !long) continue;
      if (v.toLowerCase() === String(short).toLowerCase() && !out.includes(long)) out.push(long);
      if (v.toLowerCase() === String(long).toLowerCase() && !out.includes(short)) out.push(short);
    }
    return out;
  }

  function isDecoySelect(el) {
    if (!el || el.tagName !== "SELECT") return false;
    // A real select has choices beyond the blank placeholder.
    const real = Array.from(el.options || [])
      .filter(o => cleanText(o.textContent || "") && String(o.value || "").trim());
    return real.length === 0;
  }

  // The clickable thing a user would press to open the dropdown: the select
  // itself if it's visible, else the nearest visible wrapper/button sibling.
  function decoyTrigger(el) {
    if (isVisible(el)) return el;
    let cur = el.parentElement;
    for (let d = 0; d < 3 && cur; d++, cur = cur.parentElement) {
      const btn = Array.from(cur.querySelectorAll('button,[role="combobox"],[role="button"],input'))
        .find(isVisible);
      if (btn) return btn;
      if (isVisible(cur)) return cur;
    }
    return null;
  }

  async function fillDecoySelect(el, value, waitMs = 2500) {
    for (const candidate of valueAlternates(value)) {
      if (await fillDecoySelectOnce(el, candidate, waitMs)) return true;
    }
    return false;
  }

  async function fillDecoySelectOnce(el, value, waitMs = 2500) {
    const trigger = decoyTrigger(el);
    if (!trigger) return false;
    simulateClick(trigger);
    await new Promise(r => setTimeout(r, 150));

    // If the opened panel has a search box, typing narrows the list — vital
    // for 50-state pickers that virtualise their options.
    const search = querySelectorAllDeep('input[type="search"],input[type="text"],input:not([type])')
      .filter(isVisible)
      .filter(i => i !== el && !i.value)
      .find(i => /search|filter/i.test(`${i.placeholder || ""} ${i.getAttribute("aria-label") || ""} ${i.className || ""}`))
      || null;
    if (search) {
      search.focus();
      setNativeValue(search, String(value));
      search.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      await new Promise(r => setTimeout(r, 250));
    }

    const want = String(value).trim().toLowerCase();
    const start = Date.now();
    while (Date.now() - start < waitMs) {
      const cands = querySelectorAllDeep(OPTION_SELECTOR).filter(isVisible);
      if (cands.length) {
        // Exact match strictly preferred: a substring match on a state list
        // turns "New Jersey" into "South ..." style mis-picks.
        const exact = cands.find(c => cleanText(c.textContent).toLowerCase() === want);
        const pick = exact
          || cands.find(c => cleanText(c.textContent).toLowerCase().startsWith(want))
          || pickClosestOption(cands, value);
        if (pick) {
          simulateClick(pick);
          await new Promise(r => setTimeout(r, 200));
          return true;
        }
      }
      await new Promise(r => setTimeout(r, 100));
    }
    return false;
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
        const ownLabel = labelForText(r)
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

  // ── Date input handling ───────────────────────────────────────────────────
  // Profile stores dates as "2024-07" (or "" for current jobs). HTML5
  // <input type="date"> requires YYYY-MM-DD; <input type="month"> accepts
  // YYYY-MM directly. Some forms use plain text inputs with a placeholder
  // like "MM/YYYY". This helper normalizes a profile date into whatever
  // format the input wants.
  function normalizeDate(value, el) {
    if (!value) return "";
    const v = String(value).trim();
    const t = (el.type || "").toLowerCase();
    // YYYY-MM-DD already? leave it
    if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return v;
    // YYYY-MM → format depends on input type
    if (/^\d{4}-\d{2}$/.test(v)) {
      if (t === "date")  return v + "-01";  // first of month
      if (t === "month") return v;          // already correct
      // Text input — guess based on placeholder/aria
      const hint = ((el.placeholder || "") + " " + (el.getAttribute("aria-describedby") || "")).toLowerCase();
      if (/mm\/yyyy/i.test(hint))     return v.slice(5) + "/" + v.slice(0, 4);
      if (/mm-yyyy|m\/yyyy/.test(hint)) return v.slice(5) + "/" + v.slice(0, 4);
      if (/yyyy[-\/]?mm[-\/]?dd/i.test(hint)) return v + "-01";
      return v;  // fall back to YYYY-MM
    }
    // YYYY → leave as-is for year inputs
    if (/^\d{4}$/.test(v)) return v;
    return v;
  }

  function fillDate(el, value) {
    const normalized = normalizeDate(value, el);
    if (!normalized) return false;
    if (el.value && el.value.trim()) return false;
    setNativeValue(el, normalized);
    return true;
  }

  // ── Workday-style 3-field date splits (Month / Day / Year) ─────────────────
  // Workday and a few other ATSes render a single date as three separate
  // inputs/selects. The profile stores dates as "YYYY-MM" (day unknown → 01).
  // This pass detects a Month + Year (+ optional Day) cluster, matches the
  // cluster's text against the SAME date RULES the single-input path uses, and
  // fills each sub-field.
  //
  // Deliberately conservative: it fills ONLY when a date rule matches the
  // cluster label, so unrelated M/D/Y triplets (date of birth, card expiry,
  // "today's date") are never touched. This keeps it safe on non-Workday
  // forms, which is important because Workday walls its forms behind an
  // account so this path ships without a live dogfood.
  const MONTH_NAMES = ["January","February","March","April","May","June",
                       "July","August","September","October","November","December"];

  function dateSubKind(el) {
    const ph = (el.placeholder || "").trim().toLowerCase();
    if (ph === "mm") return "month";
    if (ph === "dd") return "day";
    if (ph === "yyyy" || ph === "yy") return "year";
    const probe = (probeText(el) + " " + (el.getAttribute("aria-label") || "")).toLowerCase();
    if (/\bmonth\b/.test(probe)) return "month";
    if (/\bday\b/.test(probe))   return "day";
    if (/\byear\b/.test(probe))  return "year";
    return null;
  }

  function fillSplitDateField(el, value) {
    if (!el || (el.value && el.value.trim())) return false;
    if (el.tagName === "SELECT") {
      return fillSelect(el, value) || fillSelect(el, String(parseInt(value, 10)));
    }
    setNativeValue(el, value);
    return true;
  }

  function fillSplitDates(rules) {
    let filled = 0;
    const all = querySelectorAllDeep("input, select").filter(isFillable);
    const months = all.filter(e => dateSubKind(e) === "month");
    const usedYears = new Set();
    for (const monthEl of months) {
      // Walk up to the tightest scope that ALSO contains a Year field — that
      // scope is the date widget. Stop as soon as a year is found so we don't
      // grab a neighbouring date question's fields.
      let scope = monthEl.parentElement, yearEl = null, dayEl = null;
      for (let d = 0; d < 4 && scope; d++, scope = scope.parentElement) {
        const cands = [...scope.querySelectorAll("input, select")];
        if (!yearEl) yearEl = cands.find(c => dateSubKind(c) === "year" && !usedYears.has(c));
        if (!dayEl)  dayEl  = cands.find(c => dateSubKind(c) === "day");
        if (yearEl) break;
      }
      if (!yearEl || !scope) continue;
      usedYears.add(yearEl);
      // Match the cluster's text against the date rules — only fill on a hit.
      const label = (scope.textContent || "").replace(/\s+/g, " ").slice(0, 200);
      const rule = matchRule(rules, label);
      if (!rule) continue;
      const m = String(rule.value).match(/^(\d{4})-(\d{2})(?:-(\d{2}))?$/);
      if (!m) continue;
      const [, yyyy, mm, dd] = m;
      // Month selects usually carry names ("September"); try name then numbers.
      if (monthEl.tagName === "SELECT") {
        if (fillSelect(monthEl, MONTH_NAMES[parseInt(mm, 10) - 1])
            || fillSelect(monthEl, mm)
            || fillSelect(monthEl, String(parseInt(mm, 10)))) filled++;
      } else if (fillSplitDateField(monthEl, mm)) {
        filled++;
      }
      if (dayEl  && fillSplitDateField(dayEl, dd || "01")) filled++;
      if (fillSplitDateField(yearEl, yyyy)) filled++;
    }
    return filled;
  }

  // ── Autocomplete (Google Places / React combobox / shadow components) ──────
  function simulateClick(el) {
    // composed: true lets the events cross shadow boundaries — required for
    // options rendered inside a web component's shadow root (SPL listboxes).
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      el.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, composed: true, button: 0, view: window,
      }));
    }
  }

  async function waitForOption(maxWaitMs, wantText, scopeRoot) {
    const start = Date.now();
    const selector = [
      '[role="option"]:not([aria-disabled="true"]):not([aria-selected="true"])',
      '[role="listbox"] li:not([aria-disabled="true"])',
      "li[data-option-index]",
      ".ashby-job-posting-form-field-entry__option",
      ".pac-item",
      "spl-option",
      '[class*="autocomplete-option"]:not([class*="disabled"])',
      '[class*="MenuItem"]:not([aria-disabled="true"])',
    ].join(",");
    // Only click an option whose text actually relates to what we typed:
    // exact match first (SmartRecruiters echoes the typed free text as the
    // FIRST option), then substring, then first-meaningful-token overlap
    // (Google Places: want "Columbus" → option "Columbus, OH, USA").
    //
    // NO positional "just take the first option" fallback: now that the
    // search pierces shadow roots it sees dropdowns the old light-DOM
    // search never could — e.g. the dial-code country picker inside
    // spl-phone-field — and blind-clicking the first entry there commits a
    // wrong value. Unmatched fields fall through to a plain text-set.
    const want = String(wantText || "").trim().toLowerCase();
    const wantToken = want.split(/[\s,]+/).find(t => t.length >= 3) || "";
    const matchOf = (cands) => {
      if (!want) return null;
      return cands.find(c => cleanText(c.textContent).toLowerCase() === want)
          || cands.find(c => cleanText(c.textContent).toLowerCase().includes(want))
          || (wantToken
              ? cands.find(c => cleanText(c.textContent).toLowerCase().includes(wantToken))
              : null);
    };
    let lastSeen = [];
    while (Date.now() - start < maxWaitMs) {
      // Options inside the owning component's shadow tree first; portalled
      // dropdowns (Google Places .pac-container on <body>) via the
      // document-wide deep fallback.
      let candidates = scopeRoot
        ? querySelectorAllDeep(selector, scopeRoot).filter(isVisible)
        : [];
      if (!candidates.length) {
        candidates = querySelectorAllDeep(selector).filter(isVisible);
      }
      if (candidates.length) lastSeen = candidates;
      const matched = matchOf(candidates);
      if (matched) return { matched, candidates };
      await new Promise(r => setTimeout(r, 100));
    }
    // No text match. Hand back whatever options WERE on screen so the caller
    // can tell "this is a fixed option list I failed to match" apart from
    // "this is a free-text field with no dropdown at all".
    return { matched: null, candidates: lastSeen };
  }

  // ── Fixed-option (closed) dropdown handling ────────────────────────────────
  // Some comboboxes only accept values from their own list — Greenhouse's
  // "expected compensation range" is the canonical example. Typing free text
  // ("Negotiable") into one leaves the field invalid, and the ATS rejects the
  // whole submission with a "field is required" error AFTER you click Submit.
  // So when options are present but none matched, pick the best legal option
  // rather than injecting text that is guaranteed to fail validation.

  // Pull the numbers out of a salary-ish string: "$40,000 - $49,999" → [40000, 49999].
  // "80k" → [80000].
  function parseMoneyTokens(s) {
    const out = [];
    const re = /(\d[\d,]*(?:\.\d+)?)\s*([kK])?/g;
    let m;
    while ((m = re.exec(String(s || "")))) {
      let n = parseFloat(m[1].replace(/,/g, ""));
      if (!isFinite(n)) continue;
      if (m[2]) n *= 1000;
      out.push(n);
    }
    return out;
  }

  // Choose the option that best fits `value`.
  //  - salary/numeric: the option whose range contains (or sits nearest to)
  //    the desired figure
  //  - otherwise: highest meaningful-token overlap
  // Returns null when nothing scores above zero — better to leave a field
  // blank for the user than to commit a wrong answer on their behalf.
  function pickClosestOption(candidates, value) {
    const cands = (candidates || []).filter(Boolean);
    if (!cands.length) return null;
    const textOf = (c) => cleanText(c.textContent || "");

    const wanted = parseMoneyTokens(value).filter(n => n >= 1000);
    if (wanted.length) {
      // Use the low end of a requested range as the target figure.
      const target = Math.min(...wanted);
      let best = null, bestDist = Infinity;
      for (const c of cands) {
        const nums = parseMoneyTokens(textOf(c)).filter(n => n >= 1000);
        if (!nums.length) continue;
        const lo = Math.min(...nums), hi = Math.max(...nums);
        // Distance 0 when the target sits inside the option's range.
        const dist = target < lo ? lo - target : (target > hi ? target - hi : 0);
        if (dist < bestDist) { bestDist = dist; best = c; }
      }
      if (best) return best;
    }

    const wantTokens = qaTokens(value);
    if (!wantTokens.length) return null;
    let best = null, bestScore = 0;
    for (const c of cands) {
      const optTokens = new Set(qaTokens(textOf(c)));
      if (!optTokens.size) continue;
      let shared = 0;
      for (const t of wantTokens) if (optTokens.has(t)) shared++;
      const score = shared / wantTokens.length;
      if (score > bestScore) { bestScore = score; best = c; }
    }
    return bestScore > 0 ? best : null;
  }

  async function fillAutocomplete(el, value, waitMs = 3000) {
    el.focus();
    el.click();
    // Set the value WITHOUT firing 'blur' — blur closes the dropdown before
    // it has a chance to render. SmartRecruiters spl-autocomplete goes
    // further: blur REVERTS the field unless a dropdown option was clicked,
    // and Escape clears it — so neither is ever dispatched here.
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    // Nudge typeahead APIs (Google Places) that listen for keyup too.
    const lastChar = value.slice(-1) || "a";
    el.dispatchEvent(new KeyboardEvent("keydown", { key: lastChar, bubbles: true, composed: true }));
    el.dispatchEvent(new KeyboardEvent("keyup",   { key: lastChar, bubbles: true, composed: true }));

    // Scope the dropdown search to the outermost shadow host (the whole
    // spl-form-field / spl-autocomplete component) so its own listbox wins
    // over unrelated [role=option]s elsewhere on the page.
    const hosts = shadowHostChain(el);
    const scopeRoot = hosts.length ? hosts[hosts.length - 1] : null;
    const { matched, candidates } = await waitForOption(waitMs, value, scopeRoot);
    if (matched) {
      simulateClick(matched);
      await new Promise(r => setTimeout(r, 220));
      return { ok: true, hadOptions: true };
    }
    // Options rendered but none matched the requested text — this is a closed
    // list. Commit the nearest legal option instead of falling through to a
    // free-text set that the ATS will reject at submit time.
    if (candidates && candidates.length) {
      const closest = pickClosestOption(candidates, value);
      if (closest) {
        simulateClick(closest);
        await new Promise(r => setTimeout(r, 220));
        return { ok: true, hadOptions: true, approximate: true };
      }
      return { ok: false, hadOptions: true };
    }
    return { ok: false, hadOptions: false };
  }

  async function applyValue(el, value, opts) {
    const ok = await applyValueCore(el, value, opts);
    // Mark engine-filled fields so the Learn pass can tell them apart from
    // answers the user typed by hand (those are the ones worth learning).
    if (ok) { try { el.setAttribute("data-jt-autofilled", "1"); } catch (_) {} }
    return ok;
  }

  async function applyValueCore(el, value, opts) {
    if (value === undefined || value === null) return false;
    const v = String(value);
    if (el.tagName === "SELECT") {
      if (fillSelect2(el, v)) return true;
      // Nothing selectable in the element itself — drive the custom widget
      // that's standing in for it (BambooHR State/Country).
      if ((opts?.autocomplete ?? true) && isDecoySelect(el)) {
        return await fillDecoySelect(el, v, opts?.autocompleteWaitMs ?? 2500);
      }
      return false;
    }
    if (el.type === "radio" || el.type === "checkbox") return fillRadioOrCheckbox(el, v);
    // Native date/month inputs — convert YYYY-MM to whatever format the
    // input accepts. Browsers reject malformed values silently otherwise.
    if (el.type === "date" || el.type === "month") return fillDate(el, v);
    // Numeric inputs reject non-numeric text. The user keeps salary as
    // "Negotiable" (text), which must never be jammed into a numeric
    // Minimum/Maximum-salary box — skip and leave it for them to fill.
    if ((el.type === "number" || (el.getAttribute && el.getAttribute("inputmode") === "numeric"))
        && !/^-?\d[\d,]*\.?\d*$/.test(v.trim())) {
      return false;
    }
    // Autocomplete-aware path for combobox-like inputs. Defaults on; opts can
    // disable for fast jsdom tests.
    if ((opts?.autocomplete ?? true) && isComboboxLike(el)) {
      const res = await fillAutocomplete(el, v, opts?.autocompleteWaitMs ?? 3000);
      if (res.ok) return true;
      // A dropdown DID render and we still couldn't find a usable option:
      // the list is closed, so free text would only fail validation later.
      // Clear whatever we typed and leave the field for the user.
      if (res.hadOptions) {
        try { setNativeValue(el, ""); } catch (_) {}
        return false;
      }
      // No dropdown at all — genuine free-text input, fall through.
    }
    return fillTextLike(el, v);
  }

  function matchRule(rules, text) {
    for (const rule of rules) {
      // A rule can veto itself for fields that merely share a keyword —
      // e.g. the bare "name" rule must not fill "name of the person who
      // referred you". Checked before the patterns so the rule is skipped
      // entirely and a later, more specific rule still gets its turn.
      if (rule.skipIf && rule.skipIf.test(text)) continue;
      for (const re of rule.patterns) {
        if (re.test(text)) return rule;
      }
    }
    return null;
  }

  // ── Q&A fuzzy fallback ─────────────────────────────────────────────────────
  // Built-in defaults for very common questions that the rule list doesn't
  // cover and that nearly every job applicant answers the same way. The
  // user's profile.qa_defaults still wins — these are LAST-RESORT after both
  // the rules and the user's custom list. Designed so the user can override
  // any of these by adding their own entry to qa_defaults.
  const BUILT_IN_QA = [
    [/\b(are[\s_]+you[\s_]+(at[\s_]+least[\s_]+|over[\s_]+|)((18|eighteen)(\s+(years|yrs)([\s_]+(of[\s_]+)?age)?)?))/i, "Yes"],
    [/\bover[\s_]+(18|eighteen)\b/i, "Yes"],
    [/\b(driver'?s?[\s_]+licen[sc]e|valid[\s_]+driv)/i, "Yes"],
    [/\b(how[\s_]+did[\s_]+you[\s_]+(hear|find)[\s_]+(about|out)|where[\s_]+did[\s_]+you[\s_]+(hear|learn))/i, "LinkedIn"],
    [/\bdo[\s_]+you[\s_]+have[\s_]+(any[\s_]+|prior[\s_]+|previous[\s_]+)(work[\s_]+|professional[\s_]+|relevant[\s_]+)?experience\b/i, "Yes"],
    [/\bcan[\s_]+you[\s_]+start[\s_]+(within|in)[\s_]+(2|two)[\s_]+weeks?\b/i, "Yes"],
    [/\bavailable[\s_]+to[\s_]+(start|begin)[\s_]+(immediately|right[\s_]+away)\b/i, "Yes"],
    [/\bspeak[\s_]+english\b/i, "Yes"],
    [/\bfluent[\s_]+in[\s_]+english\b/i, "Yes"],
    [/\b(non[\s-]*compete|non[\s-]*disclosure)[\s_]+agreement\b/i, "No"],
    [/\bable[\s_]+to[\s_]+work[\s_]+(remotely|from[\s_]+home)\b/i, "Yes"],
    // "Are you able/authorized/eligible to work in the US (without
    // sponsorship)?" — the positive framing of the sponsorship question.
    // Deliberately requires an ability word so it can never fire on the
    // "do you NEED/REQUIRE sponsorship?" phrasing, which is answered No.
    [/\b(able|authoriz(?:ed)?|authoris(?:ed)?|eligible|permitted|legally)\b[^?]{0,80}\bwork\b[^?]{0,80}(without[\s_]+(?:visa[\s_]+)?sponsorship|in[\s_]+the[\s_]+(?:us\b|u\.s\.|united[\s_]+states))/i, "Yes"],
  ];

  // Words that carry no signal when comparing two phrasings of the same
  // question. Dropped before token-overlap scoring so "Do you now, or will
  // you in the future, need sponsorship..." still matches a saved answer
  // whose wording differs in the filler words.
  const QA_STOPWORDS = new Set([
    "a","an","the","and","or","of","to","in","for","on","at","by","with","as",
    "is","are","was","were","be","been","being","do","does","did","doing",
    "you","your","yours","we","our","us","i","me","my","it","its","this","that",
    "have","has","had","will","would","can","could","should","may","might",
    "any","all","some","if","from","about","please","select","choose","enter",
    "now","future","order","other","currently","ever","following","below",
  ]);

  // ── Polarity guard for fuzzy Yes/No reuse ─────────────────────────────────
  // Two questions can share nearly every word and still mean opposite things:
  //   saved: "Do you NEED sponsorship to work in the United States?"      → No
  //   form:  "Are you ABLE to work in the United States WITHOUT sponsorship?"
  // Token overlap is ~100%, so the fuzzy tier happily reused "No" — which
  // states the applicant is not authorized to work and auto-rejects them.
  // Before reusing a yes/no answer on a fuzzy (non-exact) match, require the
  // two questions to be framed the same way.
  // Classify the FRAMING of a work-authorization question. Both framings use
  // nearly identical vocabulary but invert the correct answer:
  //   needs_sponsorship   "Do you NEED sponsorship to work in the US?"   → No
  //   authorized_to_work  "Are you ABLE to work in the US w/o sponsorship?" → Yes
  // "needs_sponsorship" is tested first because the authorized-to-work
  // wording ("...renew your AUTHORIZATION to work...") frequently appears
  // inside a need-sponsorship question too.
  const RE_NEEDS_SPONSOR = /\b(need|needs|require[sd]?|requiring|request)\b[^?]{0,80}\bsponsor/i;
  const RE_SPONSOR_NEEDED = /\bsponsor\w*\b[^?]{0,40}\b(required|needed|necessary)\b/i;
  const RE_AUTHORIZED    = /\b(able|allowed|authoriz\w*|authoris\w*|eligible|permitted|legally)\b[^?]{0,80}\bwork\b/i;

  function polarityClass(text) {
    const t = String(text || "");
    if (RE_NEEDS_SPONSOR.test(t) || RE_SPONSOR_NEEDED.test(t)) return "needs_sponsorship";
    if (RE_AUTHORIZED.test(t)) return "authorized_to_work";
    return "";
  }

  function isYesNo(v) {
    return /^(yes|no|y|n|true|false)$/i.test(String(v == null ? "" : v).trim());
  }

  // Same framing? Only consulted for yes/no answers reused via fuzzy match.
  function polarityCompatible(savedQ, fieldQ) {
    const a = polarityClass(savedQ), b = polarityClass(fieldQ);
    if (a !== b) return false;
    // Neither is a work-auth question: still refuse to carry a yes/no across
    // a bare negation flip ("Do you have X?" vs "Do you NOT have X?").
    if (!a) {
      const negA = /\bwithout\b|\bnot\b|\bnever\b/i.test(savedQ || "");
      const negB = /\bwithout\b|\bnot\b|\bnever\b/i.test(fieldQ || "");
      if (negA !== negB) return false;
    }
    return true;
  }

  function qaTokens(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/[^a-z0-9\s]+/g, " ")
      .split(/\s+/)
      .filter(t => t.length > 2 && !QA_STOPWORDS.has(t));
  }

  // Resolve a question string to candidate answers, most-confident first:
  //   1. substring either direction (saved question appears in the field
  //      text, or the field text appears in the saved question)
  //   2. token overlap — needs >=2 shared meaningful tokens AND >=60% of the
  //      saved question's tokens present, so "need sponsorship ... United
  //      States?" matches "... United States or Canada?" but unrelated
  //      questions that merely share one word do not
  //   3. BUILT_IN_QA regexes
  //
  // Returns a LIST, not a single answer, because probeText deliberately casts
  // a wide net for radio groups: an over-18 radio in a flat form also sees a
  // sibling "How did you hear about us?" label, which matched a saved answer
  // ("LinkedIn") that can't apply to a Yes/No radio. Returning one guess meant
  // the field was abandoned; the caller can now fall through to the next
  // candidate (the built-in over-18 → "Yes") and actually fill it.
  function lookupQACandidates(rawText, qaDefaults) {
    const text = String(rawText || "").toLowerCase();
    if (!text) return [];
    const out = [];
    const push = (v) => {
      if (v === undefined || v === null || v === "") return;
      if (!out.includes(v)) out.push(v);
    };

    if (qaDefaults && qaDefaults.length) {
      // Tier 1 — substring in either direction.
      for (const entry of qaDefaults) {
        if (!Array.isArray(entry) || entry.length < 2) continue;
        const needle = String(entry[0]).toLowerCase().trim();
        if (!needle || needle.length < 3) continue;
        if (text.includes(needle) || (needle.length > 25 && needle.includes(text) && text.length > 12)) {
          push(entry[1]);
        }
      }
      // Tier 2 — fuzzy token overlap, best-scoring entries first.
      const fieldTokens = new Set(qaTokens(text));
      if (fieldTokens.size) {
        const scored = [];
        for (const entry of qaDefaults) {
          if (!Array.isArray(entry) || entry.length < 2) continue;
          const saved = qaTokens(entry[0]);
          if (saved.length < 2) continue;
          let shared = 0;
          for (const t of saved) if (fieldTokens.has(t)) shared++;
          const coverage = shared / saved.length;
          if (shared < 2 || coverage < 0.6) continue;
          // Yes/No answers only carry over when the question is framed the
          // same way — see polarityCompatible() above.
          if (isYesNo(entry[1]) && !polarityCompatible(entry[0], text)) continue;
          scored.push([coverage, entry[1]]);
        }
        scored.sort((a, b) => b[0] - a[0]);
        for (const [, v] of scored) push(v);
      }
    }
    // Tier 3 — built-in last-resort.
    for (const [re, value] of BUILT_IN_QA) {
      if (re.test(text)) push(value);
    }
    return out.slice(0, 6);
  }

  // Convenience wrapper for callers that only want the top answer.
  function lookupQA(rawText, qaDefaults) {
    const c = lookupQACandidates(rawText, qaDefaults);
    return c.length ? c[0] : null;
  }

  async function tryQADefaults(el, qaDefaults, opts) {
    for (const value of lookupQACandidates(probeText(el), qaDefaults)) {
      if (await applyValue(el, value, opts)) return true;
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

  // Click an option chip without letting it submit the form. These chips are
  // <button> elements with no type attribute, which the HTML spec treats as
  // type="submit" — so a synthetic click can fire the form's submit handler
  // and send a half-filled application. Real users are safe because the ATS's
  // own onClick calls preventDefault, but we must not depend on that.
  function clickWithoutSubmitting(btn) {
    const form = btn.closest && btn.closest("form");
    if (!form) { btn.click(); return; }
    const block = (e) => { e.preventDefault(); e.stopPropagation(); };
    form.addEventListener("submit", block, true);
    try {
      btn.click();
    } finally {
      // Detach on the next tick: any submit caused by our click has already
      // dispatched, and a genuine user submit can't land inside this tick.
      setTimeout(() => form.removeEventListener("submit", block, true), 0);
    }
  }

  function clickMatchingButton(buttons, value) {
    const mark = (btn) => { try { btn.setAttribute("data-jt-autofilled", "1"); } catch (_) {} };
    const want = normalizeYesNo(value);
    for (const btn of buttons) {
      const txt = cleanText(btn.innerText || btn.textContent || "").toLowerCase();
      if (txt === want) { clickWithoutSubmitting(btn); mark(btn); return true; }
    }
    for (const btn of buttons) {
      const txt = cleanText(btn.innerText || btn.textContent || "").toLowerCase();
      if (!txt) continue;
      if (txt.includes(want) || want.includes(txt)) { clickWithoutSubmitting(btn); mark(btn); return true; }
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
        // Skip type="submit"/"reset" — but read the ATTRIBUTE, not the IDL
        // property. A <button> with no type inside a <form> reports
        // .type === "submit" by spec, and Ashby's Yes/No chips are exactly
        // that: <button class="_option_...">Yes</button>. Using .type here
        // discarded every real screener chip. NAV_RE above still filters
        // genuine submit buttons, which say "Submit"/"Apply"/"Next".
        const t = (b.getAttribute("type") || "").toLowerCase();
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
  // ── Repeating-section expansion ────────────────────────────────────────────
  // Many ATSes render only the FIRST education / work-history row by default;
  // additional rows appear after the user clicks an "Add Another" button.
  // This function detects those buttons, figures out which section each
  // belongs to (education vs work), counts existing rows by parsing indexed
  // input names, and clicks the button enough times to match the profile
  // array length (capped at 5 to avoid runaway loops).
  const ADD_BTN_RE = /^[\s\+]*add(\s+(another|more|new|additional))?(\s+(education|school|college|degree|job|work|employment|experience|position|role|history))?(\s+(row|entry|item|line))?[\s\+\.]*$/i;

  async function expandRepeatingSections(profile) {
    const eduCount  = (profile.education       || []).length;
    const workCount = (profile.work_experience || []).length;
    if (eduCount < 2 && workCount < 2) return 0;

    let totalClicks = 0;
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], a'))
      .filter(isVisible)
      .filter(b => {
        const t = cleanText(b.innerText || b.textContent || "").toLowerCase();
        if (!t || t.length > 50) return false;
        return ADD_BTN_RE.test(t);
      });

    for (const btn of candidates) {
      // Determine the section by looking at the button's context
      const ancestor = btn.closest("fieldset, section, [class*='card'], [class*='section'], [class*='education'], [class*='work'], [class*='experience'], form");
      const context = ((ancestor && ancestor.textContent) || "").toLowerCase().slice(0, 1000);
      const isEdu  = /\b(education|school|university|college|degree|graduat)\b/.test(context);
      const isWork = /\b(work[\s_-]*(experience|history)|employment[\s_-]*history|previous[\s_-]*(employer|job|position))\b/.test(context);
      const target = isEdu ? eduCount : isWork ? workCount : 0;
      if (target < 2) continue;

      // Count existing rows for this section by scanning indexed input names
      const re = isEdu
        ? /\b(?:education|school)[\[_.]+(\d+)/i
        : /\b(?:work_experience|work|employment|experience|job)[\[_.]+(\d+)/i;
      const rows = new Set();
      for (const inp of ancestor ? ancestor.querySelectorAll("input, select, textarea") : []) {
        const name = (inp.name || inp.id || "");
        const m = name.match(re);
        if (m) rows.add(parseInt(m[1], 10));
      }
      // Fall back to counting visible row containers if no indexed inputs found yet
      const startRows = rows.size || 1;
      const need = Math.max(0, target - startRows);

      for (let i = 0; i < need && i < 5; i++) {
        try { btn.click(); } catch (_) {}
        totalClicks++;
        // Wait briefly for the new row's DOM to render. ATSes are typically
        // React-based and re-render in <50ms, but slower frameworks need more.
        await new Promise(r => setTimeout(r, 300));
      }
    }
    return totalClicks;
  }

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
    profile = profile || {};
    CURRENT_PROFILE = profile;   // used by valueAlternates() for state/country forms

    // ── Pass -1: click "Add another" buttons to surface hidden rows ──────────
    // Runs BEFORE we snapshot `fields` so the newly-created inputs are picked
    // up by Pass 0's indexed-name dispatch.
    try { await expandRepeatingSections(profile); } catch (_) {}

    const rules = RULES(profile);
    const fields = querySelectorAllDeep("input, select, textarea").filter(isFillable);

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
      if (rule && await applyValue(el, rule.value, opts)) {
        filled++;
        continue;
      }
      // Either no rule matched, or the matched rule's value couldn't be
      // applied to this control (e.g. a text value aimed at a Yes/No radio).
      // Falling through to the Q&A candidates rescues the field instead of
      // abandoning it after one failed attempt.
      if (await tryQADefaults(el, profile.qa_defaults, opts)) {
        filled++;
      }
    }

    // ── Pass 1b: Workday-style 3-field date splits (Month/Day/Year) ──────────
    // Runs after the single-input rules so it only handles genuine split
    // clusters that Pass 1 left untouched.
    try { filled += fillSplitDates(rules); } catch (_) {}

    // ── Pass 2: custom button-radio groups (Ashby) ───────────────────────────
    let buttonGroupTotal = 0;
    const groups = findButtonGroups();
    for (const { parent, btns } of groups) {
      buttonGroupTotal++;
      const label = findButtonGroupLabel(parent, btns);
      if (!label) continue;
      // Rules win; otherwise fall back to the user's saved answers and the
      // built-in Q&A list — the same ladder Pass 1 uses for native inputs.
      // Without this fallback, screener questions rendered as button chips
      // (Ashby "Have you worked with X?" Yes/No) were silently left blank
      // even when the exact answer was already saved in qa_defaults.
      const rule = matchRule(rules, label);
      const values = rule && rule.value !== undefined && rule.value !== null && rule.value !== ""
        ? [rule.value]
        : lookupQACandidates(label, profile.qa_defaults);
      for (const value of values) {
        if (clickMatchingButton(btns, value)) { filled++; break; }
      }
    }

    // ── Pass 4: auto-upload the user's default CV into resume file inputs ──
    // Browsers block setting input.value on file inputs, but they allow
    // assigning input.files via a DataTransfer-constructed FileList. The
    // CV is baked into the bookmarklet payload as base64, so no extra
    // cross-origin fetch is needed (which strict-CSP ATSes would block).
    try { filled += await fillResumeUpload(); } catch (_) {}

    // ── Submit-button highlight (NOT auto-click) ─────────────────────────────
    // Visual cue that helps the user find the Submit button after a long
    // scroll-through review. We NEVER click the button automatically; the
    // value of this extension is filling fast so the user can review.
    try { highlightSubmitButton(opts); } catch (_) {}

    // ── Optional auto-submit ────────────────────────────────────────────────
    // Off unless the caller explicitly opts in. Even then it only fires when
    // validateBeforeSubmit() can show every required field is satisfied and
    // no CAPTCHA is on screen — otherwise we hand back the blocker list and
    // leave the form for the user.
    let autoSubmit = { submitted: false, blockers: [] };
    try { autoSubmit = maybeAutoSubmit(opts); } catch (e) {
      autoSubmit = { submitted: false, blockers: ["auto-submit error: " + e.message] };
    }

    return {
      filled,
      total: fields.length + buttonGroupTotal,
      submitted: autoSubmit.submitted,
      blockers: autoSubmit.blockers,
    };
  }

  async function fillResumeUpload() {
    // Need a CV available
    if (!window.__jt_cv_b64) return 0;
    // Find file inputs labelled resume / CV / curriculum vitae / upload.
    // Don't use isFillable here — it excludes type="file" specifically
    // because the rule-based passes can't touch file inputs. Pass 4 CAN.
    // Deep query: SmartRecruiters wraps its dropzones in shadow-DOM web
    // components (spl-dropzone), invisible to a plain querySelectorAll.
    const fileInputs = querySelectorAllDeep('input[type="file"]')
      .filter(el => !el.disabled && !el.readOnly);
    if (!fileInputs.length) return 0;

    const RESUME_RE = /\b(resume|cv|curriculum[\s_-]*vitae|upload[\s_-]*(your[\s_-]*)?(resume|cv|file))\b|\battach[\s_-]*(your[\s_-]*)?(resume|cv|file)\b/i;
    // Never auto-attach the CV to slots meant for something else.
    const NOT_RESUME_RE = /\b(cover[\s_-]*letter|portfolio|transcript|photo|avatar|head[\s_-]*shot|certificate|reference)\b/i;
    let resumeInput = null;
    const candidates = [];
    for (const el of fileInputs) {
      const haystack = `${el.name || ""} ${el.id || ""} ${probeText(el)} ${el.accept || ""}`;
      if (NOT_RESUME_RE.test(haystack)) continue;
      if (RESUME_RE.test(haystack)) { resumeInput = el; break; }
      candidates.push(el);
    }
    // Labels ambiguous (e.g. SmartRecruiters ids every slot "file-input"):
    // take the first non-excluded input — the resume slot leads application
    // forms in practice, and the user reviews before submitting anyway.
    if (!resumeInput && candidates.length) resumeInput = candidates[0];
    if (!resumeInput) return 0;
    // Skip if a file is already attached
    if (resumeInput.files && resumeInput.files.length > 0) return 0;

    // Decode base64 → Uint8Array → File via DataTransfer
    try {
      const bin = atob(window.__jt_cv_b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const file = new File(
        [bytes],
        window.__jt_cv_filename || "cv.pdf",
        { type: window.__jt_cv_mime || "application/pdf" }
      );
      const dt = new DataTransfer();
      dt.items.add(file);
      resumeInput.files = dt.files;
      // Dispatch the events React / Vue / Angular listen for so framework
      // state updates and the "Selected: cv.pdf" indicator renders.
      resumeInput.dispatchEvent(new Event("input",  { bubbles: true }));
      resumeInput.dispatchEvent(new Event("change", { bubbles: true }));
      // Flash purple so the user can see what got uploaded
      try {
        resumeInput.style.outline = "2px solid #6f42c1";
        setTimeout(() => { resumeInput.style.outline = ""; }, 2500);
      } catch (_) {}
      return 1;
    } catch (e) {
      console.warn("[autofill] CV upload failed:", e);
      return 0;
    }
  }

  const SUBMIT_TEXT = /^(submit|submit\s+application|send\s+application|apply\s+now|finish\s+&?\s*submit|review\s+&?\s*submit)$/i;

  function findSubmitButton() {
    // Prefer explicit type="submit"
    for (const b of document.querySelectorAll('button[type="submit"], input[type="submit"]')) {
      if (isVisible(b)) return b;
    }
    // Fall back to text match
    for (const b of document.querySelectorAll('button, [role="button"], a.btn, input[type="button"]')) {
      if (!isVisible(b)) continue;
      const txt = cleanText(b.innerText || b.value || b.textContent || "");
      if (SUBMIT_TEXT.test(txt)) return b;
    }
    return null;
  }

  // ── Pre-submit validation ──────────────────────────────────────────────────
  // Auto-submit is only safe when we can prove the form is actually complete.
  // Returns { ok, blockers: [reason, ...] } — every reason is phrased for
  // display in the popup so the user knows what to finish by hand.
  function validateBeforeSubmit() {
    const blockers = [];

    // 1. A CAPTCHA means a human must act; we never attempt to solve one.
    const captcha = [
      'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
      'iframe[src*="turnstile"]', ".g-recaptcha", ".h-captcha", "[data-sitekey]",
    ].some(sel => Array.from(document.querySelectorAll(sel)).some(isVisible));
    if (captcha) blockers.push("CAPTCHA present — needs a human");

    // 2. Every required control must hold a value. Radio/checkbox groups are
    //    judged per NAME (any one checked satisfies the group).
    const seenGroups = new Set();
    for (const el of querySelectorAllDeep("input, select, textarea")) {
      const required = el.required || el.getAttribute("aria-required") === "true";
      if (!required || el.disabled) continue;
      if (!isVisible(el) && el.type !== "hidden" && el.tagName !== "SELECT") continue;

      if (el.type === "radio" || el.type === "checkbox") {
        const key = el.name || el.id;
        if (!key || seenGroups.has(key)) continue;
        seenGroups.add(key);
        const group = querySelectorAllDeep(`input[name="${CSS.escape(key)}"]`);
        if (!group.some(g => g.checked)) {
          blockers.push(`required: ${fieldLabelFor(el)}`);
        }
        continue;
      }
      if (el.type === "file") {
        if (!el.files || el.files.length === 0) blockers.push(`required file: ${fieldLabelFor(el)}`);
        continue;
      }
      if (!String(el.value || "").trim()) blockers.push(`required: ${fieldLabelFor(el)}`);
    }

    return { ok: blockers.length === 0, blockers: blockers.slice(0, 12) };
  }

  // Short human-readable name for a control, for blocker messages.
  function fieldLabelFor(el) {
    const raw = labelForText(el) || el.getAttribute("aria-label") || el.placeholder
             || el.name || el.id || "field";
    return cleanText(raw).slice(0, 60) || "field";
  }

  // Click Submit only when validation passes. Opt-in via opts.autoSubmit.
  // Returns { submitted, blockers }.
  function maybeAutoSubmit(opts) {
    if (!opts || !opts.autoSubmit) return { submitted: false, blockers: [] };
    const { ok, blockers } = validateBeforeSubmit();
    if (!ok) return { submitted: false, blockers };
    const btn = findSubmitButton();
    if (!btn) return { submitted: false, blockers: ["no submit button found"] };
    if (btn.disabled) return { submitted: false, blockers: ["submit button is disabled"] };
    simulateClick(btn);
    return { submitted: true, blockers: [] };
  }

  function highlightSubmitButton(opts) {
    const btn = findSubmitButton();
    if (!btn) return;
    // Pulse outline animation
    const prev = btn.style.cssText;
    btn.style.cssText = prev
      + ";outline:3px solid #198754;outline-offset:3px;"
      + "transition:outline-color 0.3s;animation:__jt_submit_pulse 1.5s ease-in-out 3";
    // Inject keyframes once
    if (!document.getElementById("__jt_submit_pulse_style")) {
      const style = document.createElement("style");
      style.id = "__jt_submit_pulse_style";
      style.textContent = "@keyframes __jt_submit_pulse { 0%, 100% { outline-color: #198754; } 50% { outline-color: #20c997; } }";
      document.head.appendChild(style);
    }
    // Clear the outline after 5s so the form looks normal again
    setTimeout(() => { try { btn.style.cssText = prev; } catch (_) {} }, 5000);

    // Auto-log on Submit click — fire-and-forget POST to the tracker so the
    // job appears under "Applied" without the user touching the app. The
    // listener uses capture: true so it runs BEFORE the form's default
    // submit handler, and keepalive: true so the POST survives the page
    // navigation away from the ATS. The server dedupes by (user_id, url)
    // so a re-click on the same form doesn't double-log.
    if (opts && opts.autologUrl && !btn.__jt_autolog_armed) {
      btn.__jt_autolog_armed = true;
      btn.addEventListener("click", () => {
        try {
          fetch(opts.autologUrl, {
            method: "POST",
            credentials: "include",
            keepalive: true,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              url:        location.href,
              page_title: (document.title || "").slice(0, 240),
              h1:         (document.querySelector("h1")?.innerText || "").slice(0, 240),
              hostname:   location.hostname,
              source:     "autofill",
            }),
          }).catch(() => {});
        } catch (_) {}
      }, { capture: true, once: true });
    }
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
    const fields = querySelectorAllDeep("input, select, textarea").filter(isFillable);
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
          .map(r => {
            // Prefer the radio's own visible label (e.g. "Yes"/"No") over the
            // value attribute — some ATSes (Workable) use random hash IDs as
            // option values, which the AI can't reason about or match back.
            const ownLabel = (r.closest("label")?.textContent
              || labelForText(r)
              || "");
            return cleanText(ownLabel) || r.value;
          })
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

  // ── Learn pass ─────────────────────────────────────────────────────────────
  // After the user manually answers the fields the engine missed, capture
  // those (question, answer) pairs so they can be merged into the server-side
  // qa_defaults — Pass 1's tryQADefaults then fills them automatically on the
  // next form that asks the same thing, and the AI gets them as grounding for
  // similar-but-differently-worded questions.

  // probeText joins label/name/id/placeholder with " | ". The human-readable
  // question is the segment that generalizes across sites (names/ids don't) —
  // pick the longest segment that looks like words.
  function bestQuestionSegment(text) {
    const segs = String(text || "").split("|").map(s => cleanText(s)).filter(Boolean);
    let best = "";
    for (const s of segs) {
      const words = s.split(/\s+/).length;
      if ((words >= 2 || s.length >= 12) && s.length > best.length) best = s;
    }
    return (best || segs[0] || "").slice(0, 160);
  }

  function collectLearnableAnswers(profile) {
    const rules = RULES(profile || {});
    const qa    = (profile && profile.qa_defaults) || [];
    const seenRadio = new Set();
    const out = [];
    const fields = querySelectorAllDeep("input, select, textarea").filter(isFillable);
    for (const el of fields) {
      const tag = el.tagName;
      if (el.type === "password" || el.type === "checkbox" || el.type === "search") continue;
      let value = "";
      if (el.type === "radio") {
        if (!el.name || seenRadio.has(el.name)) continue;
        seenRadio.add(el.name);
        const scope = el.form || document;
        const group = Array.from(scope.querySelectorAll(
          `input[type="radio"][name="${CSS.escape(el.name)}"]`));
        if (group.some(g => g.getAttribute("data-jt-autofilled"))) continue;
        const checked = group.find(g => g.checked);
        if (!checked) continue;
        const ownLabel = (checked.closest("label")?.textContent
          || labelForText(checked)
          || "");
        value = cleanText(ownLabel) || checked.value;
      } else {
        if (el.getAttribute("data-jt-autofilled")) continue;
        if (tag === "SELECT") {
          const o = el.options && el.options[el.selectedIndex];
          if (!o || !(o.value || "").trim()) continue;  // placeholder option
          value = cleanText(o.text || o.value);
        } else {
          value = (el.value || "").trim();
        }
      }
      if (!value || value.length > 300) continue;
      const probe = probeText(el);
      if (!probe || probe.trim().length < 3) continue;
      if (matchRule(rules, probe)) continue;  // identity/contact — profile already covers it
      const lower = probe.toLowerCase();
      if (qa.some(e => Array.isArray(e) && e[0]
            && lower.includes(String(e[0]).toLowerCase().trim()))) continue;  // already learned
      if (BUILT_IN_QA.some(([re]) => re.test(probe))) continue;
      const question = bestQuestionSegment(probe);
      if (!question || question.length < 8) continue;
      out.push([question, value]);
    }
    return out;
  }

  // Below this confidence the AI is guessing (personality/preference questions,
  // ambiguous labels) — leave the field blank and flag it for a manual answer.
  const MIN_AI_CONFIDENCE = 0.6;

  async function applyAiFills(fills, opts) {
    if (!Array.isArray(fills)) return { applied: 0, skipped: 0 };
    let applied = 0, skipped = 0;
    for (const f of fills) {
      if (!f || !f.id) { skipped++; continue; }
      if (f.skip || f.value === "" || f.value == null) { skipped++; continue; }
      const tagged = document.querySelector(`[data-jt-id="${CSS.escape(f.id)}"]`);
      if (!tagged) { skipped++; continue; }
      if (typeof f.confidence === "number" && f.confidence < MIN_AI_CONFIDENCE) {
        skipped++;
        // Radios/checkboxes/buttons are too small for an outline to be seen —
        // flag the whole question container so the skipped field is findable.
        const hl = (tagged.type === "radio" || tagged.type === "checkbox" || tagged.tagName === "BUTTON")
          ? (tagged.closest("fieldset,[role='radiogroup'],div,label") || tagged)
          : tagged;
        try {
          hl.style.outline = "3px solid #f59e0b";
          hl.style.outlineOffset = "2px";
          setTimeout(() => { hl.style.outline = ""; hl.style.outlineOffset = ""; }, 15000);
        } catch (_) {}
        continue;
      }
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

  window.__jobTrackerAutofill = {
    run, collectUnfilledFields, applyAiFills, collectLearnableAnswers,
    validateBeforeSubmit, findSubmitButton,
    // Called by the popup AFTER the AI phase has had its turn — submitting
    // straight from run() would fire before those fields were filled.
    submitIfComplete: () => maybeAutoSubmit({ autoSubmit: true }),
  };
})();
