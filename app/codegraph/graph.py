import json, os, time
from collections import defaultdict
from .parsers import sources,parse
def _resolve_module(source,module,level,files):
 parts=module.split('.') if module else []
 base=source.split('/')[:-1] if level else []
 if level>1: base=base[:len(base)-(level-1)]
 cand='/'.join(base+parts)
 for suf in ('.py','/__init__.py'):
  if cand+suf in files: return cand+suf
 return None
class CodeGraph:
 def __init__(self,root): self.root=os.path.abspath(root); self.nodes=[]; self.edges=[]; self.meta={}
 def build(self):
  t=time.time(); raw=[]; names=defaultdict(list); files={}
  for p in sources(self.root):
   f,ns,es=parse(self.root,p); files[f['id']]=f; self.nodes.append(f); self.nodes.extend(ns); raw.extend(es)
   for n in ns: names[n['name']].append(n['id'])
  ids={n['id'] for n in self.nodes}; basenames={os.path.basename(k):k for k in files}
  for e in raw:
   target=e.get('target')
   if not target:
    if e['kind']=='IMPORTS': target=_resolve_module(e['source'],e.get('target_name',''),e.get('level',0),files)
    else:
     q=e.get('target_name',''); target=(names.get(q) or [basenames.get(os.path.basename(q))])[0]
   if target and e['source'] in ids and target in ids and target!=e['source']: self.edges.append({'source':e['source'],'target':target,'kind':e['kind']})
  degree=defaultdict(int)
  for e in self.edges: degree[e['source']]+=1; degree[e['target']]+=1
  dirs={}
  for n in self.nodes:
   d=os.path.dirname(n['file']).split('/')[0] or '.'; dirs.setdefault(d,len(dirs)); n['community']=dirs[d]; n['degree']=degree[n['id']]; n['rank']=degree[n['id']]/max(1,len(self.edges))
  self.meta={'root':self.root,'built_at':time.strftime('%Y-%m-%dT%H:%M:%S'),'files':len(files),'nodes':len(self.nodes),'edges':len(self.edges),'communities':len(dirs),'build_seconds':round(time.time()-t,2)}
  return self
 def to_dict(self): return {'meta':self.meta,'nodes':self.nodes,'edges':self.edges}
 def save(self,p): json.dump(self.to_dict(),open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)
 def _summary(self):
  by_kind=defaultdict(int); comm=defaultdict(int); dirmap={}
  for n in self.nodes:
   by_kind[n.get('kind','?')]+=1; cid=n.get('community',0); comm[cid]+=1
   dirmap.setdefault(cid,os.path.dirname(n['file']).split('/')[0] or '.')
  by_edge=defaultdict(int)
  for e in self.edges: by_edge[e['kind']]+=1
  communities=[{'id':k,'dir':dirmap.get(k,'?'),'nodes':v} for k,v in sorted(comm.items(),key=lambda x:(-x[1],x[0]))]
  return {'by_kind':dict(sorted(by_kind.items(),key=lambda x:-x[1])),'by_edge':dict(sorted(by_edge.items(),key=lambda x:-x[1])),'communities':communities}
 def to_simple(self,token_budget=150000,chars_per_token=3.5):
  # AI-friendly, token-bounded view. Keeps ALL nodes/edges when possible; degrades
  # gracefully by dropping signatures first, then trimming lowest-degree nodes.
  budget=int(token_budget*chars_per_token)
  ordered=sorted(self.nodes,key=lambda n:(-n.get('degree',0),-n.get('rank',0.0),n.get('id','')))
  summary=self._summary()
  def node_obj(n,sig):
   o={'id':n['id'],'name':n.get('name'),'kind':n.get('kind'),'file':n.get('file'),'line':n.get('line',1),'deg':n.get('degree',0)}
   s=n.get('signature')
   if sig and s and s!=n['id'] and s!=n.get('file'): o['sig']=s
   return o
  def assemble(limit,sig):
   keep=ordered[:limit]; ids={n['id'] for n in keep}
   nodes=[node_obj(n,sig) for n in keep]
   edges=[[e['source'],e['target'],e['kind']] for e in self.edges if e['source'] in ids and e['target'] in ids]
   return {'meta':dict(self.meta,mode='simple',token_budget=token_budget),
     'stats':dict(summary,total_nodes=len(self.nodes),total_edges=len(self.edges),shown_nodes=len(nodes),shown_edges=len(edges),signatures=sig),
     'nodes':nodes,'edges':edges}
  size=lambda d:len(json.dumps(d,ensure_ascii=False,separators=(',',':')))
  d=assemble(len(ordered),True)
  if size(d)>budget:
   d=assemble(len(ordered),False)
   if size(d)>budget:
    lo,hi=1,len(ordered)
    while lo<hi:
     mid=(lo+hi+1)//2
     if size(assemble(mid,False))<=budget: lo=mid
     else: hi=mid-1
    d=assemble(lo,False)
  d['meta']['approx_tokens']=int(size(d)/chars_per_token)
  d['meta']['truncated']=d['stats']['shown_nodes']<d['stats']['total_nodes']
  return d
 def save_simple(self,p,token_budget=150000):
  d=self.to_simple(token_budget); json.dump(d,open(p,'w',encoding='utf-8'),ensure_ascii=False,separators=(',',':')); return d
 @classmethod
 def load(cls,p):
  d=json.load(open(p,encoding='utf-8')); o=cls(d.get('meta',{}).get('root','.')); o.meta=d.get('meta',{}); o.nodes=d.get('nodes',[]); o.edges=d.get('edges',[]); return o
