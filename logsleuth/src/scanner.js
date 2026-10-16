'use strict';
/**
 * STEP 1 - Source scanner.
 * Walks a source root, extracts every log statement it can find and turns it
 * into a normalized "log catalog" entry:
 *   { id, service, level, template, anchors, file, line, lang, raw }
 *
 * Pure node, no vscode import -> unit-testable standalone.
 */

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// language table
// ---------------------------------------------------------------------------
const LANGS = {
  '.java': 'java', '.kt': 'kotlin', '.kts': 'kotlin',
  '.c': 'c', '.h': 'c', '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp', '.hh': 'cpp',
  '.js': 'js', '.jsx': 'js', '.ts': 'ts', '.tsx': 'ts', '.mjs': 'js', '.cjs': 'js',
  '.py': 'python', '.rs': 'rust', '.go': 'go', '.swift': 'swift', '.dart': 'dart',
  '.cs': 'csharp', '.rb': 'ruby', '.php': 'php', '.sh': 'shell', '.bash': 'shell',
};

const DEFAULT_EXCLUDES = [
  'node_modules', '.git', '.svn', '.hg', 'build', 'out', 'dist', 'bin', 'obj',
  'target', 'vendor', 'third_party', 'thirdparty', 'external', '.gradle', '.idea',
  '.vscode', '__pycache__', 'coverage', 'venv', '.venv', 'Pods', 'DerivedData',
  '.logsleuth',
];

const LEVEL_MAP = {
  v: 'V', verbose: 'V', trace: 'V', t: 'V',
  d: 'D', debug: 'D', fine: 'D',
  i: 'I', info: 'I', information: 'I', notice: 'I', print: 'I', println: 'I', log: 'I',
  w: 'W', warn: 'W', warning: 'W',
  e: 'E', error: 'E', err: 'E', severe: 'E', exception: 'E',
  f: 'F', fatal: 'F', wtf: 'F', critical: 'F', crit: 'F', panic: 'F', assert: 'F',
};

// ---------------------------------------------------------------------------
// call-site patterns: each yields groups {lvl?, tag?, args}
// ---------------------------------------------------------------------------
const CALL_PATTERNS = [
  // Android: Log.e(TAG, "msg", tr) / Slog.w(LOG_TAG, ...) / Timber.d(...)
  { lang: ['java', 'kotlin'], re: /\b(?:android\.util\.)?(?:Log|Slog|Rlog|Timber|Logger|Logcat)\s*\.\s*(?<lvl>v|d|i|w|e|wtf|verbose|debug|info|warn|error|assert)\s*\(\s*(?<args>[\s\S]{0,600}?)\)\s*;/g },
  // java.util.logging / slf4j: logger.warn("...") / LOG.error("...")
  { lang: ['java', 'kotlin'], re: /\b(?<obj>[A-Za-z_$][\w$]*)\s*\.\s*(?<lvl>trace|debug|info|warn|warning|error|fatal|severe|fine)\s*\(\s*(?<args>[\s\S]{0,600}?)\)\s*;/g, needsLogObj: true },
  // Android native: ALOGE("..."), LOGW(...), SLOGD(...), IF_ALOGV()
  { lang: ['c', 'cpp'], re: /\b(?:A|S|R)?LOG(?<lvl>V|D|I|W|E|F)\s*\(\s*(?<args>[\s\S]{0,600}?)\)\s*;/g },
  // glog / absl / spdlog: LOG(ERROR) << "..."  /  SPDLOG_WARN("...")
  { lang: ['c', 'cpp'], re: /\bLOG\s*\(\s*(?<lvl>INFO|WARNING|ERROR|FATAL|DFATAL)\s*\)\s*(?<args>(?:\s*<<[\s\S]{0,200}?)+);/g },
  { lang: ['c', 'cpp'], re: /\bSPDLOG_(?<lvl>TRACE|DEBUG|INFO|WARN|ERROR|CRITICAL)\s*\(\s*(?<args>[\s\S]{0,600}?)\)\s*;/g },
  // DLT: DLT_LOG(ctx, DLT_LOG_ERROR, DLT_STRING("..."), DLT_INT(x));
  { lang: ['c', 'cpp'], re: /\bDLT_LOG\w*\s*\(\s*(?<tag>[\w.]+)\s*,\s*DLT_LOG_(?<lvl>FATAL|ERROR|WARN|INFO|DEBUG|VERBOSE)\s*,\s*(?<args>[\s\S]{0,600}?)\)\s*;/g },
  // syslog(LOG_ERR, "...") / printf-family to stderr
  { lang: ['c', 'cpp'], re: /\bsyslog\s*\(\s*LOG_(?<lvl>EMERG|ALERT|CRIT|ERR|WARNING|NOTICE|INFO|DEBUG)\s*,\s*(?<args>[\s\S]{0,600}?)\)\s*;/g },
  { lang: ['c', 'cpp'], re: /\bfprintf\s*\(\s*std(?:err|out)\s*,\s*(?<args>[\s\S]{0,600}?)\)\s*;/g },
  // qt
  { lang: ['c', 'cpp'], re: /\b(?<lvl>qDebug|qInfo|qWarning|qCritical|qFatal)\s*\(\s*\)?\s*(?<args>[\s\S]{0,400}?)\s*;/g },
  // JS/TS: console.error("...") / logger.info(`...`) / this.log.warn(...)
  { lang: ['js', 'ts'], re: /\b(?:console|logger|log|winston|pino|this\.logger|this\.log)\s*\.\s*(?<lvl>trace|debug|info|log|warn|error|fatal)\s*\(\s*(?<args>[\s\S]{0,600}?)\)\s*[;\n]/g },
  // Python: logger.warning("...") / logging.error(f"...")
  { lang: ['python'], re: /\b(?:logging|logger|log|LOG|_log|self\.log(?:ger)?)\s*\.\s*(?<lvl>debug|info|warning|warn|error|exception|critical|fatal)\s*\(\s*(?<args>[\s\S]{0,600}?)\)/g },
  // Rust: error!("..."), tracing::warn!(...)
  { lang: ['rust'], re: /\b(?:tracing\s*::\s*|log\s*::\s*)?(?<lvl>trace|debug|info|warn|error)\s*!\s*\(\s*(?<args>[\s\S]{0,600}?)\)\s*;/g },
  // Go: log.Printf("...") / logrus.WithX().Errorf("...") / slog.Error("...")
  { lang: ['go'], re: /\b(?:log|logrus|slog|zap|logger)\s*(?:\.\w+\([^)]*\))*\s*\.\s*(?<lvl>Print|Printf|Println|Debug|Debugf|Info|Infof|Warn|Warnf|Warning|Error|Errorf|Fatal|Fatalf|Panic|Panicf)\s*\(\s*(?<args>[\s\S]{0,600}?)\)/g },
  // Swift / Dart / C# / generic os_log & print
  { lang: ['swift'], re: /\b(?:os_log|NSLog|print)\s*\(\s*(?<args>[\s\S]{0,400}?)\)/g },
  { lang: ['dart'], re: /\b(?:developer\.log|print|debugPrint)\s*\(\s*(?<args>[\s\S]{0,400}?)\)/g },
  { lang: ['csharp'], re: /\b(?:_?[Ll]ogger|Log|Trace|Debug)\s*\.\s*(?<lvl>LogTrace|LogDebug|LogInformation|LogWarning|LogError|LogCritical|Write|WriteLine)\s*\(\s*(?<args>[\s\S]{0,600}?)\)\s*;/g },
  // shell
  { lang: ['shell'], re: /\becho\s+(?<args>"[^"\n]{8,200}")/g },
];

const LOG_OBJ_RE = /^(?:log|logger|LOG|LOGGER|_log|_logger|slog|sLogger|mLog|mLogger)$/;

// ---------------------------------------------------------------------------
// string-literal + placeholder extraction
// ---------------------------------------------------------------------------
const STRING_RE = /"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|`((?:\\.|[^`\\])*)`|R"\w*\(([\s\S]*?)\)\w*"/g;

// printf-style, brace-style, dollar-style, python %-style
const PLACEHOLDER_RE =
  /%(?:\d+\$)?[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|L|z|j|t|q)?[diuoxXfFeEgGaAcspn%]|%\([^)]*\)[sdifr]|\{[^{}]{0,40}\}|\$\{[^}]{0,60}\}|\$[A-Za-z_]\w*/g;

const PLACEHOLDER = '\u0001'; // internal sentinel, rendered as <*>

function decodeEscapes(s) {
  return s
    .replace(/\\n/g, '\n').replace(/\\r/g, '\r').replace(/\\t/g, '\t')
    .replace(/\\"/g, '"').replace(/\\'/g, "'").replace(/\\`/g, '`')
    .replace(/\\\\/g, '\\');
}

/**
 * Turn a raw argument list into a template:
 *   "connect failed dev=%s status=%d", dev, st   ->  connect failed dev=<*> status=<*>
 *   "state " + a + " -> " + b                    ->  state <*> -> <*>
 */
function argsToTemplate(args) {
  let out = '';
  let last = 0;
  let found = false;
  STRING_RE.lastIndex = 0;
  let m;
  while ((m = STRING_RE.exec(args)) !== null) {
    const lit = m[1] !== undefined ? m[1] : m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4];
    if (lit === undefined) continue;
    const between = args.slice(last, m.index);
    // a non-trivial gap between two literals == a runtime value was concatenated
    if (found && /[A-Za-z0-9_$)\].]/.test(between)) out += PLACEHOLDER;
    out += decodeEscapes(lit).replace(PLACEHOLDER_RE, PLACEHOLDER);
    last = m.index + m[0].length;
    found = true;
  }
  if (!found) return null;
  // A trailing runtime value only exists for real concatenation ("a" + x, "a" << x).
  // A trailing comma is a printf/slf4j argument list - those values are substituted
  // into the placeholders already inside the literal, not appended to it.
  const tail = args.slice(last);
  if (/^\s*(?:\+|<<)\s*[A-Za-z0-9_$(]/.test(tail)) out += PLACEHOLDER;
  out = out.replace(/\s+/g, ' ').trim();
  return out || null;
}

/** Longest static fragments -> what we can actually grep for in a runtime log. */
function anchorsOf(template, minLen) {
  return template
    .split(PLACEHOLDER)
    .map((s) => s.trim())
    .filter((s) => s.length >= minLen && /[A-Za-z]{3}/.test(s))
    .sort((a, b) => b.length - a.length)
    .slice(0, 4);
}

function normalizeLevel(lvl, lang) {
  if (!lvl) return '';
  const k = String(lvl).toLowerCase().replace(/^log|^_/, '');
  if (LEVEL_MAP[k]) return LEVEL_MAP[k];
  if (/^(err|crit|emerg|alert)/.test(k)) return k.startsWith('err') ? 'E' : 'F';
  if (/^q?(debug)/.test(k)) return 'D';
  if (/^q?(info|information|print)/.test(k)) return 'I';
  if (/^q?(warn)/.test(k)) return 'W';
  if (/^q?(critical|fatal|panic)/.test(k)) return 'F';
  if (/^(errorf?|fatalf?|panicf?)$/.test(k)) return k[0] === 'e' ? 'E' : 'F';
  if (/^(warnf|warningf)$/.test(k)) return 'W';
  if (/^(infof|printf?|println)$/.test(k)) return 'I';
  if (/^(debugf|tracef?)$/.test(k)) return 'D';
  return '';
}

// ---------------------------------------------------------------------------
// tag / service resolution
// ---------------------------------------------------------------------------
const TAG_DECL_RES = [
  /(?:static\s+)?(?:final\s+)?(?:const\s+)?String\s+(\w*TAG\w*)\s*=\s*"([^"]{1,64})"/g,
  /#define\s+LOG_TAG\s+"([^"]{1,64})"/g,
  /#define\s+(\w*LOG_TAG\w*)\s+"([^"]{1,64})"/g,
  /(?:val|var|const)\s+(\w*TAG\w*)\s*(?::\s*\w+)?\s*=\s*"([^"]{1,64})"/g,
  /(?:LOG_TAG|TAG|kTag|kLogTag)\s*(?:=|:)\s*"([^"]{1,64})"/g,
  /getLogger\s*\(\s*"([^"]{1,80})"\s*\)/g,
  /DLT_REGISTER_CONTEXT\w*\s*\(\s*\w+\s*,\s*"([^"]{1,8})"/g,
];

function fileTags(text) {
  const tags = [];
  for (const re of TAG_DECL_RES) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      const v = m[2] !== undefined ? m[2] : m[1];
      if (v && !/^\s*$/.test(v)) tags.push(v);
    }
  }
  return tags;
}

/** Service = explicit tag, else nearest meaningful directory/file name. */
function serviceOf(tags, relFile) {
  if (tags.length) return tags[0];
  const parts = relFile.split(/[\\/]/).filter(Boolean);
  const base = parts[parts.length - 1].replace(/\.[^.]+$/, '');
  const dir = parts.length > 1 ? parts[parts.length - 2] : '';
  if (/^(src|main|java|kotlin|lib|source|impl|internal|com|org|app)$/i.test(dir) || !dir) return base;
  return `${dir}/${base}`;
}

// ---------------------------------------------------------------------------
// walker
// ---------------------------------------------------------------------------
function walk(root, opts, onFile) {
  const excl = new Set([...DEFAULT_EXCLUDES, ...(opts.excludeDirs || [])]);
  const maxBytes = (opts.maxFileSizeMB || 2) * 1024 * 1024;
  const stack = [root];
  let scanned = 0;
  while (stack.length) {
    const dir = stack.pop();
    let ents;
    try {
      ents = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const e of ents) {
      const full = path.join(dir, e.name);
      if (e.isSymbolicLink()) continue;
      if (e.isDirectory()) {
        if (!excl.has(e.name) && !e.name.startsWith('.')) stack.push(full);
        continue;
      }
      if (!e.isFile()) continue;
      const lang = LANGS[path.extname(e.name).toLowerCase()];
      if (!lang) continue;
      let st;
      try {
        st = fs.statSync(full);
      } catch {
        continue;
      }
      if (st.size > maxBytes || st.size === 0) continue;
      scanned++;
      onFile(full, lang);
      if (opts.cancel && opts.cancel()) return scanned;
    }
  }
  return scanned;
}

function lineIndex(text) {
  const idx = [0];
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) idx.push(i + 1);
  return idx;
}
function lineAt(idx, off) {
  let lo = 0;
  let hi = idx.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (idx[mid] <= off) lo = mid;
    else hi = mid - 1;
  }
  return lo + 1;
}

/** Strip comment bodies so commented-out logs are not catalogued (keeps offsets). */
function blankComments(text, lang) {
  if (lang === 'python' || lang === 'shell' || lang === 'ruby') {
    return text.replace(/(^|\n)([ \t]*)#[^\n]*/g, (m, a, b) => a + b + ' '.repeat(m.length - a.length - b.length));
  }
  return text
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:"'`\\])\/\/[^\n]*/g, (m, p1) => p1 + ' '.repeat(m.length - p1.length));
}

// ---------------------------------------------------------------------------
// main entry
// ---------------------------------------------------------------------------
/**
 * @returns {{entries:Array, stats:Object}}
 */
function scanSource(root, options = {}) {
  const opts = {
    minAnchorLength: 5,
    maxFileSizeMB: 2,
    excludeDirs: [],
    ...options,
  };
  const entries = [];
  const seen = new Map(); // template+service -> entry (merge duplicates)
  const byLang = {};
  let filesWithLogs = 0;

  const filesScanned = walk(root, opts, (full, lang) => {
    let text;
    try {
      text = fs.readFileSync(full, 'utf8');
    } catch {
      return;
    }
    if (text.indexOf('\u0000') !== -1) return; // binary
    const rel = path.relative(root, full).replace(/\\/g, '/');
    const clean = blankComments(text, lang);
    const lidx = lineIndex(text);
    const tags = fileTags(text);
    const service = serviceOf(tags, rel);
    let hits = 0;

    for (const pat of CALL_PATTERNS) {
      if (!pat.lang.includes(lang)) continue;
      pat.re.lastIndex = 0;
      let m;
      while ((m = pat.re.exec(clean)) !== null) {
        const g = m.groups || {};
        if (pat.needsLogObj && !LOG_OBJ_RE.test(g.obj || '')) continue;
        const args = g.args;
        if (!args) continue;
        const template = argsToTemplate(args);
        if (!template || template.replace(/\u0001/g, '').trim().length < 3) continue;
        const anchors = anchorsOf(template, opts.minAnchorLength);
        if (!anchors.length) continue;

        const line = lineAt(lidx, m.index);
        const level = normalizeLevel(g.lvl, lang) || (/error|fail/i.test(template) ? 'E' : '');
        const tag = (g.tag && !/^\W/.test(g.tag) ? g.tag : tags[0]) || service;
        const key = `${service}\u0000${template}`;
        hits++;

        const prev = seen.get(key);
        if (prev) {
          if (prev.sites.length < 8) prev.sites.push(`${rel}:${line}`);
          continue;
        }
        const entry = {
          id: 'L' + String(entries.length + 1).padStart(4, '0'),
          service,
          tag,
          level,
          lang,
          template: template.replace(/\u0001/g, '<*>'),
          anchors,
          file: rel,
          line,
          sites: [`${rel}:${line}`],
        };
        seen.set(key, entry);
        entries.push(entry);
        byLang[lang] = (byLang[lang] || 0) + 1;
      }
    }
    if (hits) filesWithLogs++;
  });

  entries.sort((a, b) =>
    a.service === b.service ? a.file.localeCompare(b.file) || a.line - b.line : a.service.localeCompare(b.service));
  entries.forEach((e, i) => { e.id = 'L' + String(i + 1).padStart(4, '0'); });

  const services = {};
  for (const e of entries) {
    const s = (services[e.service] = services[e.service] || { count: 0, levels: {} });
    s.count++;
    s.levels[e.level || '-'] = (s.levels[e.level || '-'] || 0) + 1;
  }

  return {
    entries,
    stats: {
      root,
      generatedAt: new Date().toISOString(),
      filesScanned,
      filesWithLogs,
      entryCount: entries.length,
      serviceCount: Object.keys(services).length,
      byLang,
      services,
    },
  };
}

module.exports = { scanSource, DEFAULT_EXCLUDES, LANGS, normalizeLevel, argsToTemplate, anchorsOf };
