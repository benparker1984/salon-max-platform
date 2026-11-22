"""Receive a PC-signed receipt over the existing authenticated SSH connection."""
import json
import os
from pathlib import Path
import sys

packet = json.loads(sys.stdin.buffer.read(32768))
if set(packet) != {'public_key_xml', 'receipt'}:
    raise ValueError('Invalid receipt packet')
root = Path.home() / '.local/share/salonmax-recovery'
root.mkdir(mode=0o700, parents=True, exist_ok=True)
key = root / 'pc-signing-public.xml'
if key.exists() and key.read_text() != packet['public_key_xml']:
    raise ValueError('PC signing key changed; recovery trust must be installed again')
if not key.exists():
    key.write_text(packet['public_key_xml'])
    os.chmod(key, 0o600)
temporary = root / 'backup-receipt.tmp'
temporary.write_text(json.dumps(packet['receipt']))
os.chmod(temporary, 0o600)
temporary.replace(root / 'backup-receipt.json')
print('PC backup receipt recorded')
