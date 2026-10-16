class GraphQuery:
 def __init__(self,cg): self.cg=cg
 def search(self,q,limit=10):
  ts=[x.lower() for x in q.replace('_',' ').split() if len(x)>1]; out=[]
  for n in self.cg.nodes:
   h=(n.get('name','')+' '+n.get('id','')+' '+n.get('signature','')).lower(); s=sum(3 if t==n.get('name','').lower() else 1 for t in ts if t in h)
   if s: out.append((s,n))
  return sorted(out,key=lambda x:-x[0])[:limit]
