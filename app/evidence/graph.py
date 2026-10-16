"""CodeGraph build (in-process, pure Python) with a per-folder cache."""
import hashlib
import shutil
from pathlib import Path

from .. import paths
from ..codegraph.brainviz import write_brain
from ..codegraph.graph import CodeGraph
from ..codegraph.query import GraphQuery
from ..codegraph.report import write_report

OUTPUTS = ("graph.simple.json", "brain.html", "GRAPH_REPORT.md", "graph.json")


def cache_dir(source_dir):
    key = hashlib.sha1(str(Path(source_dir).resolve()).lower().encode()).hexdigest()[:12]
    return paths.GRAPHS / f"{Path(source_dir).name}-{key}"


def build(source_dir, token_budget=150000, max_nodes=2500, force=False):
    out = cache_dir(source_dir)
    if not force and all((out / name).exists() for name in OUTPUTS):
        return out, CodeGraph.load(out / "graph.json").meta
    out.mkdir(parents=True, exist_ok=True)
    cg = CodeGraph(str(source_dir)).build()
    cg.save(out / "graph.json")
    write_report(cg, out / "GRAPH_REPORT.md")
    write_brain(cg, out / "brain.html", max_nodes)
    cg.save_simple(out / "graph.simple.json", token_budget)
    return out, cg.meta


def copy_into(out, run_dir):
    for name in OUTPUTS:
        shutil.copyfile(out / name, Path(run_dir) / f"04-{name}")


def query(graph_json, question, limit=20):
    q = GraphQuery(CodeGraph.load(graph_json))
    return [{"score": s, **{k: n.get(k) for k in ("name", "kind", "file", "line", "degree")}}
            for s, n in q.search(question, limit)]
