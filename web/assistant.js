'use strict';
/* AI assistant chat bubble (bottom-right). Talks to /api/assistant; answers and proposed actions arrive
 * over the shared SSE stream (app.js forwards "assistant*" events and run updates here).
 * Uses app.js globals: $, esc, t, api, toast, guard, S. */
(function () {
  const A = {
    messages: [], busy: false, started: null, bugs: [], model: '', open: false, unread: 0,
    seen: new Map(),        // run id -> {gate, errors:Set} last observed, to fire proactive scans on transitions only
    pending: null,          // proactive scan that hit a busy assistant; retried when it is free
    loaded: false,
  };
  const ACTION_ICON = {
    download_attachments: 'i-download', resolve_gate: 'i-check', resolve_timezone: 'i-clock', run_steps: 'i-play', log_scan: 'i-refresh',
    update_inputs: 'i-sliders', update_settings: 'i-sliders', create_bug: 'i-bug', open_folder: 'i-folder',
    stop_pipeline: 'i-stop',
  };
  const icon = (id, size = 16) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true"><use href="#${id}"/></svg>`;
  const store = (key, value) => { try { localStorage.setItem(key, value); } catch { /* storage blocked */ } };
  const recall = (key) => { try { return localStorage.getItem(key); } catch { return null; } };

  // ------------------------------------------------------------ data
  async function load() {
    try {
      const data = await api('/api/assistant');
      Object.assign(A, { messages: data.messages || [], busy: data.busy, started: data.started, bugs: data.bugs || [],
        model: data.model || '', loaded: true });
    } catch { /* server not ready: the SSE reconnect brings us back */ }
    render();
  }

  function upsert(message) {
    const i = A.messages.findIndex((m) => m.id === message.id);
    if (i >= 0) A.messages[i] = message; else A.messages.push(message);
  }

  // ------------------------------------------------------------ rendering
  function fmtTime(sec) {
    if (!sec) return '';
    return new Date(sec * 1000).toLocaleTimeString(I18N.locale, { hour: '2-digit', minute: '2-digit' });
  }
  const fmtNum = (n) => new Intl.NumberFormat(I18N.locale, { notation: 'compact' }).format(n || 0);

  function actionDetail(a) {
    const rows = [];
    if (a.names && a.names.length) {
      rows.push(`<div class="asst-files">${a.names.slice(0, 8).map((n) => `<code>${esc(n)}</code>`).join('')}${a.names.length > 8 ? `<span class="muted">+${a.names.length - 8}</span>` : ''}</div>`);
    }
    if (a.time_start || a.time_end) rows.push(`<div class="asst-kv">${icon('i-clock', 13)} ${esc(a.time_start || '…')} → ${esc(a.time_end || '…')}</div>`);
    if (a.shift_minutes != null) rows.push(`<div class="asst-kv">${esc(t('asst.shift'))}: ${esc(t('asst.minutes', { n: a.shift_minutes }))}</div>`);
    if (a.type === 'resolve_timezone') rows.push(`<div class="asst-kv">${icon('i-clock', 13)} ${esc(t('tz.mode.' + a.mode))}${a.ticket_utc_offset ? ' · UTC' + esc(a.ticket_utc_offset) : ''}</div>`);
    if (a.skip_logs) rows.push(`<div class="asst-kv">${esc(t('asst.skipLogs'))}</div>`);
    if (a.changes) rows.push(Object.entries(a.changes).map(([k, v]) => `<div class="asst-kv"><code>${esc(k)}</code> → <b>${esc(JSON.stringify(v))}</b></div>`).join(''));
    if (a.type === 'run_steps') {
      const parts = [a.resume ? t('asst.resume') : '', a.from ? `${t('asst.from')} ${t('step.' + a.from)}` : '', a.until ? `${t('asst.until')} ${t('step.' + a.until)}` : '', a.only ? t('asst.only') : ''].filter(Boolean);
      if (parts.length) rows.push(`<div class="asst-kv">${esc(parts.join(' · '))}</div>`);
    }
    if (a.type === 'create_bug') rows.push(`<div class="asst-kv"><span class="sev sev-${esc(a.severity)}">${esc(a.severity)}</span> ${esc(a.title)}</div>`);
    return rows.join('');
  }

  function actionButton(m, a, i) {
    const repeatable = a.type === 'open_folder';
    if (a.status === 'done' && !repeatable) return `<span class="asst-done">${icon('i-check', 14)} ${esc(t('asst.done'))}</span>`;
    const label = a.status === 'error' ? t('asst.retry') : t('asst.do');
    return `<button type="button" class="btn btn-sm ${a.status === 'error' ? '' : 'btn-primary'}" data-act="${esc(m.id)}" data-i="${i}">${esc(label)}</button>`;
  }

  function renderMessage(m) {
    if (m.role === 'user') {
      return `<div class="asst-msg user${m.kind === 'scan' ? ' scan' : ''}"><div class="bubble">${esc(m.text)}</div>
        <div class="meta">${esc(fmtTime(m.time))}${m.issue ? ' · ' + esc(m.issue) : ''}</div></div>`;
    }
    const setup = m.error && /copilot cli/i.test(m.error)
      ? `<button type="button" class="btn btn-sm asst-setup" data-setup>${esc(t('asst.openSetup'))}</button>` : '';
    const body = m.error ? `<div class="asst-error">${icon('i-x', 14)} <span>${esc(m.error)}</span></div>${setup}` : Markdown.render(m.text || '');
    const actions = (m.actions || []).map((a, i) => `
      <div class="asst-action ${esc(a.status || 'pending')}">
        <div class="asst-action-icon">${icon(ACTION_ICON[a.type] || 'i-play', 15)}</div>
        <div class="asst-action-main">
          <div class="asst-action-label">${esc(a.label)}</div>
          ${a.why ? `<div class="asst-action-why">${esc(a.why)}</div>` : ''}
          ${actionDetail(a)}
          ${a.result ? `<div class="asst-action-result ${a.status === 'error' ? 'err' : ''}">${esc(a.result)}</div>` : ''}
        </div>
        <div class="asst-action-go">${actionButton(m, a, i)}</div>
      </div>`).join('');
    const usage = m.usage ? ' · ' + t('asst.tokens', { in: fmtNum(m.usage.input_tokens), out: fmtNum(m.usage.output_tokens) }) : '';
    return `<div class="asst-msg bot"><div class="bubble md">${body}${actions ? `<div class="asst-actions">${actions}</div>` : ''}</div>
      <div class="meta">${esc(fmtTime(m.time))}${m.model ? ' · ' + esc(m.model) : ''}${m.seconds ? ` · ${m.seconds}s` : ''}${esc(usage)}</div></div>`;
  }

  function welcome() {
    return `<div class="asst-welcome">${icon('i-spark', 28)}
      <h4>${esc(t('asst.helloTitle'))}</h4><p>${esc(t('asst.hello'))}</p></div>`;
  }

  function chips() {
    const run = S.run;
    const list = [];
    if (run && run.pending_gate) list.push(['asst.q.gate', 'chat']);
    if (run && run.pending_tz) list.push(['asst.q.tz', 'chat']);
    const failed = run ? Object.entries(run.steps || {}).find(([, st]) => st.status === 'error') : null;
    if (failed) list.push([t('asst.q.error', { step: t('step.' + failed[0]) }), 'chat', true]);
    list.push(['asst.q.scan', 'scan']);
    if (run && (run.attachments || []).length && !run.pending_gate) list.push(['asst.q.attachments', 'chat']);
    if (run && run.inputs && run.inputs.source_dir) list.push(['asst.q.code', 'chat']);
    list.push(['asst.q.setup', 'chat'], ['asst.q.bug', 'chat']);
    return list.slice(0, 5).map(([key, kind, raw]) => {
      const text = raw ? key : t(key);
      return `<button type="button" class="asst-chip" data-kind="${kind}" data-text="${esc(text)}">${esc(text)}</button>`;
    }).join('');
  }

  function contextLine() {
    const run = S.run;
    const chosen = (S.state && S.state.settings && S.state.settings.copilot_model) || A.model || 'auto';
    const model = `<span class="asst-model" title="${esc(t('asst.modelHint'))}">${icon('i-spark', 12)} Copilot · ${esc(chosen)}</span>`;
    if (!run) return `${model}<span>${esc(t('asst.ctxDraft'))}</span>`;
    const steps = Object.values(run.steps || {});
    const err = steps.filter((s) => s.status === 'error').length;
    const state = run.pending_gate ? t('asst.ctxGate') : run.pending_tz ? t('asst.ctxTz') : err ? t('asst.ctxErr', { n: err }) : (S.state && S.state.busy_run === run.id) ? t('asst.ctxRunning') : '';
    return `${model}<span>${esc(t('asst.ctxRun', { issue: run.issue }))}${state ? ` · <b class="${run.pending_gate || run.pending_tz || err ? 'warn' : ''}">${esc(state)}</b>` : ''}</span>`;
  }

  function renderBusy() {
    $('#asstSend').hidden = A.busy;
    $('#asstStop').hidden = !A.busy;
    const typing = $('#asstTyping');
    if (typing) typing.hidden = !A.busy;
    tick();
  }

  function tick() {
    const el = $('#asstElapsed');
    if (el && A.busy && A.started) el.textContent = t('asst.thinking', { s: Math.max(0, Math.round(Date.now() / 1000 - A.started)) });
  }

  function render() {
    if (!$('#asstPanel')) return;
    $('#asstContext').innerHTML = contextLine();
    const body = $('#asstBody');
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    body.innerHTML = (A.messages.length ? A.messages.map(renderMessage).join('') : welcome())
      + `<div class="asst-msg bot" id="asstTyping" hidden><div class="bubble typing"><span class="dots"><i></i><i></i><i></i></span> <span id="asstElapsed"></span></div></div>`;
    if (nearBottom || A.busy) body.scrollTop = body.scrollHeight;
    $('#asstChips').innerHTML = chips();
    const count = A.bugs.length;
    $('#asstBugCount').hidden = !count;
    $('#asstBugCount').textContent = count;
    $('#asstBugs').title = t('asst.bugs', { n: count });
    renderBusy();
    renderBadge();
  }

  function renderBadge() {
    const badge = $('#asstBadge');
    const attention = S.run && (S.run.pending_gate || S.run.pending_tz || Object.values(S.run.steps || {}).some((s) => s.status === 'error'));
    badge.hidden = A.open || (!A.unread && !attention);
    badge.textContent = A.unread ? String(Math.min(A.unread, 9)) : '!';
    $('#asstFab').classList.toggle('busy', A.busy);
    $('#asstFab').classList.toggle('attention', !A.open && !!(A.unread || attention));
  }

  function peek(message) {
    const el = $('#asstPeek');
    const text = (message.error || message.text || '').replace(/[#*`>|_-]+/g, ' ').replace(/\s+/g, ' ').trim();
    if (!text) return;
    el.innerHTML = `<b>${icon('i-spark', 14)} ${esc(t('asst.peekTitle'))}</b><span>${esc(text.slice(0, 160))}${text.length > 160 ? '…' : ''}</span>`;
    el.hidden = false;
    clearTimeout(peek.timer);
    peek.timer = setTimeout(() => { el.hidden = true; }, 9000);
  }

  // ------------------------------------------------------------ open / close / send
  function setOpen(open) {
    A.open = open;
    $('#asstPanel').hidden = !open;
    $('#asstFab').setAttribute('aria-expanded', String(open));
    $('#asstPeek').hidden = true;
    store('ta.asst.open', open ? '1' : '0');
    if (open) {
      A.unread = 0;
      render();
      setTimeout(() => $('#asstText').focus(), 30);
    }
    renderBadge();
  }

  async function send(text, kind = 'chat', extra = {}) {
    const body = { text, kind, run_id: S.run ? S.run.id : null, ...extra };
    const res = await api('/api/assistant/ask', { body });
    return res;
  }

  function autosize() {
    const el = $('#asstText');
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 140) + 'px';
  }

  // ------------------------------------------------------------ proactive scans
  function fire(trigger, reason) {
    if (S.state?.settings.assistant_proactive === false) { A.pending = null; return; }
    send('', 'scan', { reason, trigger }).then((res) => {
      if (res && res.skipped === 'busy') A.pending = { trigger, reason, run_id: S.run && S.run.id };
    }).catch(() => { /* proactive help never interrupts the user */ });
  }

  function onRun(run) {
    if (!run) return;
    const errors = new Set(Object.entries(run.steps || {}).filter(([, s]) => s.status === 'error').map(([k]) => k));
    const gate = !!run.pending_gate;
    const tz = !!run.pending_tz;
    const before = A.seen.get(run.id);
    A.seen.set(run.id, { gate, tz, errors });
    if (!before) return;  // first sight (page load / opened from history): do not scan old states
    const proactive = !S.state || !S.state.settings || S.state.settings.assistant_proactive !== false;
    if (!proactive || !S.run || S.run.id !== run.id) return;
    if (gate && !before.gate) {
      const g = run.pending_gate;
      fire(`gate:${run.id}:${g.mode}:${(g.problem || '').slice(0, 60)}:${Math.round(run.updated || 0)}`, 'gate');
    }
    if (tz && !before.tz) fire(`tz:${run.id}:${Math.round(run.steps.log.ended || run.updated || 0)}`, 'tz');
    for (const step of errors) {
      if (!before.errors.has(step)) fire(`error:${run.id}:${step}:${Math.round(run.steps[step].ended || 0)}`, 'error:' + step);
    }
  }

  // ------------------------------------------------------------ events from app.js
  function onEvent(m) {
    if (m.type === 'assistant') {
      upsert(m.message);
      if (m.message.role === 'assistant' && !A.open) { A.unread += 1; peek(m.message); }
    } else if (m.type === 'assistant_busy') {
      A.busy = m.busy; A.started = m.started || null;
      if (!m.busy && A.pending) {
        const p = A.pending; A.pending = null;
        if (S.run && S.run.id === p.run_id) setTimeout(() => fire(p.trigger, p.reason), 300);
      }
    } else if (m.type === 'assistant_action') {
      const msg = A.messages.find((x) => x.id === m.message_id);
      if (msg && msg.actions && msg.actions[m.index]) msg.actions[m.index] = m.action;
    } else if (m.type === 'assistant_cleared') {
      A.messages = []; A.bugs = m.bugs || []; A.unread = 0; A.pending = null;
      $('#asstPeek').hidden = true;
    } else if (m.type === 'assistant_bugs') {
      A.bugs = m.bugs || [];
    }
    render();
  }

  function sync() {
    if (S.state?.settings.assistant_proactive === false) A.pending = null;
    if (S.run) onRun(S.run);
    if ($('#asstPanel')) {
      $('#asstContext').innerHTML = contextLine(); $('#asstChips').innerHTML = chips();
      $('#asstBugs').title = t('asst.bugs', { n: A.bugs.length });
      renderBadge();
    }
  }

  // ------------------------------------------------------------ wiring
  function init() {
    $('#asstFab').onclick = () => setOpen(!A.open);
    $('#asstClose').onclick = () => setOpen(false);
    $('#asstPeek').onclick = () => setOpen(true);
    $('#asstScan').onclick = guard(async () => { await send('', 'scan'); });
    $('#asstBugs').onclick = guard(async () => { await api('/api/assistant/open-bugs', { body: {} }); });
    $('#asstClear').onclick = guard(async () => {
      // Clean slate: conversation, local bug tickets (the counter) and the remembered errors.
      if (!A.messages.length && !A.bugs.length) return;
      const ask = A.bugs.length ? t('asst.clearConfirmBugs', { n: A.bugs.length }) : t('asst.clearConfirm');
      if (!confirm(ask)) return;
      const res = await api('/api/assistant/clear', { body: {} });
      onEvent({ type: 'assistant_cleared', bugs: (res && res.bugs) || [] });
    });
    // New language: the welcome, labels and the app-written bubbles (scan requests, errors) come back translated.
    document.addEventListener('langchange', () => { if (A.loaded) load(); else render(); });
    $('#asstStop').onclick = guard(async () => { await api('/api/assistant/stop', { body: {} }); });
    $('#asstForm').onsubmit = guard(async (e) => {
      e.preventDefault();
      const text = $('#asstText').value.trim();
      if (!text || A.busy) return;
      $('#asstText').value = ''; autosize();
      try { await send(text); } catch (err) { $('#asstText').value = text; autosize(); throw err; }
    });
    $('#asstText').addEventListener('input', autosize);
    $('#asstText').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#asstForm').requestSubmit(); }
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && A.open && !document.querySelector('.modal:not([hidden])')) setOpen(false); });
    $('#asstChips').addEventListener('click', guard(async (e) => {
      const chip = e.target.closest('.asst-chip');
      if (!chip || A.busy) return;
      await send(chip.dataset.kind === 'scan' ? '' : chip.dataset.text, chip.dataset.kind);
    }));
    $('#asstBody').addEventListener('click', guard(async (e) => {
      if (e.target.closest('[data-setup]')) { $('#setupPill').click(); return; }
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      btn.disabled = true;
      try { await api('/api/assistant/action', { body: { message_id: btn.dataset.act, index: Number(btn.dataset.i) } }); }
      finally { btn.disabled = false; }
    }));
    setInterval(tick, 1000);
    load().then(() => { if (recall('ta.asst.open') === '1') setOpen(true); });
  }

  async function ask(text) { setOpen(true); return send(text); }

  window.Assistant = { onEvent, onRun, sync, render, reload: load, ask };
  init();
})();
