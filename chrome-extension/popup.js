// Job Search App — Auto-fill (popup)
// Talks to /api/profile/full and forwards profile data to the active tab's content script.

const statusEl  = document.getElementById('status');
const fillBtn   = document.getElementById('fillBtn');
const loginLink = document.getElementById('loginLink');
const resultsEl = document.getElementById('results');

function setStatus(msg, kind) {
  statusEl.textContent = msg;
  statusEl.className = 'status status-' + (kind || 'info');
}

function getSelectedServer() {
  const checked = document.querySelector('input[name="server"]:checked');
  return checked ? checked.value : 'https://job-search-app-9pnx.onrender.com';
}

// Restore the last-used server choice
chrome.storage.local.get(['server'], (data) => {
  if (data.server) {
    const radio = document.querySelector(`input[name="server"][value="${data.server}"]`);
    if (radio) radio.checked = true;
  }
  checkAuth();
});

// Save server choice on change
document.querySelectorAll('input[name="server"]').forEach(r =>
  r.addEventListener('change', () => {
    chrome.storage.local.set({ server: r.value });
    checkAuth();
  })
);

async function checkAuth() {
  const base = getSelectedServer();
  loginLink.style.display = 'none';
  fillBtn.disabled = true;
  setStatus('Checking session…', 'info');

  try {
    const r = await fetch(base + '/api/auth/status', { credentials: 'include' });
    if (!r.ok) throw new Error('Server returned ' + r.status);
    const s = await r.json();
    if (s.logged_in) {
      setStatus('Logged in — ready to fill', 'ok');
      fillBtn.disabled = false;
    } else if (s.has_passcode) {
      setStatus('Not signed in. Open the app and unlock first.', 'warn');
      loginLink.href = base;
      loginLink.style.display = 'block';
    } else {
      setStatus('No passcode set — first-run setup needed', 'warn');
      loginLink.href = base;
      loginLink.style.display = 'block';
    }
  } catch (e) {
    setStatus('Cannot reach API: ' + e.message, 'error');
    loginLink.href = base;
    loginLink.style.display = 'block';
  }
}

fillBtn.addEventListener('click', async () => {
  const base = getSelectedServer();
  setStatus('Fetching profile…', 'info');
  fillBtn.disabled = true;
  resultsEl.innerHTML = '';

  let profile;
  try {
    const r = await fetch(base + '/api/profile/full', { credentials: 'include' });
    if (r.status === 401) {
      setStatus('Locked. Open the app and log in first.', 'warn');
      loginLink.href = base;
      loginLink.style.display = 'block';
      fillBtn.disabled = false;
      return;
    }
    if (!r.ok) throw new Error('Server returned ' + r.status);
    profile = await r.json();
    // Cache for the content script's auto-fill (it can't fetch the API itself
    // — credentials+CORS — so the popup is the only place that can refresh it).
    chrome.storage.local.set({ profile, fetchedAt: Date.now() });
  } catch (e) {
    setStatus('Failed to fetch profile: ' + e.message, 'error');
    fillBtn.disabled = false;
    return;
  }

  // Send profile to the active tab's content script
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) {
    setStatus('No active tab', 'error');
    fillBtn.disabled = false;
    return;
  }

  setStatus('Filling form…', 'info');
  try {
    const result = await chrome.tabs.sendMessage(tab.id, { type: 'fill', profile });
    if (!result) throw new Error('No response from page (content script may not be loaded — try reloading the ATS page)');
    if (result.error) throw new Error(result.error);

    const aiFilled = (result.ai && result.ai.filled) ? result.ai.filled : 0;
    const ruleFilled = result.filled - aiFilled;
    let line = `Filled ${ruleFilled} of ${result.found} fields`;
    if (aiFilled) line += ` (+${aiFilled} AI)`;
    setStatus(line, result.filled > 0 ? 'ok' : 'warn');
    renderResults(result);
  } catch (e) {
    setStatus('Fill failed: ' + e.message, 'error');
  } finally {
    fillBtn.disabled = false;
  }
});

function renderResults(result) {
  const rows = [];
  for (const m of result.matches || []) {
    const tag = m.source === 'ai' ? ' <span class="ai-tag">AI</span>' : '';
    rows.push(`<div class="row"><strong>${escapeHtml(m.label || m.name || '(unlabeled)')}</strong>${tag} → ${escapeHtml(String(m.value).slice(0, 60))}</div>`);
  }
  for (const m of result.skipped || []) {
    rows.push(`<div class="row skipped"><em>${escapeHtml(m.label || m.name || '(unlabeled)')}</em> — ${escapeHtml(m.reason || 'no match')}</div>`);
  }
  // AI-pass status footer (only when AI was tried)
  if (result.ai && result.ai.tried) {
    const reason = result.ai.reason || result.ai.error || '';
    if (reason) {
      rows.push(`<div class="row ai-status">AI: ${escapeHtml(reason)}</div>`);
    } else {
      rows.push(`<div class="row ai-status">AI: ${result.ai.filled} field${result.ai.filled === 1 ? '' : 's'} filled, ${result.ai.skipped.length} skipped</div>`);
    }
  }
  resultsEl.innerHTML = rows.join('') || '<div class="row">No fields detected on this page.</div>';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}
