import argparse,os
from .graph import CodeGraph
from .brainviz import write_brain,json_to_html
from .report import write_report
from .query import GraphQuery
def build(a):
 root=os.path.abspath(a.root); d=os.path.join(root,'.codegraph'); os.makedirs(d,exist_ok=True); cg=CodeGraph(root).build(); cg.save(os.path.join(d,'graph.json')); write_report(cg,os.path.join(d,'GRAPH_REPORT.md')); write_brain(cg,os.path.join(d,'brain.html'),a.max_nodes); m=cg.meta; extra=''
 if not a.no_simple: s=cg.save_simple(os.path.join(d,'graph.simple.json'),a.token_budget); sm=s['stats']; extra=f" · simple {sm['shown_nodes']}/{sm['total_nodes']} nodes ~{s['meta']['approx_tokens']} tok"
 print(f"{m['nodes']} nodes · {m['edges']} edges · {m['communities']} communities · {m['files']} files · {m['build_seconds']}s{extra}")
def simple(a):
 root=os.path.abspath(a.root); src=os.path.join(root,'.codegraph','graph.json'); cg=CodeGraph.load(src); out=os.path.abspath(a.output or os.path.join(root,'.codegraph','graph.simple.json')); s=cg.save_simple(out,a.token_budget); print(f"{out} · {s['stats']['shown_nodes']}/{s['stats']['total_nodes']} nodes · ~{s['meta']['approx_tokens']} tokens · truncated={s['meta']['truncated']}")
def brain(a):
 src=os.path.abspath(a.input or os.path.join(a.root,'.codegraph','graph.json')); out=os.path.abspath(a.output or os.path.join(os.path.dirname(src),'brain.html')); json_to_html(src,out,a.max_nodes); print(out)
def query(a):
 cg=CodeGraph.load(os.path.join(os.path.abspath(a.root),'.codegraph','graph.json'))
 for s,n in GraphQuery(cg).search(a.question,a.limit): print(f"{n['name']} [{n['kind']}] {n['file']}:{n['line']} score={s}")
def main():
 p=argparse.ArgumentParser(prog='codegraph'); sp=p.add_subparsers(dest='cmd',required=True)
 b=sp.add_parser('build'); b.add_argument('root',nargs='?',default='.'); b.add_argument('--max-nodes',type=int,default=1600); b.add_argument('--token-budget',type=int,default=150000); b.add_argument('--no-simple',action='store_true'); b.set_defaults(func=build)
 sj=sp.add_parser('simple'); sj.add_argument('root',nargs='?',default='.'); sj.add_argument('-o','--output'); sj.add_argument('--token-budget',type=int,default=150000); sj.set_defaults(func=simple)
 h=sp.add_parser('brain'); h.add_argument('root',nargs='?',default='.'); h.add_argument('-i','--input'); h.add_argument('-o','--output'); h.add_argument('--max-nodes',type=int,default=1600); h.set_defaults(func=brain)
 q=sp.add_parser('query'); q.add_argument('question'); q.add_argument('root',nargs='?',default='.'); q.add_argument('--limit',type=int,default=10); q.set_defaults(func=query)
 s=sp.add_parser('serve'); s.add_argument('root',nargs='?',default='.'); s.set_defaults(func=lambda a:__import__('codegraph.mcp_server',fromlist=['run']).run(os.path.abspath(a.root)))
 a=p.parse_args(); a.func(a)
if __name__=='__main__': main()
