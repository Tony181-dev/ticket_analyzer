'use strict';
/* Ticket Analyzer UI: flow diagram, live progress (SSE), artifact drawer, setup. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => Markdown.esc(s ?? '');
const t = (k, v) => I18N.t(k, v);

const STATUS_ICON = { idle: null, running: 'i-gear', done: 'i-check', error: 'i-x', skipped: 'i-skip', stale: 'i-refresh', waiting: 'i-pause' };
const OPTIONAL = { understand: 'opt.needLogs', log: 'opt.needLogs', graph: 'opt.needSource' };
const MAX_VIEW = 400000; // chars rendered inline; bigger files open in a new tab

const S = {
  state: null,           // /api/state
  run: null,             // current run object (null = draft)
  draft: { log_files: [], source_dir: '', auto_attachments: false },
  selected: null,        // step id open in the drawer
  tab: null,             // artifact name open in the drawer
  editing: false,
  doctor: null,
};

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'),
    headers: opts.raw ? { 'Content-Type': 'text/plain; charset=utf-8' } : { 'Content-Type': 'application/json' },
    body: opts.body === undefined ? undefined : (opts.raw ? opts.body : JSON.stringify(opts.body)),
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = text; }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function toast(msg, err = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast' + (err ? ' err' : '');
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, err ? 6000 : 2800);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

// ---------------------------------------------------------------- boot
async function boot() {
  I18N.apply();
  S.state = await api('/api/state');
  I18N.set(S.state.settings.lang);
  renderMode();
  renderHistory();
  renderAll();
  connect();
  refreshDoctor();
  const last = S.state.busy_run || new URLSearchParams(location.search).get('run');
  if (last) openRun(last);
}

function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === 'run') {
      if (S.run && m.run.id === S.run.id) { S.run = m.run; renderAll(); refreshDrawer(); }
      scheduleHistory();
    } else if (m.type === 'log') {
      if (S.run && m.run_id === S.run.id) pushLog(m.line);
    } else if (m.type === 'busy') {
      S.state.busy = m.busy; S.state.busy_run = m.busy ? m.run_id : null; renderButtons();
      if (!m.busy) refreshDoctor();
    } else if (m.type === 'doctor_log') {
      const el = $('#doctorLog'); el.textContent += m.line + '\n'; el.scrollTop = el.scrollHeight;
    } else if (m.type === 'doctor_changed') {
      refreshDoctor();
    } else if (m.type === 'runs_cleared') {
      if (S.run && S.run.id !== m.keep) $('#newRun').click();
      scheduleHistory();
    } else if (m.type === 'settings') {
      S.state.settings = m.settings; renderMode();
    }
  };
  es.onerror = () => { $('#setupText').textContent = t('setup.offline'); $('#setupPill').className = 'pill bad'; };
  es.onopen = () => refreshDoctor();
}

// Coming back from a login window / terminal: re-check right away.
let lastFocusCheck = 0;
window.addEventListener('focus', () => {
  if (Date.now() - lastFocusCheck > 3000) { lastFocusCheck = Date.now(); refreshDoctor(); }
});

let historyTimer;
function scheduleHistory() {
  clearTimeout(historyTimer);
  historyTimer = setTimeout(async () => { S.state = await api('/api/state'); renderHistory(); }, 400);
}

// ---------------------------------------------------------------- header: mode + language
function renderMode() {
  const s = S.state.settings;
  for (const b of document.querySelectorAll('.mode-btn')) b.setAttribute('aria-checked', String(b.dataset.mode === s.ai_mode));
  for (const b of document.querySelectorAll('.lang-btn')) b.setAttribute('aria-checked', String(b.dataset.lang === I18N.lang));
  const models = (S.state.models || {})[s.ai_mode] || [];
  $('#modelList').innerHTML = models.map((m) => `<option value="${esc(m)}"></option>`).join('');
  if (document.activeElement !== $('#modelInput')) {
    $('#modelInput').value = s.ai_mode === 'copilot' ? (s.copilot_model || 'auto') : s.m365_model;
  }
}
$('#modelInput').onfocus = () => { $('#modelInput').select(); };
$('#modelInput').onchange = guard(async () => {
  const value = $('#modelInput').value.trim();
  const copilotMode = S.state.settings.ai_mode === 'copilot';
  if (!value && !copilotMode) { renderMode(); return; }
  const patch = copilotMode ? { copilot_model: value === 'auto' ? '' : value } : { m365_model: value };
  S.state.settings = await api('/api/settings', { body: patch });
  renderMode();
  toast(t('model.saved', { m: value || 'auto' }));
});
$('#modelInput').onkeydown = (e) => { if (e.key === 'Enter') e.target.blur(); };
for (const b of document.querySelectorAll('.mode-btn')) {
  b.onclick = guard(async () => {
    S.state.settings = await api('/api/settings', { body: { ai_mode: b.dataset.mode } });
    renderMode();
    refreshDoctor();
    toast(t(`toast.mode.${b.dataset.mode}`));
  });
}
for (const b of document.querySelectorAll('.lang-btn')) {
  b.onclick = guard(async () => {
    S.state.settings = await api('/api/settings', { body: { lang: b.dataset.lang } });
    I18N.set(b.dataset.lang);
    renderMode();
    renderHistory();
    renderAll();
    if (S.selected) renderDrawer(true);
    refreshDoctor();
  });
}

async function refreshDoctor() {
  try { S.doctor = await api('/api/doctor'); } catch { return; }
  const bad = S.doctor.checks.filter((c) => c.needed && !c.ok);
  $('#setupPill').className = 'pill ' + (bad.length ? 'bad' : 'ok');
  $('#setupText').textContent = bad.length ? t('setup.need', { n: bad.length }) : t('setup.ready');
  renderChecks();
}

// ---------------------------------------------------------------- history
function renderHistory() {
  const ul = $('#runList');
  const runs = S.state.runs || [];
  if (!runs.length) { ul.innerHTML = `<li class="muted small" style="padding:6px 10px">${esc(t('history.empty'))}</li>`; return; }
  const locale = I18N.lang === 'en' ? 'en-GB' : 'vi-VN';
  ul.innerHTML = runs.map((r) => {
    const when = new Date(r.created * 1000).toLocaleString(locale, { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' });
    const tag = r.collab && r.collab.page_id ? '<span class="tag ok">Collab</span>'
      : r.failed ? `<span class="tag err">${esc(t('history.failed'))}</span>` : `<span class="tag">${r.done}/7</span>`;
    return `<li class="run-item ${S.run && S.run.id === r.id ? 'active' : ''}" data-id="${esc(r.id)}">
      <div class="k">${esc(r.issue)} ${tag}</div><div class="s">${esc(r.summary || '—')}</div><div class="t">${when}</div></li>`;
  }).join('');
  for (const li of ul.querySelectorAll('.run-item')) li.onclick = () => openRun(li.dataset.id);
}

async function openRun(id) {
  try { S.run = await api(`/api/runs/${encodeURIComponent(id)}`); } catch (e) { return toast(e.message, true); }
  $('#console').textContent = '';
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(id)}/files/run.log`);
    if (res.ok) for (const line of (await res.text()).split('\n').filter((l) => /^\[\d\d:\d\d:\d\d\]/.test(l)).slice(-300)) pushLog(line);
  } catch { /* no log yet */ }
  history.replaceState(null, '', `?run=${encodeURIComponent(id)}`);
  $('#issue').value = S.run.issue;
  renderAll();
  renderHistory();
  closeDrawer();
}

$('#newRun').onclick = () => {
  S.run = null;
  S.draft = { log_files: [], source_dir: '', auto_attachments: false };
  $('#issue').value = '';
  $('#console').textContent = '';
  $('#consoleCount').textContent = '';
  history.replaceState(null, '', '/');
  closeDrawer();
  renderAll();
  renderHistory();
  $('#issue').focus();
};

// ---------------------------------------------------------------- inputs
const inputs = () => (S.run ? S.run.inputs : S.draft);

function renderInputs() {
  const files = inputs().log_files;
  $('#logChips').innerHTML = files.length
    ? files.map((f, i) => `<span class="chip" title="${esc(f)}"><span>${esc(f.split(/[\\/]/).pop())}</span><button data-i="${i}" aria-label="${esc(t('in.remove'))}">×</button></span>`).join('')
    : `<span class="muted small">${esc(t('in.nologs'))}</span>`;
  for (const b of $('#logChips').querySelectorAll('button')) b.onclick = guard(() => setInputs({ log_files: files.filter((_, j) => j !== +b.dataset.i) }));
  if (document.activeElement !== $('#source')) $('#source').value = inputs().source_dir || '';
  $('#autoAtt').checked = !!inputs().auto_attachments;
  $('#issue').disabled = !!S.run;
}
$('#autoAtt').onchange = guard(() => setInputs({ auto_attachments: $('#autoAtt').checked }));

async function setInputs(patch) {
  if (S.run) S.run = await api(`/api/runs/${encodeURIComponent(S.run.id)}/inputs`, { body: patch });
  else Object.assign(S.draft, patch);
  renderAll();
}

$('#pickLogs').onclick = guard(async () => {
  const { paths } = await api('/api/pick', { body: { kind: 'files' } });
  if (paths.length) await setInputs({ log_files: [...new Set([...inputs().log_files, ...paths])] });
});
$('#pickSource').onclick = guard(async () => {
  const { paths } = await api('/api/pick', { body: { kind: 'folder' } });
  if (paths.length) await setInputs({ source_dir: paths[0] });
});
$('#source').onchange = guard(() => setInputs({ source_dir: $('#source').value.trim() }));

$('#runAll').onclick = guard(async () => {
  if (S.run) {
    await api(`/api/runs/${encodeURIComponent(S.run.id)}/start`, { body: {} });
  } else {
    const issue = $('#issue').value.trim();
    if (!issue) { $('#issue').focus(); throw new Error(t('run.needIssue')); }
    S.run = await api('/api/runs', { body: { issue, ...S.draft } });
    history.replaceState(null, '', `?run=${encodeURIComponent(S.run.id)}`);
    $('#console').textContent = '';
  }
  S.state.busy = true;
  renderAll();
  scheduleHistory();
});
$('#issue').onkeydown = (e) => { if (e.key === 'Enter') $('#runAll').click(); };
$('#issue').oninput = () => { if (!S.run) renderFlow(); };
$('#stopBtn').onclick = guard(() => api('/api/stop', { body: {} }));

function renderButtons() {
  const busy = S.state.busy;
  $('#runAll').hidden = busy;
  $('#stopBtn').hidden = !busy;
  $('#runAll').innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18"><use href="#i-play"/></svg> ${esc(t(S.run ? 'run.again' : 'run.all'))}`;
  $('#runOnly').disabled = $('#runFrom').disabled = busy || !S.run;
}

// ---------------------------------------------------------------- flow
function stepState(id) {
  return S.run ? S.run.steps[id] : { status: 'idle', note: '', error: '', artifacts: [] };
}

function duration(st) {
  if (!st.started) return '';
  const sec = Math.max(0, Math.round(((st.ended || Date.now() / 1000) - st.started)));
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${String(sec % 60).padStart(2, '0')}s`;
}

// What each step needs before it can run (besides its own inputs).
const DEPS = { jira: [], understand: ['jira'], log: ['jira'], graph: [], analyze: ['jira'], format: ['analyze'], collab: ['format'] };
const isDone = (id) => S.run && ['done', 'stale', 'waiting'].includes(S.run.steps[id].status);

function hasInputs(id) {
  const inp = inputs();
  if (id === 'jira') return !!S.run || !!$('#issue').value.trim();
  if (id === 'understand' || id === 'log') return inp.log_files.length > 0 || (!!inp.auto_attachments && !isDone('jira'));
  if (id === 'graph') return !!inp.source_dir;
  return true;
}

/** 'ready' = can run now, 'blocked' = waits for earlier steps; null for any other status. */
function readiness(id, status) {
  if (!['idle', 'stale'].includes(status)) return { kind: null, missing: [] };
  const missing = DEPS[id].filter((d) => !isDone(d));
  return { kind: missing.length || !hasInputs(id) ? 'blocked' : 'ready', missing };
}

function renderFlow() {
  $('#flow').innerHTML = S.state.steps.map((s, i) => {
    const st = stepState(s.id);
    let status = st.status;
    if (OPTIONAL[s.id] && ['idle', 'stale'].includes(status) && !hasInputs(s.id)) status = 'skipped';
    const r = readiness(s.id, status);
    const label = r.kind === 'ready' && status === 'idle' ? t('st.ready') : t('st.' + status);
    const icon = r.kind === 'ready' && status === 'idle' ? 'i-play' : STATUS_ICON[status];
    const needs = r.kind === 'blocked' && r.missing.length
      ? `<div class="node-note muted">${esc(t('st.needs', { steps: r.missing.map((d) => t('step.' + d)).join(', ') }))}</div>` : '';
    const note = st.error ? `<div class="node-note err">${esc(st.error)}</div>`
      : st.note ? `<div class="node-note">${esc(st.note)}</div>`
        : status === 'skipped' && OPTIONAL[s.id] ? `<div class="node-note muted">${esc(t('opt.prefix', { why: t(OPTIONAL[s.id]) }))}</div>`
          : needs;
    const gear = status === 'running'
      ? '<span class="gear"><svg><use href="#i-gear"/></svg></span><span class="gear small"><svg><use href="#i-gear"/></svg></span>' : '';
    const publish = s.id === 'collab' && status === 'waiting'
      ? `<button class="btn btn-primary btn-sm publish-btn" data-publish>${esc(t('publish'))}</button>` : '';
    return `<li class="node s-${status} ${r.kind ? 'is-' + r.kind : ''} ${S.selected === s.id ? 'selected' : ''}" data-step="${s.id}" tabindex="0">
      <div class="node-top">
        <div class="node-icon"><svg><use href="#s-${s.id}"/></svg>${gear}</div>
        <span class="node-no">${String(i + 1).padStart(2, '0')}</span>
      </div>
      <div class="node-title">${esc(t('step.' + s.id))}</div>
      <div class="node-sub">${esc(t('sub.' + s.id))}</div>
      ${note}
      ${publish}
      <div class="node-foot">
        <span class="status">${icon ? `<svg><use href="#${icon}"/></svg>` : ''}${esc(label)}</span>
        <span class="time" data-time="${s.id}">${duration(st)}</span>
      </div>
    </li>`;
  }).join('');
  for (const li of $('#flow').querySelectorAll('.node')) {
    li.onclick = (e) => { if (!e.target.closest('[data-publish]')) openDrawer(li.dataset.step); };
    li.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDrawer(li.dataset.step); } };
  }
  const pub = $('#flow [data-publish]');
  if (pub) pub.onclick = guard(() => startStep('collab', true, true));
}

function renderHead() {
  if (!S.run) {
    $('#runTitle').textContent = t('run.newTitle');
    $('#runSub').textContent = t('run.newSub');
    $('#collabLink').hidden = $('#openFolder').hidden = true;
    return;
  }
  $('#runTitle').textContent = `${S.run.issue}${S.run.summary ? ' — ' + S.run.summary : ''}`;
  const mode = S.run.ai_mode === 'copilot' ? 'Copilot CLI' : 'M365 Copilot';
  $('#runSub').textContent = `${S.run.id} · ${mode}` + (S.run.focus && S.run.focus.time_start ? ` · log ${S.run.focus.time_start}–${S.run.focus.time_end || '…'}` : '');
  const url = S.run.collab && S.run.collab.url;
  $('#collabLink').hidden = !url;
  if (url) $('#collabLink').href = url;
  $('#openFolder').hidden = false;
}
$('#openFolder').onclick = guard(() => api(`/api/runs/${encodeURIComponent(S.run.id)}/open-folder`, { body: {} }));

function renderAll() {
  renderInputs();
  renderHead();
  renderFlow();
  renderButtons();
}

setInterval(() => {  // tick running durations
  if (!S.run) return;
  for (const el of document.querySelectorAll('[data-time]')) {
    const st = S.run.steps[el.dataset.time];
    if (st && st.status === 'running') el.textContent = duration(st);
  }
}, 1000);

// ---------------------------------------------------------------- console
function pushLog(line) {
  const el = $('#console');
  const cls = /✓/.test(line) ? 'l-ok' : /✗|lỗi|error/i.test(line) ? 'l-err' : /▶/.test(line) ? 'l-run' : '';
  el.insertAdjacentHTML('beforeend', `<span class="${cls}">${esc(line)}</span>\n`);
  el.scrollTop = el.scrollHeight;
  $('#consoleCount').textContent = t('console.lines', { n: el.childElementCount });
}
$('#clearConsole').onclick = () => {
  $('#console').textContent = '';
  $('#consoleCount').textContent = '';
};
$('#clearHistory').onclick = guard(async () => {
  if (!window.confirm(t('history.confirm'))) return;
  const { removed, kept } = await api('/api/runs/clear', { body: {} });
  if (S.run && S.run.id !== kept) $('#newRun').click();
  S.state = await api('/api/state');
  renderHistory();
  toast(t('history.cleared', { n: removed }));
});
$('#consoleToggle').onclick = () => {
  const card = $('.console-card');
  card.classList.toggle('collapsed');
  $('#consoleToggle').setAttribute('aria-expanded', String(!card.classList.contains('collapsed')));
};

// ---------------------------------------------------------------- drawer
function openDrawer(step) {
  S.selected = step;
  S.editing = false;
  const arts = stepState(step).artifacts || [];
  S.tab = arts.length ? arts[0].name : null;
  $('#drawer').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  $('#scrim').hidden = false;
  renderFlow();
  renderDrawer(true);
}
function closeDrawer() {
  S.selected = null;
  $('#drawer').classList.remove('open');
  $('#drawer').setAttribute('aria-hidden', 'true');
  $('#scrim').hidden = true;
  renderFlow();
}
$('#drawerClose').onclick = closeDrawer;
$('#scrim').onclick = closeDrawer;
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeDrawer(); closeModals(); } });

function refreshDrawer() {
  if (!S.selected || S.editing) return;
  const arts = stepState(S.selected).artifacts || [];
  if (!arts.find((a) => a.name === S.tab)) S.tab = arts.length ? arts[0].name : null;
  renderDrawer(false);
}

const artLabel = (a) => { const k = 'art.' + a.name; const v = t(k); return v === k ? a.label : v; };

function renderDrawer(reloadViewer) {
  const idx = S.state.steps.findIndex((s) => s.id === S.selected);
  const id = S.selected;
  const st = stepState(id);
  $('#drawerStepNo').textContent = t('drawer.step', { n: idx + 1, sub: t('sub.' + id) });
  $('#drawerTitle').textContent = t('step.' + id);
  $('#drawerStatus').innerHTML = `<span class="s-${st.status}"><span class="status">${esc(t('st.' + st.status))}${st.note ? ' · ' + esc(st.note) : ''}</span></span>`;
  renderExtra(id, st);
  const arts = st.artifacts || [];
  $('#drawerTabs').innerHTML = arts.map((a) => `<button class="tab ${a.name === S.tab ? 'active' : ''}" data-name="${esc(a.name)}">${esc(artLabel(a))}</button>`).join('');
  for (const b of $('#drawerTabs').querySelectorAll('.tab')) b.onclick = () => { S.tab = b.dataset.name; S.editing = false; renderDrawer(true); };
  renderToolbar(arts.find((a) => a.name === S.tab));
  if (reloadViewer || $('#viewer').dataset.loaded !== viewerKey()) loadViewer(arts.find((a) => a.name === S.tab));
  renderButtons();
}

function renderExtra(step, st) {
  const box = $('#drawerExtra');
  let html = st.error ? `<div class="err-box" style="margin:0 0 10px">${esc(st.error)}</div>` : '';
  if (step === 'jira') {
    html += attachmentsPanel(st);
  } else if (step === 'log' || step === 'understand') {
    const f = (S.run && S.run.focus) || {};
    const help = f.reason ? t('focus.ai', { reason: f.reason }) + (f.confidence ? t('focus.conf', { c: f.confidence }) : '') : t('focus.help');
    html += `<div class="extra-grid">
      <div class="field"><label>${esc(t('focus.from'))}</label><input id="fStart" placeholder="HH:MM:SS" value="${esc(f.time_start || '')}"></div>
      <div class="field"><label>${esc(t('focus.to'))}</label><input id="fEnd" placeholder="HH:MM:SS" value="${esc(f.time_end || '')}"></div>
      <div class="field"><label>${esc(t('focus.keys'))}</label><input id="fKeys" value="${esc((f.keywords || []).join(', '))}"></div>
      <button class="btn btn-primary" id="fApply" ${S.run ? '' : 'disabled'}>${esc(t('focus.apply'))}</button>
    </div>
    <div class="extra-note">${esc(help)}</div>`;
  } else if (step === 'graph') {
    const ready = st.status === 'done' || st.status === 'stale';
    html += `<div class="picker"><input id="gq" placeholder="${esc(t('graph.ph'))}"><button class="btn btn-ghost" id="gGo" ${ready ? '' : 'disabled'}>${esc(t('graph.go'))}</button></div><div class="hits" id="gHits"></div>`;
  } else if (step === 'collab' && S.run && S.run.collab && S.run.collab.url) {
    html += `<a class="btn btn-primary" href="${esc(S.run.collab.url)}" target="_blank" rel="noopener">${esc(t('run.openCollab'))}</a>
      <span class="muted small" style="margin-left:8px">${esc(S.run.collab.title || '')}</span>`;
  } else if (step === 'collab' && st.status === 'waiting') {
    html += `<button class="btn btn-primary" id="pubNow">${esc(t('publish'))}</button> <span class="muted small">${esc(t('collab.ready'))}</span>`;
  }
  box.innerHTML = html;
  if ($('#fApply')) $('#fApply').onclick = guard(async () => {
    const focus = { ...((S.run && S.run.focus) || {}), time_start: $('#fStart').value.trim(), time_end: $('#fEnd').value.trim(),
      keywords: $('#fKeys').value.split(',').map((s) => s.trim()).filter(Boolean) };
    S.run = await api(`/api/runs/${encodeURIComponent(S.run.id)}/files/02-focus.json`, { method: 'PUT', raw: true, body: JSON.stringify(focus) });
    await startStep('log', true);
  });
  if ($('#gGo')) {
    const go = guard(async () => {
      const { hits } = await api(`/api/runs/${encodeURIComponent(S.run.id)}/graph-query`, { body: { q: $('#gq').value } });
      $('#gHits').innerHTML = hits.length
        ? hits.map((h) => `<div>${esc(h.name)} <span class="muted">[${esc(h.kind)}] ${esc(h.file)}:${h.line} · deg ${h.degree}</span></div>`).join('')
        : `<div class="muted">${esc(t('graph.none'))}</div>`;
    });
    $('#gGo').onclick = go;
    $('#gq').onkeydown = (e) => { if (e.key === 'Enter') go(); };
  }
  if ($('#pubNow')) $('#pubNow').onclick = guard(() => startStep('collab', true, true));
  if (step === 'jira') bindAttachments();
}

// ---------------------------------------------------------------- jira attachments
const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);

function attachmentsPanel(st) {
  const head = `<div class="token-title">${esc(t('att.title'))}</div>`;
  if (!S.run || !['done', 'stale'].includes(st.status)) return `<div class="att">${head}<div class="muted small">${esc(t('att.needJira'))}</div></div>`;
  const list = S.run.attachments || [];
  if (!list.length) return `<div class="att">${head}<div class="muted small">${esc(t('att.none'))}</div></div>`;
  const folder = S.run.inputs.download_dir || S.state.settings.download_dir || '';
  const placeholder = (S.state.default_download_dir || '').replace('<ISSUE>', S.run.issue);
  const rows = list.map((a, i) => {
    const pre = a.kind !== 'other';
    const badges = [a.path ? t('att.have') : '', a.analyzed ? t('att.analyzed') : ''].filter(Boolean)
      .map((b) => ` <span class="tag ok">${esc(b)}</span>`).join('');
    return `<tr>
      <td class="c"><input type="checkbox" data-dl="${i}" ${pre ? 'checked' : ''}></td>
      <td class="c"><input type="checkbox" data-an="${i}" ${pre ? 'checked' : ''} ${a.kind === 'other' ? 'disabled' : ''}></td>
      <td class="n">${esc(a.filename)}${badges}</td>
      <td>${esc(t('att.kind.' + a.kind))}</td><td>${fmtSize(a.size || 0)}</td></tr>`;
  }).join('');
  return `<div class="att">${head}
    <div class="att-head">
      <div class="field"><label>${esc(t('att.folder'))}</label><input id="attFolder" value="${esc(folder)}" placeholder="${esc(placeholder)}"></div>
      <button class="btn btn-ghost" id="attPick">${esc(t('att.pick'))}</button>
    </div>
    <table><thead><tr><th class="c">${esc(t('att.dl'))}</th><th class="c">${esc(t('att.analyze'))}</th><th>${esc(t('att.name'))}</th><th></th><th>${esc(t('att.size'))}</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="actions">
      <button class="btn btn-primary" id="attRun" ${S.state.busy ? 'disabled' : ''}>${esc(t('att.run'))}</button>
      <button class="btn btn-ghost" id="attOpen">${esc(t('att.open'))}</button>
    </div>
    <div class="extra-note">${esc(t('att.hint'))}</div></div>`;
}

function bindAttachments() {
  if (!$('#attRun')) return;
  const list = S.run.attachments || [];
  for (const an of document.querySelectorAll('[data-an]')) {  // analyzing implies downloading
    an.onchange = () => { if (an.checked) document.querySelector(`[data-dl="${an.dataset.an}"]`).checked = true; };
  }
  for (const dl of document.querySelectorAll('[data-dl]')) {
    dl.onchange = () => { if (!dl.checked) { const an = document.querySelector(`[data-an="${dl.dataset.dl}"]`); if (an) an.checked = false; } };
  }
  $('#attPick').onclick = guard(async () => {
    const { paths } = await api('/api/pick', { body: { kind: 'folder' } });
    if (paths.length) $('#attFolder').value = paths[0];
  });
  $('#attRun').onclick = guard(async () => {
    const names = [...document.querySelectorAll('[data-dl]:checked')].map((x) => list[+x.dataset.dl].filename);
    const analyze = [...document.querySelectorAll('[data-an]:checked')].map((x) => list[+x.dataset.an].filename);
    if (!names.length) throw new Error(t('att.nothing'));
    await api(`/api/runs/${encodeURIComponent(S.run.id)}/attachments`, { body: { names, analyze, folder: $('#attFolder').value.trim() } });
    S.state.busy = true;
    renderButtons();
    toast(t('att.started'));
  });
  $('#attOpen').onclick = guard(() => api(`/api/runs/${encodeURIComponent(S.run.id)}/open-folder`, { body: { what: 'attachments' } }));
}

const viewerKey = () => (S.run && S.selected ? `${S.run.id}/${S.tab}/${S.run.steps[S.selected].ended}/${S.run.steps[S.selected].status}/${I18N.lang}` : '');
const fileUrl = (name) => `/api/runs/${encodeURIComponent(S.run.id)}/files/${encodeURIComponent(name)}`;

function renderToolbar(art) {
  const bar = $('#drawerToolbar');
  if (!art) { bar.innerHTML = ''; return; }
  const parts = [];
  if (art.editable) {
    parts.push(S.editing
      ? `<button class="btn btn-primary btn-sm" data-act="save">${esc(t('tb.save'))}</button><button class="btn btn-sm" data-act="cancel">${esc(t('tb.cancel'))}</button>`
      : `<button class="btn btn-sm" data-act="edit">${esc(t('tb.edit'))}</button>`);
  }
  parts.push(`<button class="btn btn-sm" data-act="copy">${esc(t('tb.copy'))}</button>`);
  parts.push(`<a class="btn btn-sm" href="${fileUrl(art.name)}" download="${esc(art.name)}">${esc(t('tb.download'))}</a>`);
  parts.push(`<a class="btn btn-sm" href="${fileUrl(art.name)}" target="_blank" rel="noopener">${esc(t('tb.newTab'))}</a>`);
  parts.push(`<span class="spacer"></span><button class="btn btn-sm" data-act="folder">${esc(t('tb.folder'))}</button>`);
  bar.innerHTML = parts.join('');
  const on = (act, fn) => { const b = bar.querySelector(`[data-act="${act}"]`); if (b) b.onclick = guard(fn); };
  on('edit', async () => { S.editing = true; renderToolbar(art); await loadViewer(art); });
  on('cancel', async () => { S.editing = false; renderToolbar(art); await loadViewer(art); });
  on('save', async () => {
    S.run = await api(fileUrl(art.name), { method: 'PUT', raw: true, body: $('#viewer textarea').value });
    S.editing = false;
    toast(t('toast.saved'));
    renderAll(); renderDrawer(true);
  });
  on('copy', async () => {
    const text = await (await fetch(fileUrl(art.name))).text();
    await navigator.clipboard.writeText(text);
    toast(t('toast.copied'));
  });
  on('folder', () => api(`/api/runs/${encodeURIComponent(S.run.id)}/open-folder`, { body: {} }));
}

async function loadViewer(art) {
  const v = $('#viewer');
  if (!S.run || !art) {
    v.innerHTML = `<div class="empty">${esc(t(S.run ? 'drawer.noFiles' : 'drawer.notRun'))}</div>`;
    v.dataset.loaded = '';
    return;
  }
  v.dataset.loaded = viewerKey();
  if (art.kind === 'html' && !S.editing) {
    v.innerHTML = `<iframe src="${fileUrl(art.name)}?t=${Date.now()}" title="${esc(artLabel(art))}"></iframe>`;
    return;
  }
  const text = await (await fetch(fileUrl(art.name))).text();
  if (S.editing) {
    v.innerHTML = '<textarea spellcheck="false"></textarea>';
    v.querySelector('textarea').value = text;
    return;
  }
  const shown = text.length > MAX_VIEW ? text.slice(0, MAX_VIEW) : text;
  const cut = text.length > MAX_VIEW
    ? `<p class="muted small">${esc(t('drawer.truncated', { shown: MAX_VIEW.toLocaleString(), total: text.length.toLocaleString() }))}</p>` : '';
  if (art.kind === 'md') v.innerHTML = cut + `<div class="md">${Markdown.render(shown)}</div>`;
  else if (art.kind === 'json') {
    let pretty = shown;
    if (text.length <= MAX_VIEW) { try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw */ } }
    v.innerHTML = cut + `<pre class="code">${esc(pretty)}</pre>`;
  } else v.innerHTML = cut + `<pre class="code">${esc(shown)}</pre>`;
}

async function startStep(step, only, publish = false) {
  await api(`/api/runs/${encodeURIComponent(S.run.id)}/start`, { body: { from: step, only, publish } });
  S.state.busy = true;
  renderButtons();
}
$('#runOnly').onclick = guard(() => startStep(S.selected, true, S.selected === 'collab'));
$('#runFrom').onclick = guard(() => startStep(S.selected, false, S.selected === 'collab'));

// ---------------------------------------------------------------- setup modal
function openModal(id) { $(id).hidden = false; }
function closeModals() { for (const m of document.querySelectorAll('.modal')) m.hidden = true; }
for (const b of document.querySelectorAll('[data-close]')) b.onclick = closeModals;
for (const m of document.querySelectorAll('.modal')) m.onclick = (e) => { if (e.target === m) closeModals(); };

$('#setupPill').onclick = () => { openModal('#setupModal'); refreshDoctor(); };
$('#aboutBtn').onclick = () => {
  $('#aboutTitle').textContent = About.title(I18N.lang);
  $('#aboutBody').innerHTML = About.render(I18N.lang);
  openModal('#aboutModal');
};

function renderChecks() {
  if (!S.doctor) return;
  const btn = (attr, val, key, primary = false) => `<button class="btn btn-sm ${primary ? 'btn-primary' : ''}" ${attr}="${val}">${esc(t(key))}</button>`;
  $('#checkList').innerHTML = S.doctor.checks.map((c) => {
    const extra = [];
    if (c.id === 'm365_login') extra.push(btn('data-action', 'm365_check', 'su.check'));
    if (c.id === 'copilot_login') extra.push(btn('data-action', 'copilot_check', 'su.verify'));
    if (c.id === 'mcp_jira' && c.ok) extra.push(btn('data-action', 'mcp_test', 'su.testConn'));
    if (c.id === 'mcp_collab' && c.ok) extra.push(btn('data-action', 'tokens', 'su.changeToken'));
    if (c.fix) extra.push(btn('data-fix', c.fix, 'su.install'));
    else if (c.action) extra.push(btn('data-action', c.action, `su.act.${c.action}`, true));
    return `<li class="check-row ${c.ok ? 'ok' : 'bad'} ${c.needed ? '' : 'optional'}">
      <span class="check-ic"><svg><use href="#${c.ok ? 'i-check' : 'i-x'}"/></svg></span>
      <div><div class="check-label">${esc(c.label)}${c.needed ? '' : ` <span class="muted small">${esc(t('su.optional'))}</span>`}</div>
      <div class="check-detail">${esc(c.detail)}</div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">${extra.join('')}</div></li>`;
  }).join('');
  const mcp = S.state.mcp || {};
  $('#jiraUrl').placeholder = (mcp.jira && mcp.jira.url) || '';
  $('#collabUrl').placeholder = (mcp.collab && mcp.collab.url) || '';
  if (S.doctor.checks.some((c) => c.action === 'tokens' && !c.ok)) $('#tokenForm').hidden = false;
  for (const b of $('#checkList').querySelectorAll('[data-fix]')) {
    b.onclick = guard(async () => { b.disabled = true; b.textContent = t('su.installing'); await api('/api/doctor/fix', { body: { id: b.dataset.fix } }); });
  }
  for (const b of $('#checkList').querySelectorAll('[data-action]')) {
    b.onclick = guard(async () => {
      const a = b.dataset.action;
      if (a === 'tokens') { $('#tokenForm').hidden = false; $('#jiraToken').focus(); return; }
      b.disabled = true;
      if (a === 'm365_login') { await api('/api/doctor/m365-login', { body: {} }); toast(t('su.m365Opening')); }
      if (a === 'm365_check') await api('/api/doctor/m365-check', { body: {} });
      if (a === 'mcp_test') await api('/api/doctor/mcp-test', { body: {} });
      if (a === 'copilot_login') { await api('/api/doctor/copilot-login', { body: {} }); toast(t('su.copilotOpening')); }
      if (a === 'copilot_check') { await api('/api/doctor/copilot-check', { body: {} }); toast(t('su.verifying')); }
    });
  }
}
$('#fixAll').onclick = guard(async () => { $('#doctorLog').textContent = ''; await api('/api/doctor/fix-all', { body: {} }); toast(t('su.fixing')); });
$('#recheck').onclick = guard(refreshDoctor);
$('#tokenForm').onsubmit = guard(async (e) => {
  e.preventDefault();
  const body = { jira_token: $('#jiraToken').value, collab_token: $('#collabToken').value, jira_url: $('#jiraUrl').value, collab_url: $('#collabUrl').value };
  if (!body.jira_token && !body.collab_token) throw new Error(t('su.needToken'));
  S.state.mcp = await api('/api/mcp/tokens', { body });
  $('#jiraToken').value = $('#collabToken').value = '';
  $('#tokenForm').hidden = true;
  toast(t('su.tokensSaved'));
  refreshDoctor();
});

// ---------------------------------------------------------------- settings modal
$('#settingsBtn').onclick = () => {
  const s = S.state.settings;
  for (const el of $('#settingsForm').elements) {
    if (!el.name) continue;
    if (el.type === 'checkbox') el.checked = !!s[el.name];
    else el.value = s[el.name] ?? '';
  }
  openModal('#settingsModal');
};
$('#settingsForm').onsubmit = guard(async (e) => {
  e.preventDefault();
  const patch = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    patch[el.name] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value.trim();
  }
  S.state.settings = await api('/api/settings', { body: patch });
  closeModals();
  toast(t('toast.settings'));
});

boot().catch((e) => toast(e.message, true));
