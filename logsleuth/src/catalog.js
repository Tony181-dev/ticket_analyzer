'use strict';
/**
 * Catalog markdown writer / reader.
 * The .md is human readable (tables per service) AND machine readable:
 * a fenced ```json block carries the exact entries so STEP 2 never has to
 * re-parse prose.
 */

const MARKER = '<!-- logsleuth:catalog:v1 -->';

function esc(s) {
  return String(s).replace(/\|/g, '\\|').replace(/\r?\n/g, '\\n').replace(/`/g, '\u02cb');
}

function renderCatalog(result) {
  const { entries, stats } = result;
  const o = [];
  o.push(MARKER);
  o.push('# Log Catalog');
  o.push('');
  o.push(`- **Source root**: \`${stats.root}\``);
  o.push(`- **Generated**: ${stats.generatedAt}`);
  o.push(`- **Files scanned**: ${stats.filesScanned.toLocaleString()} (with logs: ${stats.filesWithLogs.toLocaleString()})`);
  o.push(`- **Log statements**: ${stats.entryCount.toLocaleString()} across ${stats.serviceCount} service(s)`);
  const langs = Object.entries(stats.byLang).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k}:${v}`);
  if (langs.length) o.push(`- **By language**: ${langs.join(', ')}`);
  o.push('');

  o.push('## Services');
  o.push('');
  o.push('| Service | Statements | F | E | W | I | D | V |');
  o.push('|---|---:|---:|---:|---:|---:|---:|---:|');
  const svcs = Object.entries(stats.services).sort((a, b) => b[1].count - a[1].count);
  for (const [name, s] of svcs) {
    const L = (k) => s.levels[k] || 0;
    o.push(`| \`${esc(name)}\` | ${s.count} | ${L('F')} | ${L('E')} | ${L('W')} | ${L('I')} | ${L('D')} | ${L('V')} |`);
  }
  o.push('');

  o.push('## Statements');
  o.push('');
  let current = null;
  for (const e of entries) {
    if (e.service !== current) {
      current = e.service;
      o.push('');
      o.push(`### \`${esc(current)}\``);
      o.push('');
      o.push('| ID | Lvl | Template | Anchor | Location |');
      o.push('|---|:--:|---|---|---|');
    }
    const loc = e.sites.length > 1 ? `${e.file}:${e.line} (+${e.sites.length - 1})` : `${e.file}:${e.line}`;
    o.push(`| ${e.id} | ${e.level || '-'} | ${esc(e.template)} | ${esc(e.anchors[0] || '')} | ${esc(loc)} |`);
  }
  o.push('');
  o.push('## Machine-readable payload');
  o.push('');
  o.push('```json');
  o.push(JSON.stringify({ version: 1, stats, entries }, null, 1));
  o.push('```');
  o.push('');
  return o.join('\n');
}

/** Read back a catalog .md (or raw .json) produced above. */
function parseCatalog(text) {
  const trimmed = text.trim();
  if (trimmed.startsWith('{')) return JSON.parse(trimmed);
  const m = text.match(/```json\s*\n([\s\S]*?)\n```/);
  if (!m) throw new Error('Not a LogSleuth catalog: missing ```json payload block.');
  const data = JSON.parse(m[1]);
  if (!data || !Array.isArray(data.entries)) throw new Error('Catalog payload has no "entries" array.');
  return data;
}

module.exports = { renderCatalog, parseCatalog, MARKER };
