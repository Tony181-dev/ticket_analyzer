"""CodeGraph build (in-process, pure Python) with a per-folder cache."""
import hashlib
import json
import shutil
from pathlib import Path

from .. import paths
from ..codegraph.brainviz import write_brain
from ..codegraph.graph import CodeGraph
from ..codegraph.query import GraphQuery
from ..codegraph.report import write_report
from .source_cache import source_fingerprint

OUTPUTS = ("graph.simple.json", "brain.html", "GRAPH_REPORT.md", "graph.json")


def cache_dir(source_dir):
    key = hashlib.sha1(str(Path(source_dir).resolve()).lower().encode()).hexdigest()[:12]
    return paths.GRAPHS / f"{Path(source_dir).name}-{key}"


PARSER_VERSION = 2  # bump when the parsers learn new languages, so cached graphs are rebuilt


def build(source_dir, token_budget=150000, max_nodes=2500, force=False, cancel=None):
    out = cache_dir(source_dir)
    manifest = out / ".cache.json"
    fingerprint = f"v{PARSER_VERSION}:" + source_fingerprint(source_dir, cancel)
    cached = {}
    cache_hit = False
    if not force and all((out / name).exists() for name in OUTPUTS):
        try:
            cached = json.loads(manifest.read_text(encoding="utf-8"))
            if cached.get("source_fingerprint") == fingerprint:
                cg = CodeGraph.load(out / "graph.json")
                simple = json.loads((out / "graph.simple.json").read_text(encoding="utf-8"))
                cache_hit = isinstance(simple, dict) and isinstance(simple.get("meta"), dict)
        except (AttributeError, OSError, TypeError, ValueError):
            cache_hit = False
    if cache_hit:
        if simple.get("meta", {}).get("token_budget") != int(token_budget):
            cg.save_simple(out / "graph.simple.json", token_budget)
        if cached.get("max_nodes") != int(max_nodes):
            write_brain(cg, out / "brain.html", max_nodes)
        _write_manifest(manifest, fingerprint, max_nodes)
        return out, cg.meta
    out.mkdir(parents=True, exist_ok=True)
    cg = CodeGraph(str(source_dir)).build(cancel)
    cg.save(out / "graph.json")
    write_report(cg, out / "GRAPH_REPORT.md")
    write_brain(cg, out / "brain.html", max_nodes)
    cg.save_simple(out / "graph.simple.json", token_budget)
    _write_manifest(manifest, fingerprint, max_nodes)
    return out, cg.meta


def _write_manifest(path, fingerprint, max_nodes):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"source_fingerprint": fingerprint, "max_nodes": int(max_nodes)}), encoding="utf-8")
    tmp.replace(path)


def copy_into(out, run_dir):
    for name in OUTPUTS:
        shutil.copyfile(out / name, Path(run_dir) / f"04-{name}")


def write_focused(out, target, token_budget, focus):
    """Per-run AI view of the cached graph, ordered by relevance to the ticket; returns #matched nodes."""
    cg = CodeGraph.load(Path(out) / "graph.json")
    data = cg.save_simple(target, token_budget, focus=focus)
    return (data["meta"].get("focus") or {}).get("matched_nodes", 0)


def query(graph_json, question, limit=20):
    q = GraphQuery(CodeGraph.load(graph_json))
    return [{"score": s, **{k: n.get(k) for k in ("name", "kind", "file", "line", "degree")}}
            for s, n in q.search(question, limit)]
