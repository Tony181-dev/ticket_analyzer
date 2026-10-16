def _top(nodes,n=15):
 return sorted(nodes,key=lambda x:(-x.get('degree',0),-x.get('rank',0.0),x.get('id','')))[:n]

def write_report(cg,path):
 m=cg.meta; s=cg._summary(); L=[]
 L.append('# CodeGraph Report\n')
 L.append(f"Root: `{m.get('root','')}`  ")
 L.append(f"Built: {m.get('built_at','')}\n")
 L.append('## Overview\n')
 L.append(f"- Files: **{m.get('files',0)}**")
 L.append(f"- Nodes (neurons): **{m.get('nodes',0)}**")
 L.append(f"- Edges (synapses): **{m.get('edges',0)}**")
 L.append(f"- Communities: **{m.get('communities',0)}**")
 L.append(f"- Build time: {m.get('build_seconds',0)}s\n")
 L.append('## Node kinds\n')
 for k,v in s['by_kind'].items(): L.append(f"- {k}: {v}")
 L.append('\n## Relationships\n')
 for k,v in s['by_edge'].items(): L.append(f"- {k}: {v}")
 L.append('\n## Communities (top-level folders)\n')
 L.append('| # | Folder | Nodes |')
 L.append('|---|--------|-------|')
 for c in s['communities']: L.append(f"| {c['id']} | `{c['dir']}` | {c['nodes']} |")
 L.append('\n## Most connected nodes\n')
 L.append('| Node | Kind | Degree | Location |')
 L.append('|------|------|--------|----------|')
 for n in _top(cg.nodes):
  L.append(f"| `{n.get('name','?')}` | {n.get('kind','?')} | {n.get('degree',0)} | {n.get('file','?')}:{n.get('line',1)} |")
 L.append('\n## AI export\n')
 L.append('- `graph.json` — full graph (all nodes, edges, metadata).')
 L.append('- `graph.simple.json` — compact, token-bounded view for feeding into an AI (default budget 150k tokens).\n')
 open(path,'w',encoding='utf-8').write('\n'.join(L))
