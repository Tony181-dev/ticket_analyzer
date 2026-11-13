"""Lightweight structural parsers for Java, Kotlin and JavaScript/TypeScript.

Same contract as parsers.pyparse / cppparse: (nodes, edges) for one file. They are regex based on
purpose - no toolchain, tolerant of code that does not compile - and only claim what a graph of
*structure* needs: types, functions/methods, inheritance, imports and call names.
"""
import bisect
import os
import re

JAVA_EXT = {".java"}
KOTLIN_EXT = {".kt", ".kts"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
EXT = JAVA_EXT | KOTLIN_EXT | JS_EXT
_NOT_CALLS = {"if", "for", "while", "switch", "catch", "return", "new", "throw", "else", "do", "try", "synchronized",
              "super", "this", "function", "typeof", "instanceof", "await", "yield", "when", "assert", "sizeof",
              "constructor", "init", "fun", "class", "interface", "enum", "import", "export", "delete", "void"}
_JS_RESOLVE = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js", "/index.jsx")


def _blank(match):
    text = match.group(0)
    return re.sub(r"[^\n]", " ", text)


def _no_comments(text):
    return re.sub(r"//[^\n]*|/\*.*?\*/", _blank, text, flags=re.S)


def _no_strings(text, backtick=False):
    pattern = r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\''
    if backtick:
        pattern += r"|`(?:\\.|[^`\\])*`"
    return re.sub(pattern, _blank, text, flags=re.S)


class _Lines:
    def __init__(self, text):
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def at(self, pos):
        return bisect.bisect_right(self.starts, pos)


def _brace_end(text, start):
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def _calls(nid, body):
    seen, out = set(), []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
        name = m.group(1)
        if name not in _NOT_CALLS and name not in seen:
            seen.add(name)
            out.append({"source": nid, "target_name": name, "kind": "CALLS"})
    return out


class _Builder:
    """Collects nodes/edges and knows which type body a position is inside."""

    def __init__(self, rel, clean):
        self.rel, self.clean, self.lines = rel, clean, _Lines(clean)
        self.nodes, self.edges, self.ids, self.types = [], [], set(), []

    def add(self, name, kind, pos, signature, owner=None):
        base = f"{owner or self.rel}::{name}"
        nid, line = base, self.lines.at(pos)
        if nid in self.ids:  # overloads / redefinitions stay distinct
            nid = f"{base}#{line}"
        self.ids.add(nid)
        self.nodes.append({"id": nid, "name": name, "kind": kind, "file": self.rel, "line": line, "signature": signature})
        self.edges.append({"source": owner or self.rel, "target": nid, "kind": "CONTAINS"})
        return nid

    def type(self, name, kind, pos, brace, signature, bases=()):
        nid = self.add(name, kind, pos, signature, self.owner(pos))
        if brace is not None and brace >= 0:
            self.types.append((brace, _brace_end(self.clean, brace), nid, name))
        for base in bases:
            if base and base != name:
                self.edges.append({"source": nid, "target_name": base, "kind": "INHERITS"})
        return nid

    def owner(self, pos):
        """Innermost enclosing type as (id) or None."""
        best = None
        for start, end, nid, _ in self.types:
            if start < pos < end and (best is None or start > best[0]):
                best = (start, nid)
        return best[1] if best else None

    def owner_name(self, pos):
        best = None
        for start, end, _, name in self.types:
            if start < pos < end and (best is None or start > best[0]):
                best = (start, name)
        return best[1] if best else None

    def body(self, nid, brace):
        if brace is not None and brace >= 0:
            self.edges.extend(_calls(nid, self.clean[brace:_brace_end(self.clean, brace) + 1]))


def _type_names(text):
    """Simple type names out of 'Base<T>, pkg.Iface' (generics and packages dropped)."""
    text = re.sub(r"<[^<>]*>", "", re.sub(r"<[^<>]*>", "", text or ""))
    return [part.strip().split(".")[-1].split("(")[0].strip() for part in text.split(",") if part.strip()]


# --------------------------------------------------------------------------- Java
_JAVA_TYPE = re.compile(r"\b(class|interface|enum|record)\s+(\w+)\s*(?:<[^{>]*>)?\s*(?:\([^)]*\)\s*)?"
                        r"(?:extends\s+([\w.<>, ?]+?))?\s*(?:implements\s+([\w.<>, ?]+?))?\s*(?:permits\s+[\w., ]+?)?\s*\{")
_JAVA_METHOD = re.compile(r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)*"
                          r"(?:<[^>()]{1,80}>\s+)?([\w.]+(?:<[^()]{0,80}>)?(?:\[\])*)\s+(\w+)\s*\(([^(){};]*)\)\s*"
                          r"(?:throws\s+[\w.,\s]+?)?\s*\{")
_JAVA_CTOR = re.compile(r"(?:public|protected|private)\s+(\w+)\s*\(([^(){};]*)\)\s*(?:throws\s+[\w.,\s]+?)?\s*\{")
# Words the "return type" group may swallow when a declaration has no return type (constructors).
_NOT_TYPES = {"new", "return", "throw", "else", "case", "package", "import", "assert", "do", "goto",
              "public", "protected", "private", "static", "final", "abstract", "synchronized", "native",
              "default", "strictfp"}


def javaparse(rel, text):
    plain = _no_comments(text)
    clean = _no_strings(plain)
    b = _Builder(rel, clean)
    for m in re.finditer(r"(?m)^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;", plain):
        b.edges.append({"source": rel, "target_name": m.group(1).split(".")[-1], "kind": "USES"})
    for m in _JAVA_TYPE.finditer(clean):
        b.type(m.group(2), m.group(1), m.start(), m.end() - 1, f"{m.group(1)} {m.group(2)}",
               _type_names(m.group(3)) + _type_names(m.group(4)))
    for m in _JAVA_METHOD.finditer(clean):
        if m.group(1) in _NOT_TYPES or m.group(2) in _NOT_CALLS:
            continue
        owner = b.owner(m.start(2))
        nid = b.add(m.group(2), "method" if owner else "function", m.start(2), f"{m.group(2)}({' '.join(m.group(3).split())})", owner)
        b.body(nid, m.end() - 1)
    for m in _JAVA_CTOR.finditer(clean):
        if m.group(1) != b.owner_name(m.start(1)):
            continue
        nid = b.add(m.group(1), "constructor", m.start(1), f"{m.group(1)}({' '.join(m.group(2).split())})", b.owner(m.start(1)))
        b.body(nid, m.end() - 1)
    return b.nodes, b.edges


# --------------------------------------------------------------------------- Kotlin
_KT_TYPE = re.compile(r"\b(class|interface|object)\s+(\w+)\s*(?:<[^{>]*>)?\s*(?:(?:private|internal|protected)?\s*constructor)?"
                      r"\s*(?:\([^)]*\))?\s*(?::\s*([^{=\n]+?))?\s*(\{|\n|$)")
_KT_FUN = re.compile(r"\bfun\s+(?:<[^>()]{1,80}>\s*)?(?:[\w.<>?]+\.)?(\w+)\s*\(([^(){}]*)\)")


def kotlinparse(rel, text):
    plain = _no_comments(text)
    clean = _no_strings(plain)
    b = _Builder(rel, clean)
    for m in re.finditer(r"(?m)^\s*import\s+([\w.]+?)(?:\.\*)?(?:\s+as\s+\w+)?\s*$", plain):
        b.edges.append({"source": rel, "target_name": m.group(1).split(".")[-1], "kind": "USES"})
    for m in _KT_TYPE.finditer(clean):
        brace = m.end() - 1 if m.group(4) == "{" else None
        b.type(m.group(2), m.group(1), m.start(), brace, f"{m.group(1)} {m.group(2)}", _type_names(m.group(3)))
    for m in _KT_FUN.finditer(clean):
        owner = b.owner(m.start(1))
        nid = b.add(m.group(1), "method" if owner else "function", m.start(1), f"{m.group(1)}({' '.join(m.group(2).split())})", owner)
        rest = re.match(r"\s*(?::\s*[^{=\n]+?)?\s*\{", clean[m.end():m.end() + 200])
        b.body(nid, m.end() + rest.end() - 1 if rest else None)
    return b.nodes, b.edges


# --------------------------------------------------------------------------- JavaScript / TypeScript
_JS_TYPE = re.compile(r"\b(class|interface|enum)\s+(\w+)\s*(?:<[^{>]*>)?\s*(?:extends\s+([\w.<>, ]+?))?\s*"
                      r"(?:implements\s+([\w.<>, ]+?))?\s*\{")
_JS_FUNCTION = re.compile(r"\bfunction\s*\*?\s*(\w+)\s*(?:<[^>()]{1,80}>)?\s*\(([^(){}]*)\)")
_JS_ASSIGNED = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*(?::[^=\n]{1,120})?=\s*(?:async\s+)?"
                          r"(?:function\b[^(]*\(([^(){}]*)\)|\(([^(){}]*)\)\s*(?::[^=\n{]{1,80})?=>|(\w+)\s*=>)")
_JS_METHOD = re.compile(r"(?m)^[ \t]*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*"
                        r"(\*?\s*#?\w+)\s*(?:<[^>()]{1,80}>)?\s*\(([^(){}]*)\)\s*(?::[^{;\n]{1,120})?\s*\{")


def _js_imports(rel, plain):
    folder = os.path.dirname(rel)
    out = []
    for m in re.finditer(r"""(?:\bfrom\s+|\bimport\s+|\brequire\s*\(\s*)['"]([^'"\n]+)['"]""", plain):
        module = m.group(1)
        if not module.startswith("."):
            continue
        base = os.path.normpath(os.path.join(folder, module)).replace(os.sep, "/")
        out.append({"source": rel, "kind": "IMPORTS", "target_name": module, "candidates": [base + suffix for suffix in _JS_RESOLVE]})
    return out


def jsparse(rel, text):
    plain = _no_comments(text)
    clean = _no_strings(plain, backtick=True)
    b = _Builder(rel, clean)
    b.edges.extend(_js_imports(rel, plain))
    for m in _JS_TYPE.finditer(clean):
        bases = _type_names(m.group(3)) + _type_names(m.group(4))
        b.type(m.group(2), m.group(1), m.start(), m.end() - 1, f"{m.group(1)} {m.group(2)}", bases)
    taken = set()

    def function(name, args, pos, after):
        if name in _NOT_CALLS or pos in taken:
            return
        taken.add(pos)
        owner = b.owner(pos)
        nid = b.add(name, "method" if owner else "function", pos, f"{name}({' '.join((args or '').split())})", owner)
        brace = clean.find("{", after, after + 200)
        b.body(nid, brace if brace >= 0 else None)

    for m in _JS_FUNCTION.finditer(clean):
        function(m.group(1), m.group(2), m.start(1), m.end())
    for m in _JS_ASSIGNED.finditer(clean):
        function(m.group(1), m.group(2) or m.group(3) or m.group(4), m.start(1), m.end())
    for m in _JS_METHOD.finditer(clean):
        name = m.group(1).replace("*", "").strip()
        if b.owner(m.start(1)) and name not in _NOT_CALLS | {"constructor"} or (name == "constructor" and b.owner(m.start(1))):
            if m.start(1) in taken:
                continue
            taken.add(m.start(1))
            nid = b.add(name, "constructor" if name == "constructor" else "method", m.start(1),
                        f"{name}({' '.join(m.group(2).split())})", b.owner(m.start(1)))
            b.body(nid, m.end() - 1)
    return b.nodes, b.edges


def parse(rel, text, ext):
    if ext in JAVA_EXT:
        return javaparse(rel, text)
    if ext in KOTLIN_EXT:
        return kotlinparse(rel, text)
    return jsparse(rel, text)
