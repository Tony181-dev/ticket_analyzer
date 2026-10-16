def run(root):
 try: from mcp.server.fastmcp import FastMCP
 except ImportError: raise SystemExit('Install MCP support: pip install mcp')
 from .graph import CodeGraph
 from .query import GraphQuery
 import json,os
 q=GraphQuery(CodeGraph.load(os.path.join(root,'.codegraph','graph.json'))); m=FastMCP('codegraph')
 @m.tool()
 def query_graph(question:str,limit:int=10)->str: return json.dumps([n for _,n in q.search(question,limit)],indent=2)
 @m.tool()
 def graph_overview(token_budget:int=150000)->str:
  """Return a compact, token-bounded overview of the whole codebase graph for AI reading."""
  return json.dumps(q.cg.to_simple(token_budget),ensure_ascii=False,separators=(',',':'))
 m.run()
