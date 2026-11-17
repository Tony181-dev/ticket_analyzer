'use strict';
/** Final-output guard around the unchanged LogSleuth renderer.
 *
 * UTF-8 bytes are a conservative upper bound for byte-based tokenizers (rather
 * than the engine's chars/4 heuristic). Counts include headings/tables/fences.
 * Whole low-priority chunks are removed; log lines are NEVER cut mid-record.
 */
const { renderBrief } = require('./slicer');
const MAX_TOKENS = 50000;
const MIN_TOKENS = 2000;
const tokenBound = (text) => Buffer.byteLength(text, 'utf8');

function budgetLimit(value = MAX_TOKENS) {
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n < MIN_TOKENS) throw new Error(`Token budget must be an integer >= ${MIN_TOKENS}`);
  return Math.min(n, MAX_TOKENS);
}

function renderBoundedBrief(result, options = {}) {
  const budget = budgetLimit(options.budgetTokens);
  // The engine can merge thousands of adjacent regions into one chunk. Split
  // at record boundaries so the final cap does not discard that entire chunk.
  // Continuation chains stay with their parent, even if the chain is too large.
  const candidates = [];
  const targetBytes = Math.max(512, Math.floor(budget / 3));
  for (const original of result.chunks) {
    let chunk = [], bytes = 0;
    for (const p of original) {
      const cost = tokenBound(result.recs[p].raw) + 1;
      if (chunk.length && bytes + cost > targetBytes && !result.recs[p].cont) {
        candidates.push(chunk); chunk = []; bytes = 0;
      }
      chunk.push(p); bytes += cost;
    }
    if (chunk.length) candidates.push(chunk);
  }
  let ranked = candidates.map((chunk) => ({
    chunk, score: chunk.reduce((s, p) => Math.max(s, result.recs[p].score || 0), 0),
  })).sort((a, b) => b.score - a.score || a.chunk[0] - b.chunk[0]);
  const notice = `> Output cap: ${budget} tokens (conservative UTF-8 byte bound, including all Markdown).\n` +
    '> Lower-priority regions may be omitted to fit. An absent log statement is not proof that a code path did not execute.\n\n';

  function render(count, compact, selected = null) {
    const chunks = (selected || ranked.slice(0, count).map((x) => x.chunk)).slice().sort((a, b) => a[0] - b[0]);
    const kept = chunks.reduce((n, ch) => n + ch.length, 0);
    const logTokens = chunks.reduce((n, ch) => n + ch.reduce((k, p) => k + Math.ceil(result.recs[p].raw.length / 4), 0), 0);
    const view = { ...result, chunks, stats: { ...result.stats, kept, tokens: logTokens, chunkCount: chunks.length } };
    if (!compact) return { text: notice + renderBrief(view, options), kept, chunks };
    const s = result.stats;
    const header = '# Log Brief\n\n' + notice +
      `- ${s.totalLines} lines read; ${kept} kept; ${s.sources.length} source file(s).\n` +
      (s.timeWindow ? `- **Time window**: ${(s.timeWindows && s.timeWindows.length ? s.timeWindows : [s.timeWindow])
        .map((w) => `${w.start} -> ${w.end}`).join('; ')}\n` : '') +
      '- Detailed source tables omitted for budget; see 03-log-stats.json.\n\n## Log slices\n\n';
    const body = chunks.map((ch, i) => {
      const a = result.recs[ch[0]], b = result.recs[ch[ch.length - 1]];
      return `### Chunk ${i + 1} - ${JSON.stringify(a.file)} lines ${a.fileLine}-${b.fileLine}\n\n` +
        '```log\n' + ch.map((p) => result.recs[p].raw).join('\n') + '\n```\n';
    }).join('\n');
    return { text: header + body, kept, chunks };
  }

  // One oversized stack/record must not make every ranked prefix fail and
  // suppress the smaller, independently useful regions behind it.
  ranked = ranked.filter((item) => tokenBound(render(1, true, [item.chunk]).text) <= budget);

  // Oversized metadata must not consume the entire budget before log evidence.
  let compact = tokenBound(render(0, false).text) > Math.min(budget / 2, 8000);
  function fit(useCompact) {
    let lo = 0, hi = ranked.length, best = render(0, useCompact);
    while (lo <= hi) {
      const mid = Math.floor((lo + hi) / 2);
      const candidate = render(mid, useCompact);
      if (tokenBound(candidate.text) <= budget) { best = candidate; lo = mid + 1; }
      else hi = mid - 1;
    }
    return best;
  }
  let best = fit(compact);
  if (!compact && !best.kept && ranked.length) {
    const smaller = fit(true);
    if (smaller.kept > best.kept) { best = smaller; compact = true; }
  }
  // Minimal metadata is bounded, even if catalog/file names are pathological.
  if (tokenBound(best.text) > budget) best = render(0, true);
  if (tokenBound(best.text) > budget) throw new Error('Cannot fit log brief metadata into token budget');
  const bytes = tokenBound(best.text);
  return {
    text: best.text,
    stats: { ...result.stats, kept: best.kept, chunkCount: best.chunks.length,
      sliceTokens: result.stats.tokens, tokens: bytes, tokenUpperBound: bytes,
      tokenEstimate: Math.ceil(best.text.length / 4), budgetTokens: budget,
      tokenCounting: 'utf8-byte-upper-bound', outputBytes: bytes,
      outputTruncated: best.kept < result.stats.kept || result.stats.seedsDropped > 0, compactMetadata: compact },
  };
}

module.exports = { renderBoundedBrief, tokenBound, budgetLimit, MAX_TOKENS, MIN_TOKENS };