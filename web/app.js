'use strict';
/* Ticket Analyzer UI: flow diagram, live progress (SSE), artifact drawer, setup. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => Markdown.esc(s ?? '');

const STATUS = {
  idle: ['Chờ', null], running: ['Đang chạy', 'i-gear'], done: ['Xong', 'i-check'], error: ['Lỗi', 'i-x'],
  skipped: ['Bỏ qua', 'i-skip'], stale: ['Cần chạy lại', 'i-refresh'], waiting: ['Chờ duyệt', 'i-pause'],
};
const MAX_VIEW = 400000; // chars rendered inline; bigger files open in a new tab

const S = {
  state: null,           // /api/state
  run: null,             // current run object (null = draft)
  draft: { log_files: [], source_dir: '' },
  selected: null,        // step id open in the drawer
  tab: null,             // artifact name open in the drawer
  editing: false,
  doctor: null,
  logs: [],
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
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' err' : '');
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, err ? 6000 : 2800);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

// ---------------------------------------------------------------- boot
async function boot() {
  S.state = await api('/api/state');
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
    } else if (m.type === 'settings') {
      S.state.settings = m.settings; renderMode();
    }
  };
  es.onerror = () => { $('#setupText').textContent = 'Mất kết nối server…'; $('#setupPill').className = 'pill bad'; };
  es.onopen = () => refreshDoctor();
}

let historyTimer;
function scheduleHistory() {
  clearTimeout(historyTimer);
  historyTimer = setTimeout(async () => { S.state = await api('/api/state'); renderHistory(); }, 400);
}

// ---------------------------------------------------------------- header / mode
function renderMode() {
  for (const b of document.querySelectorAll('.mode-btn')) b.setAttribute('aria-checked', String(b.dataset.mode === S.state.settings.ai_mode));
}
for (const b of document.querySelectorAll('.mode-btn')) {
  b.onclick = guard(async () => {
    S.state.settings = await api('/api/settings', { body: { ai_mode: b.dataset.mode } });
    renderMode();
    refreshDoctor();
    toast(b.dataset.mode === 'm365' ? 'Dùng M365 Copilot' : 'Dùng GitHub Copilot CLI');
  });
}

async function refreshDoctor() {
  try { S.doctor = await api('/api/doctor'); } catch { return; }
  const pill = $('#setupPill');
  const bad = S.doctor.checks.filter((c) => c.needed && !c.ok);
  pill.className = 'pill ' + (bad.length ? 'bad' : 'ok');
  $('#setupText').textContent = bad.length ? `Cần cài đặt (${bad.length})` : 'Môi trường sẵn sàng';
  renderChecks();
}

// ---------------------------------------------------------------- history
function renderHistory() {
  const ul = $('#runList');
  const runs = S.state.runs || [];
  if (!runs.length) { ul.innerHTML = '<li class="muted small" style="padding:6px 10px">Chưa có lần chạy nào</li>'; return; }
  ul.innerHTML = runs.map((r) => {
    const when = new Date(r.created * 1000).toLocaleString('vi-VN', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' });
    const tag = r.collab && r.collab.page_id ? '<span class="tag ok">Collab</span>' : r.failed ? '<span class="tag err">Lỗi</span>' : `<span class="tag">${r.done}/7</span>`;
    return `<li class="run-item ${S.run && S.run.id === r.id ? 'active' : ''}" data-id="${esc(r.id)}">
      <div class="k">${esc(r.issue)} ${tag}</div><div class="s">${esc(r.summary || '—')}</div><div class="t">${when}</div></li>`;
  }).join('');
  for (const li of ul.querySelectorAll('.run-item')) li.onclick = () => openRun(li.dataset.id);
}

async function openRun(id) {
  try { S.run = await api(`/api/runs/${encodeURIComponent(id)}`); } catch (e) { return toast(e.message, true); }
  $('#console').textContent = '';
  try {
    const past = await (await fetch(`/api/runs/${encodeURIComponent(id)}/files/run.log`)).text();
    for (const line of past.split('\n').filter((l) => /^\[\d\d:\d\d:\d\d\]/.test(l)).slice(-300)) pushLog(line);
  } catch { /* no log yet */ }
  history.replaceState(null, '', `?run=${encodeURIComponent(id)}`);
  $('#issue').value = S.run.issue;
  renderAll();
  renderHistory();
  closeDrawer();
}

$('#newRun').onclick = () => {
  S.run = null;
  S.draft = { log_files: [], source_dir: '' };
  $('#issue').value = '';
  $('#console').textContent = '';
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
    ? files.map((f, i) => `<span class="chip" title="${esc(f)}"><span>${esc(f.split(/[\\/]/).pop())}</span><button data-i="${i}" aria-label="Bỏ">×</button></span>`).join('')
    : '<span class="muted small">Chưa chọn file log</span>';
  for (const b of $('#logChips').querySelectorAll('button')) b.onclick = guard(() => setInputs({ log_files: files.filter((_, j) => j !== +b.dataset.i) }));
  if (document.activeElement !== $('#source')) $('#source').value = inputs().source_dir || '';
  $('#issue').disabled = !!S.run;
}

async function setInputs(patch) {
  if (S.run) {
    S.run = await api(`/api/runs/${encodeURIComponent(S.run.id)}/inputs`, { body: patch });
  } else {
    Object.assign(S.draft, patch);
  }
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
    if (!issue) { $('#issue').focus(); throw new Error('Nhập Jira key hoặc link'); }
    S.run = await api('/api/runs', { body: { issue, ...S.draft } });
    history.replaceState(null, '', `?run=${encodeURIComponent(S.run.id)}`);
    $('#console').textContent = '';
  }
  S.state.busy = true;
  renderAll();
  scheduleHistory();
});
$('#issue').onkeydown = (e) => { if (e.key === 'Enter') $('#runAll').click(); };
$('#stopBtn').onclick = guard(() => api('/api/stop', { body: {} }));

function renderButtons() {
  const busy = S.state.busy;
  $('#runAll').hidden = busy;
  $('#stopBtn').hidden = !busy;
  $('#runAll').innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18"><use href="#i-play"/></svg> ${S.run ? 'Chạy lại toàn bộ' : 'Chạy toàn bộ'}`;
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

function renderFlow() {
  const steps = S.state.steps;
  const optional = { understand: 'cần file log', log: 'cần file log', graph: 'cần folder source' };
  $('#flow').innerHTML = steps.map((s, i) => {
    const st = stepState(s.id);
    let status = st.status;
    if (!S.run && optional[s.id]) {
      const has = s.id === 'graph' ? !!S.draft.source_dir : S.draft.log_files.length > 0;
      if (!has) status = 'skipped';
    }
    const [label, icon] = STATUS[status] || STATUS.idle;
    const note = st.error ? `<div class="node-note err">${esc(st.error)}</div>`
      : st.note ? `<div class="node-note">${esc(st.note)}</div>`
        : (status === 'skipped' && optional[s.id] ? `<div class="node-note muted">Tuỳ chọn — ${optional[s.id]}</div>` : '');
    const gear = status === 'running'
      ? '<span class="gear"><svg><use href="#i-gear"/></svg></span><span class="gear small"><svg><use href="#i-gear"/></svg></span>' : '';
    const publish = s.id === 'collab' && status === 'waiting'
      ? '<button class="btn btn-primary btn-sm publish-btn" data-publish>Đăng lên Collab</button>' : '';
    return `<li class="node s-${status} ${S.selected === s.id ? 'selected' : ''}" data-step="${s.id}" tabindex="0">
      <div class="node-top">
        <div class="node-icon"><svg><use href="#s-${s.id}"/></svg>${gear}</div>
        <span class="node-no">${String(i + 1).padStart(2, '0')}</span>
      </div>
      <div class="node-title">${esc(s.title)}</div>
      <div class="node-sub">${esc(s.sub)}</div>
      ${note}
      ${publish}
      <div class="node-foot">
        <span class="status">${icon ? `<svg><use href="#${icon}"/></svg>` : ''}${label}</span>
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
    $('#runTitle').textContent = 'Ticket mới';
    $('#runSub').textContent = 'Nhập ticket rồi bấm “Chạy toàn bộ”. Bấm vào từng bước để xem file.';
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
  $('#consoleCount').textContent = `${el.childElementCount} dòng`;
}
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

function renderDrawer(reloadViewer) {
  const step = S.state.steps.find((s) => s.id === S.selected);
  const idx = S.state.steps.indexOf(step);
  const st = stepState(step.id);
  const [label] = STATUS[st.status] || STATUS.idle;
  $('#drawerStepNo').textContent = `Bước ${idx + 1} / 7 · ${step.sub}`;
  $('#drawerTitle').textContent = step.title;
  $('#drawerStatus').innerHTML = `<span class="node s-${st.status}" style="all:unset"><span class="status">${label}${st.note ? ' · ' + esc(st.note) : ''}</span></span>`;
  renderExtra(step.id, st);
  const arts = st.artifacts || [];
  $('#drawerTabs').innerHTML = arts.map((a) => `<button class="tab ${a.name === S.tab ? 'active' : ''}" data-name="${esc(a.name)}">${esc(a.label)}</button>`).join('');
  for (const b of $('#drawerTabs').querySelectorAll('.tab')) b.onclick = () => { S.tab = b.dataset.name; S.editing = false; renderDrawer(true); };
  renderToolbar(arts.find((a) => a.name === S.tab));
  if (reloadViewer || $('#viewer').dataset.loaded !== viewerKey()) loadViewer(arts.find((a) => a.name === S.tab));
  renderButtons();
}

function renderExtra(step, st) {
  const box = $('#drawerExtra');
  let html = st.error ? `<div class="err-box" style="margin:0 0 10px">${esc(st.error)}</div>` : '';
  if (step === 'log' || step === 'understand') {
    const f = (S.run && S.run.focus) || {};
    html += `<div class="extra-grid">
      <div class="field"><label>Từ (log clock)</label><input id="fStart" placeholder="HH:MM:SS" value="${esc(f.time_start || '')}"></div>
      <div class="field"><label>Đến</label><input id="fEnd" placeholder="HH:MM:SS" value="${esc(f.time_end || '')}"></div>
      <div class="field"><label>Từ khoá ưu tiên (phẩy)</label><input id="fKeys" value="${esc((f.keywords || []).join(', '))}"></div>
      <button class="btn btn-primary" id="fApply" ${S.run ? '' : 'disabled'}>Cắt lại log</button>
    </div>
    <div class="extra-note">${f.reason ? 'AI: ' + esc(f.reason) + (f.confidence ? ` (độ tin cậy: ${esc(f.confidence)})` : '') : 'Khung giờ do AI chọn sau khi đọc ticket. Có thể sửa rồi bấm “Cắt lại log”. Để trống = toàn bộ log.'}</div>`;
  } else if (step === 'graph') {
    html += `<div class="picker"><input id="gq" placeholder="Tìm symbol: class, hàm, file…"><button class="btn btn-ghost" id="gGo" ${st.status === 'done' || st.status === 'stale' ? '' : 'disabled'}>Tìm</button></div><div class="hits" id="gHits"></div>`;
  } else if (step === 'collab' && S.run && S.run.collab && S.run.collab.url) {
    html += `<a class="btn btn-primary" href="${esc(S.run.collab.url)}" target="_blank" rel="noopener">Mở trang Collab ↗</a>
      <span class="muted small" style="margin-left:8px">${esc(S.run.collab.title || '')}</span>`;
  } else if (step === 'collab' && st.status === 'waiting') {
    html += `<button class="btn btn-primary" id="pubNow">Đăng lên Collab</button> <span class="muted small">Report đã sẵn sàng ở bước 6 — kiểm tra/sửa trước khi đăng.</span>`;
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
      $('#gHits').innerHTML = hits.length ? hits.map((h) => `<div>${esc(h.name)} <span class="muted">[${esc(h.kind)}] ${esc(h.file)}:${h.line} · deg ${h.degree}</span></div>`).join('') : '<div class="muted">Không tìm thấy</div>';
    });
    $('#gGo').onclick = go;
    $('#gq').onkeydown = (e) => { if (e.key === 'Enter') go(); };
  }
  if ($('#pubNow')) $('#pubNow').onclick = guard(() => startStep('collab', true, true));
}

const viewerKey = () => (S.run && S.selected ? `${S.run.id}/${S.tab}/${S.run.steps[S.selected].ended}/${S.run.steps[S.selected].status}` : '');
const fileUrl = (name) => `/api/runs/${encodeURIComponent(S.run.id)}/files/${encodeURIComponent(name)}`;

function renderToolbar(art) {
  const bar = $('#drawerToolbar');
  if (!art) { bar.innerHTML = ''; return; }
  const parts = [];
  if (art.editable) {
    parts.push(S.editing
      ? '<button class="btn btn-primary btn-sm" data-act="save">Lưu</button><button class="btn btn-sm" data-act="cancel">Huỷ</button>'
      : '<button class="btn btn-sm" data-act="edit">✎ Sửa</button>');
  }
  parts.push('<button class="btn btn-sm" data-act="copy">Copy</button>');
  parts.push(`<a class="btn btn-sm" href="${fileUrl(art.name)}" download="${esc(art.name)}">Tải về</a>`);
  parts.push(`<a class="btn btn-sm" href="${fileUrl(art.name)}" target="_blank" rel="noopener">Mở tab mới ↗</a>`);
  parts.push('<span class="spacer"></span><button class="btn btn-sm" data-act="folder">Mở thư mục</button>');
  bar.innerHTML = parts.join('');
  const on = (act, fn) => { const b = bar.querySelector(`[data-act="${act}"]`); if (b) b.onclick = guard(fn); };
  on('edit', async () => { S.editing = true; renderToolbar(art); await loadViewer(art); });
  on('cancel', async () => { S.editing = false; renderToolbar(art); await loadViewer(art); });
  on('save', async () => {
    S.run = await api(fileUrl(art.name), { method: 'PUT', raw: true, body: $('#viewer textarea').value });
    S.editing = false;
    toast('Đã lưu. Các bước sau được đánh dấu “Cần chạy lại”.');
    renderAll(); renderDrawer(true);
  });
  on('copy', async () => {
    const text = await (await fetch(fileUrl(art.name))).text();
    await navigator.clipboard.writeText(text);
    toast('Đã copy');
  });
  on('folder', () => api(`/api/runs/${encodeURIComponent(S.run.id)}/open-folder`, { body: {} }));
}

async function loadViewer(art) {
  const v = $('#viewer');
  if (!S.run || !art) {
    v.innerHTML = `<div class="empty">${S.run ? 'Bước này chưa có file. Chạy bước này để tạo kết quả.' : 'Chưa chạy. Bấm “Chạy toàn bộ” để bắt đầu.'}</div>`;
    v.dataset.loaded = '';
    return;
  }
  v.dataset.loaded = viewerKey();
  if (art.kind === 'html' && !S.editing) {
    v.innerHTML = `<iframe src="${fileUrl(art.name)}?t=${Date.now()}" title="${esc(art.label)}"></iframe>`;
    return;
  }
  const text = await (await fetch(fileUrl(art.name))).text();
  if (S.editing) {
    v.innerHTML = '<textarea spellcheck="false"></textarea>';
    v.querySelector('textarea').value = text;
    return;
  }
  const shown = text.length > MAX_VIEW ? text.slice(0, MAX_VIEW) : text;
  const cut = text.length > MAX_VIEW ? `<p class="muted small">Đang hiển thị ${MAX_VIEW.toLocaleString()} / ${text.length.toLocaleString()} ký tự — bấm “Mở tab mới” để xem toàn bộ.</p>` : '';
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

function renderChecks() {
  if (!S.doctor) return;
  const actions = { tokens: 'Nhập token', m365_login: 'Đăng nhập M365', copilot_login: 'Đăng nhập Copilot' };
  $('#checkList').innerHTML = S.doctor.checks.map((c) => {
    const btn = c.fix ? `<button class="btn btn-sm" data-fix="${c.fix}">Tự cài</button>`
      : c.action ? `<button class="btn btn-sm btn-primary" data-action="${c.action}">${actions[c.action]}</button>` : '';
    const recheck = c.id === 'm365_login' ? '<button class="btn btn-sm" data-action="m365_check">Kiểm tra</button>' : '';
    const test = (c.id === 'mcp_jira' && c.ok) ? '<button class="btn btn-sm" data-action="mcp_test">Kiểm tra kết nối</button>' : '';
    const tokenEdit = (c.id === 'mcp_collab' && c.ok) ? '<button class="btn btn-sm" data-action="tokens">Đổi token</button>' : '';
    return `<li class="check-row ${c.ok ? 'ok' : 'bad'} ${c.needed ? '' : 'optional'}">
      <span class="check-ic"><svg><use href="#${c.ok ? 'i-check' : 'i-x'}"/></svg></span>
      <div><div class="check-label">${esc(c.label)}${c.needed ? '' : ' <span class="muted small">(không cần cho chế độ này)</span>'}</div>
      <div class="check-detail">${esc(c.detail)}</div></div>
      <div style="display:flex;gap:6px">${recheck}${test}${tokenEdit}${btn}</div></li>`;
  }).join('');
  const mcp = S.state.mcp || {};
  $('#jiraUrl').placeholder = (mcp.jira && mcp.jira.url) || '';
  $('#collabUrl').placeholder = (mcp.collab && mcp.collab.url) || '';
  if (S.doctor.checks.some((c) => c.action === 'tokens' && !c.ok)) $('#tokenForm').hidden = false;
  for (const b of $('#checkList').querySelectorAll('[data-fix]')) b.onclick = guard(async () => { b.disabled = true; b.textContent = 'Đang cài…'; await api('/api/doctor/fix', { body: { id: b.dataset.fix } }); });
  for (const b of $('#checkList').querySelectorAll('[data-action]')) {
    b.onclick = guard(async () => {
      const a = b.dataset.action;
      if (a === 'tokens') { $('#tokenForm').hidden = false; $('#jiraToken').focus(); return; }
      b.disabled = true;
      if (a === 'm365_login') { await api('/api/doctor/m365-login', { body: {} }); toast('Đang mở Chromium — hãy đăng nhập M365 trong cửa sổ đó'); }
      if (a === 'm365_check') await api('/api/doctor/m365-check', { body: {} });
      if (a === 'mcp_test') await api('/api/doctor/mcp-test', { body: {} });
      if (a === 'copilot_login') { await api('/api/doctor/copilot-login', { body: {} }); toast('Đã mở cửa sổ đăng nhập Copilot. Làm theo hướng dẫn rồi bấm “Kiểm tra lại”.'); }
    });
  }
}
$('#fixAll').onclick = guard(async () => { $('#doctorLog').textContent = ''; await api('/api/doctor/fix-all', { body: {} }); toast('Đang tự cài những mục còn thiếu…'); });
$('#recheck').onclick = guard(refreshDoctor);
$('#tokenForm').onsubmit = guard(async (e) => {
  e.preventDefault();
  const body = { jira_token: $('#jiraToken').value, collab_token: $('#collabToken').value, jira_url: $('#jiraUrl').value, collab_url: $('#collabUrl').value };
  if (!body.jira_token && !body.collab_token) throw new Error('Nhập ít nhất một token');
  S.state.mcp = await api('/api/mcp/tokens', { body });
  $('#jiraToken').value = $('#collabToken').value = '';
  $('#tokenForm').hidden = true;
  toast('Đã lưu token. Đang kiểm tra kết nối…');
  refreshDoctor();
});

// ---------------------------------------------------------------- settings modal
$('#settingsBtn').onclick = () => {
  const s = S.state.settings;
  const f = $('#settingsForm');
  for (const el of f.elements) {
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
  toast('Đã lưu cấu hình');
});

boot().catch((e) => toast(e.message, true));
