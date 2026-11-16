'use strict';
/* Ticket Analyzer UI: flow diagram, live progress (SSE), artifact drawer, setup. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => Markdown.esc(s ?? '');
const t = (k, v) => I18N.t(k, v);

const STATUS_ICON = { idle: null, running: 'i-gear', done: 'i-check', error: 'i-x', skipped: 'i-skip', stale: 'i-refresh', waiting: 'i-pause' };
const MAX_VIEW = 400000; // chars rendered inline; bigger files open in a new tab
const PREVIEW_STEP_MS = 500;

const S = {
  state: null,           // /api/state
  run: null,             // current run object (null = draft)
  draft: { log_files: [], source_dir: '', gate: 'skip', auto_attachments: true, use_codegraph: true,
    use_infographic: true, auto_publish_to_collab: false },
  selected: null,        // step id open in the drawer
  tab: null,             // artifact name open in the drawer
  editing: false,
  doctor: null,
  consoleTab: 'log',
  preview: null,          // local-only workflow simulation
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

// A new ticket starts from the "defaults for a new ticket" in Settings.
function freshDraft() {
  const s = S.state.settings;
  return { log_files: [], source_dir: '', gate: s.default_gate || 'skip',
    auto_attachments: s.default_use_attachments !== false, use_codegraph: s.default_use_codegraph !== false,
    use_infographic: s.default_use_infographic !== false, auto_publish_to_collab: !s.review_before_publish };
}

// ---------------------------------------------------------------- boot
async function boot() {
  I18N.apply();
  S.state = await api('/api/state');
  I18N.set(S.state.settings.lang);
  S.draft = freshDraft();
  $('#copilotModels').innerHTML = ((S.state.models || {}).copilot || [])
    .map((m) => `<option value="${esc(m)}"></option>`).join('');
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
      if (m.busy && S.preview) stopPreview();
      S.state.busy = m.busy; S.state.busy_run = m.busy ? m.run_id : null; renderInputs(); renderButtons(); renderFlow();
      if (!m.busy) { maybePromptGate(); refreshDoctor(); }
      if (S.selected === 'log') refreshDrawer();
    } else if (m.type === 'doctor_log') {
      const el = $('#doctorLog'); el.textContent += m.line + '\n'; el.scrollTop = el.scrollHeight;
    } else if (m.type === 'doctor_changed') {
      refreshDoctor();
    } else if (m.type === 'runs_cleared') {
      if (S.run && S.run.id !== m.keep) $('#newRun').click();
      scheduleHistory();
    } else if (m.type.startsWith('assistant')) {
      if (window.Assistant) Assistant.onEvent(m);
    } else if (m.type === 'settings') {
      S.state.settings = m.settings; renderMode();
      if (window.Assistant) Assistant.sync();
      if (!S.run) {
        S.draft = { ...freshDraft(), log_files: S.draft.log_files, source_dir: S.draft.source_dir };
        renderInputs();
      }
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
  if (document.activeElement !== $('#modelInput')) $('#modelInput').value = currentModel();
  renderModelMenu();
}
const currentModel = () => {
  const s = S.state.settings;
  return s.ai_mode === 'copilot' ? (s.copilot_model || 'auto') : s.m365_model;
};
const modelItems = () => [...$('#modelMenu').querySelectorAll('[role="option"]')];

function renderModelMenu() {
  const s = S.state.settings;
  const known = (S.state.models || {})[s.ai_mode] || [];
  const cur = currentModel();
  const list = !cur || known.includes(cur) ? known : [cur, ...known];
  const check = '<svg viewBox="0 0 24 24" width="14" height="14"><use href="#i-check"/></svg>';
  $('#modelMenu').innerHTML = list.map((m) => `<li role="option" data-value="${esc(m)}" aria-selected="${m === cur}"><span>${esc(m)}</span>${m === cur ? check : ''}</li>`).join('')
    + `<li class="model-hint" role="presentation">${esc(t('model.hint'))}</li>`;
}

function openModelMenu(open) {
  $('#modelMenu').hidden = !open;
  $('#modelInput').setAttribute('aria-expanded', String(open));
  if (open) renderModelMenu();
}

const saveModel = guard(async (raw) => {
  const value = raw.trim();
  const copilotMode = S.state.settings.ai_mode === 'copilot';
  if (!value && !copilotMode) { renderMode(); return; }
  const patch = copilotMode ? { copilot_model: value === 'auto' ? '' : value } : { m365_model: value };
  S.state.settings = await api('/api/settings', { body: patch });
  renderMode();
  toast(t('model.saved', { m: value || 'auto' }));
});

function commitModel() {
  const value = $('#modelInput').value.trim();
  if (!value || value === currentModel()) { $('#modelInput').value = currentModel(); return; }
  return saveModel(value);
}

$('#modelInput').onfocus = () => { $('#modelInput').select(); openModelMenu(true); };
$('#modelInput').onblur = () => { openModelMenu(false); commitModel(); };
$('#modelInput').onkeydown = (e) => {
  const input = e.target;
  if (e.key === 'Enter') { input.blur(); return; }
  if (e.key === 'Escape') { input.value = currentModel(); input.blur(); return; }
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  e.preventDefault();
  openModelMenu(true);
  const items = modelItems();
  if (!items.length) return;
  const step = e.key === 'ArrowDown' ? 1 : -1;
  const now = items.findIndex((li) => li.classList.contains('hot'));
  const next = now < 0 ? items.findIndex((li) => li.dataset.value === currentModel()) + step : now + step;
  const target = items[(next + items.length) % items.length];
  items.forEach((li) => li.classList.toggle('hot', li === target));
  target.scrollIntoView({ block: 'nearest' });
  input.value = target.dataset.value;
};
$('#modelToggle').onmousedown = (e) => e.preventDefault();
$('#modelToggle').onclick = () => {
  if ($('#modelMenu').hidden) $('#modelInput').focus(); else openModelMenu(false);
};
$('#modelMenu').onmousedown = (e) => e.preventDefault();
$('#modelMenu').onclick = guard(async (e) => {
  const li = e.target.closest('[data-value]');
  if (!li) return;
  openModelMenu(false);
  $('#modelInput').value = li.dataset.value;
  await saveModel(li.dataset.value);
  $('#modelInput').blur();
});
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

$('#themeBtn').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('ta.theme', next); } catch { /* storage blocked */ }
  $('#themeBtn').setAttribute('aria-pressed', String(next === 'dark'));
};
$('#themeBtn').setAttribute('aria-pressed', String(document.documentElement.dataset.theme === 'dark'));

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
  const locale = I18N.locale;
  ul.innerHTML = runs.map((r) => {
    const when = new Date(r.created * 1000).toLocaleString(locale, { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' });
    const tag = r.collab && r.collab.page_id ? '<span class="tag ok">Collab</span>'
      : r.failed ? `<span class="tag err">${esc(t('history.failed'))}</span>` : `<span class="tag">${r.done}/8</span>`;
    return `<li class="run-item ${S.run && S.run.id === r.id ? 'active' : ''}" data-id="${esc(r.id)}">
      <div class="k">${esc(r.issue)} ${tag}</div><div class="s">${esc(r.summary || '—')}</div><div class="t">${when}</div></li>`;
  }).join('');
  for (const li of ul.querySelectorAll('.run-item')) li.onclick = () => openRun(li.dataset.id);
}

async function openRun(id) {
  try { S.run = await api(`/api/runs/${encodeURIComponent(id)}`); } catch (e) { return toast(e.message, true); }
  $('#console').textContent = '';
  $('#consoleCount').textContent = '';
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
  S.draft = freshDraft();
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
const logOptions = () => {
  const options = { ...S.state.logsleuth.defaults, budget_tokens: S.state.settings.log_budget_tokens, ...(inputs().logsleuth || {}) };
  options.budget_tokens = Math.min(options.budget_tokens, S.state.settings.log_budget_tokens);
  return options;
};

function renderInputs() {
  const files = inputs().log_files;
  $('#logChips').innerHTML = files.length
    ? `<span class="chip" title="${esc(files.map((f) => f.split(/[\\/]/).pop()).join('\n'))}"><span>${esc(t('in.logsCount', { n: files.length }))}</span><button aria-label="${esc(t('in.clear'))}" title="${esc(t('in.clear'))}">×</button></span>`
    : `<span class="muted small">${esc(t('in.nologs'))}</span>`;
  for (const b of $('#logChips').querySelectorAll('button')) b.onclick = guard(() => setInputs({ log_files: [] }));
  if (document.activeElement !== $('#source')) $('#source').value = inputs().source_dir || '';
  $('#autoAtt').checked = !!inputs().auto_attachments;
  $('#logMode').value = logOptions().mode;
  $('#logMode').disabled = !!S.state.busy;
  $('#useCodeGraph').checked = inputs().use_codegraph !== false;
  for (const b of document.querySelectorAll('.gate-btn')) {
    b.setAttribute('aria-checked', String(b.dataset.gate === (inputs().gate || 'skip')));
    b.disabled = !!S.state.busy;
  }
  $('#infographicAuto').checked = inputs().use_infographic !== false;
  $('#infographicAuto').disabled = !!S.state.busy;
  $('#autoPublishCollab').checked = inputs().auto_publish_to_collab === true;
  $('#autoPublishCollab').disabled = !!S.state.busy;
  $('#issue').disabled = !!S.run;
}
$('#autoAtt').onchange = guard(() => setInputs({ auto_attachments: $('#autoAtt').checked }));
$('#useCodeGraph').onchange = guard(() => setInputs({ use_codegraph: $('#useCodeGraph').checked }));
for (const b of document.querySelectorAll('.gate-btn')) {
  b.onclick = guard(async () => {
    await setInputs({ gate: b.dataset.gate });
    toast(t('gate.saved', { m: t('gate.' + b.dataset.gate) }));
  });
}
$('#infographicAuto').onchange = guard(() => setInputs({ use_infographic: $('#infographicAuto').checked }));
$('#autoPublishCollab').onchange = guard(async () => {
  const enabled = $('#autoPublishCollab').checked;
  await setInputs({ auto_publish_to_collab: enabled });
  if (enabled && S.run && S.run.steps.collab.status === 'waiting') {
    await startStep('collab', true, true);
  }
});
$('#logMode').onchange = guard(async () => {
  await setInputs({ logsleuth: { ...logOptions(), mode: $('#logMode').value } });
  if (S.selected === 'log') renderDrawer(true);
});

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
$('#resumeBtn').onclick = guard(async () => {
  await api(`/api/runs/${encodeURIComponent(S.run.id)}/start`, { body: { resume: true } });
  S.state.busy = true;
  renderButtons();
});
$('#issue').onkeydown = (e) => { if (e.key === 'Enter') $('#runAll').click(); };
$('#issue').oninput = () => { if (!S.run) { renderFlow(); renderButtons(); } };
$('#stopBtn').onclick = guard(() => api('/api/stop', { body: {} }));

// "Continue" makes sense when the run moved forward but has steps left that are not just the publish gate.
function resumable() {
  if (!S.run || S.state.busy || S.preview) return false;
  const statuses = Object.values(S.run.steps).map((st) => st.status);
  if (!statuses.some((x) => x === 'done')) return false;
  if (S.run.pending_gate || S.run.pending_tz) return false; // the gate / time-zone panel is the way forward
  return statuses.some((x) => ['idle', 'stale', 'error'].includes(x))
    || (S.run.steps.log.status === 'waiting');
}

function renderButtons() {
  const busy = S.state.busy;
  $('#runAll').hidden = busy;
  $('#stopBtn').hidden = !busy;
  $('#resumeBtn').hidden = busy || !resumable();
  $('#runAll').innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18"><use href="#i-play"/></svg> ${esc(t(S.run ? 'run.again' : 'run.all'))}`;
  $('#runOnly').disabled = $('#runFrom').disabled = busy || !(S.run || $('#issue').value.trim());
  const previewing = !!S.preview;
  const previewButton = $('#previewBtn');
  previewButton.disabled = busy && !previewing;
  previewButton.classList.toggle('btn-primary', !previewing);
  previewButton.classList.toggle('btn-danger', previewing);
  previewButton.setAttribute('aria-pressed', String(previewing));
  const previewLabel = t(previewing ? 'preview.stop' : 'preview.start');
  const previewTitle = t(previewing ? 'preview.stopTitle' : 'preview.startTitle');
  $('#previewBtnText').textContent = previewLabel;
  $('#previewIcon').setAttribute('href', previewing ? '#i-stop' : '#i-play');
  previewButton.title = previewTitle;
  previewButton.setAttribute('aria-label', previewTitle);
}

function setPreviewUiLocked(preview, locked) {
  if (locked) {
    preview.inertStates = new Map();
    const targets = [
      ...document.querySelectorAll('.topbar > :not(#previewBtn)'),
      ...document.querySelectorAll('.layout, #drawer, #scrim, .modal'),
    ];
    for (const el of targets) {
      preview.inertStates.set(el, el.inert);
      el.inert = true;
    }
    document.body.classList.add('preview-active');
    return;
  }
  for (const [el, wasInert] of preview.inertStates || []) el.inert = wasInert;
  document.body.classList.remove('preview-active');
}

function previewStepDelay(preview) {
  return new Promise((resolve) => {
    preview.resolveDelay = resolve;
    preview.timer = setTimeout(() => {
      preview.timer = null;
      preview.resolveDelay = null;
      resolve();
    }, PREVIEW_STEP_MS);
  });
}

function finishPreview(preview) {
  if (!preview || S.preview !== preview) return;
  if (preview.timer) clearTimeout(preview.timer);
  preview.timer = null;
  preview.resolveDelay = null;
  S.preview = null;
  setPreviewUiLocked(preview, false);
  renderFlow();
  renderButtons();
}

function stopPreview() {
  const preview = S.preview;
  if (!preview) return;
  if (preview.timer) clearTimeout(preview.timer);
  preview.timer = null;
  const resolve = preview.resolveDelay;
  preview.resolveDelay = null;
  S.preview = null;
  setPreviewUiLocked(preview, false);
  renderFlow();
  renderButtons();
  if (resolve) resolve();
}

async function runPreview() {
  if (S.state.busy) return;
  const skipped = new Set(S.state.steps
    .filter((step) => stepState(step.id).status === 'skipped' || !!optionalReason(step.id))
    .map((step) => step.id));
  const preview = { current: null, completed: new Set(), skipped, timer: null, resolveDelay: null, inertStates: null };
  S.preview = preview;
  setPreviewUiLocked(preview, true);
  renderButtons();
  renderFlow();

  for (const step of S.state.steps) {
    if (preview.skipped.has(step.id)) continue;
    if (S.preview !== preview) return;
    preview.current = step.id;
    renderFlow();
    await previewStepDelay(preview);
    if (S.preview !== preview) return;
    preview.completed.add(step.id);
  }
  finishPreview(preview);
}

$('#previewBtn').onclick = guard(async () => {
  if (S.preview) { stopPreview(); return; }
  await runPreview();
});

// ---------------------------------------------------------------- flow
function stepState(id) {
  return S.run ? S.run.steps[id] : { status: 'idle', note: '', error: '', artifacts: [] };
}

function stepElapsedSeconds(st, now = Date.now() / 1000) {
  if (!st) return 0;
  let accumulated = Number(st.elapsed_seconds);
  if (!Number.isFinite(accumulated)) {
    accumulated = st.started != null && st.ended != null ? Math.max(0, Number(st.ended) - Number(st.started)) : 0;
  }
  if (st.status === 'running' && st.started != null) accumulated += Math.max(0, now - Number(st.started));
  return Math.max(0, accumulated);
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = total % 60;
  if (hours) return `${hours}h ${minutes}m ${remaining}s`;
  if (total >= 60) return `${minutes}m ${remaining}s`;
  return `${remaining}s`;
}

function duration(st) {
  if (!st || (st.started == null && !(Number(st.elapsed_seconds) > 0))) return '';
  return formatDuration(stepElapsedSeconds(st));
}

// What each step needs before it can run (besides its own inputs).
const DEPS = { jira: [], understand: ['jira'], log: ['jira'], graph: ['jira'], analyze: ['jira'], format: ['analyze'], infographic: ['analyze'], collab: ['format'] };
const isDone = (id) => S.run && ['done', 'stale', 'waiting'].includes(S.run.steps[id].status);

// Logs may come from picked files or from the ticket's own attachments (unknown until Jira ran).
function mayHaveLogs() {
  const inp = inputs();
  if (inp.log_files.length) return true;
  if (!inp.auto_attachments) return false;
  if (S.run && isDone('jira')) {
    return (S.run.attachments || []).some((a) => a.kind !== 'other');
  }
  return true; // not fetched yet: assume the ticket may have logs
}

function hasInputs(id) {
  const inp = inputs();
  if (id === 'jira') return !!S.run || !!$('#issue').value.trim();
  if (id === 'understand') return mayHaveLogs() && inp.gate !== 'manual';
  if (id === 'log') return mayHaveLogs() && !(S.run && (S.run.gate_state || {}).skip_logs);
  if (id === 'graph') return !!inp.source_dir && inp.use_codegraph !== false;
  if (id === 'infographic') return inp.use_infographic !== false;
  return true;
}

function optionalReason(id) {
  const inp = inputs();
  if (id === 'understand') {
    if (inp.gate === 'manual' && mayHaveLogs()) return 'opt.manualGate';
    return mayHaveLogs() ? null : 'opt.needLogs';
  }
  if (id === 'log') {
    if (S.run && (S.run.gate_state || {}).skip_logs) return 'opt.logsSkipped';
    return !mayHaveLogs() ? 'opt.needLogs' : null;
  }
  if (id === 'graph') return inp.use_codegraph === false ? 'opt.graphDisabled' : !inp.source_dir ? 'opt.needSource' : null;
  if (id === 'infographic') return inp.use_infographic === false ? 'opt.infographicDisabled' : null;
  return null;
}

/** 'ready' = can run now, 'queued' = will run in this pipeline after earlier steps, 'blocked' = cannot run yet. */
const willRun = (id) => hasInputs(id) && DEPS[id].every(willRun);
function readiness(id, status) {
  if (!['idle', 'stale'].includes(status)) return { kind: null, missing: [] };
  const missing = DEPS[id].filter((d) => !isDone(d));
  if (!S.run && $('#issue').value.trim() && willRun(id)) return { kind: missing.length ? 'queued' : 'ready', missing };
  return { kind: missing.length || !hasInputs(id) ? 'blocked' : 'ready', missing };
}

function renderFlow() {
  const preview = S.preview;
  const runningHere = !preview && !!S.state.busy && (!S.state.busy_run || !S.run || S.state.busy_run === S.run.id);
  $('#flow').innerHTML = S.state.steps.map((s, i) => {
    const sourceState = stepState(s.id);
    let st = sourceState;
    let status = st.status;
    const skipReason = optionalReason(s.id);
    if (preview) {
      if (preview.skipped.has(s.id)) status = 'skipped';
      else if (preview.completed.has(s.id)) status = 'done';
      else if (preview.current === s.id) status = 'running';
      else status = 'idle';
      st = {
        status,
        note: status === 'skipped' && sourceState.status === 'skipped' ? sourceState.note : '',
        error: '',
      };
    }
    if (!preview && skipReason && ['idle', 'stale'].includes(status)) status = 'skipped';
    // While any step runs, every other not-yet-done step just waits its turn.
    const waiting = !preview && runningHere && ['idle', 'stale'].includes(status);
    if (waiting) status = 'idle';
    const r = preview
      ? { kind: status === 'idle' ? 'wait' : null, missing: [] }
      : waiting ? { kind: 'wait', missing: [] } : readiness(s.id, status);
    const pending = status === 'idle' ? r.kind : null;
    const label = pending === 'ready' ? t('st.ready') : pending === 'wait' ? t('st.wait') : pending === 'queued' ? t('st.queued') : t('st.' + status);
    const icon = pending === 'ready' ? 'i-play' : pending === 'wait' ? 'i-clock' : STATUS_ICON[status];
    const needs = r.kind === 'blocked' && r.missing.length
      ? `<div class="node-note muted">${esc(t('st.needs', { steps: r.missing.map((d) => t('step.' + d)).join(', ') }))}</div>` : '';
    const note = st.error ? `<div class="node-note err">${esc(st.error)}</div>`
      : st.note ? `<div class="node-note">${esc(st.note)}</div>`
        : status === 'skipped' && skipReason ? `<div class="node-note muted">${esc(t('opt.prefix', { why: t(skipReason) }))}</div>`
          : needs;
    const gear = status === 'running'
      ? '<span class="gear"><svg><use href="#i-gear"/></svg></span><span class="gear small"><svg><use href="#i-gear"/></svg></span>' : '';
    const publish = s.id === 'collab' && status === 'waiting'
      ? `<button class="btn btn-primary btn-sm publish-btn" data-publish>${esc(t('publish'))}</button>`
      : s.id === 'log' && status === 'waiting' && S.run && (S.run.pending_gate || S.run.pending_tz) && !S.state.busy
        ? `<button class="btn btn-primary btn-sm publish-btn" data-gate-open>${esc(t(S.run.pending_gate ? 'gate.open' : 'tz.open'))}</button>` : '';
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
  const gate = $('#flow [data-gate-open]');
  if (gate) gate.onclick = () => openDrawer('log');
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
  renderUsagePanel();
  maybePromptGate();
  renderAttProgress();
  if (window.Assistant) Assistant.sync();
}

function formatCount(value) {
  const count = Math.max(0, Math.round(Number(value) || 0));
  return new Intl.NumberFormat(I18N.locale).format(count);
}

function renderUsagePanel() {
  const panel = $('#usagePanel');
  if (!panel) return;
  if (!S.run) {
    panel.innerHTML = `<p class="usage-empty">${esc(t('usage.noRun'))}</p>`;
    return;
  }

  const total = S.run.usage_total || {};
  const hasUsage = Number(total.model_calls) > 0;
  const estMark = total.estimated ? ' ~' : '';
  const totalTokens = hasUsage ? Number(total.total_tokens || 0) : null;
  const totalSeconds = S.state.steps.reduce((sum, step) => sum + stepElapsedSeconds(S.run.steps[step.id]), 0);
  const tokenNote = hasUsage
    ? t('usage.modelCalls', { n: formatCount(total.model_calls) }) + (total.estimated ? ' · ' + t('usage.estimated') : '')
    : t('usage.notMeasured');
  const tokenBreakdown = hasUsage
    ? `<div class="usage-detail-grid">
        <div><span>${esc(t('usage.inputTokens'))}</span><strong>${formatCount(total.input_tokens)}</strong></div>
        <div><span>${esc(t('usage.outputTokens'))}</span><strong>${formatCount(total.output_tokens)}</strong></div>
        <div><span>${esc(t('usage.cacheReadTokens'))}</span><strong>${formatCount(total.cache_read_tokens)}</strong></div>
        <div><span>${esc(t('usage.cacheWriteTokens'))}</span><strong>${formatCount(total.cache_write_tokens)}</strong></div>
        <div><span>${esc(t('usage.reasoningTokens'))}</span><strong>${formatCount(total.reasoning_tokens)}</strong></div>
      </div><p class="usage-note">${esc(tokenNote)} · ${esc(t('usage.tokenDefinition'))}</p>`
    : `<p class="usage-note">${esc(tokenNote)}</p>`;
  // One row per press of Run ("lượt chạy"): re-running the same ticket adds a pass, never overwrites.
  const passes = (S.run.usage_passes || []).map((pass) => {
    const mark = pass.estimated ? '~' : '';
    const open = pass.ended == null ? ' <span class="tag">' + esc(t('usage.passRunning')) + '</span>' : '';
    return `<li class="usage-step"><span class="usage-step-name">${esc(t('usage.pass', { n: pass.n }))}
      <small>${esc(t('step.' + (pass.from || 'jira')))} · ${esc(pass.ai_mode === 'copilot' ? 'Copilot CLI' : 'M365')}${open}</small></span>
      <strong>${mark}${formatCount(pass.total_tokens)} tok · ${formatDuration(pass.seconds)}</strong></li>`;
  }).join('');
  const passSection = passes
    ? `<section class="usage-step-section"><h3>${esc(t('usage.passes', { n: (S.run.usage_passes || []).length }))}</h3>
       <ul class="usage-steps">${passes}</ul><p class="usage-note">${esc(t('usage.passHint'))}</p></section>` : '';
  const rows = S.state.steps.map((step) => {
    const state = S.run.steps[step.id] || {};
    const hasTime = state.started != null || state.ended != null || Number(state.elapsed_seconds) > 0;
    return `<li class="usage-step"><span class="usage-step-name">${esc(t('step.' + step.id))}<small>${esc(t('st.' + (state.status || 'idle')))}</small></span>
      <strong>${hasTime ? formatDuration(stepElapsedSeconds(state)) : '—'}</strong></li>`;
  }).join('');

  panel.innerHTML = `<div class="usage-content">
    <div class="usage-summary">
      <article class="usage-stat"><span class="usage-label">${esc(t('usage.totalTokens'))}</span>
        <strong class="usage-value">${totalTokens == null ? '—' : estMark + formatCount(totalTokens)}</strong>${tokenBreakdown}</article>
      <article class="usage-stat"><span class="usage-label">${esc(t('usage.totalTime'))}</span>
        <strong class="usage-value">${formatDuration(totalSeconds)}</strong><p class="usage-note">${esc(t('usage.stepSumHint'))}</p></article>
    </div>
    ${passSection}
    <section class="usage-step-section"><h3>${esc(t('usage.stepBreakdown'))}</h3><ul class="usage-steps">${rows}</ul></section>
  </div>`;
}

setInterval(() => {  // tick running durations
  if (!S.run) return;
  for (const el of document.querySelectorAll('[data-time]')) {
    const st = S.run.steps[el.dataset.time];
    if (st && st.status === 'running') el.textContent = duration(st);
  }
  if (S.consoleTab === 'usage') renderUsagePanel();
}, 1000);

// ---------------------------------------------------------------- console
function pushLog(line) {
  const el = $('#console');
  const cls = /✓/.test(line) ? 'l-ok' : /✗|lỗi|error/i.test(line) ? 'l-err' : /▶/.test(line) ? 'l-run' : '';
  el.insertAdjacentHTML('beforeend', `<span class="${cls}">${esc(line)}</span>\n`);
  el.scrollTop = el.scrollHeight;
  $('#consoleCount').textContent = t('console.lines', { n: el.childElementCount });
}

function selectConsoleTab(tab) {
  S.consoleTab = tab;
  const showLog = tab === 'log';
  $('#consoleLogTab').classList.toggle('active', showLog);
  $('#consoleLogTab').setAttribute('aria-selected', String(showLog));
  $('#consoleUsageTab').classList.toggle('active', !showLog);
  $('#consoleUsageTab').setAttribute('aria-selected', String(!showLog));
  $('#consoleLogPanel').hidden = !showLog;
  $('#usagePanel').hidden = showLog;
  $('#clearConsole').hidden = !showLog;
  $('#consoleCount').hidden = !showLog;
  const card = $('.console-card');
  if (card.classList.contains('collapsed')) {
    card.classList.remove('collapsed');
    $('#consoleToggle').setAttribute('aria-expanded', 'true');
  }
  if (!showLog) renderUsagePanel();
}
$('#consoleLogTab').onclick = () => selectConsoleTab('log');
$('#consoleUsageTab').onclick = () => selectConsoleTab('usage');
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
  box.classList.toggle('logsleuth-extra', step === 'log');
  if (step === 'log' && box.querySelector('#lsForm, .gate-panel, .tz-panel') && box.contains(document.activeElement) && !S.state.busy) return;
  let html = st.error ? `<div class="err-box" style="margin:0 0 10px">${esc(st.error)}</div>` : '';
  if (step === 'jira') {
    html += attachmentsPanel();
  } else if (step === 'log') {
    html += logPanel();
  } else if (step === 'understand') {
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
  if (step === 'log' && $('#gateRun')) bindGatePanel();
  else if (step === 'log' && $('#tzRun')) bindTzPanel();
  else if (step === 'log' && $('#lsForm')) bindLogPanel();
}

// The evidence gate: confirm/enter which attachments to download, the time window and the clock shift.
function gatePanel() {
  const g = S.run.pending_gate;
  const f = S.run.focus || {};
  const review = g.mode === 'review';
  const list = (S.run.attachments || []).filter((a) => a.kind !== 'other');
  const suggested = new Set(g.suggested || []);
  const rows = list.map((a, i) => {
    const reason = g.reasons && g.reasons[a.filename] ? `<div class="muted small">${esc(g.reasons[a.filename])}</div>` : '';
    const badges = [a.analyzed ? t('att.analyzed') : a.path ? t('att.have') : ''].filter(Boolean)
      .map((b) => ` <span class="tag ok">${esc(b)}</span>`).join('');
    return `<label class="att-row"><input type="checkbox" data-gate-i="${i}" ${suggested.has(a.filename) ? 'checked' : ''}>
      <span class="att-name">${esc(a.filename)}${badges}${reason}</span>
      <span class="tag">${esc(t('att.kind.' + a.kind))}</span><span class="muted small att-size">${fmtSize(a.size || 0)}</span></label>`;
  }).join('');
  const picked = inputs().log_files || [];
  const shift = inputs().log_shift_minutes;
  const zone = f.ticket_utc_offset ? t('gate.zone', { z: f.ticket_utc_offset }) : t('gate.noZone');
  const reason = f.reason ? `<p class="muted small">AI: ${esc(f.reason)}</p>` : '';
  return `<div class="ls-panel gate-panel">
    ${g.problem ? `<div class="err-box" style="margin:0 0 10px">${esc(g.problem)}</div>` : ''}
    <p class="muted small">${esc(t(review ? 'gate.introReview' : 'gate.introManual'))}</p>
    ${reason}
    ${list.length ? `<div class="att-list">${rows}</div>` : `<p class="muted small">${esc(t('gate.noAttachments'))}</p>`}
    ${picked.length ? `<p class="muted small">${esc(t('gate.ownLogs', { n: picked.length }))}</p>` : ''}
    <button type="button" class="btn btn-sm" id="gateAddLogs">${esc(t('in.pickLogs'))}</button>
    <div class="ls-grid" style="margin-top:10px">
      <div class="field"><label for="gateStart">${esc(t('focus.from'))}</label><input id="gateStart" placeholder="HH:MM:SS" value="${esc(f.time_start || '')}"></div>
      <div class="field"><label for="gateEnd">${esc(t('focus.to'))}</label><input id="gateEnd" placeholder="HH:MM:SS" value="${esc(f.time_end || '')}"></div>
      <div class="field"><label for="gateShift">${esc(t('gate.shift'))}</label><input id="gateShift" type="number" step="30" min="-840" max="840" placeholder="${esc(t('gate.shiftAuto'))}" value="${shift == null ? '' : esc(String(shift))}"></div>
    </div>
    <p class="muted small">${esc(zone)} · ${esc(t('gate.shiftHelp'))}</p>
    <div class="ls-actions">
      <button type="button" class="btn btn-primary" id="gateRun">${esc(t('gate.run'))}</button>
      <button type="button" class="btn" id="gateSkipLogs">${esc(t('gate.skipLogs'))}</button>
    </div></div>`;
}

function bindGatePanel() {
  $('#gateAddLogs').onclick = guard(async () => {
    const { paths } = await api('/api/pick', { body: { kind: 'files' } });
    if (paths.length) { await setInputs({ log_files: [...new Set([...inputs().log_files, ...paths])] }); renderDrawer(false); }
  });
  const answer = (body) => guard(async () => {
    await api(`/api/runs/${encodeURIComponent(S.run.id)}/gate`, { body });
    S.state.busy = true;
    closeDrawer();
    renderButtons();
  });
  $('#gateRun').onclick = () => {
    const list = (S.run.attachments || []).filter((a) => a.kind !== 'other');
    const names = [...document.querySelectorAll('[data-gate-i]')].filter((b) => b.checked).map((b) => list[+b.dataset.gateI].filename);
    const shiftText = $('#gateShift').value.trim();
    const body = { names, time_start: $('#gateStart').value.trim(), time_end: $('#gateEnd').value.trim() };
    if (shiftText !== '') body.shift_minutes = Number(shiftText);
    else body.shift_minutes = null;
    if (!names.length && !inputs().log_files.length) { toast(t('gate.needSomething'), true); return; }
    answer(body)();
  };
  $('#gateSkipLogs').onclick = answer({ skip_logs: true });
}

// Time zone of the ticket's incident time (Review / Manual, when the ticket states none): pick a suggested
// alignment, set the zones yourself, or drop the time filter. Previews are checked against each log's range.
const TZ_OFFSETS = [-720, -660, -600, -570, -540, -480, -420, -360, -300, -240, -210, -180, -120, -60, 0, 60, 120, 180,
  210, 240, 270, 300, 330, 345, 360, 390, 420, 480, 525, 540, 570, 600, 630, 660, 720, 765, 780, 840];
const fmtOffset = (m) => `${m < 0 ? '-' : '+'}${String(Math.floor(Math.abs(m) / 60)).padStart(2, '0')}:${String(Math.abs(m) % 60).padStart(2, '0')}`;
const fmtShift = (m) => (m === 0 ? '0' : `${m > 0 ? '+' : '−'}${Math.abs(m) % 60 ? (Math.abs(m) / 60).toFixed(2).replace(/0+$/, '') : Math.abs(m) / 60}h`);
const DAY_MS = 86400000;
function clockMs(c) {
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,3}))?)?$/.exec(String(c || '').trim());
  if (!m || +m[1] > 23 || +m[2] > 59 || +(m[3] || 0) > 59) return null;
  return ((+m[1] * 60 + +m[2]) * 60 + +(m[3] || 0)) * 1000 + +String(m[4] || '0').padEnd(3, '0').slice(0, 3);
}
function shiftClock(c, minutes) {
  const ms = clockMs(c);
  if (ms == null) return null;
  const v = (((ms + minutes * 60000) % DAY_MS) + DAY_MS) % DAY_MS;
  const sec = Math.floor(v / 1000);
  return [Math.floor(sec / 3600), Math.floor(sec / 60) % 60, sec % 60].map((x) => String(x).padStart(2, '0')).join(':');
}
function windowCovered(spans, start, end) {  // mirrors timealign.window_covered
  let a = clockMs(start), b = clockMs(end);
  if (a == null && b == null) return null;
  if (a == null) a = b;
  if (b == null) b = a;
  const seg = (x, y) => (x <= y ? [[x, y]] : [[x, DAY_MS], [0, y]]);
  let known = false;
  for (const sp of spans || []) {
    const f = clockMs(sp.first), l = clockMs(sp.last);
    if (f == null || l == null) continue;
    known = true;
    if (sp.wraps) return true;
    for (const [x1, x2] of seg(a, b)) for (const [y1, y2] of seg(f, l)) if (x1 <= y2 && y1 <= x2) return true;
  }
  return known ? false : null;
}
const coverTag = (c) => (c === true ? `<span class="tag ok">${esc(t('tz.inside'))}</span>`
  : c === false ? `<span class="tag bad">${esc(t('tz.outside'))}</span>` : `<span class="tag">${esc(t('tz.unknownRange'))}</span>`);

function tzCandidates() {
  const z = S.run.pending_tz, sug = z.suggested || {};
  const list = (z.candidates || []).map((c) => ({ ...c }));
  if (sug.shift_minutes != null && !list.some((c) => c.shift_minutes === sug.shift_minutes)) list.push({ ...sug });
  return list;
}

function offsetMinutes(text) {
  const m = /^([+-])(\d{2}):(\d{2})$/.exec(text || '');
  return m ? (m[1] === '-' ? -1 : 1) * (+m[2] * 60 + +m[3]) : null;
}

function tzPanel() {
  const z = S.run.pending_tz, sug = z.suggested || {};
  const spans = (z.spans || []).map((sp) => `<tr><td>${esc(sp.name)}</td><td>${esc(sp.first_ts || '—')}</td><td>${esc(sp.last_ts || '—')}</td></tr>`).join('');
  const cands = tzCandidates().map((c, i) => `
    <label class="tz-row"><input type="radio" name="tzPick" value="${i}" ${c.shift_minutes === sug.shift_minutes ? 'checked' : ''}>
      <span><b>${esc(t('tz.shiftLabel', { s: fmtShift(c.shift_minutes) }))}</b> · ${esc(t('tz.src.' + c.source))}
        ${c.shift_minutes === sug.shift_minutes ? `<span class="tag accent">${esc(t('tz.suggested'))}</span>` : ''}
        <span class="muted small tz-line">${esc(t('tz.window'))}: <code>${esc(c.time_start || '…')} → ${esc(c.time_end || '…')}</code> ${coverTag(c.covered)}</span>
        ${c.shift_minutes === sug.shift_minutes && sug.source === 'ai' && sug.reason ? `<span class="muted small tz-line">${esc(sug.reason)}</span>` : ''}</span></label>`).join('');
  const local = -new Date().getTimezoneOffset();
  const jira = offsetMinutes(z.jira_offset);
  const ticketDefault = jira != null && TZ_OFFSETS.includes(jira) ? jira : (TZ_OFFSETS.includes(local) ? local : 0);
  const opts = (sel) => TZ_OFFSETS.map((m) => `<option value="${m}" ${m === sel ? 'selected' : ''}>UTC${fmtOffset(m)}${m === jira ? ' · Jira' : ''}${m === local ? ' · ' + t('tz.yourPc') : ''}</option>`).join('');
  return `<div class="ls-panel tz-panel">
    <div class="warn-box">${esc(t('tz.intro', { start: z.ticket_start || '…', end: z.ticket_end || '…' }))}${z.incident_date ? ` (${esc(z.incident_date)})` : ''}</div>
    ${spans ? `<p class="muted small" style="margin:10px 0 4px">${esc(t('tz.logRanges'))}</p>
      <div class="tz-spans"><table><thead><tr><th>${esc(t('tz.file'))}</th><th>${esc(t('tz.first'))}</th><th>${esc(t('tz.last'))}</th></tr></thead><tbody>${spans}</tbody></table></div>` : ''}
    <fieldset class="tz-group"><legend>${esc(t('tz.optSuggested'))}</legend>${cands || `<p class="muted small">${esc(t('tz.noCandidates'))}</p>`}</fieldset>
    <fieldset class="tz-group"><legend><label class="tz-row inline"><input type="radio" name="tzPick" value="manual"> ${esc(t('tz.optManual'))}</label></legend>
      <div class="ls-grid tz-manual">
        <div class="field"><label for="tzTicket">${esc(t('tz.ticketZone'))}</label><select id="tzTicket">${opts(ticketDefault)}</select></div>
        <div class="field"><label for="tzLog">${esc(t('tz.logZone'))}</label><select id="tzLog">${opts(ticketDefault)}</select></div>
        <div class="field"><label for="tzStart">${esc(t('tz.from'))}</label><input id="tzStart" placeholder="HH:MM:SS" value="${esc(z.ticket_start || '')}"></div>
        <div class="field"><label for="tzEnd">${esc(t('tz.to'))}</label><input id="tzEnd" placeholder="HH:MM:SS" value="${esc(z.ticket_end || '')}"></div>
      </div>
      <p class="muted small" id="tzPreview"></p></fieldset>
    <label class="tz-row tz-bypass"><input type="radio" name="tzPick" value="bypass"> <span><b>${esc(t('tz.optBypass'))}</b>
      <span class="muted small tz-line">${esc(t('tz.bypassHelp'))}</span></span></label>
    <div class="ls-actions">
      <button type="button" class="btn btn-primary" id="tzRun">${esc(t('tz.apply'))}</button>
      <button type="button" class="btn" id="tzAsk">${esc(t('tz.askAi'))}</button>
    </div></div>`;
}

function bindTzPanel() {
  const z = S.run.pending_tz;
  const picked = () => (document.querySelector('[name="tzPick"]:checked') || {}).value;
  const manual = () => {
    const ticket = Number($('#tzTicket').value), log = Number($('#tzLog').value);
    return { ticket, shift: log - ticket, start: $('#tzStart').value.trim(), end: $('#tzEnd').value.trim() };
  };
  const preview = () => {
    const m = manual();
    const a = shiftClock(m.start, m.shift), b = shiftClock(m.end, m.shift);
    $('#tzPreview').innerHTML = `${esc(t('tz.shiftLabel', { s: fmtShift(m.shift) }))} · ${esc(t('tz.window'))}: <code>${esc(a || '…')} → ${esc(b || '…')}</code> ${coverTag(windowCovered(z.spans, a, b))}`;
    $('.tz-manual').classList.toggle('dim', picked() !== 'manual');
  };
  for (const el of document.querySelectorAll('.tz-manual select, .tz-manual input')) {
    el.addEventListener('input', () => { document.querySelector('[name="tzPick"][value="manual"]').checked = true; preview(); });
  }
  for (const r of document.querySelectorAll('[name="tzPick"]')) r.addEventListener('change', preview);
  preview();
  $('#tzRun').onclick = guard(async () => {
    const choice = picked();
    let body;
    if (choice === 'bypass') body = { mode: 'bypass' };
    else if (choice === 'manual') {
      const m = manual();
      body = { mode: 'manual', shift_minutes: m.shift, ticket_utc_offset: fmtOffset(m.ticket), time_start: m.start, time_end: m.end };
    } else if (choice != null) body = { mode: 'suggested', shift_minutes: tzCandidates()[Number(choice)].shift_minutes };
    else throw new Error(t('tz.pickOne'));
    await api(`/api/runs/${encodeURIComponent(S.run.id)}/timezone`, { body });
    S.state.busy = true;
    closeDrawer();
    renderButtons();
  });
  $('#tzAsk').onclick = guard(async () => { if (window.Assistant) await Assistant.ask(t('asst.q.tz')); });
}

// LogSleuth: the original Scan -> catalog/service selection -> Slice workflow.
function logPanel() {
  if (S.run && S.run.pending_gate && S.run.steps.log.status === 'waiting') return gatePanel();
  if (S.run && S.run.pending_tz && S.run.steps.log.status === 'waiting') return tzPanel();
  const o = logOptions(), manual = o.mode === 'manual', busy = !!S.state.busy;
  if (!manual) {
    const files = inputs().log_files || [];
    const source = inputs().source_dir || t('ls.noSource');
    const stats = S.run && S.run.steps.log.status === 'done' ? S.run.log_stats : null;
    const statsNote = stats
      ? `${t('ls.result', { n: stats.tokens, max: stats.budgetTokens, kept: stats.kept })}${stats.outputTruncated ? ' · ' + t('ls.truncated') : ''}${!stats.kept ? ' · ' + t('ls.empty') : ''}`
      : '';
    return `<div class="ls-panel"><p class="muted small">${esc(t('ls.autoHelp'))}</p>
      <p class="muted small">${esc(t('ls.autoInputs', { n: files.length, source }))}</p>
      ${statsNote ? `<p class="extra-note">${esc(statsNote)}</p>` : ''}</div>`;
  }
  const cat = (S.run && S.run.log_catalog) || {};
  const info = S.catalogInfo && S.catalogInfo.path === o.catalog_path ? S.catalogInfo : cat;
  const services = info.services || [];
  const f = (S.run && S.run.focus) || {};
  const number = (key, label, min, max) => `<div class="field"><label for="ls_${key}">${esc(t(label))}</label><input id="ls_${key}" name="${key}" type="number" min="${min}" max="${max}" value="${o[key]}" required></div>`;
  const list = (key, label) => `<div class="field span2"><label for="ls_${key}">${esc(t(label))}</label><textarea id="ls_${key}" name="${key}" rows="2">${esc(o[key].join('\n'))}</textarea></div>`;
  const s = S.run && S.run.steps.log.status === 'done' ? S.run.log_stats : null;
  const canProceed = !!(S.run && (s || S.run.steps.log.status === 'error'));
  return `<form id="lsForm" class="ls-panel" data-mode="manual"><fieldset ${busy ? 'disabled' : ''}>
    <legend>LogSleuth · Scan → Slice</legend><p class="muted small">${esc(t(manual ? 'ls.manualHelp' : 'ls.autoHelp'))}</p>
    <div class="ls-grid">${number('budget_tokens', 'cfg.budget', S.state.logsleuth.min_tokens, S.state.settings.log_budget_tokens)}</div>
    <p class="muted small">${esc(t('ls.capHelp', { n: S.state.settings.log_budget_tokens }))}</p>
    <fieldset><legend>1 · ${esc(t('ls.scan'))}</legend>
      <div class="field"><label>${esc(t('in.source'))}</label><div class="picker"><input id="lsSource" value="${esc(inputs().source_dir || '')}" readonly><button type="button" class="btn" id="lsPickSource">${esc(t('in.pickSource'))}</button></div></div>
      <p class="muted small">${esc(info.stats ? `${info.stats.entryCount || 0} statements · ${services.length} services · ${info.stats.filesScanned || 0} files` : t('ls.noCatalog'))}</p>
      <div class="ls-actions"><button type="button" class="btn" id="lsScan">${esc(t('ls.scan'))}</button><button type="button" class="btn" id="lsCatalogs">${esc(t('ls.catalogs'))}</button></div>
      <div class="field"><label for="lsCatalog">${esc(t('ls.catalog'))}</label><div class="picker"><input id="lsCatalog" value="${esc(o.catalog_path || cat.source_path || '')}" placeholder="catalog.md / catalog.json"><button type="button" class="btn" id="lsBrowseCatalog">${esc(t('att.pick'))}</button><button type="button" class="btn" id="lsLoadCatalog">${esc(t('ls.load'))}</button></div></div>
      <div id="lsCatalogList" class="ls-services" hidden></div>
    </fieldset>
    <fieldset><legend>2 · ${esc(t('ls.slice'))}</legend>
      <div class="ls-files">${inputs().log_files.map((file, i) => `<div class="ls-file"><span title="${esc(file)}">${esc(file.split(/[\\/]/).pop())}</span><button type="button" class="btn btn-sm" data-remove-log="${i}" aria-label="${esc(t('in.remove'))}">×</button></div>`).join('')}</div>
      <button type="button" class="btn btn-sm" id="lsAddLogs">${esc(t('in.pickLogs'))}</button>
      <div class="field"><div class="check-help"><label>${esc(t('ls.services'))}</label><button type="button" class="help-icon" data-help="ls.servicesHelp" title="${esc(t('help.open'))}" aria-label="${esc(t('help.open'))}">?</button></div><div class="ls-services">${services.map((x) => `<label class="check"><input type="checkbox" name="service" value="${esc(x.name)}" ${o.services.includes(x.name) ? 'checked' : ''} ${manual ? '' : 'disabled'}>${esc(x.name)} <span class="muted small">${x.statements} (${x.errors} E/F)</span></label>`).join('') || `<span class="muted small">${esc(t('ls.noCatalog'))}</span>`}</div><span class="muted small">${esc(t('ls.allServices'))}</span></div>
      <div class="check-help"><label class="check"><input type="checkbox" id="lsTime" ${o.time_enabled ? 'checked' : ''} ${manual ? '' : 'disabled'}>${esc(t('ls.timeEnabled'))}</label><button type="button" class="help-icon" data-help="ls.timeEnabledHelp" title="${esc(t('help.open'))}" aria-label="${esc(t('help.open'))}">?</button></div>
      <div class="ls-grid">
        <div class="field"><label for="lsStart">${esc(t('focus.from'))}</label><input id="lsStart" placeholder="HH:MM:SS.mmm" value="${esc(manual ? o.time_start : f.time_start || '')}" ${manual ? '' : 'disabled'}></div>
        <div class="field"><label for="lsEnd">${esc(t('focus.to'))}</label><input id="lsEnd" placeholder="HH:MM:SS.mmm" value="${esc(manual ? o.time_end : f.time_end || '')}" ${manual ? '' : 'disabled'}></div>
      </div>
      <details><summary>${esc(t('ls.tuning'))}</summary><div class="ls-grid">
        ${number('before', 'ls.before', 0, 100)}${number('after', 'ls.after', 0, 100)}
        ${number('window_ms', 'ls.windowMs', 0, 60000)}${number('max_repeat', 'ls.repeats', 1, 100)}
        ${number('min_score', 'ls.minScore', 0, 100)}${number('min_anchor_length', 'ls.anchor', 1, 200)}
        ${number('max_file_size_mb', 'ls.fileSize', 1, 100)}
        <div class="field"><label for="lsLevel">${esc(t('ls.level'))}</label><select id="lsLevel">${['', 'V', 'D', 'I', 'W', 'E', 'F'].map((v) => `<option value="${v}" ${o.min_level === v ? 'selected' : ''}>${v || 'All'}</option>`).join('')}</select></div>
        <div class="field span2"><label for="lsParser">Parser</label><select id="lsParser">${S.state.logsleuth.parsers.map((v) => `<option ${o.parser === v ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
        ${list('extra_grep', 'ls.grep')}${list('exclude', 'ls.exclude')}${list('exclude_dirs', 'ls.excludeDirs')}
      </div></details>
    </fieldset>
    <div class="ls-actions"><button type="submit" class="btn">${esc(t('cfg.save'))}</button><button type="button" class="btn btn-primary" id="lsSlice">${esc(t('ls.slice'))}</button>${canProceed ? `<button type="button" class="btn" id="lsProceed" title="${esc(t('ls.proceedHelp'))}">${esc(t('ls.proceed'))}</button>` : ''}</div>
    ${s ? `<p class="extra-note">${esc(t('ls.result', { n: s.tokens, max: s.budgetTokens, kept: s.kept }))}${s.outputTruncated ? ' · ' + esc(t('ls.truncated')) : ''}${!s.kept ? ' · ' + esc(t('ls.empty')) : ''}</p>` : ''}
    </fieldset></form>`;
}

function readLogForm() {
  const form = $('#lsForm'), o = { ...logOptions() };
  for (const key of ['budget_tokens', 'before', 'after', 'window_ms', 'max_repeat', 'min_score', 'min_anchor_length', 'max_file_size_mb']) o[key] = Number(form.elements[key].value);
  for (const key of ['extra_grep', 'exclude', 'exclude_dirs']) o[key] = form.elements[key].value.split('\n').map((v) => v.trim()).filter(Boolean);
  o.catalog_path = $('#lsCatalog').value.trim();
  if (form.dataset.mode === 'manual') {
    o.services = [...form.querySelectorAll('[name="service"]:checked')].map((el) => el.value);
    o.time_enabled = $('#lsTime').checked;
    o.time_start = $('#lsStart').value.trim(); o.time_end = $('#lsEnd').value.trim();
  }
  o.parser = $('#lsParser').value; o.min_level = $('#lsLevel').value;
  return o;
}

async function ensureRun() {
  if (S.run) return;
  const issue = $('#issue').value.trim();
  if (!issue) throw new Error(t('run.needIssue'));
  S.run = await api('/api/runs', { body: { issue, ...S.draft, start: false } });
  history.replaceState(null, '', `?run=${encodeURIComponent(S.run.id)}`);
  scheduleHistory();
}

function bindLogPanel() {
  const save = async () => { const options = readLogForm(); await setInputs({ logsleuth: options }); };
  $('#lsForm').onsubmit = guard(async (e) => { e.preventDefault(); await save(); document.activeElement.blur(); renderDrawer(false); toast(t('toast.saved')); });
  $('#lsPickSource').onclick = guard(async () => {
    await save(); const { paths } = await api('/api/pick', { body: { kind: 'folder' } });
    if (paths.length) { await setInputs({ source_dir: paths[0] }); document.activeElement.blur(); renderDrawer(false); }
  });
  $('#lsScan').onclick = guard(async () => {
    await save(); await ensureRun();
    await api(`/api/runs/${encodeURIComponent(S.run.id)}/log-scan`, { body: {} });
    S.state.busy = true; renderAll(); renderDrawer(false);
  });
  $('#lsBrowseCatalog').onclick = guard(async () => {
    const { paths } = await api('/api/pick', { body: { kind: 'catalog' } });
    if (paths.length) { $('#lsCatalog').value = paths[0]; $('#lsLoadCatalog').click(); }
  });
  $('#lsLoadCatalog').onclick = guard(async () => {
    const path = $('#lsCatalog').value.trim();
    const info = await api('/api/logsleuth/catalog-info', { body: { path } });
    S.catalogInfo = { ...info, path };
    const options = { ...readLogForm(), catalog_path: path, services: [] };
    await setInputs({ logsleuth: options }); document.activeElement.blur(); renderDrawer(false);
  });
  $('#lsCatalogs').onclick = guard(async () => {
    const { catalogs } = await api('/api/logsleuth/catalogs');
    const list = $('#lsCatalogList'); list.hidden = false;
    list.innerHTML = catalogs.map((c, i) => `<button type="button" class="btn btn-sm" data-catalog="${i}" title="${esc(c.path)}">${esc(c.name)}</button>`).join('') || esc(t('ls.noCatalog'));
    for (const b of list.querySelectorAll('[data-catalog]')) b.onclick = () => { $('#lsCatalog').value = catalogs[+b.dataset.catalog].path; $('#lsLoadCatalog').click(); };
  });
  for (const b of $('#lsForm').querySelectorAll('[data-remove-log]')) b.onclick = guard(async () => {
    await save(); await setInputs({ log_files: inputs().log_files.filter((_, i) => i !== +b.dataset.removeLog) }); document.activeElement.blur(); renderDrawer(false);
  });
  $('#lsAddLogs').onclick = guard(async () => { await save(); await $('#pickLogs').onclick(); document.activeElement.blur(); renderDrawer(false); });
  const slice = (only) => guard(async () => {
    if (!$('#lsForm').reportValidity()) return;
    await save(); await startStep('log', only); renderDrawer(false);
  });
  $('#lsSlice').onclick = slice(true);
  if ($('#lsProceed')) $('#lsProceed').onclick = guard(async () => {
    await startStep('graph', false);
    closeDrawer();
  });
}

// ---------------------------------------------------------------- jira attachments (selection menu)
const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);

function attachmentsPanel() {
  const n = S.run ? (S.run.attachments || []).length : 0;
  if (!n) return '';
  return `<div class="att-inline"><span class="muted small">${esc(t('att.count', { n }))}</span>
    <button class="btn btn-sm" id="attMenu" ${S.state.busy ? 'disabled' : ''}>${esc(t('att.choose'))}</button></div>`;
}

function bindAttachments() {
  if ($('#attMenu')) $('#attMenu').onclick = openAttachments;
}

function openAttachments() {
  const run = S.run;
  if (!run) return;
  const list = run.attachments || [];
  $('#attSub').textContent = `${run.issue}${run.summary ? ' - ' + run.summary : ''}`;
  $('#attFolder').value = run.inputs.download_dir || S.state.settings.download_dir || '';
  $('#attFolder').placeholder = (S.state.default_download_dir || '').replace('<ISSUE>', run.issue);
  $('#attList').innerHTML = list.length ? list.map((a, i) => {
    const badges = [a.path ? t('att.have') : '', a.analyzed ? t('att.analyzed') : ''].filter(Boolean)
      .map((b) => ` <span class="tag ok">${esc(b)}</span>`).join('');
    return `<label class="att-row"><input type="checkbox" data-i="${i}" ${a.kind !== 'other' ? 'checked' : ''}>
      <span class="att-name">${esc(a.filename)}${badges}</span>
      <span class="tag">${esc(t('att.kind.' + a.kind))}</span><span class="muted small att-size">${fmtSize(a.size || 0)}</span></label>`;
  }).join('') : `<div class="muted small">${esc(t('att.none'))}</div>`;
  $('#attSkip').textContent = t('att.close');
  updateAttSelection();
  openModal('#attModal');
}

const attBoxes = () => [...document.querySelectorAll('#attList [data-i]')];

function updateAttSelection() {
  const boxes = attBoxes();
  const n = boxes.filter((b) => b.checked).length;
  $('#attCount').textContent = t('att.selected', { n, total: boxes.length });
  $('#attAll').checked = boxes.length > 0 && n === boxes.length;
  $('#attAll').indeterminate = n > 0 && n < boxes.length;
  $('#attAll').disabled = !boxes.length;
  $('#attRun').disabled = n === 0;
}

// Pause point of the pipeline: the evidence gate at the Log step (Review / Manual).
function maybePromptGate() {
  const pending = S.run && (S.run.pending_gate || S.run.pending_tz);
  if (!pending || S.state.busy || S.preview) return;
  const key = `${S.run.id}:${S.run.steps.log.ended}:${pending.problem || (S.run.pending_tz ? 'tz' : '')}`;
  if (S.gatePrompted === key) return;
  S.gatePrompted = key;
  openDrawer('log');
}

$('#attList').onchange = updateAttSelection;
$('#attAll').onchange = () => { for (const b of attBoxes()) b.checked = $('#attAll').checked; updateAttSelection(); };
$('#attPick').onclick = guard(async () => {
  const { paths } = await api('/api/pick', { body: { kind: 'folder' } });
  if (paths.length) $('#attFolder').value = paths[0];
});
$('#attOpen').onclick = guard(() => api(`/api/runs/${encodeURIComponent(S.run.id)}/open-folder`, { body: { what: 'attachments' } }));
$('#attRun').onclick = guard(async () => {
  const list = S.run.attachments || [];
  const names = attBoxes().filter((b) => b.checked).map((b) => list[+b.dataset.i].filename);
  if (!names.length) throw new Error(t('att.nothing'));
  await api(`/api/runs/${encodeURIComponent(S.run.id)}/attachments`, { body: { names, folder: $('#attFolder').value.trim() } });
  S.state.busy = true;
  S.run.att_progress = S.run.att_progress || { phase: 'download', done: 0, total: names.length, name: '', pct: null };
  closeModals();
  renderButtons();
  renderAttProgress();
});
$('#attStop').onclick = guard(() => api('/api/stop', { body: {} }));
$('#attProgBtn').onclick = () => { renderAttProgress(); openModal('#attProgModal'); };

// Download progress lives behind the button under the checkbox; it is never forced open.
function renderAttProgress() {
  const p = S.run && S.run.att_progress;
  $('#attProgBtn').classList.toggle('is-active', !!p);
  $('#attProgBtnText').textContent = p
    ? t('att.progBtnActive', { done: p.done, total: p.total, pct: p.pct != null ? ` ${p.pct}%` : '' })
    : t('att.progBtn');
  $('#attProgActive').hidden = !p;
  $('#attProgIdle').hidden = !!p;
  if (p) {
    S.attWorking = true;
    const pct = p.total ? Math.round(((p.done + (p.pct || 0) / 100) / p.total) * 100) : 0;
    $('#attProgressText').textContent = t(p.phase === 'extract' ? 'att.progEx' : 'att.progDl', { done: p.done, total: p.total })
      + (p.pct != null ? ` ${p.pct}%` : '');
    $('#attProgressName').textContent = p.name || '';
    $('#attProgressBar').style.width = `${Math.max(pct, 3)}%`;
    renderAttFiles(p.files || []);
  } else if (S.attWorking) {
    S.attWorking = false;
    $('#attProgModal').hidden = true;
  }
}
// One bar per file: queued -> downloading (percent) -> downloaded / already present / failed -> extracting -> ready.
function renderAttFiles(files) {
  const width = (f) => (['done', 'present', 'ready', 'extract'].includes(f.state) ? 100 : f.state === 'download' ? Math.max(f.pct || 0, 2) : 0);
  $('#attProgressFiles').innerHTML = files.map((f) => {
    const pct = f.state === 'download' ? `${f.pct || 0}%` : '';
    return `<li class="att-file s-${esc(f.state)}">
      <div class="att-file-top"><span class="att-file-name" title="${esc(f.name)}">${esc(f.name)}</span>
        <span class="muted small">${f.size ? fmtSize(f.size) : ''}</span>
        <span class="att-file-state">${esc(t('att.st.' + f.state))}${pct ? ' · ' + pct : ''}</span></div>
      <div class="att-bar-track"><div class="att-bar-fill" style="width:${width(f)}%"></div></div></li>`;
  }).join('');
}
$('#attSkip').onclick = () => closeModals();

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
  if (art.kind !== 'image') parts.push(`<button class="btn btn-sm" data-act="copy">${esc(t('tb.copy'))}</button>`);
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
  if (art.kind === 'image') {
    v.innerHTML = `<img class="art-img" src="${fileUrl(art.name)}?t=${Date.now()}" alt="${esc(artLabel(art))}">`;
    return;
  }
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
  if (!S.run) {  // draft: create the run first, without starting the full pipeline
    const issue = $('#issue').value.trim();
    if (!issue) { $('#issue').focus(); throw new Error(t('run.needIssue')); }
    S.run = await api('/api/runs', { body: { issue, ...S.draft, start: false } });
    history.replaceState(null, '', `?run=${encodeURIComponent(S.run.id)}`);
    $('#console').textContent = '';
    $('#consoleCount').textContent = '';
    renderAll();
    scheduleHistory();
  }
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
document.addEventListener('click', (e) => {
  const button = e.target.closest('[data-help]');
  if (!button) return;
  e.preventDefault();
  e.stopPropagation();
  $('#helpTitle').textContent = t('help.title');
  $('#helpText').textContent = t(button.dataset.help);
  openModal('#helpModal');
});

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
  if (!S.run) S.draft.auto_publish_to_collab = !S.state.settings.review_before_publish;
  closeModals();
  renderInputs();
  toast(t('toast.settings'));
});

boot().catch((e) => toast(e.message, true));
