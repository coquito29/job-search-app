/* Presentation and motion for the workspace. Application behavior stays in the app. */
(function () {
  'use strict';

  const pages = {
    today: {
      label: 'Overview',
      eyebrow: 'A little progress, every day',
      title: 'Make your next move.',
      description: 'Your search, your progress, and the opportunities worth a closer look.',
      primary: 'Explore jobs',
      primaryIcon: 'bi-search',
      secondary: 'Update your profile',
      secondaryIcon: 'bi-person-circle',
      sections: ['todaySection'],
    },
    jobs: {
      label: 'Find jobs',
      eyebrow: 'Good work starts with a good match',
      title: 'Find your next opportunity.',
      description: 'Discover roles that fit your skills and take the next step with confidence.',
      primary: 'Search for jobs',
      primaryIcon: 'bi-search',
      secondary: 'Edit search profile',
      secondaryIcon: 'bi-sliders',
      sections: ['searchSection', 'resultsSection'],
    },
    tracker: {
      label: 'Applications',
      eyebrow: 'Keep your momentum',
      title: 'Every step, in one place.',
      description: 'Keep your applications, follow-ups, and next steps together.',
      primary: 'Find more opportunities',
      primaryIcon: 'bi-search',
      secondary: 'Update your profile',
      secondaryIcon: 'bi-person-circle',
      sections: ['trackerSection', 'statsSection'],
    },
    setup: {
      label: 'My profile',
      eyebrow: 'Your next chapter starts here',
      title: 'Bring your experience to life.',
      description: 'Build a profile that helps every application tell your story.',
      primary: 'Upload a CV',
      primaryIcon: 'bi-cloud-arrow-up',
      secondary: 'Explore jobs',
      secondaryIcon: 'bi-search',
      sections: ['tokenSection', 'cvSection', 'workHistorySection', 'answerLibrarySection'],
    },
  };

  const motionPreference = typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  const animations = new Set();
  const seenCards = new Set();
  const seenElements = new WeakSet();
  let activeTransition = null;
  let entryFrame = null;
  let metricsFrame = null;
  let tab = 'today';
  let initialized = false;

  function reducedMotion() {
    return !!(motionPreference && motionPreference.matches);
  }

  function reportError(error) {
    if (typeof window.reportError === 'function') {
      window.reportError(error);
    } else {
      // Surface asynchronous render errors through the browser's normal error channel.
      window.setTimeout(function () { throw error; }, 0);
    }
  }

  function stopAnimations() {
    if (entryFrame !== null) window.cancelAnimationFrame(entryFrame);
    entryFrame = null;
    animations.forEach(function (animation) { animation.cancel(); });
    animations.clear();
  }

  function skipTransition(record) {
    if (record && record.transition && typeof record.transition.skipTransition === 'function') {
      record.transition.skipTransition();
    }
  }

  function transition(render, animate = true) {
    if (typeof render !== 'function') throw new TypeError('A workspace transition needs a render callback.');
    stopAnimations();

    // The browser schedules its snapshot callback asynchronously. Flush it before
    // a newer navigation so a delayed callback can never restore an older tab.
    const previous = activeTransition;
    if (previous) {
      try {
        previous.run();
      } catch (error) {
        reportError(error);
      } finally {
        skipTransition(previous);
      }
    }

    if (!animate || reducedMotion() || typeof document.startViewTransition !== 'function') {
      activeTransition = null;
      return render();
    }

    const record = { rendered: false, transition: null, run: null };
    record.run = function () {
      if (record.rendered) return;
      record.rendered = true;
      return render();
    };
    activeTransition = record;
    try {
      record.transition = document.startViewTransition(record.run);
    } catch (error) {
      activeTransition = null;
      // If a synchronous implementation already invoked the callback, preserve
      // its error. Otherwise native motion failed; still render synchronously.
      if (record.rendered) throw error;
      return record.run();
    }

    // A skipped transition may reject ready even when the render succeeded.
    if (record.transition.ready) record.transition.ready.catch(function () {
      if (activeTransition === record && record.rendered && !reducedMotion()) animateEntry(tab);
    });
    if (record.transition.updateCallbackDone) {
      record.transition.updateCallbackDone.catch(reportError);
    }
    if (record.transition.finished) {
      record.transition.finished.then(function () {
        if (activeTransition === record) activeTransition = null;
      }, function () {
        if (activeTransition === record) activeTransition = null;
      });
    }
    return record.transition;
  }

  function visible(element) {
    return !!(element && element.isConnected && !element.closest('.tab-hidden')
      && element.getClientRects().length);
  }

  function animateElements(elements) {
    if (reducedMotion()) return;
    let position = 0;
    elements.forEach(function (element) {
      if (!visible(element) || typeof element.animate !== 'function') return;
      const animation = element.animate([
        { opacity: 0, transform: 'translateY(12px)' },
        { opacity: 1, transform: 'translateY(0)' },
      ], {
        duration: 280,
        delay: Math.min(position++ * 30, 120),
        easing: 'cubic-bezier(.22, 1, .36, 1)',
        fill: 'both',
      });
      animations.add(animation);
      if (animation.finished) {
        animation.finished.then(function () {
          animations.delete(animation);
          animation.cancel();
        }, function () {
          animations.delete(animation);
        });
      }
    });
  }

  function animateEntry(name) {
    if (entryFrame !== null) window.cancelAnimationFrame(entryFrame);
    if (reducedMotion()) return;
    entryFrame = window.requestAnimationFrame(function () {
      entryFrame = null;
      if (name !== tab || reducedMotion()) return;
      const sections = pages[name].sections.map(function (id) { return document.getElementById(id); });
      const quickActions = Array.from(document.querySelectorAll(
        '.quick-action-card, .quick-action, .workspace-quick-action, [data-workspace-enter]'
      ));
      animateElements(Array.from(new Set(sections.concat(quickActions))));
    });
  }

  function setText(id, text) {
    const element = document.getElementById(id);
    if (element && element.textContent !== text) element.textContent = text;
  }

  function setButton(id, label, iconClass) {
    const button = document.getElementById(id);
    if (!button) return;
    const icon = document.createElement('i');
    icon.className = 'bi ' + iconClass;
    icon.setAttribute('aria-hidden', 'true');
    const text = document.createElement('span');
    text.textContent = label;
    const arrow = document.createElement('i');
    arrow.className = 'bi bi-arrow-up-right';
    arrow.setAttribute('aria-hidden', 'true');
    button.replaceChildren(icon, text, arrow);
  }

  function goTo(name) {
    if (typeof showTab === 'function') showTab(name);
  }

  function uploadCv() {
    const area = document.getElementById('uploadArea');
    const input = document.getElementById('cvFileInput');
    if (area) area.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'center' });
    if (input) {
      input.focus({ preventScroll: true });
      input.click();
    }
  }

  function updatePage(name) {
    tab = pages[name] ? name : 'today';
    const page = pages[tab];
    setText('workspaceBreadcrumb', page.label);
    setText('workspaceTitle', page.title);
    setText('workspaceDescription', page.description);
    setText('heroEyebrow', page.eyebrow);
    setButton('heroPrimaryBtn', page.primary, page.primaryIcon);
    setButton('heroSecondaryBtn', page.secondary, page.secondaryIcon);
    const primary = document.getElementById('heroPrimaryBtn');
    const secondary = document.getElementById('heroSecondaryBtn');
    if (primary) primary.onclick = function () {
      if (tab === 'setup') uploadCv();
      else if (tab === 'jobs' && typeof searchJobs === 'function') searchJobs();
      else goTo('jobs');
    };
    if (secondary) secondary.onclick = function () { goTo(tab === 'setup' ? 'jobs' : 'setup'); };
  }

  function badgeCount(id) {
    const badge = document.getElementById(id);
    const value = badge ? Number.parseInt(badge.textContent, 10) : 0;
    return Number.isFinite(value) ? value : 0;
  }

  function updateMetrics() {
    let pool = [];
    let ready = 0;
    try {
      if (typeof allJobs !== 'undefined' && Array.isArray(allJobs)) pool = allJobs;
      if (typeof applyReady === 'function') ready = pool.filter(applyReady).length;
    } catch (_) { /* The app may still be initializing its state. */ }
    const counts = {
      overviewMatches: pool.length,
      overviewReady: ready,
      overviewApplications: badgeCount('trackerBadge'),
      overviewCvs: badgeCount('cvCountBadge'),
    };
    Object.keys(counts).forEach(function (id) {
      setText(id, counts[id].toLocaleString());
    });
  }

  function scheduleMetrics() {
    if (metricsFrame !== null) return;
    metricsFrame = window.requestAnimationFrame(function () {
      metricsFrame = null;
      updateMetrics();
    });
  }

  function cardKey(card) {
    const urlElement = card.querySelector('[data-url], a[href]');
    const title = card.querySelector('.job-title');
    const meta = card.querySelector('.job-meta');
    const url = urlElement && (urlElement.getAttribute('data-url') || urlElement.getAttribute('href'));
    return (card.classList.contains('tracker-card') ? 'tracker:' : 'job:')
      + (url || ((title && title.textContent) || '') + '|' + ((meta && meta.textContent) || ''));
  }

  function animateNewCards(records) {
    const cards = [];
    records.forEach(function (record) {
      record.addedNodes.forEach(function (node) {
        if (node.nodeType !== 1) return;
        if (node.matches('.job-card, .tracker-card')) cards.push(node);
        node.querySelectorAll('.job-card, .tracker-card').forEach(function (card) { cards.push(card); });
      });
    });
    const newCards = cards.filter(function (card) {
      if (seenElements.has(card)) return false;
      seenElements.add(card);
      const key = cardKey(card);
      if (seenCards.has(key)) return false;
      seenCards.add(key);
      return true;
    });
    // Native snapshots already animate cards inserted as part of tab navigation.
    if (!activeTransition) animateElements(newCards);
  }

  function initialize() {
    if (initialized) return;
    initialized = true;
    let initialTab = tab;
    try { if (typeof currentTab !== 'undefined') initialTab = currentTab; } catch (_) {}
    updatePage(initialTab);
    setText('workspaceDate', new Date().toLocaleDateString(undefined, {
      weekday: 'short', month: 'short', day: 'numeric',
    }));
    updateMetrics();

    if (typeof MutationObserver === 'function') {
      ['trackerBadge', 'cvCountBadge', 'todayStats', 'jobsList', 'trackerList'].forEach(function (id) {
        const element = document.getElementById(id);
        if (!element) return;
        const observer = new MutationObserver(function (records) {
          scheduleMetrics();
          if (id === 'jobsList' || id === 'trackerList') animateNewCards(records);
        });
        observer.observe(element, { childList: true, characterData: true, subtree: true });
      });
    }
    if (!activeTransition) animateEntry(tab);
  }

  document.addEventListener('workspace:tabchange', function (event) {
    const detail = event.detail || {};
    updatePage(detail.tab);
    scheduleMetrics();
    if (!activeTransition) animateEntry(tab);
  });

  function onMotionChange() {
    if (!reducedMotion()) return;
    stopAnimations();
    skipTransition(activeTransition);
  }
  if (motionPreference) {
    if (typeof motionPreference.addEventListener === 'function') {
      motionPreference.addEventListener('change', onMotionChange);
    } else if (typeof motionPreference.addListener === 'function') {
      motionPreference.addListener(onMotionChange);
    }
  }

  window.workspaceUI = { transition: transition };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, { once: true });
  } else {
    initialize();
  }
})();
