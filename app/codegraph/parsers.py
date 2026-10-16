import ast, os, re
EXT={'.py','.pyi','.c','.cc','.cpp','.cxx','.h','.hh','.hpp','.hxx','.md','.txt','.rst'}
SKIP={'.git','.vscode','.codegraph','node_modules','build','dist','out','__pycache__','.venv','venv'}
def sources(root):
  for base,dirs,files in os.walk(root):
    dirs[:]=[d for d in dirs if d not in SKIP and not d.startswith('.')]
    for f in files:
      p=os.path.join(base,f)
      if os.path.splitext(f)[1].lower() in EXT: yield p

def _pysig(n):
  a=n.args; parts=[x.arg for x in a.posonlyargs]+[x.arg for x in a.args]
  if a.vararg: parts.append('*'+a.vararg.arg)
  parts+=[x.arg for x in a.kwonlyargs]
  if a.kwarg: parts.append('**'+a.kwarg.arg)
  return f'{n.name}({", ".join(parts)})'

def _pycalls(fn):
  out=[]; stack=list(ast.iter_child_nodes(fn))
  while stack:
    x=stack.pop()
    if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): continue
    if isinstance(x,ast.Call):
      name=x.func.id if isinstance(x.func,ast.Name) else x.func.attr if isinstance(x.func,ast.Attribute) else None
      if name: out.append(name)
    stack.extend(ast.iter_child_nodes(x))
  return out

def pyparse(rel,text):
  nodes=[]; edges=[]
  try: tree=ast.parse(text)
  except SyntaxError: return nodes,edges
  def walk(node,scope_id,scope_kind):
    for child in ast.iter_child_nodes(node):
      if isinstance(child,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
        is_class=isinstance(child,ast.ClassDef)
        kind='class' if is_class else ('method' if scope_kind=='class' else 'function')
        nid=f'{scope_id}::{child.name}'
        signature=f'class {child.name}' if is_class else _pysig(child)
        nodes.append({'id':nid,'name':child.name,'kind':kind,'file':rel,'line':child.lineno,'end_line':getattr(child,'end_lineno',child.lineno),'signature':signature})
        edges.append({'source':scope_id,'target':nid,'kind':'CONTAINS'})
        if is_class:
          for base in child.bases:
            bname=base.id if isinstance(base,ast.Name) else base.attr if isinstance(base,ast.Attribute) else None
            if bname: edges.append({'source':nid,'target_name':bname,'kind':'INHERITS'})
        else:
          for name in _pycalls(child): edges.append({'source':nid,'target_name':name,'kind':'CALLS'})
        walk(child,nid,kind)
      else:
        walk(child,scope_id,scope_kind)
  walk(tree,rel,'file')
  for x in ast.walk(tree):
    if isinstance(x,ast.Import):
      for a in x.names: edges.append({'source':rel,'target_name':a.name,'kind':'IMPORTS','level':0})
    elif isinstance(x,ast.ImportFrom):
      if x.module: edges.append({'source':rel,'target_name':x.module,'kind':'IMPORTS','level':x.level})
      else:
        for a in x.names: edges.append({'source':rel,'target_name':a.name,'kind':'IMPORTS','level':x.level})
  return nodes,edges

def _brace_end(s,start):
  depth=0
  for i in range(start,len(s)):
    if s[i]=='{': depth+=1
    elif s[i]=='}':
      depth-=1
      if depth==0: return i
  return len(s)-1

_CPP_SKIP_CALLS={'if','for','while','switch','catch','sizeof','return','new','delete','defined','static_cast','dynamic_cast','reinterpret_cast','const_cast'}

def _cpp_calls(nid,clean,brace_start):
  end=_brace_end(clean,brace_start); out=[]
  for cm in re.finditer(r'\b(\w+)\s*\(',clean[brace_start:end+1]):
    name=cm.group(1)
    if name not in _CPP_SKIP_CALLS: out.append({'source':nid,'target_name':name,'kind':'CALLS'})
  return out

def cppparse(rel,text):
  nodes=[]; edges=[]
  nostr=re.sub(r'//[^\n]*|/\*.*?\*/',' ',text,flags=re.S)
  for m in re.finditer(r'#\s*include\s*[<"]([^>"]+)',nostr): edges.append({'source':rel,'target_name':os.path.basename(m.group(1)),'kind':'INCLUDES'})
  clean=re.sub(r'"(?:\\.|[^"\\])*"',' ',nostr)
  for m in re.finditer(r'\b(class|struct)\s+(\w+)\s*(?::\s*([^{;]+))?\{',clean):
    nid=f'{rel}::{m.group(2)}'; nodes.append({'id':nid,'name':m.group(2),'kind':m.group(1),'file':rel,'line':clean.count('\n',0,m.start())+1,'signature':f'{m.group(1)} {m.group(2)}'}); edges.append({'source':rel,'target':nid,'kind':'CONTAINS'})
    for base in re.findall(r'\w+',m.group(3) or ''):
      if base not in {'public','private','protected','virtual'}: edges.append({'source':nid,'target_name':base,'kind':'INHERITS'})
  rx=re.compile(r'\b(?:[\w:<>,*&~]+\s+)+(?:(\w+)::)?(\w+)\s*\(([^;{}]*)\)\s*(?:const\s*)?\{')
  for m in rx.finditer(clean):
    name=m.group(2)
    if name in {'if','for','while','switch','catch'}: continue
    nid=f'{rel}::{m.group(1)+"::" if m.group(1) else ""}{name}'
    nodes.append({'id':nid,'name':name,'kind':'method' if m.group(1) else 'function','file':rel,'line':clean.count('\n',0,m.start())+1,'signature':f'{name}({m.group(3).strip()})'})
    edges.append({'source':rel,'target':nid,'kind':'CONTAINS'})
    edges.extend(_cpp_calls(nid,clean,m.end()-1))
  ctor_rx=re.compile(r'\b(\w+)::(~?\w+)\s*\(([^;{}]*)\)\s*(?::\s*[^{]*)?\{')
  for m in ctor_rx.finditer(clean):
    cls,name=m.group(1),m.group(2)
    if name not in (cls,'~'+cls): continue
    nid=f'{rel}::{cls}::{name}'
    nodes.append({'id':nid,'name':name,'kind':'destructor' if name.startswith('~') else 'constructor','file':rel,'line':clean.count('\n',0,m.start())+1,'signature':f'{name}({m.group(3).strip()})'})
    edges.append({'source':rel,'target':nid,'kind':'CONTAINS'})
    edges.extend(_cpp_calls(nid,clean,m.end()-1))
  return nodes,edges

def parse(root,path):
  rel=os.path.relpath(path,root).replace(os.sep,'/')
  text=open(path,encoding='utf-8',errors='ignore').read()
  ext=os.path.splitext(path)[1].lower()
  file_node={'id':rel,'name':os.path.basename(rel),'kind':'doc' if ext in {'.md','.txt','.rst'} else 'file','file':rel,'line':1,'signature':rel}
  if ext in {'.py','.pyi'}: n,e=pyparse(rel,text)
  elif ext in {'.c','.cc','.cpp','.cxx','.h','.hh','.hpp','.hxx'}: n,e=cppparse(rel,text)
  else: n,e=[],[]
  return file_node,n,e
