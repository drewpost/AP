/* ApplyPilot Web UI — Vanilla JS */

let currentPage = 1;
let selectedUrls = new Set();
let searchTimeout = null;

// -- Streaming state --------------------------------------------------------
let _streamDebounce = null;
let _lastDiscoveredAt = null;
let _knownUrls = new Set();
let _logManualClose = false;
const LOG_MAX_LINES = 500;

// -- Job loading & rendering ------------------------------------------------

function getFilters() {
  const f = {};
  const el = (id) => document.getElementById(id);
  if (el('filter-min-score')) f.min_score = el('filter-min-score').value || undefined;
  if (el('filter-remote')) f.remote_type = el('filter-remote').value || undefined;
  if (el('filter-site')) f.site = el('filter-site').value || undefined;
  if (el('filter-country')) f.country_code = el('filter-country').value || undefined;
  if (el('filter-company-tag')) f.company_tag = el('filter-company-tag').value || undefined;
  if (el('filter-user-status')) f.user_status = el('filter-user-status').value || undefined;
  if (el('filter-hide-dismissed')) f.hide_dismissed = el('filter-hide-dismissed').value || '1';
  if (el('filter-search')) f.search = el('filter-search').value || undefined;
  f.page = currentPage;
  f.per_page = 50;
  return f;
}

async function loadJobs(page) {
  if (page !== undefined) currentPage = page;
  const filters = getFilters();
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined) params.set(k, v);
  }

  const grid = document.getElementById('job-grid');
  if (!grid) return;
  grid.innerHTML = '<div class="loading">Loading...</div>';

  try {
    const resp = await fetch('/api/jobs?' + params);
    const data = await resp.json();
    renderJobs(data.jobs);
    renderPagination(data.page, data.pages, data.total);
    document.getElementById('job-count').textContent =
      `Showing ${data.jobs.length} of ${data.total} jobs (page ${data.page}/${data.pages})`;
  } catch (e) {
    grid.innerHTML = '<div class="loading">Error loading jobs</div>';
  }
}

function renderJobs(jobs) {
  const grid = document.getElementById('job-grid');
  if (!jobs.length) {
    grid.innerHTML = '<div class="loading">No jobs match your filters</div>';
    return;
  }
  grid.innerHTML = jobs.map(j => renderJobCard(j)).join('');
  updateSelectionCount();
}

function renderJobCard(j) {
  const score = j.fit_score || 0;
  const scoreColor = score >= 7 ? '#10b981' : (score >= 5 ? '#f59e0b' : '#ef4444');
  const isSelected = j.ui_selected === 1 || selectedUrls.has(j.url);
  const selectedClass = isSelected ? ' selected' : '';

  // NEW badge: show if user has never viewed this job
  const isNew = !j.user_viewed_at;
  const newBadge = isNew ? '<span class="badge-new">NEW</span>' : '';

  let metaTags = '';
  if (j.company) metaTags += `<span class="meta-tag">${esc(j.company)}</span>`;
  if (j.site) metaTags += `<span class="meta-tag">${esc(j.site)}</span>`;
  if (j.remote_type && j.remote_type !== 'unknown') {
    const rtClass = j.remote_type === 'remote' ? 'remote' :
                    j.remote_type === 'hybrid' ? 'remote-hybrid' : 'remote-onsite';
    metaTags += `<span class="meta-tag ${rtClass}">${esc(j.remote_type)}</span>`;
  }
  if (j.country_code) metaTags += `<span class="meta-tag country">${esc(j.country_code)}</span>`;
  if (j.company_tag) metaTags += `<span class="meta-tag tag">${esc(j.company_tag)}</span>`;

  let salaryTag = '';
  if (j.salary_min || j.salary_max) {
    const cur = j.salary_currency || '';
    const period = j.salary_period ? '/' + j.salary_period.slice(0, 3) : '';
    const min = j.salary_min ? formatNum(j.salary_min) : '?';
    const max = j.salary_max ? formatNum(j.salary_max) : '?';
    salaryTag = `<span class="meta-tag salary">${cur} ${min}-${max}${period}</span>`;
  } else if (j.salary) {
    salaryTag = `<span class="meta-tag salary">${esc(j.salary)}</span>`;
  }

  let pipelineBadge = '';
  if (j.pipeline_status) {
    pipelineBadge = `<span class="pipeline-badge pipeline-${j.pipeline_status}" data-url="${esc(j.url)}">${esc(j.pipeline_status)}</span>`;
  }

  const brief = j.brief_description || (j.location || '');

  return `
    <div class="job-card${selectedClass}" data-score="${score}" data-url="${esc(j.url)}">
      <div class="card-top">
        <input type="checkbox" class="card-checkbox" ${isSelected ? 'checked' : ''}
               onclick="toggleSelect(event, '${esc(j.url)}')" title="Select for processing">
        <span class="score-pill" style="background:${scoreColor}">${score || '?'}</span>
        ${newBadge}
        <span class="card-title" onclick="showDetail('${esc(j.url)}')">${esc(j.title || 'Untitled')}</span>
      </div>
      <div class="meta-row">${metaTags}${salaryTag}</div>
      <div class="card-brief">${esc(brief)}</div>
      <div class="card-footer">
        ${pipelineBadge}
        ${j.application_url ? `<a href="${esc(j.application_url)}" class="apply-link" target="_blank" onclick="event.stopPropagation()">Apply</a>` : ''}
        <button class="btn-dismiss" onclick="dismissJob(event, '${esc(j.url)}')" title="Dismiss">&times;</button>
      </div>
    </div>`;
}

function renderPagination(page, pages, total) {
  const el = document.getElementById('pagination');
  if (!el) return;
  if (pages <= 1) { el.innerHTML = ''; return; }

  let html = '';
  if (page > 1) html += `<button class="page-btn" onclick="loadJobs(${page - 1})">&laquo;</button>`;

  const start = Math.max(1, page - 3);
  const end = Math.min(pages, page + 3);
  for (let i = start; i <= end; i++) {
    html += `<button class="page-btn${i === page ? ' active' : ''}" onclick="loadJobs(${i})">${i}</button>`;
  }

  if (page < pages) html += `<button class="page-btn" onclick="loadJobs(${page + 1})">&raquo;</button>`;
  el.innerHTML = html;
}

// -- Selection --------------------------------------------------------------

function toggleSelect(event, url) {
  event.stopPropagation();
  const card = event.target.closest('.job-card');
  if (event.target.checked) {
    selectedUrls.add(url);
    card.classList.add('selected');
  } else {
    selectedUrls.delete(url);
    card.classList.remove('selected');
  }
  updateSelectionCount();
}

function selectAllVisible() {
  document.querySelectorAll('.job-card').forEach(card => {
    const url = card.dataset.url;
    const cb = card.querySelector('.card-checkbox');
    if (cb && !cb.checked) {
      cb.checked = true;
      selectedUrls.add(url);
      card.classList.add('selected');
    }
  });
  updateSelectionCount();
}

function deselectAll() {
  selectedUrls.clear();
  document.querySelectorAll('.card-checkbox').forEach(cb => cb.checked = false);
  document.querySelectorAll('.job-card.selected').forEach(c => c.classList.remove('selected'));
  updateSelectionCount();
}

function updateSelectionCount() {
  const el = document.getElementById('selection-count');
  if (el) el.textContent = `${selectedUrls.size} selected`;
}

// -- Dismiss ----------------------------------------------------------------

async function dismissJob(event, url) {
  event.stopPropagation();
  const card = event.target.closest('.job-card');

  await fetch('/api/jobs/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({urls: [url], status: 'dismissed'}),
  });

  if (card) {
    card.style.transition = 'opacity 0.3s, transform 0.3s';
    card.style.opacity = '0';
    card.style.transform = 'scale(0.95)';
    setTimeout(() => card.remove(), 300);
  }
}

async function bulkDismissLowScore() {
  const threshold = prompt('Dismiss all jobs with score below:', '5');
  if (threshold === null) return;
  const score = parseInt(threshold, 10);
  if (isNaN(score)) return;

  // Get all jobs below threshold currently visible
  const cards = document.querySelectorAll('.job-card');
  const urls = [];
  cards.forEach(card => {
    const s = parseInt(card.dataset.score, 10);
    if (s < score) urls.push(card.dataset.url);
  });

  if (!urls.length) { alert('No jobs below that score on this page.'); return; }

  if (!confirm(`Dismiss ${urls.length} jobs with score below ${score}?`)) return;

  await fetch('/api/jobs/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({urls, status: 'dismissed'}),
  });

  // Animate out
  cards.forEach(card => {
    const s = parseInt(card.dataset.score, 10);
    if (s < score) {
      card.style.transition = 'opacity 0.3s, transform 0.3s';
      card.style.opacity = '0';
      card.style.transform = 'scale(0.95)';
      setTimeout(() => card.remove(), 300);
    }
  });
}

// -- Processing -------------------------------------------------------------

async function processSelected() {
  if (selectedUrls.size === 0) return;

  const urls = Array.from(selectedUrls);

  // Mark selected in DB
  await fetch('/api/jobs/select', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({urls}),
  });

  // Start worker
  await fetch('/api/pipeline/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({}),
  });

  loadJobs();
  loadStats();
}

// -- Discovery trigger ------------------------------------------------------

async function triggerDiscover() {
  const btn = document.getElementById('btn-discover');
  btn.textContent = 'Scanning...';
  btn.disabled = true;

  // Reset streaming state
  _lastDiscoveredAt = new Date().toISOString();
  _knownUrls = new Set();
  document.querySelectorAll('.job-card').forEach(c => _knownUrls.add(c.dataset.url));
  _logManualClose = false;

  // Auto-open log panel
  const panel = document.getElementById('log-panel');
  if (panel && !panel.classList.contains('open')) {
    panel.classList.add('open');
  }

  await fetch('/api/discover', {method: 'POST'});
  // Will get updates via SSE
}

// -- Job detail modal -------------------------------------------------------

let currentDetailUrl = null;

async function showDetail(url) {
  currentDetailUrl = url;
  const overlay = document.getElementById('modal-overlay');
  const content = document.getElementById('modal-content');
  content.innerHTML = '<div class="loading">Loading...</div>';
  overlay.classList.add('open');

  try {
    const resp = await fetch('/api/jobs/' + encodeURIComponent(url));
    const j = await resp.json();
    renderDetail(j);
  } catch (e) {
    content.innerHTML = '<div class="loading">Error loading job detail</div>';
  }
}

function renderDetail(j) {
  const content = document.getElementById('modal-content');
  const score = j.fit_score || 0;
  const scoreColor = score >= 7 ? '#10b981' : (score >= 5 ? '#f59e0b' : '#ef4444');

  let metaTags = '';
  if (j.company) metaTags += `<span class="meta-tag">${esc(j.company)}</span>`;
  if (j.site) metaTags += `<span class="meta-tag">${esc(j.site)}</span>`;
  if (j.remote_type && j.remote_type !== 'unknown') {
    metaTags += `<span class="meta-tag remote">${esc(j.remote_type)}</span>`;
  }
  if (j.location) metaTags += `<span class="meta-tag">${esc(j.location)}</span>`;
  if (j.country_code) metaTags += `<span class="meta-tag country">${esc(j.country_code)}</span>`;
  if (j.company_tag) metaTags += `<span class="meta-tag tag">${esc(j.company_tag)}</span>`;

  let salaryTag = '';
  if (j.salary_min || j.salary_max) {
    const cur = j.salary_currency || '';
    const period = j.salary_period || '';
    salaryTag = `<span class="meta-tag salary">${cur} ${formatNum(j.salary_min || 0)}-${formatNum(j.salary_max || 0)} ${period}</span>`;
  }

  let pipelineHtml = '';
  if (j.pipeline_status) {
    pipelineHtml = `<span class="pipeline-badge pipeline-${j.pipeline_status}">${j.pipeline_status}</span>`;
    if (j.pipeline_error) {
      pipelineHtml += `<span style="color:#fca5a5;font-size:0.8rem;margin-left:0.5rem">${esc(j.pipeline_error)}</span>`;
    }
  }

  const currentStatus = j.user_status || 'new';
  const statusOptions = ['new', 'reviewing', 'shortlisted', 'applied', 'interviewing', 'offered', 'rejected', 'dismissed'];

  content.innerHTML = `
    <h2>${esc(j.title || 'Untitled')}</h2>
    <div class="meta-row">${metaTags}${salaryTag}</div>
    <div style="display:flex;gap:0.5rem;align-items:center;margin-bottom:0.5rem">
      <span class="score-pill" style="background:${scoreColor};font-size:0.9rem;width:2rem;height:2rem">${score}</span>
      ${pipelineHtml}
    </div>

    <!-- Status & shortlist -->
    <div class="status-section">
      <label>Status:</label>
      <select id="detail-status" onchange="updateDetailStatus('${esc(j.url)}')">
        ${statusOptions.map(s => `<option value="${s}" ${s === currentStatus ? 'selected' : ''}>${s}</option>`).join('')}
      </select>
      ${currentStatus !== 'shortlisted' ? `<button class="btn btn-sm btn-primary" onclick="shortlistJob('${esc(j.url)}')">Shortlist</button>` : ''}
    </div>

    <!-- Notes -->
    <div class="notes-section">
      <label>Notes:</label>
      <textarea id="detail-notes" placeholder="Add notes..." onblur="saveNotes('${esc(j.url)}')">${esc(j.user_notes || '')}</textarea>
    </div>

    ${j.score_reasoning ? `<div class="reasoning">${esc(j.score_reasoning)}</div>` : ''}
    ${j.brief_description ? `<p style="color:#94a3b8;font-size:0.85rem;margin-bottom:1rem">${esc(j.brief_description)}</p>` : ''}
    ${j.full_description ? `
      <div class="detail-section">
        <h3>Full Description</h3>
        <pre>${esc(j.full_description)}</pre>
      </div>` : ''}
    <div class="action-row">
      ${j.application_url ? `<a href="${esc(j.application_url)}" class="btn btn-primary" target="_blank">Apply Externally</a>` : ''}
      <a href="${esc(j.url)}" class="btn" target="_blank">View Original</a>
    </div>
  `;
}

async function updateDetailStatus(url) {
  const status = document.getElementById('detail-status').value;
  await fetch('/api/jobs/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({urls: [url], status}),
  });
}

async function shortlistJob(url) {
  await fetch('/api/jobs/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({urls: [url], status: 'shortlisted'}),
  });
  // Update the dropdown
  const sel = document.getElementById('detail-status');
  if (sel) sel.value = 'shortlisted';
}

async function saveNotes(url) {
  const notes = document.getElementById('detail-notes').value;
  await fetch('/api/jobs/notes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, notes}),
  });
}

function closeModal(event) {
  if (event && event.target !== document.getElementById('modal-overlay')) return;
  document.getElementById('modal-overlay').classList.remove('open');
  currentDetailUrl = null;
}

// -- Tracker (Kanban) -------------------------------------------------------

async function loadTracker() {
  try {
    const resp = await fetch('/api/tracker');
    const data = await resp.json();
    const columns = ['shortlisted', 'applied', 'interviewing', 'offered', 'rejected'];

    for (const status of columns) {
      const container = document.getElementById('col-' + status);
      const countEl = document.getElementById('count-' + status);
      const jobs = data[status] || [];

      if (countEl) countEl.textContent = jobs.length;
      if (!container) continue;

      if (!jobs.length) {
        container.innerHTML = '<div class="kanban-empty">No jobs</div>';
        continue;
      }

      container.innerHTML = jobs.map(j => renderKanbanCard(j, status)).join('');
    }
  } catch (e) {
    console.error('Failed to load tracker:', e);
  }
}

function renderKanbanCard(j, currentStatus) {
  const score = j.fit_score || 0;
  const scoreColor = score >= 7 ? '#10b981' : (score >= 5 ? '#f59e0b' : '#ef4444');
  const notesPreview = j.user_notes ? j.user_notes.slice(0, 80) + (j.user_notes.length > 80 ? '...' : '') : '';

  // Transition buttons based on current status
  let buttons = '';
  const transitions = {
    shortlisted: ['applied'],
    applied: ['interviewing', 'rejected'],
    interviewing: ['offered', 'rejected'],
    offered: [],
    rejected: [],
  };
  const targets = transitions[currentStatus] || [];
  for (const t of targets) {
    buttons += `<button class="btn btn-sm" onclick="moveJob(event, '${esc(j.url)}', '${t}')">${t}</button>`;
  }

  return `
    <div class="kanban-card" onclick="showDetail('${esc(j.url)}')">
      <div class="kanban-card-top">
        <span class="score-pill" style="background:${scoreColor};width:1.3rem;height:1.3rem;font-size:0.65rem">${score}</span>
        <span class="kanban-card-title">${esc(j.title || 'Untitled')}</span>
      </div>
      <div class="kanban-card-company">${esc(j.company || '')}</div>
      ${notesPreview ? `<div class="kanban-card-notes">${esc(notesPreview)}</div>` : ''}
      ${buttons ? `<div class="kanban-card-actions" onclick="event.stopPropagation()">${buttons}</div>` : ''}
    </div>`;
}

async function moveJob(event, url, newStatus) {
  event.stopPropagation();
  await fetch('/api/jobs/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({urls: [url], status: newStatus}),
  });
  loadTracker();
}

// -- Stats ------------------------------------------------------------------

async function loadStats() {
  try {
    const resp = await fetch('/api/stats');
    const s = await resp.json();
    const el = (id) => document.getElementById(id);

    if (el('stat-total')) el('stat-total').textContent = s.total;
    if (el('stat-scored')) el('stat-scored').textContent = s.scored;
    if (el('stat-unseen')) el('stat-unseen').textContent = s.unseen || 0;

    const highFit = (s.score_distribution || [])
      .filter(([score]) => score >= 7)
      .reduce((sum, [, count]) => sum + count, 0);
    if (el('stat-high')) el('stat-high').textContent = highFit;

    // Worker badge
    const badge = el('worker-badge');
    if (badge) {
      if (s.worker_running) {
        badge.textContent = 'Worker: running';
        badge.className = 'badge badge-green';
      } else {
        badge.textContent = 'Worker: idle';
        badge.className = 'badge badge-dim';
      }
    }
  } catch (e) {
    // Ignore stat loading errors
  }
}

// -- Streaming: prepend new jobs in real time --------------------------------

function scheduleStreamRefresh() {
  clearTimeout(_streamDebounce);
  _streamDebounce = setTimeout(fetchAndPrependNew, 500);
}

async function fetchAndPrependNew() {
  const grid = document.getElementById('job-grid');
  if (!grid) return;

  try {
    const params = new URLSearchParams({limit: '30'});
    if (_lastDiscoveredAt) params.set('after', _lastDiscoveredAt);
    const resp = await fetch('/api/jobs/recent?' + params);
    const data = await resp.json();
    prependNewJobs(data.jobs);
  } catch (e) {
    // Ignore fetch errors during streaming
  }
}

function prependNewJobs(jobs) {
  const grid = document.getElementById('job-grid');
  if (!grid) return;

  const fragment = document.createDocumentFragment();
  let added = 0;

  for (const j of jobs) {
    if (_knownUrls.has(j.url)) continue;
    _knownUrls.add(j.url);

    const tmp = document.createElement('div');
    tmp.innerHTML = renderJobCard(j);
    const card = tmp.firstElementChild;
    card.classList.add('card-new');
    fragment.appendChild(card);
    added++;
  }

  if (added > 0) {
    grid.prepend(fragment);
    // Update discovered_at watermark
    if (jobs.length > 0 && jobs[0].discovered_at) {
      _lastDiscoveredAt = jobs[0].discovered_at;
    }
    // Update stats incrementally
    loadStats();
  }
}

// -- Log panel controls -----------------------------------------------------

function toggleLogPanel() {
  const panel = document.getElementById('log-panel');
  if (!panel) return;
  const wasOpen = panel.classList.contains('open');
  panel.classList.toggle('open');
  if (wasOpen) _logManualClose = true;
}

function clearLogPanel() {
  const output = document.getElementById('log-output');
  if (output) output.innerHTML = '';
}

function appendLogLine(data) {
  const output = document.getElementById('log-output');
  if (!output) return;

  const line = document.createElement('div');
  line.className = 'log-line log-level-' + (data.level || 'info');

  const ts = data.timestamp ? new Date(data.timestamp).toLocaleTimeString() : '';
  line.innerHTML =
    `<span class="log-ts">${esc(ts)}</span>` +
    `<span class="log-source">${esc(data.logger || '')}</span>` +
    `<span class="log-msg">${esc(data.message || '')}</span>`;

  output.appendChild(line);

  // Cap lines
  while (output.children.length > LOG_MAX_LINES) {
    output.removeChild(output.firstChild);
  }

  // Auto-scroll
  output.scrollTop = output.scrollHeight;
}

// -- SSE real-time updates --------------------------------------------------

function connectSSE() {
  const es = new EventSource('/api/events/stream');

  es.addEventListener('job_status', (e) => {
    const data = JSON.parse(e.data);
    const card = document.querySelector(`.job-card[data-url="${CSS.escape(data.url)}"]`);
    if (card) {
      const badge = card.querySelector('.pipeline-badge');
      if (badge) {
        badge.textContent = data.status || '';
        badge.className = `pipeline-badge pipeline-${data.status || ''}`;
      } else if (data.status) {
        const footer = card.querySelector('.card-footer');
        if (footer) {
          const span = document.createElement('span');
          span.className = `pipeline-badge pipeline-${data.status}`;
          span.dataset.url = data.url;
          span.textContent = data.status;
          footer.prepend(span);
        }
      }
    }
  });

  es.addEventListener('job_status_change', (e) => {
    // Refresh tracker if on tracker page
    if (document.getElementById('kanban-board')) {
      loadTracker();
    }
  });

  es.addEventListener('worker_status', (e) => {
    const data = JSON.parse(e.data);
    const badge = document.getElementById('worker-badge');
    if (badge) {
      if (data.status === 'running') {
        badge.textContent = 'Worker: running';
        badge.className = 'badge badge-green';
      } else if (data.status === 'error') {
        badge.textContent = 'Worker: error';
        badge.className = 'badge badge-red';
      } else {
        badge.textContent = 'Worker: idle';
        badge.className = 'badge badge-dim';
      }
    }
  });

  es.addEventListener('discover_status', (e) => {
    const data = JSON.parse(e.data);
    const btn = document.getElementById('btn-discover');
    if (btn) {
      if (data.status === 'done') {
        btn.textContent = 'Scan Now';
        btn.disabled = false;
        // Full refresh to get proper sort/pagination
        loadJobs();
        loadStats();
      } else if (data.status === 'error') {
        btn.textContent = 'Scan Now';
        btn.disabled = false;
      } else {
        btn.textContent = data.status + '...';
      }
    }
  });

  es.addEventListener('jobs_discovered', (e) => {
    // Debounce: batch-fetch new jobs from API
    scheduleStreamRefresh();
  });

  es.addEventListener('scan_log', (e) => {
    const data = JSON.parse(e.data);
    appendLogLine(data);

    // Auto-open log panel on first log message (unless user manually closed)
    if (!_logManualClose) {
      const panel = document.getElementById('log-panel');
      if (panel && !panel.classList.contains('open')) {
        panel.classList.add('open');
      }
    }
  });

  es.onerror = () => {
    setTimeout(connectSSE, 5000);
  };
}

// -- Utilities --------------------------------------------------------------

function esc(str) {
  if (!str) return '';
  const d = document.createElement('div');
  d.textContent = String(str);
  return d.innerHTML;
}

function formatNum(n) {
  if (n === null || n === undefined) return '?';
  return Number(n).toLocaleString();
}

function debounceSearch() {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => loadJobs(1), 300);
}

// -- Init -------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  // Only load jobs if on the job browser page
  if (document.getElementById('job-grid')) {
    loadJobs();
    loadStats();
  }
  connectSSE();

  // Close modal on Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
});
