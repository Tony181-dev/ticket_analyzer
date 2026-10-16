"""
Validates the list of attachment file paths supplied with a chat request before they are handed to the
browser automation layer. It resolves each path, rejects requests with more than 10 attachments, enforces a 10MB per-file / 25MB total size limit, refuses sensitive files or directories (via security.py) and unclassified binary content, returning the list of validated absolute paths for worker.py to use.
"""
from pathlib import Path
from security import is_binary,is_sensitive
def validate(items):
 result=[];total=0
 if len(items or[])>10:raise ValueError('Too many attachments')
 for item in items or[]:
  raw=item.get('path') if isinstance(item,dict) else item
  if not raw:raise ValueError('Attachment path is required')
  p=Path(raw).expanduser().resolve(strict=True)
  if not p.is_file():raise FileNotFoundError(str(p))
  if is_sensitive(p):raise ValueError('Refusing sensitive file: '+p.name)
  size=p.stat().st_size;total+=size
  if size>10485760 or total>26214400:raise ValueError('Attachment size limit exceeded')
  if not is_binary(p) and b'\0' in p.read_bytes()[:8192]:raise ValueError('Unclassified binary content: '+p.name)
  result.append(str(p))
 return result
