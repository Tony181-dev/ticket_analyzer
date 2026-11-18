"""
Validates the list of attachment file paths supplied with a chat request before they are handed to the
browser automation layer. It resolves each path, rejects requests with more than 3 attachments (M365 Copilot's limit per prompt; the app combines extra evidence beforehand, see app/ai/__init__.py), enforces a 10MB per-file / 25MB total size limit, refuses sensitive files or directories (via security.py) and unclassified binary content, returning the list of validated absolute paths for worker.py to use.
"""
from pathlib import Path
import shutil
from security import is_binary,is_sensitive
MAX_FILES=3

def transport_files(files, folder):
 """Stage already-validated text as byte-identical .txt files for tenant upload compatibility."""
 folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
 result=[];mapping=[]
 for index,file in enumerate(files,1):
  source=Path(file)
  if source.suffix.lower() in {'.md','.json','.log','.jsonl','.yaml','.yml','.xml','.csv'}:
   target=folder/f'{index:02}-{source.name}.txt'
   shutil.copyfile(source,target)
   result.append(str(target));mapping.append(f'{source.name} -> {target.name}')
  else:result.append(str(source))
 note=('Text attachments are uploaded as .txt for compatibility, with byte-for-byte identical content. '
       'Use these mappings when an instruction refers to an original filename: '+ '; '.join(mapping)) if mapping else ''
 return result,note

def validate(items):
 result=[];total=0
 if len(items or[])>MAX_FILES:raise ValueError(f'Too many attachments: M365 Copilot accepts at most {MAX_FILES} per prompt (got {len(items)})')
 for item in items or[]:
  raw=item.get('path') if isinstance(item,dict) else item
  if not raw:raise ValueError('Attachment path is required')
  p=Path(raw).expanduser().resolve(strict=True)
  if not p.is_file():raise FileNotFoundError(str(p))
  if is_sensitive(p):raise ValueError('Refusing sensitive file: '+p.name)
  size=p.stat().st_size;total+=size
  if size>10485760:raise ValueError(f'Attachment too large: {p.name} ({size:,} B); per-file upload limit is 10 MiB')
  if total>26214400:raise ValueError(f'Attachment total size limit exceeded at {p.name}: {total:,} B; total upload limit is 25 MiB')
  if not is_binary(p) and b'\0' in p.read_bytes()[:8192]:raise ValueError('Unclassified binary content: '+p.name)
  result.append(str(p))
 return result
