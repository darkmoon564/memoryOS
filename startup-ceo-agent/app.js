const config = JSON.parse(localStorage.getItem('founderos-config') || '{}');
const $ = (selector) => document.querySelector(selector);
const hasConnection = () => config.apiUrl && config.workspaceId && config.userId && config.apiKey;
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
const clean = (value = '', length = 130) => value.replace(/\s+/g, ' ').trim().slice(0, length);

function setConnectionState() { $('#connection-label').textContent = hasConnection() ? `MemoryOS / ${config.userId}` : 'Connect MemoryOS'; }
function switchView(name) { document.querySelectorAll('.view').forEach((v) => v.classList.remove('active-view')); document.querySelectorAll('.nav-item').forEach((n) => n.classList.toggle('active', n.dataset.view === name)); $(`#${name}-view`).classList.add('active-view'); $('#page-title').textContent = ({ today: 'Company context, in one place.', memory: 'Company memory', decisions: 'Recorded decisions', signals: 'Company signals' })[name]; }
document.querySelectorAll('.nav-item').forEach((item) => item.addEventListener('click', () => switchView(item.dataset.view)));
document.querySelectorAll('[data-view-target]').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.viewTarget)));
function showResponse(message) {
  const box = $('#agent-response');
  const safe = escapeHtml(message || 'No answer was generated.');
  box.innerHTML = safe
    .replace(/^\*\*(.+?):\*\*/gm, '<strong>$1</strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^[-*]\s+(.+)$/gm, '<span class="brief-bullet">$1</span>')
    .replace(/\n/g, '<br>');
  box.hidden = false;
}
async function refreshLlmBudget() {
  if (!hasConnection()) { $('#llm-budget').textContent = 'LLM budget: connect to check'; return; }
  try {
    const response = await fetch(`${config.apiUrl.replace(/\/$/, '')}/v1/ceo/usage?workspace_id=${encodeURIComponent(config.workspaceId)}`, { headers: { Authorization: `Bearer ${config.apiKey}` } });
    if (!response.ok) throw new Error();
    const usage = await response.json();
    $('#llm-budget').textContent = `LLM budget: ${usage.remaining} / ${usage.limit} calls remaining today`;
  } catch { $('#llm-budget').textContent = 'LLM budget: unavailable'; }
}

async function retrieve(query, limit = 6) {
  if (!hasConnection()) throw new Error('Connect MemoryOS first to retrieve company context.');
  const response = await fetch(`${config.apiUrl.replace(/\/$/, '')}/v1/memories/retrieve`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.apiKey}` }, body: JSON.stringify({ user_id: config.userId, workspace_id: config.workspaceId, query, limit }) });
  if (!response.ok) throw new Error('MemoryOS could not retrieve context. Check your connection settings.');
  const data = await response.json(); return data.results || data.memories || [];
}
function renderResults(memories) { $('#memory-results').innerHTML = memories.length ? memories.map((m) => `<article class="result-card"><span>${escapeHtml(m.type || 'MEMORY')}</span><strong>${escapeHtml(clean(m.content, 55) || 'Relevant company context')}</strong><p>${escapeHtml(clean(m.content, 180))}</p><small>${escapeHtml(m.occurred_at || m.created_at || 'MemoryOS')}</small></article>`).join('') : '<p class="empty-state">No relevant memories were found for this query.</p>'; }
async function ask(query) {
  if (!query.trim()) return;
  if (!hasConnection()) { showResponse('Connect MemoryOS first to ask your CEO agent.'); return; }
  showResponse('Retrieving context and preparing an executive answer...');
  try {
    const response = await fetch(`${config.apiUrl.replace(/\/$/, '')}/v1/ceo/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.apiKey}` },
      body: JSON.stringify({ user_id: config.userId, workspace_id: config.workspaceId, query, limit: 6 })
    });
    if (!response.ok) throw new Error('The CEO Agent could not answer. Check the MemoryOS connection.');
    const data = await response.json();
    showResponse(data.answer || 'No answer was generated.');
    renderResults(data.results || []);
    refreshLlmBudget();
    switchView('memory');
  } catch (error) { showResponse(error.message); }
}

function renderList(target, memories, kind) {
  const el = $(target); if (!memories.length) { el.innerHTML = '<p class="empty-state">No matching memories yet.</p>'; return; }
  if (kind === 'priority') el.innerHTML = memories.slice(0, 3).map((m, i) => `<article><span class="priority-num">0${i + 1}</span><div><strong>${escapeHtml(clean(m.content, 58))}</strong><p>${escapeHtml(m.type || 'MEMORY')} / score ${Number(m.score || 0).toFixed(2)}</p></div><button class="arrow" data-query="${escapeHtml(m.content)}">-&gt;</button></article>`).join('');
  if (kind === 'pulse') el.innerHTML = memories.slice(0, 3).map((m) => `<article><span class="source ${m.type === 'GRAPH_FACT' ? 'github' : 'slack'}">${escapeHtml((m.type || 'M').slice(0, 1))}</span><div><strong>${escapeHtml(clean(m.content, 60))}</strong><p>${escapeHtml(clean(m.content, 105))}</p><time>${escapeHtml(m.occurred_at || m.created_at || 'MemoryOS')}</time></div></article>`).join('');
  if (kind === 'decision') el.innerHTML = memories.slice(0, 8).map((m) => `<article><span class="decision-date">${escapeHtml((m.occurred_at || m.created_at || 'MEMORY').slice(0, 10))}</span><div><strong>${escapeHtml(clean(m.content, 78))}</strong><p>${escapeHtml(clean(m.content, 180))}</p><span>${escapeHtml(m.type || 'MEMORY')} / confidence ${Number(m.confidence || .5).toFixed(2)}</span></div></article>`).join('');
  if (kind === 'signal') el.innerHTML = memories.slice(0, 5).map((m, i) => `<article class="signal ${['danger','good','info'][i % 3]}"><span>!</span><div><strong>${escapeHtml(clean(m.content, 72))}</strong><p>${escapeHtml(clean(m.content, 170))}</p></div><button class="text-button" data-query="${escapeHtml(m.content)}">Explore -&gt;</button></article>`).join('');
  el.querySelectorAll('[data-query]').forEach((button) => button.addEventListener('click', () => ask(button.dataset.query)));
}
function setContextCard(prefix, memories) { $(`#${prefix}-count`).textContent = `${memories.length}`; $(`#${prefix}-trend`).textContent = memories.length ? 'retrieved now' : 'no match'; $(`#${prefix}-detail`).textContent = memories.length ? clean(memories[0].content, 72) : 'No matching context found'; }
async function hydrateDashboard() {
  if (!hasConnection()) return;
  $('#briefing-title').innerHTML = 'Loading <em>MemoryOS.</em>'; $('#briefing-copy').textContent = 'Retrieving the company context most relevant to the CEO.';
  try {
    const [briefing, priorities, pulse, decisions, signals, revenue, customers, capital] = await Promise.all([
      retrieve('CEO briefing current company priorities risks decisions', 6), retrieve('urgent priorities blockers decisions needing CEO attention', 5), retrieve('recent company updates Slack GitHub customers investors', 5), retrieve('recorded company decisions roadmap fundraising product priorities', 8), retrieve('company risks opportunities leading signals churn blockers', 5), retrieve('revenue MRR ARR growth operating metrics', 3), retrieve('customers accounts renewals expansion churn', 3), retrieve('runway cash burn fundraising investor capital', 3)
    ]);
    const lead = briefing[0]; $('#briefing-title').innerHTML = lead ? `Your top context: <em>${escapeHtml(clean(lead.content, 36))}</em>` : 'No <em>briefing context</em> yet.'; $('#briefing-copy').textContent = lead ? clean(lead.content, 240) : 'Add company context in MemoryOS, then refresh this briefing.'; $('#health-score').textContent = briefing.length; $('#health-caption').textContent = `${briefing.length} memories retrieved now`;
    setContextCard('revenue', revenue); setContextCard('customer', customers); setContextCard('capital', capital); renderList('#priority-list', priorities, 'priority'); renderList('#pulse-list', pulse, 'pulse'); renderList('#decision-list', decisions, 'decision'); renderList('#signal-list', signals, 'signal');
  } catch (error) { $('#briefing-title').textContent = 'MemoryOS is not reachable.'; $('#briefing-copy').textContent = error.message; $('#health-caption').textContent = 'Check your connection settings'; }
}

$('#agent-form').addEventListener('submit', (e) => { e.preventDefault(); const input = $('#agent-input'); ask(input.value); input.value = ''; });
$('#agent-input').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('#agent-form').requestSubmit(); } });
$('#memory-search-button').addEventListener('click', () => ask($('#memory-search-input').value)); $('#memory-search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') ask(e.target.value); }); $('#ask-briefing').addEventListener('click', () => ask('CEO briefing current company priorities risks decisions'));
document.querySelectorAll('.memory-categories article').forEach((card) => card.addEventListener('click', () => ask(card.dataset.query))); $('#refresh-dashboard').addEventListener('click', hydrateDashboard);
$('#open-settings').addEventListener('click', () => { $('#api-url').value = config.apiUrl || 'http://127.0.0.1:8088'; $('#workspace-id').value = config.workspaceId || ''; $('#user-id').value = config.userId || ''; $('#api-key').value = config.apiKey || ''; $('#settings-modal').showModal(); });
$('#settings-form').addEventListener('submit', () => { config.apiUrl = $('#api-url').value.trim(); config.workspaceId = $('#workspace-id').value.trim(); config.userId = $('#user-id').value.trim(); config.apiKey = $('#api-key').value.trim(); localStorage.setItem('founderos-config', JSON.stringify(config)); setConnectionState(); setTimeout(hydrateDashboard, 0); setTimeout(refreshLlmBudget, 0); });
$('#open-capture').addEventListener('click', () => $('#capture-modal').showModal()); $('#capture-form').addEventListener('submit', async () => { const content = $('#capture-content').value.trim(); if (!content) return; if (!hasConnection()) { showResponse('Connect MemoryOS before saving company context.'); return; } try { const response = await fetch(`${config.apiUrl.replace(/\/$/, '')}/v1/memories`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.apiKey}` }, body: JSON.stringify({ user_id: config.userId, workspace_id: config.workspaceId, content, occurred_at: new Date().toISOString() }) }); if (!response.ok) throw new Error(); $('#capture-content').value = ''; showResponse('Context saved to MemoryOS. Refreshing the dashboard...'); await hydrateDashboard(); } catch { showResponse('Could not save context. Check your MemoryOS connection.'); } });
setConnectionState(); if (hasConnection()) { hydrateDashboard(); refreshLlmBudget(); }
