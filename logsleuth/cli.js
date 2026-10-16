#!/usr/bin/env node
'use strict';
/**
 * Command-line bridge used by the Python server (app/evidence/logsleuth.py).
 *
 *   node cli.js <command> <request.json>
 *
 * The request is a JSON file (avoids Windows command-line quoting/length
 * limits); the result is ONE JSON object printed on stdout. Errors are printed
 * as {"error": "..."} with exit code 1.
 *
 *   scan  {root, out, excludeDirs?, maxFileSizeMB?, minAnchorLength?}
 *   slice {catalog|null, files[], out, services?, timeStart?, timeEnd?,
 *          extraGrep?, budgetTokens?, before?, after?, minLevel?, minScore?}
 *   span  {files[]}  -> first/last wall clock seen in each log file
 */
const fs = require('fs');
const path = require('path');
const { scanSource } = require('./src/scanner');
const { renderCatalog, parseCatalog } = require('./src/catalog');
const { sliceLog, renderBrief } = require('./src/slicer');
const { readOne, clockOf, fmtClock } = require('./src/formats');

// Used when no source folder is available: the slicer then seeds purely from
// extraGrep (AI keywords + error signals) inside the time window.
const NO_SOURCE_CATALOG = {
  entries: [{ id: '_no_source', service: '(no source)', tag: '', level: 'I', anchors: [], file: '', line: 0 }],
  stats: { serviceCount: 0, filesScanned: 0, entryCount: 0 },
};
const ERROR_SIGNALS = ['\\b(error|fatal|exception|crash|fail(ed|ure)?|timeout|abort|panic|assert)\\b'];

function scan(q) {
  const result = scanSource(q.root, {
    excludeDirs: q.excludeDirs || [],
    maxFileSizeMB: q.maxFileSizeMB || 2,
    minAnchorLength: q.minAnchorLength || 5,
  });
  fs.writeFileSync(q.out, renderCatalog(result), 'utf8');
  return { out: q.out, stats: result.stats };
}

function slice(q) {
  const catalog = q.catalog ? parseCatalog(fs.readFileSync(q.catalog, 'utf8')) : NO_SOURCE_CATALOG;
  const extraGrep = [...(q.extraGrep || [])];
  if (!q.catalog) extraGrep.push(...ERROR_SIGNALS);
  const opt = {
    services: q.services || [],
    before: q.before ?? 5,
    after: q.after ?? 12,
    windowMs: q.windowMs ?? 300,
    budgetTokens: q.budgetTokens ?? 50000,
    maxRepeat: q.maxRepeat ?? 3,
    minLevel: q.minLevel || '',
    minScore: q.minScore ?? 0,
    minAnchorLength: q.minAnchorLength ?? 5,
    extraGrep,
    exclude: q.exclude || [],
    parser: q.parser || 'auto',
    timeStart: q.timeStart || '',
    timeEnd: q.timeEnd || '',
  };
  const result = sliceLog(q.files, catalog, opt);
  fs.writeFileSync(q.out, renderBrief(result, opt), 'utf8');
  const s = result.stats;
  return {
    out: q.out,
    stats: {
      totalLines: s.totalLines, kept: s.kept, tokens: s.tokens, chunkCount: s.chunkCount,
      firedCount: s.firedCount, timeWindow: s.timeWindow || null,
      sources: (s.sources || []).map((x) => ({ name: x.name, format: x.format, lines: x.lines, kept: x.kept })),
    },
  };
}

function span(q) {
  return {
    files: q.files.map((file) => {
      const one = readOne(file, 'auto');
      let first = null;
      let last = null;
      for (const r of one.records) {
        const c = clockOf(r.ts);
        if (c === null) continue;
        if (first === null) first = c;
        last = c;
      }
      return {
        file, name: path.basename(file), format: one.format, lines: one.records.length,
        first: fmtClock(first), last: fmtClock(last),
        firstTs: (one.records.find((r) => r.ts) || {}).ts || '',
      };
    }),
  };
}

const COMMANDS = { scan, slice, span };

function main() {
  const [command, requestFile] = process.argv.slice(2);
  const fn = COMMANDS[command];
  if (!fn || !requestFile) throw new Error('usage: node cli.js scan|slice|span <request.json>');
  const q = JSON.parse(fs.readFileSync(requestFile, 'utf8'));
  process.stdout.write(JSON.stringify(fn(q)) + '\n');
}

try {
  main();
} catch (e) {
  process.stdout.write(JSON.stringify({ error: e.message || String(e) }) + '\n');
  process.exit(1);
}
