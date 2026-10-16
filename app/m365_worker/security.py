"""
Small security helper module used by attachments.py. It defines the set of recognized binary file
extensions plus a list of sensitive filename patterns (env files, private keys, credentials, kube/npm/pypi config) and sensitive directory names (browser profile/data folders), exposing is_binary() and
is_sensitive() predicate functions to keep secrets and unsupported binaries out of chat attachments.
"""
from pathlib import Path
BINARY_EXTENSIONS={'.png','.jpg','.jpeg','.gif','.webp','.bmp','.svg','.pdf','.zip','.tar','.gz','.mp3','.mp4','.wav','.woff','.woff2','.ttf','.otf','.exe','.dll','.so','.bin','.dat','.pyc','.pyo','.class','.o','.obj'}
SENSITIVE_PATTERNS={'.env','.env.local','.env.production','.env.development','.env.staging','.env.test','.env.ci','credentials.json','service-account.json','keyfile.json','*.pem','*.key','*.p12','*.pfx','*.jks','id_rsa','id_ed25519','id_ecdsa','.netrc','.npmrc','.pypirc','kubeconfig'}
SENSITIVE_DIRS={'.kube','browser_profile','browser_data'}
def is_binary(p):return Path(p).suffix.lower() in BINARY_EXTENSIONS
def is_sensitive(p):
 parts=[x.lower() for x in Path(p).parts];name=parts[-1] if parts else''
 return any(x in SENSITIVE_DIRS for x in parts[:-1]) or any(name.endswith(x[1:].lower()) if x.startswith('*') else name==x.lower() for x in SENSITIVE_PATTERNS)
