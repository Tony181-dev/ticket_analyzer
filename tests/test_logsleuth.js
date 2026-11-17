'use strict';
// Original engine semantics + final Markdown cap, independent of VS Code.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { readRecords, parseClock, inWindow } = require('../logsleuth/src/formats');
const { scanSource } = require('../logsleuth/src/scanner');
const { parseCatalog, renderCatalog } = require('../logsleuth/src/catalog');
const { sliceLog } = require('../logsleuth/src/slicer');
const { renderBoundedBrief, budgetLimit } = require('../logsleuth/src/budget');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ls-test-'));
try {
  assert.strictEqual(parseClock('9:05'), 32700000);
  assert.strictEqual(parseClock('25:00'), null);
  assert.strictEqual(parseClock('12:30:00.250'), 45000250);
  assert(inWindow(1000, 86390000, 10000));
  const src = path.join(dir, 'src'); fs.mkdirSync(src);
  fs.writeFileSync(path.join(src, 'svc.py'), 'import logging\nlog = logging.getLogger("Svc")\nlog.error("decisive failure value=%d", 1)\n');
  const catalog = parseCatalog(renderCatalog(scanSource(src)));
  assert.strictEqual(catalog.entries.length, 1);
  const a = path.join(dir, 'a.log'), b = path.join(dir, 'b.log');
  fs.writeFileSync(a, '10-20 23:00:00.000  1 2 I Svc: boot\n10-20 23:59:59.900  1 2 E Svc: decisive failure value=1\n\tat Frame.method(File.java:1)\n');
  fs.writeFileSync(b, '10-21 00:00:00.100  1 2 E Svc: decisive failure value=2\n10-21 12:00:00.000  1 2 I Other: outside\n');
  fs.utimesSync(a, new Date(1000), new Date(1000)); fs.utimesSync(b, new Date(2000), new Date(2000));
  const records = readRecords([b, a], 'auto', { timeStart: '23:59:59.800', timeEnd: '00:00:00.200' });
  assert.strictEqual(records.sources[0].name, 'a.log');
  assert.strictEqual(records.keptLines, 3);
  assert(records.records[1].cont);
  const sliced = sliceLog([b, a], catalog, { timeStart: '23:59:59.800', timeEnd: '00:00:00.200', budgetTokens: 2500 });
  assert(sliced.chunks.every((ch) => new Set(ch.map((p) => sliced.recs[p].srcIdx)).size === 1));
  const bounded = renderBoundedBrief(sliced, { budgetTokens: 2500 });
  assert(Buffer.byteLength(bounded.text, 'utf8') <= 2500);
  assert.strictEqual(bounded.stats.tokens, Buffer.byteLength(bounded.text, 'utf8'));
  assert(bounded.text.includes('decisive failure'));
  assert.strictEqual((bounded.text.match(/```/g) || []).length % 2, 0);
  assert.throws(() => budgetLimit(-1));
  assert.strictEqual(budgetLimit(1000000), 50000);

  // Extra windows: a line inside ANY window is kept; the main window stays the first one reported.
  const multi = readRecords([b, a], 'auto', { timeStart: '23:59:59.800', timeEnd: '00:00:00.200',
    timeWindows: [{ start: '11:59:00', end: '12:01:00' }, { start: '', end: '' }] });
  assert.strictEqual(multi.keptLines, 4, 'the 12:00 line joins the three around midnight');
  assert.deepStrictEqual(multi.timeWindows.map((w) => [w.start, w.end]), [['23:59:59', '00:00:00'], ['11:59:00', '12:01:00']]);
  assert.strictEqual(multi.timeWindow.start, '23:59:59');
  const onlyExtra = readRecords([b, a], 'auto', { timeWindows: [{ start: '22:59:00', end: '23:01:00' }] });
  assert.strictEqual(onlyExtra.keptLines, 1, 'an extra window alone also filters');
  const multiBrief = renderBoundedBrief(sliceLog([b, a], catalog, { timeStart: '23:59:59.800', timeEnd: '00:00:00.200',
    timeWindows: [{ start: '11:59:00', end: '12:01:00' }], budgetTokens: 4000 }), { budgetTokens: 4000 });
  assert(multiBrief.text.includes('`11:59:00` → `12:01:00`'), 'the brief names every window');

  // Metadata alone may exceed the cap; it must not evict all real evidence.
  sliced.stats.fired[0].file = 'very-long-name'.repeat(1000);
  const compact = renderBoundedBrief(sliced, { budgetTokens: 2000 });
  assert(compact.stats.compactMetadata);
  assert(Buffer.byteLength(compact.text, 'utf8') <= 2000);
  assert(compact.text.includes('decisive failure'));
  sliced.recs[sliced.chunks[0][0]].raw = 'oversized record '.repeat(1000);
  const oversized = renderBoundedBrief(sliced, { budgetTokens: 2000 });
  assert(Buffer.byteLength(oversized.text, 'utf8') <= 2000);
  assert(oversized.text.includes('value=2'), 'an oversized first record must not evict all smaller regions');
  assert(oversized.stats.outputTruncated);
  console.log('LogSleuth engine and budget tests passed');
} finally {
  fs.rmSync(dir, { recursive: true, force: true });
}