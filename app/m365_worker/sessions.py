"""
Persists M365 Copilot conversation state as one JSON file per session under <extension>/sessions/. The
Store class provides get/list/create/put/clear operations and evicts the oldest session once a configurable limit (default 5) is exceeded, so worker.py can resume conversations across chat calls. Writes are atomic (via atomic_write) and clean_unicode() sanitizes strings containing invalid UTF-16 surrogate characters before they are serialized to JSON.
"""
import json, os, re, tempfile, time
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / 'sessions'


def clean_unicode(value):
    """Return JSON-safe Unicode, replacing isolated UTF-16 surrogates."""
    if isinstance(value, str):
        return value.encode('utf-8', errors='replace').decode('utf-8')
    if isinstance(value, list):
        return [clean_unicode(item) for item in value]
    if isinstance(value, tuple):
        return [clean_unicode(item) for item in value]
    if isinstance(value, dict):
        return {clean_unicode(key) if isinstance(key, str) else str(key): clean_unicode(item) for key, item in value.items()}
    return value

def atomic_write(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(clean_unicode(data), stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


class Store:
    def __init__(self, directory=None, limit=5):
        self.directory = Path(directory) if directory else DEFAULT_DIR
        self.limit = max(1, int(limit)); self.directory.mkdir(parents=True, exist_ok=True)

    def validate_id(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', session_id):
            raise ValueError('Invalid session id')

    def path(self, session_id):
        self.validate_id(session_id); return self.directory / f'{session_id}.json'

    def get(self, session_id):
        path = self.path(session_id)
        if not path.exists(): return None
        try: return json.loads(path.read_text(encoding='utf-8'))
        except Exception: return None

    def list(self):
        output = []
        for path in self.directory.glob('*.json'):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
                self.validate_id(item.get('id'))
                output.append(item)
            except Exception:
                continue
        output.sort(key=lambda x: x.get('last_used_at', x.get('created_at', 0)), reverse=True)
        return output[:self.limit]

    def load(self): return {'sessions': self.list()}


    def create(self, session_id, model=None, conversation_url=None):
        self.validate_id(session_id)
        existing = self.get(session_id)
        if existing is not None: return existing
        now = time.time()
        current = self.list()
        if len(current) >= self.limit:
            oldest = min(current, key=lambda x: x.get('last_used_at', x.get('created_at', 0)))
            try: self.path(oldest['id']).unlink()
            except OSError: pass
        item = {'id': session_id, 'created_at': now, 'last_used_at': now, 'conversation_url': conversation_url, 'model': model or 'GPT 5.6 Sol Think deeper', 'messages': []}
        atomic_write(self.path(session_id), item)
        return item

    def put(self, session_id, url, model=None, messages=None, assistant_response=None):
        self.validate_id(session_id); now = time.time(); item = self.get(session_id)
        if item is None:
            existing = self.list()
            if len(existing) >= self.limit:
                oldest = min(existing, key=lambda x: x.get('last_used_at', x.get('created_at', 0)))
                try: self.path(oldest['id']).unlink()
                except OSError: pass
            item = {'id': session_id, 'created_at': now, 'messages': []}
        item.update({'conversation_url': url, 'model': model or item.get('model'), 'last_used_at': now})
        for message in messages or []:
            if isinstance(message, dict):
                item['messages'].append({'role': str(message.get('role', 'user')), 'content': str(message.get('content', '')), 'timestamp': now})
        if assistant_response is not None:
            item['messages'].append({'role': 'assistant', 'content': str(assistant_response), 'timestamp': time.time()})
        item = clean_unicode(item)
        atomic_write(self.path(session_id), item); return item

    def clear(self):
        for path in self.directory.glob('*.json'):
            try: path.unlink()
            except OSError: pass
