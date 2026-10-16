#!/usr/bin/env python3
"""
Long-running M365 Copilot automation worker process spawned by extension.js. It reads newline-delimited
JSON command messages from stdin (ping, status, login, logout, chat, shutdown) and writes newline-delimited JSON results to stdout. It drives the Playwright-based Browser (browser.py) to authenticate and chat with M365 Copilot, validates any attached files via attachments.py before sending them, and persists conversation state for each session using the Store class from sessions.py.
"""
import os
import sys
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
import atexit,json,time,traceback,uuid
from pathlib import Path
from browser import Browser,PROFILE,DEFAULT_MODEL
from sessions import Store,clean_unicode
from attachments import validate
def emit(x):
 payload=(json.dumps(clean_unicode(x),ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8',errors='replace')
 sys.stdout.buffer.write(payload);sys.stdout.buffer.flush()
def err(i,c,m,r=False):return{'id':i,'ok':False,'error':{'code':c,'message':m,'retryable':r}}
def collect(payload):
 out=[]
 def add(v):
  if isinstance(v,str) and v.strip():out.append({'path':v.strip()})
  elif isinstance(v,dict):add(v.get('path') or v.get('attachedFile'))
  elif isinstance(v,(list,tuple)):
   for x in v:add(x)
 add(payload.get('attachments'))
 if str(payload.get('model','')).lower()=='copilot-365-web':
  for m in payload.get('messages') or[]:
   if isinstance(m,dict):add(m.get('attachedFile'));add(m.get('attachedFiles'))
 seen=set();result=[]
 for x in out:
  key=os.path.normcase(os.path.normpath(os.path.expanduser(x['path'])))
  if key not in seen:seen.add(key);result.append(x)
 return result
def one_prompt(messages):
 parts=[]
 for m in messages or[]:
  if not isinstance(m,dict):continue
  text=str(m.get('content') or'').strip()
  if text:parts.append(text)
 return '\n\n'.join(parts)
sessions_dir=Path(os.getenv('M365_SESSIONS_DIR',Path(__file__).resolve().parent.parent/'sessions'))
b=Browser();atexit.register(b.close);s=Store(sessions_dir,5);auth=None
PREFER_HEADED_FILE=PROFILE/'.prefer_headed'
# Once we discover this tenant/profile cannot run M365 Copilot headless (the
# headless probe/chat fails with LOGIN_REQUIRED even though a headed login is
# valid), remember to run subsequent chats headed so we don't waste time on a
# headless attempt that will always fail.
prefer_headed=PREFER_HEADED_FILE.exists()
def remember_browser_mode(headed):
 global prefer_headed
 prefer_headed=bool(headed)
 try:
  if prefer_headed:PREFER_HEADED_FILE.touch(exist_ok=True)
  else:PREFER_HEADED_FILE.unlink(missing_ok=True)
 except OSError:pass
def ensure_auth(force=False,quick=False):
 # Confirm the saved login in the mode the tenant supports. Browser.auth()
 # already performs two headless navigation attempts, so repeating it here
 # only delays the headed fallback without adding a distinct recovery path.
 global auth,prefer_headed
 if auth is True and not force:return True
 if not prefer_headed and b.auth(quick=quick):auth=True;remember_browser_mode(False);return True
 if b.auth_headed(quick=quick):auth=True;remember_browser_mode(True);return True
 auth=False;return False
def chat_with_auth_recovery(prompt,conversation_url,files,requested_headless,model):
 # LOGIN_REQUIRED can be a transient SSO redirect or a tenant rejecting
 # headless Chromium. Verify the saved profile and retry once in the proven
 # mode before reporting that interactive login is genuinely required.
 global auth,prefer_headed
 h=bool(requested_headless) and not prefer_headed
 try:return (*b.chat(prompt,conversation_url,files,h,model),h)
 except Exception as error:
  if str(error)!='LOGIN_REQUIRED':raise
  if h:remember_browser_mode(True)
  auth=None
  if not ensure_auth(force=True):raise
  h=bool(requested_headless) and not prefer_headed
  return (*b.chat(prompt,conversation_url,files,h,model),h)
for line in sys.stdin:
 i=None
 try:
  m=json.loads(line);i=m.get('id');a=m.get('action');p=m.get('payload')or{}
  if a=='ping':
   x={'worker':'ready'}
  elif a=='sessions.list':
   x={'sessions':sorted(s.load()['sessions'],key=lambda z:z.get('last_used_at',0),reverse=True)}
  elif a=='status':
   # A headless-only status probe produced false "setup required" states for tenants
   # that allow the saved profile only in a visible browser.
   if auth is not True:
    ensure_auth(quick=True)
   x={'state':'ready'if auth else'setup_required','authenticated':bool(auth),'sessions':len(s.load()['sessions']),'max_sessions':5,'queue_size':0}
  elif a=='login':
   auth=b.login(int(p.get('timeout_seconds',300)))
   x={'authenticated':bool(auth)}
  elif a=='logout':
   b.logout();s.clear();auth=False;prefer_headed=False;x={'authenticated':False}
  elif a=='shutdown':
   b.close();emit({'id':i,'ok':True,'result':{}});break
  elif a=='chat':
   h=bool(p.get('headless',True));model=str(p.get('m365_model')or DEFAULT_MODEL).strip()or DEFAULT_MODEL
   if not ensure_auth():
    emit(err(i,'M365_LOGIN_REQUIRED','M365 login required'));continue
   ss=p.get('session')or{};sid=ss.get('id');rec=s.get(sid)if sid else None;given=ss.get('conversation_url')
   if sid and not rec and not given:
    if p.get('create_session'):
     try:
      rec=s.create(sid,model)
     except Exception as session_error:
      traceback.print_exc(file=sys.stderr);emit(err(i,'SESSION_NOT_FOUND','Session not found: '+sid+' ('+str(session_error)+')'));continue
    else:
     emit(err(i,'SESSION_NOT_FOUND','Session not found: '+sid));continue
   try:
    files=validate(collect(p))
   except FileNotFoundError as e:
    emit(err(i,'ATTACHMENT_NOT_FOUND',str(e)));continue
   except Exception as e:
    emit(err(i,'INVALID_ATTACHMENT',str(e)));continue
   prompt=one_prompt(p.get('messages'));started=time.time()
   if not prompt:
    emit(err(i,'INVALID_REQUEST','No message content'));continue
   conversation_url=given or(rec or{}).get('conversation_url')
   try:
    content,url,chosen,h=chat_with_auth_recovery(prompt,conversation_url,files,h,model)
   except Exception as e:
    if str(e)=='LOGIN_REQUIRED':
     auth=False;emit(err(i,'M365_LOGIN_REQUIRED','M365 login required'));continue
    else:
     emit(err(i,'M365_BROWSER_ERROR',str(e),True));continue
   auth=True;session_warning=None
   if sid:
    try:
     s.create(sid,chosen,given or(rec or{}).get('conversation_url'))
     session_record=s.put(sid,url,chosen,p.get('messages'),content)
    except Exception as session_error:
     traceback.print_exc(file=sys.stderr);session_warning=str(session_error);session_record={'id':sid,'conversation_url':url,'model':chosen}
   else:
    session_record=None
   x={'content':content,'session':session_record,'elapsed_seconds':round(time.time()-started,3),'headless':h,'m365_model':chosen,'attached_files':files,'session_warning':session_warning}
  else:
   emit(err(i,'UNKNOWN_ACTION','Unknown action: '+str(a)));continue
  emit({'id':i,'ok':True,'result':x})
 except Exception as e:
  traceback.print_exc(file=sys.stderr);b.close();auth=None;emit(err(i,'INTERNAL_ERROR',str(e),True))
