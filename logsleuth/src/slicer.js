'use strict';
/**
 * STEP 2 - Log slicer.
 * Takes the catalog from STEP 1 + one or more runtime log files, finds the
 * lines actually produced by the selected service(s), expands context,
 * collapses repeats, fits a token budget and renders Markdown for an LLM.
 */

const path = require('path');
const { readRecords, LEVEL_ORDER, clockOf } = require('./formats');

const LEVEL_WEIGHT = { F: 10, E: 6, W: 3, I: 1, D: 0.3, V: 0.1, '': 0.5 };

const DEFAULT_SIGNALS = [
  [/\b(FATAL|ANR|SIGSEGV|SIGABRT|tombstone|panic|abort)\b/i, 10],
  [/\b(Exception|Traceback|backtrace|at [\w.$]+\([\w.]+:\d+\))/, 7],
  [/\b(timeout|timed out|deadlock|watchdog|OOM|out of memory|died|killed)\b/i, 6],
  [/\b(fail(?:ed|ure)?|error|denied|refused|reject(?:ed)?|unavailable)\b/i, 4],
  [/\b(retry|reconnect|restart(?:ing)?|fallback|degrade)\b/i, 2],
];

// ---------------------------------------------------------------------------
// template normalization (dedupe)
// ---------------------------------------------------------------------------
const NORM = [
  [/0x[0-9a-fA-F]+/g, '<HEX>'],
  [/\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{18,}\b/g, '<UUID>'],
  [/\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b/g, '<MAC>'],
  [/\b\d{1,3}(?:\.\d{1,3}){3}\b/g, '<IP>'],
  [/\/[\w./-]{4,}/g, '<PATH>'],
  [/\b\d+\b/g, '<N>'],
];
function templatize(msg) {
  let t = msg;
  for (const [re, rep] of NORM) t = t.replace(re, rep);
  return t.replace(/\s+/g, ' ').trim().slice(0, 200);
}

// ---------------------------------------------------------------------------
// anchor index built from the catalog
// ---------------------------------------------------------------------------
function escRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function buildIndex(entries, minAnchorLength) {
  const map = new Map();
  for (const e of entries) {
    for (const a of e.anchors || []) {
      if (a.length < minAnchorLength) continue;
      const k = a.toLowerCase();
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(e);
    }
  }
  const anchors = [...map.keys()].sort((a, b) => b.length - a.length);
  const groups = [];
  for (let i = 0; i < anchors.length; i += 400) {
    groups.push(new RegExp('(' + anchors.slice(i, i + 400).map(escRe).join('|') + ')', 'i'));
  }
  const tags = new Set();
  for (const e of entries) {
    if (e.tag) tags.add(String(e.tag).toLowerCase());
    if (e.service) tags.add(String(e.service).split('/').pop().toLowerCase());
  }
  return { map, groups, anchorCount: anchors.length, tags };
}

function matchLine(index, text) {
  for (const g of index.groups) {
    const m = g.exec(text);
    if (m) {
      const hit = index.map.get(m[1].toLowerCase());
      if (hit) return { entry: hit[0], all: hit, anchor: m[1] };
    }
  }
  return null;
}

const estTokens = (s) => Math.max(1, Math.ceil(s.length / 4));

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
/**
 * @param {string|string[]} logFiles  one path, or many (merged by mtime)
 * @param {{entries:Array,stats:Object}} catalog
 * @param {Object} opt
 */
function sliceLog(logFiles, catalog, opt = {}) {
  const o = {
    services: [],
    before: 5,
    after: 12,
    windowMs: 300,
    budgetTokens: 50000,
    maxRepeat: 3,
    minLevel: '',
    minScore: 0,
    minAnchorLength: 5,
    extraGrep: [],
    exclude: [],
    parser: 'auto',
    timeStart: '',
    timeEnd: '',
    ...opt,
  };

  const wanted = new Set(o.services.map((s) => s.toLowerCase()));
  const entries = wanted.size
    ? catalog.entries.filter((e) => wanted.has(String(e.service).toLowerCase()))
    : catalog.entries;
  if (!entries.length) throw new Error('No catalog entries for the selected service(s).');

  const index = buildIndex(entries, o.minAnchorLength);
  const incRes = o.extraGrep.filter(Boolean).map((s) => new RegExp(s, 'i'));
  const excRes = o.exclude.filter(Boolean).map((s) => new RegExp(s, 'i'));
  const minLvl = o.minLevel ? LEVEL_ORDER.indexOf(o.minLevel[0].toUpperCase()) : -1;

  // --- ingest: many files, mtime order, optional time window --------------
  const src = readRecords(logFiles, o.parser, { timeStart: o.timeStart, timeEnd: o.timeEnd });

  // --- hard filter ---------------------------------------------------------
  const recs = [];
  for (const r of src.records) {
    if (excRes.some((re) => re.test(r.raw))) continue;
    // Continuation lines (stack frames) carry their parent's level; never drop
    // them on level alone or the trace gets decapitated.
    if (!r.cont && minLvl >= 0 && r.level && LEVEL_ORDER.indexOf(r.level) < minLvl) continue;
    recs.push(r);
  }

  // --- seed scoring --------------------------------------------------------
  const seeds = [];
  const seedSet = new Set();
  const hitCount = new Map();

  for (let p = 0; p < recs.length; p++) {
    const r = recs[p];
    let score = 0;
    const why = [];

    const hit = matchLine(index, r.msg || r.raw);
    if (hit) {
      score += 6 + Math.min(6, hit.anchor.length / 4);
      why.push('catalog');
      r.entry = hit.entry;
      hitCount.set(hit.entry.id, (hitCount.get(hit.entry.id) || 0) + 1);
    }
    // DLT carries two identifiers (APID + CTID); either may match a catalog tag.
    const tagHit = (r.tag && index.tags.has(r.tag.toLowerCase())) ||
                   (r.apid && index.tags.has(r.apid.toLowerCase()));
    if (tagHit) {
      score += 5;
      why.push('tag');
    }
    for (const re of incRes) {
      if (re.test(r.raw)) { score += 5; why.push('grep'); break; }
    }
    if (why.length) {
      for (const [re, w] of DEFAULT_SIGNALS) {
        if (re.test(r.msg || r.raw)) { score += w; why.push('signal'); break; }
      }
      score += LEVEL_WEIGHT[r.level] != null ? LEVEL_WEIGHT[r.level] : 0.5;
    }

    if (why.length && score >= Math.max(0.01, o.minScore)) {
      r.score = score;
      r.reason = [...new Set(why)].join('+');
      seeds.push(p);
      seedSet.add(p);
    }
  }

  // --- collapse repeated seeds --------------------------------------------
  // A chatty service emits the same message hundreds of times. Keeping every
  // instance produces a brief made of chunk headers instead of evidence, so we
  // keep only the highest-scoring `maxRepeat` instances of each message shape
  // and report the true occurrence count in the summary.
  for (const p of seeds) recs[p].template = templatize(recs[p].msg || recs[p].raw);
  const occurrences = new Map();
  for (const p of seeds) {
    const t = recs[p].template;
    occurrences.set(t, (occurrences.get(t) || 0) + 1);
  }
  const seedsByScore = seeds.slice().sort((a, b) => recs[b].score - recs[a].score || a - b);
  const perTpl = new Map();
  const keptSeeds = [];
  for (const p of seedsByScore) {
    const t = recs[p].template;
    const n = perTpl.get(t) || 0;
    if (n >= o.maxRepeat) continue;
    perTpl.set(t, n + 1);
    keptSeeds.push(p);
  }

  // --- budget by region: a seed is only worth keeping WITH its context ------
  const CHUNK_OVERHEAD_TOKENS = 30;
  const chosenSet = new Set();
  let used = 0;
  let dropped = 0;
  for (const p of keptSeeds) {
    const r = recs[p];
    const region = [];
    const lo = Math.max(0, p - o.before);
    const hi = Math.min(recs.length - 1, p + o.after);
    for (let j = lo; j <= hi; j++) {
      // never pull context across a file boundary
      if (recs[j].srcIdx === r.srcIdx) region.push(j);
    }
    if (o.windowMs && r.ts) {
      const rc = clockOf(r.ts);
      const wlo = Math.max(0, p - 400);
      const whi = Math.min(recs.length, p + 400);
      for (let j = wlo; j < whi; j++) {
        if (j >= lo && j <= hi) continue;
        const c = recs[j];
        if (c.srcIdx !== r.srcIdx || !c.ts) continue;
        const cc = clockOf(c.ts);
        if (rc === null || cc === null) continue;
        const sameThread = (c.tid && c.tid === r.tid) || (c.pid && c.pid === r.pid);
        if (sameThread && Math.abs(cc - rc) <= o.windowMs) region.push(j);
      }
    }
    const fresh = region.filter((j) => !chosenSet.has(j));
    const cost = fresh.reduce((s, j) => s + estTokens(recs[j].raw), 0) + CHUNK_OVERHEAD_TOKENS;
    if (used + cost > o.budgetTokens) { dropped++; continue; }
    used += cost;
    for (const j of region) chosenSet.add(j);
  }

  const chosen = [...chosenSet].sort((a, b) => a - b);

  // --- chunk (every chunk contains a seed by construction) -----------------
  const gap = Math.max(o.after, 10);
  const chunks = [];
  let cur = [];
  for (const p of chosen) {
    const brk = cur.length &&
      (p - cur[cur.length - 1] > gap || recs[p].srcIdx !== recs[cur[cur.length - 1]].srcIdx);
    if (brk) { chunks.push(cur); cur = []; }
    cur.push(p);
  }
  if (cur.length) chunks.push(cur);

  const kept = chunks.reduce((s, c) => s + c.length, 0);
  const fired = entries.filter((e) => hitCount.has(e.id));

  return {
    recs,
    chunks,
    stats: {
      logFiles: src.sources.map((s) => s.file),
      sources: src.sources,
      parser: src.format,
      formatNote: src.note,
      timeWindow: src.timeWindow,
      totalLines: src.totalLines,
      afterTime: src.keptLines,
      droppedByTime: src.droppedByTime,
      afterFilter: recs.length,
      seeds: seeds.length,
      seedsKept: keptSeeds.length,
      seedsDropped: dropped,
      kept,
      tokens: used,
      chunkCount: chunks.length,
      catalogEntries: entries.length,
      anchors: index.anchorCount,
      firedCount: fired.length,
      services: o.services,
      counts: occurrences,
      hitCount,
      fired,
      entries,
    },
  };
}

// ---------------------------------------------------------------------------
// render
// ---------------------------------------------------------------------------
function renderBrief(result, opt = {}) {
  const { recs, chunks, stats } = result;
  const o = [];
  // When a time window is active the meaningful denominator is what survived
  // the window, not everything that was read off disk.
  const base = stats.timeWindow ? stats.afterTime : stats.totalLines;
  const pct = base ? ((stats.kept / base) * 100).toFixed(2) : '0';
  const tsAll = recs.filter((r) => r.ts);
  const multi = stats.sources.length > 1;

  const title = multi
    ? `${stats.sources.length} log files`
    : path.basename(stats.sources[0] ? stats.sources[0].name : 'log');

  o.push(`# Log Brief - \`${title}\``);
  o.push('');
  o.push('> Generated by LogSleuth. Lines below are the subset of the runtime log that maps to');
  o.push('> log statements found in the source code, plus surrounding context.');
  o.push('');
  o.push('## Summary');
  o.push('');
  o.push(`- **Format detected**: \`${stats.parser}\`` + (stats.formatNote ? ` — ${stats.formatNote}` : ''));
  o.push(`- **Services**: ${stats.services.length ? stats.services.map((s) => `\`${s}\``).join(', ') : '_all in catalog_'}`);
  if (stats.timeWindow) {
    o.push(`- **Time window**: \`${stats.timeWindow.start}\` → \`${stats.timeWindow.end}\`` +
      (stats.droppedByTime ? ` (${stats.droppedByTime.toLocaleString()} line(s) outside the window discarded)` : ''));
  }
  o.push(`- **Catalog**: ${stats.catalogEntries.toLocaleString()} statements / ${stats.anchors.toLocaleString()} anchors; **${stats.firedCount}** appeared in this log`);
  o.push(`- **Reduction**: ${stats.totalLines.toLocaleString()} lines read` +
    (stats.timeWindow ? ` -> ${stats.afterTime.toLocaleString()} in window` : '') +
    ` -> **${stats.kept.toLocaleString()}** kept (${pct}%), ~**${stats.tokens.toLocaleString()} tokens**, ${stats.chunkCount} chunk(s)`);
  o.push(`- **Match seeds**: ${stats.seeds.toLocaleString()} found, ${stats.seedsKept.toLocaleString()} kept after collapsing repeats` +
    (stats.seedsDropped ? `, ${stats.seedsDropped.toLocaleString()} skipped for budget` : ''));
  if (tsAll.length) o.push(`- **Time range**: \`${tsAll[0].ts}\` -> \`${tsAll[tsAll.length - 1].ts}\``);
  o.push('');

  // --- source files, in the order they were merged ------------------------
  if (multi) {
    o.push('## Source log files');
    o.push('');
    o.push('Merged oldest-first by file modified time.');
    o.push('');
    o.push('| # | File | Modified | Format | Lines |' + (stats.timeWindow ? ' In window |' : ''));
    o.push('|---:|---|---|---|---:|' + (stats.timeWindow ? '---:|' : ''));
    stats.sources.forEach((s, i) => {
      o.push(`| ${i + 1} | \`${s.name}\` | ${s.mtimeText} | \`${s.format}\` | ${s.lines.toLocaleString()} |` +
        (stats.timeWindow ? ` ${s.kept.toLocaleString()} |` : ''));
    });
    o.push('');
  }

  const fired = stats.fired
    .map((e) => ({ e, n: stats.hitCount.get(e.id) || 0 }))
    .sort((a, b) => b.n - a.n)
    .slice(0, 25);
  if (fired.length) {
    o.push('## Source statements observed');
    o.push('');
    o.push('| Hits | Lvl | Template | Source |');
    o.push('|---:|:--:|---|---|');
    for (const { e, n } of fired) {
      o.push(`| ${n} | ${e.level || '-'} | ${e.template.replace(/\|/g, '\\|').slice(0, 110)} | \`${e.file}:${e.line}\` |`);
    }
    o.push('');
  }

  const rep = [...stats.counts.entries()].filter(([, c]) => c > 1).sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (rep.length) {
    o.push('## Repeated patterns (collapsed)');
    o.push('');
    o.push(`Shown at most ${opt.maxRepeat || 3}x each in the slices below; the counts are the real totals.`);
    o.push('');
    o.push('| Occurrences | Normalized message |');
    o.push('|---:|---|');
    for (const [t, c] of rep) o.push(`| ${c} | ${t.replace(/\|/g, '\\|').slice(0, 120)} |`);
    o.push('');
  }

  o.push('## Log slices');
  o.push('');
  if (!chunks.length) {
    o.push('_No lines matched. Try lowering `minScore`, clearing `minLevel`, widening the time window, or selecting more services._');
    o.push('');
  }
  chunks.forEach((ch, i) => {
    const a = recs[ch[0]];
    const b = recs[ch[ch.length - 1]];
    let head = `### Chunk ${i + 1} - `;
    head += multi ? `\`${a.file}\` lines ${a.fileLine}-${b.fileLine}` : `lines ${a.fileLine}-${b.fileLine}`;
    if (a.ts) head += ` (\`${a.ts}\` -> \`${b.ts}\`)`;
    o.push(head);
    o.push('');
    const srcs = [...new Set(ch.map((p) => recs[p].entry).filter(Boolean).map((e) => `${e.file}:${e.line}`))].slice(0, 6);
    if (srcs.length) o.push(`*source: ${srcs.map((s) => `\`${s}\``).join(', ')}*`);
    o.push('');
    o.push('```log');
    let prev = null;
    for (const p of ch) {
      if (prev !== null && p - prev > 1) o.push(`... (${p - prev - 1} lines omitted) ...`);
      o.push(recs[p].raw);
      prev = p;
    }
    o.push('```');
    o.push('');
  });

  const never = stats.entries.filter((e) => !stats.hitCount.has(e.id) && (e.level === 'E' || e.level === 'F'));
  if (never.length) {
    o.push('## Error paths NOT hit');
    o.push('');
    o.push(`${never.length} error/fatal statement(s) in the catalog never appeared in this log.`);
    o.push('');
    o.push('<details><summary>Show first 20</summary>');
    o.push('');
    for (const e of never.slice(0, 20)) o.push(`- \`${e.file}:${e.line}\` - ${e.template.slice(0, 110)}`);
    o.push('');
    o.push('</details>');
    o.push('');
  }
  return o.join('\n');
}

module.exports = { sliceLog, renderBrief, templatize, buildIndex };
