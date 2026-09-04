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
        // "References (Name, Company, and Contact Info)" got the applicant's
        // own name (seen live on dfn.bamboohr.com 2026-08-31): the old
        // `reference's name` alternative never matched the bare plural, so
        // match `reference`/`references` on their own.
        skipIf: /\b(referr?(ed|al)|references?|referred[\s_-]*by|emergency[\s_-]*contact|supervisor|manager'?s?[\s_-]*name|next[\s_-]*of[\s_-]*kin|spouse|guardian)\b/i,
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

      // "Current location" (Lever, Ashby) — the applicant's own whereabouts,
      // built from the address so it fills even before any answer is saved.
      // Must NOT claim "Which location are you applying for?" (Breezy) —
      // that's the employer's site picker, not the applicant's location.
      { value: [addr.city, addr.state_full || addr.state].filter(Boolean).join(", "),
        skipIf: /\b(which|preferred|office|store|branch)[\s_-]*location\b|\blocation\b[^?]{0,40}\bapplying\b|\bapplying\b[^?]{0,40}\blocation\b/i,
        patterns: [/\b(current|your)[\s_-]*location\b/i] },

      { value: p.linkedin,         patterns: [/\blinkedin\b/i] },
      { value: p.portfolio,        patterns: [/\b(portfolio|website|personal[\s_-]*site|homepage|github)\b/i, /\burl\b/i] },

      // Demographic / EEO answers — these are usually selects/radios; the
      // dropdown matcher below tries to find an <option> whose text contains
      // this value.
      // Skipped for need-sponsorship questions: "...need sponsorship to renew
      // your AUTHORIZATION TO WORK in the US?" contains the authorization
      // wording but is asking the opposite thing, and answering "Yes" there
      // says the applicant needs sponsorship.
      { value: ans.work_authorized_us,
        skipIf: (text) => polarityClass(text) === "needs_sponsorship",
        patterns: [/\bauthor(i[sz]ed|i[sz]ation)\b.*\b(work|us|u\.s\.)\b/i, /\beligible\b.*\bwork\b.*\bus\b/i, /\bus[\s_-]*work[\s_-]*author/i] },
      // "Are you ABLE to work in the US for any employer WITHOUT sponsorship?"
      // is the positive framing — the answer is work_authorized_us ("Yes"),
      // NOT sponsorship_needed ("No"). It must be matched before the
      // sponsorship rule below, whose bare /\bsponsor(ship)?\b/ pattern
      // otherwise claims it and answers "No" — i.e. "I am not allowed to
      // work here", an instant rejection. Skipped when the question is
      // actually asking whether sponsorship is NEEDED.
      { value: ans.work_authorized_us,
        skipIf: (text) => polarityClass(text) === "needs_sponsorship",
        patterns: [/\b(able|allowed|eligible|permitted|legally)\b[^?]{0,80}\bwork\b/i] },
      { value: ans.sponsorship_needed,
        skipIf: (text) => polarityClass(text) === "authorized_to_work",
        patterns: [/\bsponsor(ship)?\b/i, /\bvisa\b.*\bsponsor/i, /\brequire[\s_-]*sponsor/i] },
      { value: ans.veteran_status,           patterns: [/\bveteran\b/i] },
      { value: ans.disability,               patterns: [/\bdisab(il)ity\b/i, /\bdisabled\b/i] },
      { value: ans.gender,                   patterns: [/\bgender\b/i, /\bsex\b/i] },
      { value: ans.hispanic_latino,          patterns: [/\bhispanic\b/i, /\blatino\b/i, /\blatinx\b/i] },
      { value: ans.race,                     patterns: [/\brace\b/i, /\bethnicity\b/i, /\bethnic\b/i] },
      { value: ans.salary_expectation,       patterns: [/\b(salary|compensation|pay)[\s_-]*(expectation|requirement|range|desired)\b/i, /\bdesired[\s_-]*(salary|pay|compensation|wage)\b/i, /\bexpected[\s_-]*(salary|pay|compensation)\b/i] },
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

  // Parked entirely off the page's canvas (Breezy renders honeypot inputs at
  // x:-9999). Bots that fill them are auto-rejected, so they must never be
  // filled, counted, or sent to the AI phase. Positions are viewport-relative,
  // so add the scroll offset — a legitimate field scrolled above the fold is
  // NOT offscreen.
  function isOffscreen(el) {
    const rect = el.getBoundingClientRect();
    return rect.right + window.scrollX <= 0 || rect.bottom + window.scrollY <= 0;
  }

  function isFillable(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.type === "hidden" || el.type === "file" || el.type === "submit"
        || el.type === "button" || el.type === "reset" || el.type === "image") return false;
    if (!el.offsetParent && el.type !== "radio" && el.type !== "checkbox") return false;
    if (isOffscreen(el)) return false;
    return true;
  }

  function isVisible(el) {
    if (!el) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== "fixed") return false;
    if (isOffscreen(el)) return false;
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
    // Focus first. Ashby rejected a submit with "Missing entry for required
    // field: Name / Phone Number" while both inputs visibly held the right
    // text (Cyberhaven, 2026-09-01); retyping the identical value by hand
    // fixed it. Its form state commits on the focus/blur cycle, which nothing
    // here was producing -- so the application looked complete and submitted
    // as empty. Cheap enough to do for every field.
    try { el.focus({ preventScroll: true }); } catch (_) { try { el.focus(); } catch (_) {} }
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
    // ...and blur, which is where form libraries mark a field touched and
    // copy it into their own state.
    el.dispatchEvent(new Event("blur", { bubbles: false, composed: true }));
    el.dispatchEvent(new FocusEvent("focusout", { bubbles: true, composed: true }));
    try { el.blur(); } catch (_) {}
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
    const rawTokens = want.replace(/[^a-z0-9]+/g, " ").trim().split(/\s+/).filter(Boolean);

    // A saved answer often NAMES the thing it denies: "I am NOT a veteran"
    // contains "veteran". The regex below is a plain OR over every token, so
    // the checkbox labelled "Veteran" matched its own negation and got ticked
    // on a real application (Cyberhaven, 2026-09-01) -- a false claim of
    // protected status. The filler words are just as bad: the token "not"
    // matches an option called "Not Listed", and "a"/"i"/"am" match almost
    // anything. Drop them, and decide the polarity separately.
    const FILLER = new Set(["i", "im", "am", "a", "an", "the", "is", "are", "was",
      "have", "has", "had", "do", "does", "did", "my", "me", "of", "to", "in", "on",
      "at", "for", "and", "or", "with", "as", "that", "this", "it", "be", "been",
      "not", "no", "never", "none", "dont", "doesnt", "cannot", "cant", "nor",
      "na", "prefer", "answer", "status", "please", "select", "all", "apply"]);
    const NEGATED_RE = /\b(?:not|no|never|none|non|dont|doesn'?t|do not|does not|n\/a|neither)\b/i;
    const isNegated = NEGATED_RE.test(want);

    // Protected characteristics are only ever ticked from an explicitly
    // affirmative answer -- never inferred from token overlap.
    const PROTECTED_OPTION_RE = /\b(veterans?|disabilit(?:y|ies)|disabled|neurodiverse|neurodivergent|pregnan\w*|refugees?|immigrants?|lgbtq?\+?|transgender|non-?binary)\b/i;

    const wantTokens = rawTokens.filter(t => !FILLER.has(t));
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
        //
        // The ADJACENT sibling label wins over the document-wide [for]
        // lookup: sloppy forms stamp the same id on every radio in the
        // group, and getElementById-style resolution then hands every
        // radio the FIRST label in the document — so a "No" radio reads
        // as "Yes" and the group never fills.
        const adjacent = [r.previousElementSibling, r.nextElementSibling]
          .filter(s => s && s.tagName === "LABEL")
          .filter(s => { const f = s.getAttribute("for"); return !f || f === r.id; })
          .map(s => s.textContent).join(" ").trim();
        const ownLabel = (adjacent || labelForText(r))
          + " " + (r.closest("label")?.textContent || "")
          + " " + (r.value || "");
        if (wordBoundaryRe.test(ownLabel)) {
          // The answer mentions this option but denies it -- "I am NOT a
          // veteran" against the box labelled "Veteran". Guarantee it stays
          // clear rather than treating the mention as agreement.
          if (isNegated && r.type === "checkbox") {
            if (r.checked) r.click();
            return true;
          }
          // Never claim a protected characteristic off a fuzzy match; that
          // needs an answer that plainly says yes.
          if (PROTECTED_OPTION_RE.test(ownLabel) && !/^(yes|y|true|1)$/i.test(want)
              && !PROTECTED_OPTION_RE.test(want)) {
            continue;
          }
          if (!r.checked) r.click(); return true;
        }
      }
    }
    // 3b. A sentence answer against a plain Yes/No group. "I am not a
    // protected veteran" shares no token with either label, so the group was
    // left blank and the form came back with a missing required field. The
    // polarity of the sentence is the answer.
    if (group.length === 2) {
      const labelOf = r => (
        (r.closest("label")?.textContent || "") + " " +
        (labelForText(r) || "") + " " + (r.value || "")
      ).trim().toLowerCase();
      const yes = group.find(r => /^\W*(yes|true|1)\b/.test(labelOf(r)));
      const no  = group.find(r => /^\W*(no|false|0)\b/.test(labelOf(r)));
      if (yes && no && yes !== no) {
        const affirmative = /^(yes|y|true|1)$/i.test(want)
          || (!isNegated && /\b(i am|i have|authorized|eligible)\b/i.test(want));
        const target = isNegated ? no : (affirmative ? yes : null);
        if (target) { if (!target.checked) target.click(); return true; }
      }
    }

    // 3c. Affirmative answer to ONE option of a multi-select. applyValue runs
    // per element, so the rule that produced this value matched THIS
    // checkbox's own text -- "Hispanic or LatinX" matching hispanic_latino.
    // A bare "Yes" shares no token with the option label, so steps 1-3 all
    // missed and the box stayed clear on a real EEO form.
    if (el.type === "checkbox" && group.length > 1) {
      if (/^(yes|y|true|1)$/i.test(want)) { if (!el.checked) el.click(); return true; }
      if (/^(no|n|false|0)$/i.test(want)) { if (el.checked)  el.click(); return true; }
    }

    // 4. Lone checkbox answering a yes/no question — "Do you have a valid
    // driver's licence? [x]". There is no sibling to match against and its
    // value attribute is the useless default "on", so steps 1-3 all miss and
    // the box was left permanently unticked. Affirmative answers tick it,
    // negative answers guarantee it stays clear.
    if (el.type === "checkbox" && group.length === 1) {
      const affirmative = /^(yes|y|true|1)$/i.test(want);
      const negative    = /^(no|n|false|0)$/i.test(want);
      if (affirmative) { if (!el.checked) el.click(); return true; }
      if (negative)    { if (el.checked)  el.click(); return true; }
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
    // YYYY → native pickers reject a bare year; January is the convention.
    // Text inputs keep the bare year as-is.
    if (/^\d{4}$/.test(v)) {
      if (t === "date")  return v + "-01-01";
      if (t === "month") return v + "-01";
      return v;
    }
    return v;
  }

  function fillDate(el, value) {
    const normalized = normalizeDate(value, el);
    if (!normalized) return false;
    if (el.value && el.value.trim()) return false;
    setNativeValue(el, normalized);
    // Browsers silently discard malformed values on date/month inputs — a
    // company name landing in a decoy-placeholder date box left value ""
    // while we counted and marked it as filled. Trust the DOM, not the set.
    if (!el.value || !el.value.trim()) return false;
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
      // Try equivalent spellings too — a native State select listing
      // "New Jersey" never matches the profile's "NJ" (and vice versa for
      // country codes). The decoy-select path already did this; native
      // selects need it just as much.
      for (const cand of valueAlternates(v)) {
        if (fillSelect2(el, cand)) return true;
      }
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
    // A single free-text salary box cannot hold a range, and some ATSes strip
    // every non-digit in their own input handler after we write. Breezy turned
    // "$50,000 - $60,000" into "5000060000" — five billion dollars, on a live
    // application. Verified against the real field 2026-09-03: type="text",
    // no inputmode, no pattern, so the numeric guard above cannot see it, and
    // the damage happens after the value lands.
    //
    // Write the low end as a bare number. It survives stripping, and it is the
    // honest reading of "desired salary" — the top of a range is the number a
    // candidate would rather not be held to.
    //
    // This sits at the very bottom deliberately. Placed any earlier it also
    // rewrote the value handed to the combobox path, and a salary DROPDOWN
    // needs the full range to pick the band it falls into ("$50,000 -
    // $59,999") — that is a real assertion in the accuracy suite, and moving
    // this block is what broke it.
    //
    // Plain string tests, no word-boundary escapes: writing this with regex
    // boundaries through a shell heredoc silently produced backspace bytes,
    // the same corruption 1324135 had to undo.
    {
      const probe = `${el.placeholder || ""} | ${el.name || ""} | ${el.id || ""} | ${labelForText(el) || ""}`.toLowerCase();
      const salaryish = probe.includes("salary")
        || probe.includes("compensation")
        || probe.includes("wage");
      if (salaryish && !/^[0-9]+$/.test(String(v).trim())) {
        const tokens = parseMoneyTokens(v);
        if (tokens.length >= 2) {
          return fillTextLike(el, String(Math.round(Math.min(...tokens))));
        }
      }
    }
    return fillTextLike(el, v);
  }

  function matchRule(rules, text) {
    for (const rule of rules) {
      // A rule can veto itself for fields that merely share a keyword —
      // e.g. the bare "name" rule must not fill "name of the person who
      // referred you". Checked before the patterns so the rule is skipped
      // entirely and a later, more specific rule still gets its turn.
      if (rule.skipIf) {
        const veto = typeof rule.skipIf === "function"
          ? rule.skipIf(text)
          : rule.skipIf.test(text);
        if (veto) continue;
      }
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

  // Agreements and marketing opt-ins — never auto-ticked. Kept deliberately
  // narrow so ordinary screener checkboxes ("Do you have a driver's licence?")
  // still fill normally.
  const CONSENT_RE = /\b(i\s+(agree|consent|accept|acknowledge)|privacy\s+polic|terms\s+(and|&|of)\s+(conditions|use|service)|gdpr|data\s+protection|can\s+contact\s+me|contact\s+me\s+(directly\s+)?about|marketing|newsletter|subscribe|promotional|receive\s+(emails|updates|communications|news))\b/i;

  // The element's OWN label only — no ambient sibling text. probeText()
  // deliberately casts a wide net for checkboxes, which means a plain
  // screener box sitting next to a privacy-policy box inherits the word
  // "consent" and would be wrongly skipped. Consent detection must look at
  // the box's own wording and nothing else.
  function ownLabelText(el) {
    const parts = [];
    const forLabel = labelForText(el);
    if (forLabel) parts.push(forLabel);
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      if (p.tagName === "LABEL" && p.textContent) { parts.push(p.textContent); break; }
    }
    const aria = el.getAttribute("aria-label");
    if (aria) parts.push(aria);
    if (el.getAttribute("aria-labelledby")) {
      const root = (el.getRootNode && el.getRootNode()) || document;
      el.getAttribute("aria-labelledby").split(/\s+/).forEach(id => {
        const n = (root.getElementById && root.getElementById(id)) || document.getElementById(id);
        if (n && n.textContent) parts.push(n.textContent);
      });
    }
    return cleanText(parts.join(" "));
  }

  function isConsentControl(el) {
    const own = ownLabelText(el);
    // With no label of its own, fall back to the name/id so machine-named
    // boxes like candidate[consent_given] are still caught.
    const basis = own || `${el.name || ""} ${el.id || ""}`;
    // Machine names separate words with _ - [ ] rather than spaces, and \b
    // treats "_" as a word character — /\bconsent\b/ never matches
    // "candidate_consent". Normalize the separators to spaces first.
    const machine = `${el.name || ""} ${el.id || ""}`.replace(/[_\-\[\]().]+/g, " ");
    return CONSENT_RE.test(basis) || /\bconsent\b/i.test(machine);
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
  function lookupQACandidates(rawText, qaDefaults, savedOnly) {
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
    // Tier 3 — built-in last-resort. Skipped when the caller needs an answer
    // the USER actually gave (consent boxes) rather than a sensible guess.
    if (!savedOnly) {
      for (const [re, value] of BUILT_IN_QA) {
        if (re.test(text)) push(value);
      }
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
                         || /label/i.test(child.className || "")
                         // Ashby renders the question as a bare <div>/<p> with
                         // no label class at all. Accept plain-text siblings
                         // only when they read like a question ("?") and hold
                         // no controls of their own, so ambient prose can't
                         // hijack the group label.
                         || ((tag === "div" || tag === "span" || tag === "p")
                             && !child.querySelector("input,select,textarea,button")
                             && /\?/.test(child.textContent || ""));
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
    if (eduCount < 1 && workCount < 1) return 0;

    let totalClicks = 0;
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], a'))
      .filter(isVisible)
      .filter(b => {
        const t = cleanText(b.innerText || b.textContent || "").toLowerCase();
        if (!t || t.length > 50) return false;
        return ADD_BTN_RE.test(t);
      });

    // Rendered-row count for a section: indexed input names when the ATS
    // uses them, else visible anchor fields (Company / School placeholders).
    // Breezy renders ZERO rows until Add is clicked — the old "assume one
    // pre-rendered row" fallback left every Breezy section a row short.
    const countRows = (isEdu) => {
      const nameRe = isEdu
        ? /\b(?:education|school)[\[_.]+(\d+)/i
        : /\b(?:work_experience|work|employment|experience|job)[\[_.]+(\d+)/i;
      const anchorRe = isEdu ? EDU_ANCHOR_RE : WORK_ANCHOR_RE;
      const idx = new Set();
      let anchors = 0;
      for (const inp of document.querySelectorAll("input")) {
        const m = (inp.name || inp.id || "").match(nameRe);
        if (m) idx.add(parseInt(m[1], 10));
        // Date inputs can carry decoy placeholders (Breezy stamps "Company"
        // on its date boxes) — never count them as row anchors.
        if (/^(date|month|file|radio|checkbox)$/.test(inp.type || "")) continue;
        if (!isVisible(inp)) continue;
        if (anchorRe.test(`${inp.placeholder || ""} | ${inp.name || ""} | ${inp.id || ""}`)) anchors++;
      }
      return Math.max(idx.size, anchors);
    };

    for (const btn of candidates) {
      // The button's own text names its section ("Add Position", "Add
      // Education") — trust that first. Ancestor context is only a fallback
      // for bare "Add another" buttons: the nearest ancestor is often a tiny
      // footer div with no section words at all (Breezy's .section-footer),
      // which used to classify NEITHER section and skip the form entirely.
      const btnText = cleanText(btn.innerText || btn.textContent || "").toLowerCase();
      let isEdu  = /\b(education|school|college|university|degree)\b/.test(btnText);
      let isWork = !isEdu && /\b(position|job|work|employment|experience|role)\b/.test(btnText);
      if (!isEdu && !isWork) {
        const ancestor = btn.closest("fieldset, section, [class*='card'], [class*='section'], [class*='education'], [class*='work'], [class*='experience'], form");
        const context = ((ancestor && ancestor.textContent) || "").toLowerCase().slice(0, 1000);
        isEdu  = /\b(education|school|university|college|degree|graduat)\b/.test(context);
        isWork = !isEdu && /\b(work[\s_-]*(experience|history)|employment[\s_-]*history|previous[\s_-]*(employer|job|position))\b/.test(context);
      }
      if (!isEdu && !isWork) continue;
      const target = isEdu ? eduCount : workCount;
      if (target < 1) continue;

      // Click until the section holds one row per profile entry. Re-counting
      // after every click self-corrects for ATSes that pre-render a blank
      // row; bailing when a click adds nothing prevents runaway loops.
      for (let guard = 0; guard < 6; guard++) {
        const have = countRows(isEdu);
        if (have >= target) break;
        try { btn.click(); } catch (_) { break; }
        totalClicks++;
        // Wait briefly for the new row's DOM to render. ATSes are typically
        // React-based and re-render in <50ms, but slower frameworks need more.
        await new Promise(r => setTimeout(r, 300));
        if (countRows(isEdu) <= have) break;
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

  // ── Positional repeating rows (Breezy-style unindexed sections) ────────────
  // Breezy (and other ATSes) render repeating work-history / education rows
  // with NO indexed names at all — every row is just placeholder="Company",
  // placeholder="Title", two bare date inputs, repeated N times. Pass 0's
  // name parser can't see rows there, and the single-row Pass 1 rules used
  // to stamp work_experience[0].company into EVERY row.
  //
  // Strategy: fields matching a company/school ANCHOR pattern, in DOM order,
  // mark the start of row 0, 1, 2…; the fields between two anchors belong to
  // the earlier row. Within a row, text fields are classified by their own
  // label and date inputs are assigned positionally (first = start date,
  // second = end date) — Breezy's captured date inputs carry a bogus
  // "Company" placeholder, so their labels can't be trusted anyway.
  //
  // Every classified field is marked handled EVEN WHEN the profile has no
  // row for it, so Pass 1 can never cross-fill row 3 with row 0's employer.
  const WORK_ANCHOR_RE = /(?:^|\|)\s*(company|employer|organization|org)\s*\*?\s*(?:\||$)|\b(company|employer|organization)[\s_-]*name\b/i;
  const EDU_ANCHOR_RE  = /(?:^|\|)\s*(school|university|college|institution)\s*\*?\s*(?:\||$)|\bname[\s_-]*of[\s_-]*(school|university|college|institution)\b/i;

  async function fillPositionalRows(profile, fields, handledByRow, opts) {
    let filled = 0;
    const probes = new Map();
    const probeOf = (el) => {
      if (!probes.has(el)) probes.set(el, probeText(el));
      return probes.get(el);
    };
    const isTextish = (el) =>
      (el.tagName === "INPUT"
        && !/^(radio|checkbox|date|month|file|number)$/.test(el.type || "text"))
      || el.tagName === "TEXTAREA";

    for (const section of ["work", "education"]) {
      const anchorRe = section === "work" ? WORK_ANCHOR_RE : EDU_ANCHOR_RE;
      const otherRe  = section === "work" ? EDU_ANCHOR_RE  : WORK_ANCHOR_RE;
      const rows = section === "work"
        ? (profile.work_experience || [])
        : (profile.education || []);
      const anchorIdx = [];
      fields.forEach((el, i) => {
        if (handledByRow.has(el)) return;
        if (!isTextish(el) || el.tagName === "TEXTAREA") return;
        if (anchorRe.test(probeOf(el))) anchorIdx.push(i);
      });
      // One anchor is the single-row case Pass 1 already handles well.
      if (anchorIdx.length < 2) continue;

      for (let k = 0; k < anchorIdx.length; k++) {
        const start = anchorIdx[k];
        // Last row's slice extends as far as the first row's did — repeating
        // rows are uniform, and an open-ended slice would swallow whatever
        // section follows the history block (EEO, references, …).
        const end = k + 1 < anchorIdx.length
          ? anchorIdx[k + 1]
          : Math.min(fields.length, start + (anchorIdx[1] - anchorIdx[0]));
        const row = rows[k];   // may be undefined — still claim the fields
        const dates = [];
        for (let i = start; i < end; i++) {
          const el = fields[i];
          if (handledByRow.has(el)) continue;
          const probe = probeOf(el);
          if (otherRe.test(probe) && i !== start) break; // ran into the other section
          let value;
          let claimed = true;
          if (i === start) {
            value = section === "work" ? row?.company : row?.school;
          } else if (el.type === "date" || el.type === "month") {
            dates.push(el);   // positional — assigned after the loop
            continue;
          } else if (el.tagName === "TEXTAREA"
                     && /\b(summary|description|duties|responsibilit)/i.test(probe)) {
            value = undefined; // nothing in the profile for these — claim, leave blank
          } else if (el.type === "checkbox" && /\bcurrent/i.test(probe)) {
            value = row ? (row.current ? "Yes" : "No") : undefined;
          } else if (!isTextish(el)) {
            claimed = false;
          } else if (section === "work" && /\b(job[\s_-]*)?title\b|\bposition\b|\brole\b/i.test(probe)) {
            value = row?.title;
          } else if (section === "education" && /\bdegree\b/i.test(probe)) {
            value = row?.degree;
          } else if (section === "education" && /\b(major|field[\s_-]*of[\s_-]*study|concentration)\b/i.test(probe)) {
            value = row?.field;
          // Bare "From"/"To"/"Until" only count when they are a field's ENTIRE
          // label segment — as loose words they appear in ordinary prose
          // ("willing to relocate") and would misclassify neighbours.
          } else if (/\b(start|begin)[\s_-]*(date|year|month)?\b/i.test(probe)
                     || /(?:^|\|)\s*from\s*\*?\s*(?:\||$)/i.test(probe)) {
            value = row?.start_date;
          } else if (/\b(end|finish)[\s_-]*(date|year|month)?\b/i.test(probe)
                     || /\blast[\s_-]*day\b/i.test(probe)
                     || /(?:^|\|)\s*(to|until)\s*\*?\s*(?:\||$)/i.test(probe)) {
            value = row?.end_date;
          } else if (section === "work" && /\b(location|city)\b/i.test(probe)) {
            value = row?.location;
          } else {
            claimed = false;  // not a row field — leave it for Pass 1
          }
          if (!claimed) continue;
          handledByRow.add(el);
          if (value && await applyValue(el, value, opts)) filled++;
        }
        for (let d = 0; d < dates.length && d < 2; d++) {
          const el = dates[d];
          handledByRow.add(el);
          const value = d === 0 ? row?.start_date : row?.end_date;
          if (value && await applyValue(el, value, opts)) filled++;
        }
      }
    }
    return filled;
  }

  // ── Greenhouse "value-holder twins" (Pass 1c) ──────────────────────────────
  // Greenhouse's combobox pattern renders each screener question TWICE: a
  // visible input[type=text]#question_<id> that carries the label and takes
  // the typing, and an anonymous input[required] right after it with no id,
  // name, placeholder, aria-* or label of its own. Greenhouse's validation
  // reads the anonymous one — so filling only the visible input looks
  // complete on screen and submits every screener answer EMPTY.
  //
  // Mirror each answered control's value into such a twin. The
  // no-identity-whatsoever requirement is the safety valve: any input a
  // human could be expected to fill directly carries at least a label or a
  // name, so the only things we ever touch are these machine value-holders.
  // Hidden inputs are deliberately excluded — anonymous hidden fields are
  // honeypots/CSRF tokens and must stay untouched.
  function hasOwnIdentity(el) {
    if (el.id || el.name || el.placeholder) return true;
    for (const a of el.attributes) {
      if (a.name.startsWith("aria-")) return true;
    }
    if (labelForText(el)) return true;
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      if (p.tagName === "LABEL") return true;
    }
    return false;
  }

  // The controls a value-holder twin can be found among, in DOM order. Shared
  // with questionLabelFor() so the "twin follows its question" pairing is one
  // rule, not two that can drift apart.
  function valueHolderControls() {
    const textLike = el =>
      el.tagName === "TEXTAREA"
      || (el.tagName === "INPUT" && ["text", "email", "tel", "url", "search", ""].includes(el.type || ""));
    return querySelectorAllDeep("input, textarea")
      .filter(el => !el.disabled && !el.readOnly && el.type !== "hidden" && textLike(el));
  }

  // The QUESTION a blocker should name, not the machine control that holds
  // the answer. A Greenhouse value-holder twin has no id, name, label or
  // aria of its own, so fieldLabelFor() falls through to its last resort and
  // every unanswered screener reported as "required: field". That is
  // unanswerable twice over: it tells the user nothing, and the blockers
  // endpoint drops it because there is no question to attach an answer to --
  // leaving the ATS that stalls most often invisible in that list. The twin's
  // question sits on the visible input immediately before it, the same
  // pairing Pass 1c mirrors values through.
  function questionLabelFor(el) {
    const own = fieldLabelFor(el);
    if (own !== "field" || hasOwnIdentity(el)) return own;
    const controls = valueHolderControls();
    const i = controls.indexOf(el);
    if (i > 0 && hasOwnIdentity(controls[i - 1])) {
      const holder = fieldLabelFor(controls[i - 1]);
      if (holder && holder !== "field") return holder;
    }
    return own;
  }

  function mirrorValueHolderTwins() {
    let mirrored = 0;
    const controls = valueHolderControls();

    for (let i = 1; i < controls.length; i++) {
      const twin = controls[i];
      if (!twin.required) continue;             // the value-holder is always required
      if (String(twin.value || "").trim()) continue;
      if (hasOwnIdentity(twin)) continue;
      // The twin immediately follows the visible question input in DOM order.
      const holder = controls[i - 1];
      if (!hasOwnIdentity(holder)) continue;    // two anonymous inputs in a row — not the pattern
      const value = String(holder.value || "").trim();
      if (!value) continue;
      setNativeValue(twin, value);
      mirrored++;
    }
    return mirrored;
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

    // ── Pass 0.5: positional rows (unindexed repeating sections) ─────────────
    // Catches Breezy-style repeats that carry no row index in their names.
    // Must run before Pass 1 for the same reason Pass 0 does.
    try {
      filled += await fillPositionalRows(profile, fields, handledByRow, opts);
    } catch (_) {}

    // ── Pass 1: native inputs ────────────────────────────────────────────────
    for (const el of fields) {
      if (handledByRow.has(el)) continue;
      // Consent is the applicant's to give. Rules, built-ins, and the AI
      // never tick a privacy-policy / terms / marketing box on their behalf —
      // those are legal and commercial choices, not data entry. The ONE
      // exception: an explicit answer the user SAVED for this question
      // ("Do you agree to receive text messages?" → Yes) is the applicant
      // giving that consent once for every form. A saved "No" or no match
      // leaves the box alone and it surfaces as an auto-submit blocker.
      if ((el.type === "checkbox" || el.type === "radio") && isConsentControl(el)) {
        if (el.type === "checkbox" && !el.checked) {
          const saved = lookupQACandidates(probeText(el), profile.qa_defaults, true);
          if (saved.length && normalizeYesNo(saved[0]) === "yes") {
            simulateClick(el);
            if (el.checked) filled++;
          }
        }
        continue;
      }
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

    // ── Pass 1c: Greenhouse value-holder twins ───────────────────────────────
    // Runs after every text-filling pass so each answered question input can
    // be mirrored into the anonymous required input Greenhouse validates.
    // Runs AGAIN after applyAiFills, for answers the AI phase supplies later.
    try { filled += mirrorValueHolderTwins(); } catch (_) {}

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
    try { autoSubmit = await maybeAutoSubmit(opts); } catch (e) {
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
    // Ashby puts TWO file inputs on an application: a convenience parser at
    // the top ("Autofill from resume - Upload your resume here to autofill key
    // application fields") and the real slot lower down, id="_systemfield_resume".
    // The helper's own copy says "resume" three times, so RESUME_RE matched it
    // first and the CV went into the parser while the required Resume field
    // stayed empty -- every Ashby run came back "required file: Resume"
    // (C1 and Cyberhaven, 2026-09-01). Feeding the parser is also destructive:
    // it rewrites fields the rule passes already filled correctly.
    const AUTOFILL_HELPER_RE = /\bauto[\s_-]*fill\b|\bprefill\b|\bparse[sd]?\b/i;

    // BambooHR (and SmartRecruiters) give EVERY file slot the same useless
    // identity — aria-label "file-input", no name, no id, identical accept
    // list — so neither regex matched and the old "just take the first one"
    // fallback put the CV in the Cover Letter slot and left Resume* empty
    // (seen live on ebq.bamboohr.com 2026-09-01: the form then refused to
    // submit with "Please upload a file"). The section heading above each
    // control is the only thing that distinguishes them, so read that:
    // walk up a few ancestors — crossing shadow boundaries — and take the
    // nearest ancestor whose text is small enough to describe just this
    // field rather than the whole form.
    // Which slot a control belongs to, read off the DOM around it.
    //
    // Taking the nearest non-empty ancestor (the previous rule) does not work
    // on BambooHR: the two innermost wrappers of BOTH file inputs say only
    // "Choose File / No file selected", so cover letter and resume were
    // indistinguishable and first-match took the cover-letter slot -- the very
    // thing that rule was added to prevent. The heading sits two levels up.
    // Verified on ebq.bamboohr.com 2026-09-01: depth 0-1 "Choose File...",
    // depth 2 "Resume* Choose File*...", depth 3 holds both slots at only 110
    // characters, so a length cap alone would not separate them either.
    //
    // So: climb until the text actually NAMES a slot and stop there, keeping
    // the last short text as a fallback. Bailing out once the text has grown
    // past a field's worth stops it swallowing neighbouring controls.
    const SLOT_ID_RE = /\b(resume|cv|curriculum[\s_-]*vitae|cover[\s_-]*letter|portfolio|transcript|photo|certificate|reference)\b/i;
    function slotContext(el) {
      let node = el.parentElement || (el.getRootNode() && el.getRootNode().host) || null;
      let best = "";
      for (let d = 0; d < 8 && node; d++) {
        const t = cleanText(node.innerText || "");
        if (t) {
          if (t.length > 160) break;
          best = t;
          if (SLOT_ID_RE.test(t)) return t;
        }
        node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
      }
      return best;
    }

    // Ranked, not first-match. The field's OWN identity beats the words near
    // it, because surrounding copy is what the Ashby helper hijacks.
    let byIdentity = null;          // name/id says resume  (Ashby: _systemfield_resume)
    const byContext = [];           // nearby heading says resume  (BambooHR)
    const rest = [];                // nothing excluded it
    for (const el of fileInputs) {
      // Underscores and hyphens are word characters, so "_systemfield_resume"
      // has no \b before "resume" and RESUME_RE misses it. Split them.
      const own = `${el.name || ""} ${el.id || ""}`.replace(/[_\-]+/g, " ");
      const ctx = slotContext(el);
      const haystack = `${own} ${probeText(el)} ${el.accept || ""} ${ctx}`;
      if (NOT_RESUME_RE.test(haystack)) continue;
      if (AUTOFILL_HELPER_RE.test(ctx)) continue;
      if (!byIdentity && RESUME_RE.test(own)) { byIdentity = el; continue; }
      if (RESUME_RE.test(haystack)) { byContext.push(el); continue; }
      rest.push(el);
    }
    // BambooHR gives every slot the same empty identity, so it falls through to
    // byContext and the heading walk above still decides it.
    const resumeInput = byIdentity || byContext[0] || rest[0] || null;
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
  // True once a human has completed the challenge: reCAPTCHA / hCaptcha /
  // Turnstile all write their verification token into a response field.
  // We only ever READ this — the token is produced by the user's own tick.
  function captchaSatisfied() {
    for (const sel of ['#g-recaptcha-response',
                       'textarea[name="g-recaptcha-response"]',
                       'textarea[name="h-captcha-response"]',
                       'input[name="cf-turnstile-response"]',
                       'input[name="cf-chl-widget-response"]']) {
      for (const el of querySelectorAllDeep(sel)) {
        if (el && String(el.value || "").trim()) return true;
      }
    }
    return false;
  }

  function validateBeforeSubmit() {
    const blockers = [];

    // Consent boxes the engine deliberately left alone. Teamtailor marks the
    // privacy-policy box "Required." in its LABEL while leaving the input
    // without a required attribute, so the generic required-field sweep below
    // can't see it. Any unticked consent box blocks auto-submit.
    for (const el of querySelectorAllDeep('input[type="checkbox"]')) {
      if (el.disabled || el.checked) continue;
      if (!isConsentControl(el)) continue;
      // Judge "required" from the box's OWN label only. probeText() walks
      // sideways into neighbouring fields, so an optional opt-in sitting
      // near any "*"-marked field inherited the star and blocked every
      // auto-submit on the page (seen on Breezy).
      let basis = ownLabelText(el);
      // Teamtailor puts "Required. I agree to the Privacy Policy..." in a bare
      // <span> BEFORE the input instead of a <label>, so ownLabelText() reads
      // "" and a genuinely required, unticked consent box sailed straight
      // through the guard -- auto-submit fired on a form the ATS would refuse.
      // Fall back to the IMMEDIATELY preceding sibling, and only when that
      // text is itself consent wording: an ordinary opt-in then still cannot
      // inherit a neighbouring field's "*" the way probeText's walk-up did on
      // Breezy, which is the false-positive this guard was narrowed to avoid.
      if (!/\brequired\b|\*/i.test(basis)) {
        const prev = el.previousElementSibling;
        const near = (prev && prev.textContent || "").trim().slice(0, 300);
        if (near && CONSENT_RE.test(near)) basis = near;
      }
      if (/\brequired\b|\*/i.test(basis)) {
        blockers.push(`consent needed: ${fieldLabelFor(el)}`);
      }
    }

    // 1. A CAPTCHA means a human must act; we never attempt to solve one.
    //    But once a human HAS solved it the widget stays on screen, so
    //    presence alone can't keep blocking — otherwise the user ticks the
    //    box and submission is still refused. A non-empty response token is
    //    the challenge's own proof that a person satisfied it.
    const captcha = [
      'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
      'iframe[src*="turnstile"]', ".g-recaptcha", ".h-captcha", "[data-sitekey]",
    ].some(sel => Array.from(document.querySelectorAll(sel)).some(isVisible));
    if (captcha && !captchaSatisfied()) blockers.push("CAPTCHA present — needs a human");

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
          blockers.push(`required: ${questionLabelFor(el)}`);
        }
        continue;
      }
      if (el.type === "file") {
        if (!el.files || el.files.length === 0) blockers.push(`required file: ${fieldLabelFor(el)}`);
        continue;
      }
      if (!String(el.value || "").trim()) blockers.push(`required: ${questionLabelFor(el)}`);
    }

    return { ok: blockers.length === 0, blockers: blockers.slice(0, 12) };
  }

  // Short human-readable name for a control, for blocker messages.
  function fieldLabelFor(el) {
    const raw = labelForText(el) || el.getAttribute("aria-label") || el.placeholder
             || el.name || el.id || "field";
    return cleanText(raw).slice(0, 60) || "field";
  }

  // Visible form-rejection messages an ATS shows AFTER its own client-side
  // validation runs — attribute-level checks can all pass and the submit
  // still bounce (Breezy validates in Angular, not via required attrs).
  const FORM_ERROR_RE = /\b(contains errors|please (agree|correct|fix|complete)|is required|required field|fix the (errors|highlighted)|field is missing)\b/i;

  function visibleFormErrors() {
    const seen = new Set();
    const out = [];
    for (const el of document.querySelectorAll(
        "[class*='error' i], [role='alert'], .help-block, .invalid-feedback")) {
      if (!isVisible(el)) continue;
      const t = cleanText(el.innerText || "").slice(0, 80);
      if (t && FORM_ERROR_RE.test(t) && !seen.has(t)) { seen.add(t); out.push(t); }
    }
    return out;
  }

  // Click Submit only when validation passes. Opt-in via opts.autoSubmit.
  // Returns { submitted, blockers }.
  async function maybeAutoSubmit(opts) {
    if (!opts || !opts.autoSubmit) return { submitted: false, blockers: [] };
    const { ok, blockers } = validateBeforeSubmit();
    if (!ok) return { submitted: false, blockers };
    const btn = findSubmitButton();
    if (!btn) return { submitted: false, blockers: ["no submit button found"] };
    if (btn.disabled) return { submitted: false, blockers: ["submit button is disabled"] };
    simulateClick(btn);
    // The ATS's own validation gets the last word: give it a moment, then
    // look for a visible rejection banner. Reporting "submitted" while the
    // form is still on screen with errors poisons the tracker with
    // applications that never happened.
    await new Promise(r => setTimeout(r, 1500));
    const errors = visibleFormErrors();
    if (errors.length) {
      return { submitted: false,
               blockers: errors.slice(0, 12).map(t => "rejected by form: " + t) };
    }
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
          // Don't log on the click itself — the ATS's validation may still
          // bounce the submit (the "Applied but never applied" badge bug).
          // Log when the page navigates away (a successful multi-page
          // submit; keepalive survives the unload), or after a beat with no
          // visible rejection banner (a successful SPA submit).
          let logged = false;
          const send = () => {
            if (logged) return;
            logged = true;
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
          };
          window.addEventListener("pagehide", send, { once: true });
          setTimeout(() => {
            try {
              if (visibleFormErrors().length) return; // bounced — not applied
            } catch (_) {}
            send();
          }, 1800);
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
    // AI answers land in Greenhouse's visible question inputs — mirror them
    // into their anonymous value-holder twins just like Pass 1c did for the
    // rule-based fills, or the late answers still submit empty.
    try { mirrorValueHolderTwins(); } catch (_) {}
    return { applied, skipped };
  }

  // ── Fixture capture ────────────────────────────────────────────────────────
  // When a run fills almost nothing, the interesting object is the FORM, and it
  // dies with the tab. Greenhouse is solved and Stability AI is not, and the
  // only real difference between them is that one has a fixture. This
  // snapshots the same descriptor set form_fixtures.json already stores, so a
  // page that beat the engine becomes an offline test that can be fixed and
  // re-run for free, instead of costing a real application per guess.
  //
  // STRUCTURE ONLY: type, identity, label, required flag, option TEXT. Never
  // el.value, never el.checked, never anything typed. These uploads leave the
  // page, and an application form holds an address, work history and salary —
  // so nothing that could carry an answer is allowed into the payload.
  function headingAbove(el) {
    let node = el;
    while (node && node !== document.body) {
      let prev = node.previousElementSibling;
      while (prev) {
        if (/^H[1-6]$/.test(prev.tagName || "")) {
          const t = (prev.textContent || "").trim();
          if (t) return t.slice(0, 120);
        }
        prev = prev.previousElementSibling;
      }
      node = node.parentElement;
    }
    return null;
  }

  function captureFormShape(limit) {
    const cap = Math.max(1, Math.min(300, limit || 150));
    const all = querySelectorAllDeep("input, select, textarea");
    const fillable = all.filter(isFillable);
    // When isFillable rejected EVERYTHING, the fillable list is empty and a
    // capture of it says nothing -- which is the no_form case, the one where
    // the engine is most blind and the evidence matters most. Fall back to the
    // raw list and mark what was rejected, so the capture answers "why did the
    // engine see no fields here" instead of just restating that it didn't.
    const usingRaw = fillable.length === 0 && all.length > 0;
    const els = usingRaw ? all : fillable;
    const fillableSet = new Set(fillable);
    const fields = [];
    let section = null;
    for (const el of els.slice(0, cap)) {
      const f = {};
      f.type = el.tagName === "SELECT"
        ? (el.multiple ? "select-multiple" : "select-one")
        : el.tagName === "TEXTAREA"
          ? "textarea"
          : String(el.getAttribute("type") || "text").toLowerCase();
      // An id/name/label of "" is meaningful — Greenhouse's value-holder twins
      // are recognizable ONLY by having no identity at all — so omit the keys
      // rather than writing empty strings the fixture builder would treat as
      // present. buildForm() relies on that same absence.
      if (el.id) f.id = String(el.id).slice(0, 120);
      if (el.name) f.name = String(el.name).slice(0, 120);
      let lbl = "";
      try { lbl = labelForText(el) || ""; } catch (_) { lbl = ""; }
      if (lbl) f.label = lbl.trim().slice(0, 200);
      // labelForText() resolves <label for> and nothing else, but probeText()
      // -- what the MATCHER actually reads -- also follows aria-labelledby.
      // Rippling labels every field that way (aria-labelledby="field-8-label"
      // over a <span>), so all nine of its controls came back label-less on
      // 2026-09-04 and the fixture looked blinder than the page really was.
      // Record it separately: a capture has to show what the engine saw, or
      // triage starts from a form nobody ever met.
      try {
        const lb = el.getAttribute("aria-labelledby");
        if (lb) {
          const root = (el.getRootNode && el.getRootNode()) || document;
          const txt = lb.split(/\s+/)
            .map(id => ((root.getElementById && root.getElementById(id))
                        || document.getElementById(id) || {}).textContent || "")
            .filter(Boolean).join(" ").trim();
          if (txt) f.labelledby = txt.slice(0, 200);
        }
      } catch (_) { /* detached or exotic root */ }
      const aria = el.getAttribute("aria-label");
      if (aria) f.aria = aria.trim().slice(0, 200);
      const ph = el.getAttribute("placeholder");
      if (ph) f.placeholder = ph.trim().slice(0, 200);
      if (el.required || el.getAttribute("aria-required") === "true") f.required = true;
      // The attributes that decide WHICH FILL PATH the engine takes. Without
      // them a capture cannot tell "the engine skipped this" from "the engine
      // wrote a value and the page threw it away" -- and those need opposite
      // fixes. Greenhouse's demographic questions arrive as type="text" backed
      // by a popup listbox, indistinguishable from a plain input without these.
      const role = el.getAttribute("role");
      if (role) f.role = role.slice(0, 40);
      const aac = el.getAttribute("aria-autocomplete");
      if (aac) f.aria_autocomplete = aac.slice(0, 40);
      const ahp = el.getAttribute("aria-haspopup");
      if (ahp) f.aria_haspopup = ahp.slice(0, 40);
      if (el.getAttribute("list")) f.list = true;
      if (el.readOnly) f.readonly = true;
      if (el.disabled) f.disabled = true;
      // SmartRecruiters-style components put no combobox ARIA on the inner
      // input; the host element's tag name is the only signal.
      try {
        const hosts = shadowHostChain(el).map(h => (h.tagName || "").toLowerCase())
                        .filter(Boolean).slice(0, 6);
        if (hosts.length) f.shadow_hosts = hosts;
      } catch (_) { /* not in a shadow root */ }
      // The engine's OWN verdict, which is the fastest way to see a
      // misclassification: a listbox-backed input reported as plain text is
      // the bug, stated directly instead of inferred.
      try { if (isComboboxLike(el)) f.combobox = true; } catch (_) {}
      // Whether the control ended up non-empty -- the BOOLEAN only, never the
      // content. This is the datum that separates "never touched" from
      // "filled, then rejected", and it carries nothing the user typed.
      try {
        const v = el.type === "checkbox" || el.type === "radio"
          ? el.checked : (el.value || "");
        if (v === true || (typeof v === "string" && v.trim() !== "")) f.has_value = true;
      } catch (_) {}
      if (el.tagName === "SELECT") {
        // Option TEXT only. It is authored by the site, not the applicant, and
        // the engine's matching cannot be reproduced offline without it.
        f.options = Array.from(el.options || []).slice(0, 60)
          .map(o => (o.textContent || "").trim().slice(0, 120))
          .filter(Boolean);
      }
      if (!fillableSet.has(el)) f.not_fillable = true;
      const head = headingAbove(el);
      if (head && head !== section) { section = head; f.section = head; }
      fields.push(f);
    }
    return {
      // Both counts, because their gap IS the diagnosis: controls present in
      // the DOM versus controls the engine was willing to touch.
      dom_controls: all.length,
      engine_fillable: fillable.length,
      raw_fallback: usingRaw,
      // Query string dropped: tracking parameters and one-time apply tokens
      // live there, and the path is all the fixture needs to identify the ATS.
      url: String(location.href).split("?")[0].slice(0, 300),
      hostname: location.hostname,
      title: (document.title || "").slice(0, 200),
      field_count: fields.length,
      truncated: els.length > cap,
      fields,
    };
  }

  window.__jobTrackerAutofill = {
    run, collectUnfilledFields, applyAiFills, collectLearnableAnswers,
    validateBeforeSubmit, findSubmitButton, captureFormShape,
    // Called by the popup AFTER the AI phase has had its turn — submitting
    // straight from run() would fire before those fields were filled.
    submitIfComplete: () => maybeAutoSubmit({ autoSubmit: true }),
  };
})();
