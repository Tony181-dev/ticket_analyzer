'use strict';
/**
 * Multi-format, multi-file log reader.
 *
 * Turns ANY supported log file (or a SET of them) into a uniform record list:
 *   { idx, raw, ts, level, tag, apid, pid, tid, msg, cont, srcIdx, file, fileLine }
 *
 * Handles:
 *   - BINARY AUTOSAR DLT (.dlt)  -> full protocol decode, rendered as clean text
 *   - DLT text export (dlt-convert -a)
 *   - Android logcat: threadtime / time / brief
 *   - syslog RFC3164 + RFC5424, journalctl (short, short-iso)
 *   - QNX slog2info
 *   - log4j / logback / Serilog / ISO-8601 app logs
 *   - JSON lines (bunyan/pino/winston/ECS)
 *   - plain .txt / .log / .out, gzip, UTF-16
 *   - multi-line stack traces (attached to their parent record)
 *
 * Multi-file: files are concatenated in MODIFIED-TIME order (oldest first),
 * each file detected independently so a mixed set still parses correctly.
 *
 * Time window: an optional HH:MM:SS start/end keeps only records inside the
 * window, carrying the last seen clock forward across untimestamped lines.
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const LEVEL_ORDER = 'VDIWEF';

// ===========================================================================
// level normalization
// ===========================================================================
function normLevel(v) {
  if (v === undefined || v === null || v === '') return '';
  const s = String(v).toUpperCase().trim();
  if (s.length === 1) return LEVEL_ORDER.includes(s) ? s : (s === 'S' || s === 'A') ? 'F' : '';
  if (s.startsWith('WARN')) return 'W';
  if (s.startsWith('ERR')) return 'E';
  if (s.startsWith('FATAL') || s.startsWith('CRIT') || s.startsWith('EMERG') ||
      s.startsWith('ALERT') || s.startsWith('SEVERE') || s.startsWith('PANIC')) return 'F';
  if (s.startsWith('INFO') || s.startsWith('NOTICE') || s.startsWith('LOG')) return 'I';
  if (s.startsWith('DEBUG') || s.startsWith('FINE')) return 'D';
  if (s.startsWith('TRACE') || s.startsWith('VERB')) return 'V';
  return '';
}

// ===========================================================================
// clock helpers (time-window filtering)
// ===========================================================================
/** "HH:MM:SS(.mmm)" or "HH:MM" -> ms since midnight, else null. */
function parseClock(s) {
  if (s === undefined || s === null) return null;
  const m = /^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d{1,6}))?\s*$/.exec(String(s));
  if (!m) return null;
  const h = +m[1];
  const mi = +m[2];
  const se = m[3] ? +m[3] : 0;
  if (h > 23 || mi > 59 || se > 59) return null;
  const frac = m[4] ? Number((m[4] + '000').slice(0, 3)) : 0;
  return ((h * 60 + mi) * 60 + se) * 1000 + frac;
}

/** Pull a wall clock out of ANY timestamp string -> ms since midnight. */
function clockOf(ts) {
  if (!ts) return null;
  const m = /(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?/.exec(ts);
  if (!m) return null;
  const frac = m[4] ? Number((m[4] + '000').slice(0, 3)) : 0;
  return ((+m[1] * 60 + +m[2]) * 60 + +m[3]) * 1000 + frac;
}

/** Inclusive range test that also handles a window crossing midnight. */
function inWindow(t, a, b) {
  if (a === null || b === null) return true;
  return a <= b ? (t >= a && t <= b) : (t >= a || t <= b);
}

function fmtClock(ms) {
  if (ms === null || ms === undefined) return '';
  const p = (n, w = 2) => String(n).padStart(w, '0');
  const s = Math.floor(ms / 1000);
  return `${p(Math.floor(s / 3600) % 24)}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}`;
}

// ===========================================================================
// 1. BINARY DLT
// ===========================================================================
const DLT_LOG_LEVEL = { 1: 'F', 2: 'E', 3: 'W', 4: 'I', 5: 'D', 6: 'V' };
const DLT_MSTP = { 0: 'log', 1: 'app_trace', 2: 'nw_trace', 3: 'control' };

const TYPE_LENGTH = 0x0000000f;
const TYPE_BOOL = 0x00000010;
const TYPE_SINT = 0x00000020;
const TYPE_UINT = 0x00000040;
const TYPE_FLOA = 0x00000080;
const TYPE_ARAY = 0x00000100;
const TYPE_STRG = 0x00000200;
const TYPE_RAWD = 0x00000400;
const TYPE_VARI = 0x00000800;
const TYPE_FIXP = 0x00001000;
const TYPE_TRAI = 0x00002000;
const TYPE_STRU = 0x00004000;
const TYPE_SCOD = 0x00038000;

const TYLE_BYTES = { 0: 0, 1: 1, 2: 2, 3: 4, 4: 8, 5: 16 };

function readId(buf, off, len) {
  let s = '';
  for (let i = 0; i < len && off + i < buf.length; i++) {
    const c = buf[off + i];
    if (c === 0) break;
    s += c >= 0x20 && c < 0x7f ? String.fromCharCode(c) : '';
  }
  return s.trim();
}

function cleanText(s) {
  return s
    .replace(/\u0000+$/g, '')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, ' ')
    .replace(/\s+$/g, '');
}

function decodeVerbosePayload(buf, msbf, noar) {
  const u32 = (p) => (msbf ? buf.readUInt32BE(p) : buf.readUInt32LE(p));
  const u16 = (p) => (msbf ? buf.readUInt16BE(p) : buf.readUInt16LE(p));
  const parts = [];
  let o = 0;
  const limit = Math.min(noar || 64, 64);

  for (let i = 0; i < limit && o + 4 <= buf.length; i++) {
    const info = u32(o);
    o += 4;
    const tyle = info & TYPE_LENGTH;
    const width = TYLE_BYTES[tyle] || 0;

    let varName = '';
    if (info & TYPE_VARI) {
      if (o + 2 > buf.length) return null;
      const nameLen = u16(o);
      o += 2;
      let unitLen = 0;
      if (info & (TYPE_FIXP | TYPE_UINT | TYPE_SINT | TYPE_FLOA)) {
        if (o + 2 > buf.length) return null;
        unitLen = u16(o);
        o += 2;
      }
      if (nameLen > 0 && o + nameLen <= buf.length) {
        varName = cleanText(buf.toString('utf8', o, o + nameLen)).replace(/\0/g, '');
        o += nameLen;
      }
      if (unitLen > 0 && o + unitLen <= buf.length) o += unitLen;
    }
    const pfx = varName ? `${varName}=` : '';

    if (info & (TYPE_STRG | TYPE_RAWD | TYPE_TRAI)) {
      if (o + 2 > buf.length) return null;
      const n = u16(o);
      o += 2;
      if (n < 0 || o + n > buf.length) return null;
      const slice = buf.slice(o, o + n);
      o += n;
      if (info & TYPE_RAWD) {
        const hex = slice.slice(0, 32).toString('hex').replace(/(..)/g, '$1 ').trim();
        parts.push(`${pfx}<raw ${n}B: ${hex}${n > 32 ? ' …' : ''}>`);
      } else {
        const isUtf8 = ((info & TYPE_SCOD) >> 15) === 1;
        let str = slice.toString(isUtf8 ? 'utf8' : 'latin1');
        parts.push(pfx + cleanText(str.replace(/\0+$/, '')));
      }
      continue;
    }

    if (info & TYPE_BOOL) {
      const w = width || 1;
      if (o + w > buf.length) return null;
      parts.push(pfx + (buf[o] ? 'true' : 'false'));
      o += w;
      continue;
    }

    if (info & (TYPE_UINT | TYPE_SINT | TYPE_FIXP)) {
      if (!width || o + width > buf.length) return null;
      const signed = !!(info & TYPE_SINT);
      let val;
      if (width === 1) val = signed ? buf.readInt8(o) : buf.readUInt8(o);
      else if (width === 2) val = signed ? (msbf ? buf.readInt16BE(o) : buf.readInt16LE(o)) : u16(o);
      else if (width === 4) val = signed ? (msbf ? buf.readInt32BE(o) : buf.readInt32LE(o)) : u32(o);
      else if (width === 8) {
        const b = signed
          ? (msbf ? buf.readBigInt64BE(o) : buf.readBigInt64LE(o))
          : (msbf ? buf.readBigUInt64BE(o) : buf.readBigUInt64LE(o));
        val = b.toString();
      } else val = '0x' + buf.slice(o, o + width).toString('hex');
      parts.push(pfx + val);
      o += width;
      continue;
    }

    if (info & TYPE_FLOA) {
      if (!width || o + width > buf.length) return null;
      let val;
      if (width === 4) val = msbf ? buf.readFloatBE(o) : buf.readFloatLE(o);
      else if (width === 8) val = msbf ? buf.readDoubleBE(o) : buf.readDoubleLE(o);
      else val = '0x' + buf.slice(o, o + width).toString('hex');
      parts.push(pfx + (typeof val === 'number' ? shortFloat(val) : val));
      o += width;
      continue;
    }

    if (info & (TYPE_ARAY | TYPE_STRU)) {
      parts.push(`${pfx}<${info & TYPE_ARAY ? 'array' : 'struct'}>`);
      break;
    }

    return null;
  }

  if (!parts.length) return null;
  return parts.join(' ').trim();
}

function shortFloat(n) {
  if (!isFinite(n)) return String(n);
  if (Number.isInteger(n)) return String(n);
  return String(Math.round(n * 1e6) / 1e6);
}

function printableRuns(buf, minRun = 4) {
  const out = [];
  let cur = '';
  for (let i = 0; i < buf.length; i++) {
    const c = buf[i];
    if (c >= 0x20 && c < 0x7f) cur += String.fromCharCode(c);
    else {
      if (cur.length >= minRun) out.push(cur);
      cur = '';
    }
  }
  if (cur.length >= minRun) out.push(cur);
  return out.join(' ');
}

function fmtEpoch(secs, usecs) {
  const d = new Date(secs * 1000 + Math.floor(usecs / 1000));
  if (isNaN(d.getTime()) || secs <= 0) return '';
  const p = (n, w = 2) => String(n).padStart(w, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(Math.floor(usecs / 1000) % 1000, 3)}`;
}

function decodeDltMessage(buf, p, storage) {
  if (p + 4 > buf.length) return null;

  const htyp = buf[p];
  if (((htyp >> 5) & 0x07) !== 1) return null;

  const UEH = !!(htyp & 0x01);
  const MSBF = !!(htyp & 0x02);
  const WEID = !!(htyp & 0x04);
  const WSID = !!(htyp & 0x08);
  const WTMS = !!(htyp & 0x10);

  const len = buf.readUInt16BE(p + 2); // LEN is ALWAYS big-endian
  const minLen = 4 + (WEID ? 4 : 0) + (WSID ? 4 : 0) + (WTMS ? 4 : 0) + (UEH ? 10 : 0);
  if (len < minLen || p + len > buf.length) return null;

  let q = p + 4;
  let ecu = storage ? storage.ecu : '';
  if (WEID) { ecu = readId(buf, q, 4) || ecu; q += 4; }
  if (WSID) q += 4;
  let tmsp = 0;
  if (WTMS) { tmsp = buf.readUInt32BE(q); q += 4; }

  let apid = '';
  let ctid = '';
  let level = '';
  let kind = 'log';
  let verbose = false;
  let noar = 0;

  if (UEH) {
    const msin = buf[q];
    verbose = !!(msin & 0x01);
    const mstp = (msin >> 1) & 0x07;
    const mtin = (msin >> 4) & 0x0f;
    noar = buf[q + 1];
    apid = readId(buf, q + 2, 4);
    ctid = readId(buf, q + 6, 4);
    q += 10;
    kind = DLT_MSTP[mstp] || 'log';
    if (mstp === 0) level = DLT_LOG_LEVEL[mtin] || '';
  }

  const payload = buf.slice(q, p + len);
  let msg = verbose ? decodeVerbosePayload(payload, MSBF, noar) : null;

  if (msg === null) {
    if (payload.length >= 4) {
      const id = MSBF ? payload.readUInt32BE(0) : payload.readUInt32LE(0);
      const text = printableRuns(payload.slice(4));
      msg = `[id=0x${id.toString(16).padStart(8, '0')}]${text ? ' ' + text : ''}`;
    } else {
      msg = printableRuns(payload) || '<empty>';
    }
  }

  if (kind === 'control') msg = `[control] ${msg}`;
  else if (kind !== 'log') msg = `[${kind}] ${msg}`;

  return {
    len,
    rec: {
      ts: storage ? fmtEpoch(storage.secs, storage.usecs) : '',
      uptime: tmsp ? (tmsp / 10000).toFixed(4) : '',
      level, ecu, apid,
      tag: ctid || apid,
      pid: apid,
      tid: ctid,
      msg,
    },
  };
}

function findStorageMarker(buf, from) {
  for (let i = from; i + 4 <= buf.length; i++) {
    if (buf[i] === 0x44 && buf[i + 1] === 0x4c && buf[i + 2] === 0x54 && buf[i + 3] === 0x01) return i;
  }
  return -1;
}

function decodeDltBinary(buf) {
  const records = [];
  let off = 0;
  let idx = 0;
  let skipped = 0;

  const first = findStorageMarker(buf, 0);
  const hasStorage = first !== -1 && first < 4096;
  if (hasStorage && first > 0) { skipped += first; off = first; }

  while (off < buf.length) {
    let storage = null;
    let msgStart = off;

    if (hasStorage) {
      if (off + 16 > buf.length) break;
      if (!(buf[off] === 0x44 && buf[off + 1] === 0x4c && buf[off + 2] === 0x54 && buf[off + 3] === 0x01)) {
        const next = findStorageMarker(buf, off + 1);
        if (next === -1) break;
        skipped += next - off;
        off = next;
        continue;
      }
      storage = {
        secs: buf.readUInt32LE(off + 4),
        usecs: buf.readInt32LE(off + 8),
        ecu: readId(buf, off + 12, 4),
      };
      msgStart = off + 16;
    }

    const decoded = decodeDltMessage(buf, msgStart, storage);
    if (!decoded) {
      const next = findStorageMarker(buf, off + 1);
      if (next === -1) break;
      skipped += next - off;
      off = next;
      continue;
    }

    const r = decoded.rec;
    idx++;
    const head = [r.ts, r.ecu, `${r.apid}/${r.tag}`, r.level || '-'].filter(Boolean).join(' ');
    records.push({
      idx,
      raw: `${head} ${r.msg}`.trim(),
      ts: r.ts, level: r.level, tag: r.tag, apid: r.apid,
      pid: r.pid, tid: r.tid, msg: r.msg, cont: false,
    });
    off = msgStart + decoded.len;
  }

  return { records, skippedBytes: skipped };
}

/**
 * Does this buffer look like ordinary text?
 * Needed because ASCII digits (0x30-0x3f) happen to encode a "valid" DLT
 * version field, so a naive protocol probe reports false positives on
 * perfectly normal logcat files.
 */
function looksLikeText(buf) {
  const n = Math.min(buf.length, 8192);
  if (n === 0) return false;
  let printable = 0;
  let newlines = 0;
  for (let i = 0; i < n; i++) {
    const c = buf[i];
    if (c === 0x0a) { newlines++; printable++; } else if (c === 0x09 || c === 0x0d) printable++;
    else if (c >= 0x20 && c < 0x7f) printable++;
    else if (c >= 0x80) printable++; // assume UTF-8 continuation
  }
  return printable / n >= 0.95 && newlines > 0;
}

/** A DLT message is only credible if its extended header holds printable ids. */
function plausibleDltMessage(buf, p) {
  const d = decodeDltMessage(buf, p, null);
  if (!d) return null;
  if (d.len < 16 || d.len > 65535) return null;
  // Require the extended header: without APID/CTID there is nothing to verify.
  if (!(buf[p] & 0x01)) return null;
  const r = d.rec;
  if (!r.apid || !r.tag) return null;
  if (!/^[\x20-\x7e]{1,4}$/.test(r.apid) || !/^[\x20-\x7e]{1,4}$/.test(r.tag)) return null;
  return d;
}

function looksLikeDltBinary(buf) {
  if (buf.length < 20) return false;
  // Storage header at offset 0 is unambiguous.
  if (buf[0] === 0x44 && buf[1] === 0x4c && buf[2] === 0x54 && buf[3] === 0x01) return true;
  // Anything that reads as clean text is not a binary DLT trace.
  if (looksLikeText(buf)) return false;
  if (findStorageMarker(buf.slice(0, 65536), 0) !== -1) return true;
  // Storage-less stream (serial/TCP capture): demand several consecutive,
  // structurally plausible messages before believing it.
  let off = 0;
  let ok = 0;
  for (let i = 0; i < 8; i++) {
    const d = plausibleDltMessage(buf, off);
    if (!d) break;
    ok++;
    off += d.len;
  }
  return ok >= 5;
}

// ===========================================================================
// 2. TEXT PARSERS
// ===========================================================================
const SLOG2_SEV = { 0: 'F', 1: 'F', 2: 'E', 3: 'W', 4: 'I', 5: 'I', 6: 'D', 7: 'V' };
const SYSLOG_SEV = { 0: 'F', 1: 'F', 2: 'F', 3: 'E', 4: 'W', 5: 'I', 6: 'I', 7: 'D' };

const TEXT_PARSERS = [
  {
    name: 'logcat-threadtime',
    re: /^(?<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(?<pid>\d+)\s+(?<tid>\d+)\s+(?<level>[VDIWEFSA])\s+(?<tag>[^:]{1,64}?)\s*:\s?(?<msg>.*)$/,
  },
  {
    name: 'logcat-time',
    re: /^(?<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(?<level>[VDIWEFSA])\/(?<tag>[^(\s]{1,64})\(\s*(?<pid>\d+)\s*\):\s?(?<msg>.*)$/,
  },
  {
    name: 'logcat-brief',
    re: /^(?<level>[VDIWEFSA])\/(?<tag>[^(\s]{1,64})\(\s*(?<pid>\d+)\s*\):\s?(?<msg>.*)$/,
  },
  {
    name: 'dlt-text',
    re: /^(?<num>\d+)?\s*(?<ts>\d{4}\/\d{2}\/\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+(?<uptime>[\d.]+)\s+(?<mcnt>\d+)\s+(?<ecu>\S+)\s+(?<apid>\S+)\s+(?<tag>\S+)\s+(?<kind>log|app_trace|nw_trace|control)\s+(?<level>\S+)\s+\S*\s*(?<msg>.*)$/,
  },
  {
    name: 'slog2',
    re: /^(?<ts>(?:\w{3}\s+\d{1,2}|\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}\.\d+)\s+(?<tag>[\w.\-]+)\s+(?<sev>\d{1,2})\s+(?<code>\d+)\s+(?<msg>.*)$/,
    map: (g, r) => { r.level = SLOG2_SEV[+g.sev] || ''; },
  },
  {
    name: 'syslog5424',
    re: /^<(?<pri>\d{1,3})>(?<ver>\d)\s+(?<ts>\S+)\s+(?<host>\S+)\s+(?<tag>\S+)\s+(?<pid>\S+)\s+(?<msgid>\S+)\s+(?:\[[^\]]*\]|-)\s*(?<msg>.*)$/,
    map: (g, r) => { r.level = SYSLOG_SEV[+g.pri & 7] || ''; if (r.pid === '-') r.pid = ''; },
  },
  {
    name: 'journal-iso',
    re: /^(?<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\s+(?<host>\S+)\s+(?<tag>[\w\-.@]+)(?:\[(?<pid>\d+)\])?:\s(?<msg>.*)$/,
  },
  {
    name: 'syslog',
    re: /^(?<ts>\w{3}\s+\d{1,2} \d{2}:\d{2}:\d{2})\s+(?<host>\S+)\s+(?<tag>[\w\-./]+)(?:\[(?<pid>\d+)\])?:\s(?<msg>.*)$/,
  },
  {
    name: 'log4j',
    re: /^(?<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d{1,6})\s*(?:\[(?<tid>[^\]]{1,48})\])?\s*(?<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE)\s+(?:\[(?<tid2>[^\]]{1,48})\]\s+)?(?<tag>[\w.$/\-]{1,80})?\s*[-:]?\s*(?<msg>.*)$/i,
  },
  {
    name: 'iso',
    re: /^(?<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+\[?(?<level>TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL|NOTICE)\]?\s+\[?(?<tag>[\w\-.$/]+)?\]?\s*[-:]?\s*(?<msg>.*)$/i,
  },
  {
    name: 'ts-level',
    re: /^\[?(?<ts>\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]?\s+\[?(?<level>TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL|[VDIWEF])\]?\s*\[?(?<tag>[\w\-.$/]{1,48})?\]?\s*[-:]?\s*(?<msg>.*)$/i,
  },
  { name: 'generic', re: null },
];

const JSON_TS = ['timestamp', 'time', 'ts', '@timestamp', 'datetime', 'date', 'eventTime'];
const JSON_LVL = ['level', 'severity', 'lvl', 'levelname', 'log.level', 'loglevel', 'Level'];
const JSON_TAG = ['logger', 'tag', 'service', 'name', 'component', 'module', 'category', 'source', 'unit'];
const JSON_MSG = ['message', 'msg', 'text', 'log', 'event', 'Message', 'MessageTemplate'];
const JSON_SKIP = new Set([...JSON_TS, ...JSON_LVL, ...JSON_TAG, ...JSON_MSG, 'v', 'hostname', 'pid', 'tid', 'thread']);

function firstKey(obj, keys) {
  for (const k of keys) if (obj[k] !== undefined && obj[k] !== null && obj[k] !== '') return obj[k];
  return undefined;
}

const BUNYAN = { 10: 'V', 20: 'D', 30: 'I', 40: 'W', 50: 'E', 60: 'F' };

function parseJsonLine(line) {
  const t = line.trim();
  if (!t.startsWith('{') || !t.endsWith('}')) return null;
  let o;
  try { o = JSON.parse(t); } catch { return null; }
  if (!o || typeof o !== 'object' || Array.isArray(o)) return null;

  const rawLvl = firstKey(o, JSON_LVL);
  const level = typeof rawLvl === 'number' ? (BUNYAN[rawLvl] || '') : normLevel(rawLvl);
  const msg = firstKey(o, JSON_MSG);
  if (msg === undefined && level === '') return null;

  const ts = firstKey(o, JSON_TS);
  const tag = firstKey(o, JSON_TAG);
  const extras = [];
  for (const [k, v] of Object.entries(o)) {
    if (JSON_SKIP.has(k)) continue;
    if (v === null || typeof v === 'object') continue;
    extras.push(`${k}=${v}`);
    if (extras.length >= 8) break;
  }
  return {
    ts: ts === undefined ? '' : String(ts),
    level,
    tag: tag === undefined ? '' : String(tag),
    pid: o.pid === undefined ? '' : String(o.pid),
    tid: String(o.tid || o.thread || ''),
    msg: [msg === undefined ? '' : String(msg), extras.join(' ')].filter(Boolean).join(' '),
  };
}

// Continuation lines: stack traces and wrapped output belong to the line above.
// An exception header has no leading whitespace, so it must be matched
// explicitly - otherwise it breaks the chain and orphans every frame below it.
const CONT_RE = new RegExp(
  '^(?:' +
  '\\s+at\\s' +
  '|\\s*at [\\w$.<>]+\\(' +
  '|\\s*Caused by:' +
  '|\\s*Suppressed:' +
  '|\\s*\\.\\.\\.\\s*\\d+\\s+more' +
  '|\\s*#\\d{1,3}\\s' +
  '|\\s*File "' +
  '|Traceback \\(most recent call last\\)' +
  '|[\\w$.]*(?:Exception|Error|Throwable)(?::|\\s*$)' +
  '|\\t' +
  '|\\s{4,}\\S' +
  ')'
);

function isContinuation(line) {
  return line.trim().length > 0 && CONT_RE.test(line);
}

// ===========================================================================
// 3. READER
// ===========================================================================
function readBuffer(file) {
  let buf = fs.readFileSync(file);
  if (buf.length > 2 && buf[0] === 0x1f && buf[1] === 0x8b) {
    try { buf = zlib.gunzipSync(buf); } catch { /* keep raw */ }
  }
  return buf;
}

function bufferToText(buf) {
  if (buf.length > 2 && buf[0] === 0xff && buf[1] === 0xfe) return buf.slice(2).toString('utf16le');
  if (buf.length > 2 && buf[0] === 0xfe && buf[1] === 0xff) {
    const swapped = Buffer.from(buf.slice(2));
    swapped.swap16();
    return swapped.toString('utf16le');
  }
  if (buf.length > 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) return buf.slice(3).toString('utf8');
  return buf.toString('utf8');
}

function pickTextParser(sample, forced) {
  if (forced && forced !== 'auto') {
    const p = TEXT_PARSERS.find((x) => x.name === forced);
    if (p) return p;
  }
  // Stack-trace lines never match any parser, so scoring them would drag the
  // hit ratio down and misdetect an otherwise well-structured log as generic.
  const scored = sample.filter((l) => !isContinuation(l));
  const pool = scored.length ? scored : sample;

  let best = TEXT_PARSERS[TEXT_PARSERS.length - 1];
  let bestHits = 0;
  for (const p of TEXT_PARSERS) {
    if (!p.re) continue;
    let hits = 0;
    for (const l of pool) if (p.re.test(l)) hits++;
    if (hits > bestHits) { best = p; bestHits = hits; }
  }
  return bestHits >= Math.max(2, pool.length * 0.2) ? best : TEXT_PARSERS[TEXT_PARSERS.length - 1];
}

/** Read ONE file into records (no global indexing, no time filter yet). */
function readOne(file, forced) {
  const buf = readBuffer(file);

  // ---- binary DLT --------------------------------------------------------
  if (forced === 'dlt-binary' || (forced === 'auto' && looksLikeDltBinary(buf))) {
    const { records, skippedBytes } = decodeDltBinary(buf);
    if (records.length || forced === 'dlt-binary') {
      return {
        records,
        format: 'dlt-binary',
        note: skippedBytes
          ? `${skippedBytes.toLocaleString()} byte(s) skipped while resynchronising on corrupt/partial frames`
          : '',
      };
    }
    // empty + auto -> fall through to text
  }

  // ---- text --------------------------------------------------------------
  const lines = bufferToText(buf).split(/\r?\n/);
  const sample = lines.filter((l) => l.trim()).slice(0, 300);

  let jsonHits = 0;
  for (const l of sample.slice(0, 60)) if (parseJsonLine(l)) jsonHits++;
  const useJson = forced === 'json' ||
    (forced === 'auto' && jsonHits >= Math.max(3, Math.min(60, sample.length) * 0.5));

  const records = [];

  if (useJson) {
    for (const line of lines) {
      if (!line.trim()) continue;
      const j = parseJsonLine(line);
      if (j) {
        const head = [j.ts, j.level || '-', j.tag].filter(Boolean).join(' ');
        records.push({
          raw: `${head} ${j.msg}`.trim(), ts: j.ts, level: j.level,
          tag: j.tag, apid: '', pid: j.pid, tid: j.tid, msg: j.msg, cont: false,
        });
      } else {
        records.push({ raw: line, ts: '', level: '', tag: '', apid: '', pid: '', tid: '', msg: line, cont: false });
      }
    }
    return { records, format: 'json', note: '' };
  }

  const parser = pickTextParser(sample, forced);
  let prev = null;
  let chainAlive = false;

  for (const line of lines) {
    if (!line.trim()) continue;
    const r = { raw: line, ts: '', level: '', tag: '', apid: '', pid: '', tid: '', msg: line, cont: false };

    const m = parser.re ? parser.re.exec(line) : null;

    if (m && m.groups) {
      const g = m.groups;
      r.ts = g.ts || '';
      r.level = normLevel(g.level);
      r.tag = (g.tag || '').trim();
      r.apid = (g.apid || '').trim();
      r.pid = g.pid || '';
      r.tid = g.tid || g.tid2 || '';
      r.msg = g.msg !== undefined && g.msg !== null ? g.msg : line;
      if (parser.map) parser.map(g, r);
      prev = r;
      chainAlive = true;
    } else if (prev && chainAlive && isContinuation(line)) {
      r.cont = true;
      r.level = prev.level;
      r.tag = prev.tag;
      r.apid = prev.apid;
      r.pid = prev.pid;
      r.tid = prev.tid;
      r.ts = prev.ts;
    } else {
      chainAlive = false;
    }

    records.push(r);
  }

  return { records, format: parser.name, note: '' };
}

/**
 * Read one OR MANY log files into a single uniform record list.
 *
 * @param {string|string[]} files
 * @param {string} forced   format name or 'auto'
 * @param {{timeStart?:string, timeEnd?:string}} [opts]
 * @returns {{records, format, formats, sources, totalLines, readLines, note, timeWindow}}
 */
function readRecords(files, forced = 'auto', opts = {}) {
  const list = (Array.isArray(files) ? files : [files]).filter(Boolean);
  if (!list.length) throw new Error('No log file given.');

  // --- order by modified time, oldest first ------------------------------
  const stated = list.map((f) => {
    let mtime = 0;
    let size = 0;
    try {
      const st = fs.statSync(f);
      mtime = st.mtimeMs;
      size = st.size;
    } catch {
      throw new Error(`Cannot read log file: ${f}`);
    }
    return { file: f, mtime, size };
  });
  stated.sort((a, b) => a.mtime - b.mtime || a.file.localeCompare(b.file));

  // --- time window -------------------------------------------------------
  const startMs = parseClock(opts.timeStart);
  const endMs = parseClock(opts.timeEnd);
  const timeOn = startMs !== null || endMs !== null;
  const lo = timeOn ? (startMs === null ? 0 : startMs) : null;
  const hi = timeOn ? (endMs === null ? 86400000 - 1 : endMs) : null;

  const records = [];
  const sources = [];
  const notes = [];
  let idx = 0;
  let readLines = 0;
  let lastClock = null;
  let droppedByTime = 0;

  for (let s = 0; s < stated.length; s++) {
    const { file, mtime, size } = stated[s];
    const one = readOne(file, forced);
    const base = path.basename(file);
    let kept = 0;

    for (let i = 0; i < one.records.length; i++) {
      const r = one.records[i];
      readLines++;

      if (timeOn) {
        const c = clockOf(r.ts);
        if (c !== null) lastClock = c;
        // Untimestamped lines (stack frames, banners) inherit the last clock
        // seen, so a trace is never cut in half by the window.
        const eff = c !== null ? c : lastClock;
        if (eff !== null && !inWindow(eff, lo, hi)) {
          droppedByTime++;
          continue;
        }
      }

      idx++;
      r.idx = idx;
      r.srcIdx = s;
      r.file = base;
      r.fileLine = i + 1;
      records.push(r);
      kept++;
    }

    sources.push({
      file, name: base, format: one.format, mtime,
      mtimeText: new Date(mtime).toLocaleString(),
      size, lines: one.records.length, kept,
    });
    if (one.note) notes.push(`${base}: ${one.note}`);
  }

  // NOTE: the time-window drop count is deliberately NOT added to `note` -
  // the caller renders it on its own dedicated line, and duplicating it here
  // made the summary say the same thing twice.
  const formats = [...new Set(sources.map((s) => s.format))];

  return {
    records,
    format: formats.length === 1 ? formats[0] : `mixed (${formats.join(', ')})`,
    formats,
    sources,
    totalLines: readLines,      // lines actually read from disk
    keptLines: records.length,  // lines surviving the time window
    droppedByTime,
    note: notes.join('; '),
    timeWindow: timeOn ? { start: fmtClock(lo), end: fmtClock(hi), startMs: lo, endMs: hi } : null,
  };
}

const FORMAT_NAMES = ['auto', 'dlt-binary', ...TEXT_PARSERS.map((p) => p.name), 'json'];

module.exports = {
  readRecords,
  readOne,
  readBuffer,
  bufferToText,
  decodeDltBinary,
  decodeDltMessage,
  looksLikeDltBinary,
  decodeVerbosePayload,
  parseJsonLine,
  isContinuation,
  normLevel,
  parseClock,
  clockOf,
  inWindow,
  fmtClock,
  FORMAT_NAMES,
  LEVEL_ORDER,
};
