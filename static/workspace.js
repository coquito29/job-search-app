/* UI overview and responsive filter disclosure; no search or application side effects. */
function renderWorkspaceOverview() {
  const pool = Array.isArray(allJobs) ? allJobs : [];
  const cvs = Array.isArray(cvLibrary) ? cvLibrary : [];
  const pending = Array.isArray(lastTodayStatus?.pending) ? lastTodayStatus.pending : [];
  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  setText('overviewMatches', pool.length
    ? `${pool.length} saved match${pool.length === 1 ? '' : 'es'} to explore`
    : 'Review matches or start a search');
  setText('overviewCvs', cvs.length
    ? `${cvs.length} CV${cvs.length === 1 ? '' : 's'} in your library`
    : 'Add your CV and build your profile');
  setText('overviewPending', pending.length
    ? `${pending.length} application${pending.length === 1 ? '' : 's'} need${pending.length === 1 ? 's' : ''} your review`
    : 'Track applications and next steps');
}

function updateFilterSummary() {
  const freshness = document.querySelector('.freshness-btn.active');
  const ats = document.querySelector('.ats-filter-btn.active');
  const salary = Number(document.getElementById('salaryMinInput')?.value) || 0;
  const text = [freshness?.textContent.trim(), ats?.textContent.trim(), salary ? `$${salary}k+` : ''].filter(Boolean);
  const summary = document.getElementById('activeFilterSummary');
  if (summary) summary.textContent = text.join(' · ');
}


function syncWorkspaceMotion() {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let paused = false;
  try { paused = localStorage.getItem('workspace_motion_paused') === '1'; } catch (_) {}
  document.body.classList.toggle('motion-paused', reduced || paused);
  const button = document.getElementById('workspaceMotionToggle');
  if (!button) return;
  button.disabled = reduced;
  button.setAttribute('aria-pressed', String(reduced || paused));
  button.setAttribute('aria-label', reduced ? 'Decorative animation disabled by reduced motion setting' : paused ? 'Play decorative animation' : 'Pause decorative animation');
  button.innerHTML = '<i class="bi bi-' + (reduced || paused ? 'play' : 'pause') + '-fill" aria-hidden="true"></i><span>' + (reduced ? 'Motion off' : paused ? 'Play motion' : 'Pause motion') + '</span>';
}

function toggleWorkspaceMotion() {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const paused = !document.body.classList.contains('motion-paused');
  try { localStorage.setItem('workspace_motion_paused', paused ? '1' : '0'); } catch (_) {}
  // Keep the immediate control useful even if storage is unavailable.
  syncWorkspaceMotion();
  document.body.classList.toggle('motion-paused', paused);
  const button = document.getElementById('workspaceMotionToggle');
  if (button) {
    button.setAttribute('aria-pressed', String(paused));
    button.setAttribute('aria-label', paused ? 'Play decorative animation' : 'Pause decorative animation');
    button.innerHTML = '<i class="bi bi-' + (paused ? 'play' : 'pause') + '-fill" aria-hidden="true"></i><span>' + (paused ? 'Play motion' : 'Pause motion') + '</span>';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  syncWorkspaceMotion();
  window.matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', syncWorkspaceMotion);
  const panel = document.getElementById('resultsFilterPanel');
  const wideScreen = window.matchMedia('(min-width: 768px)');
  const setDefaultDisclosure = () => { if (panel) panel.open = wideScreen.matches; };
  setDefaultDisclosure();
  wideScreen.addEventListener('change', setDefaultDisclosure);
  panel?.addEventListener('click', updateFilterSummary);
  panel?.addEventListener('input', updateFilterSummary);
  renderWorkspaceOverview();
  updateFilterSummary();

  // Preserve existing tab handlers while making the navigation keyboard usable.
  document.querySelectorAll('.step-item[data-tab]').forEach(tab => {
    tab.setAttribute('role', 'button');
    tab.setAttribute('tabindex', '0');
    tab.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        tab.click();
      }
    });
  });
});
