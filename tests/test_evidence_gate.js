/* Evidence gate DOM behavior without external dependencies. Run: node --test tests/test_evidence_gate.js */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const gateSource = source.slice(source.indexOf('// The evidence gate:'), source.indexOf('// LogSleuth: the original'));

function fixture(mode = 'review', zone = '+07:00', extra = {}) {
  const attachments = Array.from({ length: 19 }, (_, i) => ({
    filename: `capture.7z.${String(i + 1).padStart(3, '0')}`, kind: 'archive', size: 1048576,
  })).concat([{ filename: 'shot.png', kind: 'other', size: 1024 }]); // old saved runs used kind=other
  const boxes = attachments.map((_, i) => ({ dataset: { gateI: String(i) }, checked: mode === 'review' }));
  const elements = Object.fromEntries(['gateSelection', 'gateAddLogs', 'gateRun', 'gateSkipLogs',
    'gateStart', 'gateEnd', 'gateZone', ...(extra.gate && extra.gate.over_budget ? ['gateAddOver'] : [])]
    .map((id) => [id, { value: '' }]));
  Object.assign(elements.gateStart, { value: '19:38' });
  Object.assign(elements.gateEnd, { value: '19:40' });
  Object.assign(elements.gateZone, { value: zone || '' });
  const S = { state: { busy: false }, run: { id: 'TEST-1', attachments,
    inputs: { log_files: [] }, focus: { time_start: '19:38:00', time_end: '19:40:00', ticket_utc_offset: zone },
    pending_gate: { mode, max_mb: 20, suggested: mode === 'review' ? attachments.map(a => a.filename) : [], ...extra.gate },
  } };
  const requests = [];
  const context = vm.createContext({ S, document: { querySelectorAll: () => boxes }, ...(extra.window ? { window: extra.window } : {}),
    $: s => elements[s.slice(1)], esc: s => String(s ?? ''), inputs: () => S.run.inputs,
    t: (key, vars) => key + (vars ? JSON.stringify(vars) : ''), fmtSize: n => `${n} B`,
    guard: fn => fn, api: async (url, opts) => { requests.push({ url, ...opts }); return {}; },
    closeDrawer() {}, renderButtons() {}, toast() {},
  });
  vm.runInContext(gateSource, context);
  return { context, boxes, elements, requests };
}

test('gate renders images, incident clocks and UTC zone, never a minutes-shift input', () => {
  const { context } = fixture();
  const html = vm.runInContext('gatePanel()', context);
  assert.match(html, /shot\.png/);
  assert.match(html, /att.kind.image/);
  assert.match(html, /gateZone/);
  assert.match(html, /value="\+07:00" selected>UTC\+07:00/);
  assert.match(html, /19:38:00/);
  assert.doesNotMatch(html, /gateShift|focus.from/);
});

test('unchecking or checking one part toggles the entire 19-part set, not the image', () => {
  const { context, boxes } = fixture();
  vm.runInContext('bindGatePanel()', context);
  boxes[8].checked = false;
  boxes[8].onchange();
  assert.equal(boxes.filter(b => b.checked).length, 1);
  assert.equal(boxes[19].checked, true);
  boxes[3].checked = true;
  boxes[3].onchange();
  assert.equal(boxes.filter(b => b.checked).length, 20);
});

test('manual preselects nothing and submits a UTC zone with normalized-clock input, not a zone-derived shift', async () => {
  const { context, boxes, elements, requests } = fixture('manual', '+00:00');
  const html = vm.runInContext('gatePanel()', context);
  assert.match(html, /value="\+00:00" selected>UTC\+00:00/);
  assert.equal((html.match(/data-gate-i="\d+" checked/g) || []).length, 0);
  vm.runInContext('bindGatePanel()', context);
  boxes[19].checked = true;
  elements.gateRun.onclick();
  await Promise.resolve();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.ticket_utc_offset, '+00:00');
  assert.equal(requests[0].body.shift_minutes, null);
  assert.equal(requests[0].body.time_start, '19:38');
  assert.deepEqual(Array.from(requests[0].body.names), ['shot.png']);
});
test('review warns about what the download limit left out and lets the user add it back', () => {
  const over = { over_budget: [{ filename: 'capture.7z.001', files: Array.from({ length: 19 }, (_, i) => `capture.7z.${String(i + 1).padStart(3, '0')}`), bytes: 19 * 1048576, reason: 'budget' }],
    over_bytes: 19 * 1048576, suggested: ['shot.png'], max_mb: 5 };
  const { context, boxes, elements } = fixture('review', '+07:00', { gate: over });
  const html = vm.runInContext('gatePanel()', context);
  assert.match(html, /gate\.overTitle\{"mb":5\}/);
  assert.match(html, /gateAddOver/);
  assert.equal((html.match(/gate\.overTag/g) || []).length, 19, 'every part of the left-out set is tagged');
  boxes.forEach((b, i) => { b.checked = i === 19; });
  vm.runInContext('bindGatePanel()', context);
  assert.equal(elements.gateSelection.className, 'muted small');
  elements.gateAddOver.onclick();
  assert.equal(boxes.filter(b => b.checked).length, 20, 'the whole set is selected again');
  assert.match(elements.gateSelection.textContent, /gate\.overBudget/);
  assert.equal(elements.gateSelection.className, 'small warn-text');
});

test('downloading past the limit asks first; cancelling sends nothing', async () => {
  const asked = [];
  const window = { confirm: (msg) => { asked.push(msg); return false; } };
  const { context, boxes, elements, requests } = fixture('review', '+07:00', { gate: { max_mb: 5 }, window });
  vm.runInContext('bindGatePanel()', context);
  assert.ok(boxes.every(b => b.checked));
  elements.gateRun.onclick();
  await Promise.resolve();
  assert.equal(asked.length, 1);
  assert.match(asked[0], /gate\.overConfirm/);
  assert.equal(requests.length, 0);
  window.confirm = () => true;
  elements.gateRun.onclick();
  await Promise.resolve();
  assert.equal(requests.length, 1, 'confirmed: the over-limit selection is downloaded');
});

test('manual mode shows no over-limit proposal box', () => {
  const { context } = fixture('manual', '+00:00', { gate: { over_budget: [{ filename: 'big.zip', files: ['big.zip'], bytes: 1, reason: 'budget' }] } });
  assert.doesNotMatch(vm.runInContext('gatePanel()', context), /gateAddOver/);
});
