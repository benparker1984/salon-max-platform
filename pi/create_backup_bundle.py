"""Stream a verified recovery ZIP to an authenticated SSH client; no plaintext on PC.

The caller must encrypt stdout before saving it. Contains credentials and PII.
Never run this in an interactive terminal without redirecting stdout.
"""
import hashlib
import http.cookiejar
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPCookieProcessor
import zipfile
from contextlib import closing
sys.path.insert(0, str(Path(__file__).with_name('groundhog')))
from backup_fingerprint import business_fingerprint


def cloud_snapshot(root):
    values = {}
    for line in (root / '.salonmax_systemd_env').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            parts = shlex.split(value)
            values[key] = parts[0] if parts else ''
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    body = urlencode({'username': values['SALONMAX_PLATFORM_ADMIN_USERNAME'],
                      'password': values['SALONMAX_PLATFORM_ADMIN_PASSWORD'],
                      'next': '/platform/backups/database.json'}).encode()
    with opener.open('https://www.salonmax.co.uk/platform-login', body, timeout=90) as response:
        raw = response.read()
    data = json.loads(raw)
    if data.get('format') != 'salonmax-application-database' or data.get('version') != 1:
        raise ValueError('Unexpected hosted backup format')
    names = {(t['schema'], t['name']) for t in data['tables']}
    required = {'web_accounts', 'web_orders', 'cloud_customers',
                'cloud_customer_minute_ledger', 'cloud_customer_minute_summary'}
    if not required <= {name for schema, name in names if schema == 'public'}:
        raise ValueError('Hosted backup is missing vital account tables')
    if any(t['row_count'] != len(t['rows']) for t in data['tables']):
        raise ValueError('Hosted backup row count mismatch')
    return raw, len(data['tables'])


def create_bundle(home):
    root = home / 'Salon-Max'
    manifest = {'format': 'salonmax-recovery-bundle', 'version': 1,
                'created_at': datetime.now(timezone.utc).isoformat(), 'files': {},
                'databases': {}, 'cloud_status': 'failed'}
    for name, source in (('pi_serial', Path('/sys/firmware/devicetree/base/serial-number')),
                         ('sd_cid', Path('/sys/block/mmcblk0/device/cid'))):
        if source.exists():
            manifest[name] = source.read_text().strip('\x00\n ')
    output = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix='salonmax-backup-') as staging:
        os.chmod(staging, 0o700)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            def add(name, data):
                archive.writestr(name, data)
                manifest['files'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}

            sources = {'databases/till_backoffice.db': home / 'till-build/till_backoffice.db',
                       'databases/local_platform.db': root / 'salonmax_platform.db',
                       'databases/legacy_backoffice.db': root / 'till_backoffice.db'}
            for name, source in sources.items():
                if not source.exists():
                    if name == 'databases/till_backoffice.db':
                        raise FileNotFoundError('Live till database is missing')
                    continue
                target = Path(staging) / Path(name).name
                with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=15)) as live:
                    with closing(sqlite3.connect(target)) as copy:
                        live.backup(copy, pages=128, sleep=0.05)
                        if copy.execute('pragma integrity_check').fetchall() != [('ok',)]:
                            raise ValueError('Database backup failed integrity check')
                        info = {'integrity': 'ok'}
                        if name == 'databases/till_backoffice.db':
                            rows = copy.execute('''select customer_number,first_name,last_name,
                                account_balance,package_minutes,is_active from customers order by id''').fetchall()
                            info.update(customers=len(rows), active_customers=sum(bool(r[5]) for r in rows),
                                        total_minutes=sum(r[4] or 0 for r in rows),
                                        customer_accounts_sha256=hashlib.sha256(json.dumps(rows).encode()).hexdigest())
                            info['business_data_sha256'] = business_fingerprint(copy)
                        manifest['databases'][name] = info
                add(name, target.read_bytes())

            for relative in ('.salonmax_systemd_env', '.salonmax_env', 'app.py', 'schema.sql',
                             'profile_sync.py', 'course_sync.py', 'requirements.txt', 'requirements-native.txt'):
                source = root / relative
                if source.is_file() and not source.is_symlink():
                    add('pi/Salon-Max/' + relative, source.read_bytes())
            for relative in ('templates', 'static/uploads', 'static/sunbed-catalogue', 'ops'):
                for source in sorted((root / relative).rglob('*')):
                    if source.is_file() and not source.is_symlink():
                        add('pi/Salon-Max/' + source.relative_to(root).as_posix(), source.read_bytes())
            updater = home / '.local/share/salonmax-updater'
            for relative in ('state.json', 'release-public-key.hex', 'managed_updater.py'):
                source = updater / relative
                if source.is_file():
                    add('pi/updater/' + relative, source.read_bytes())
            if (updater / 'state.json').exists():
                version = json.loads((updater / 'state.json').read_text()).get('installed', '')
                # Only a simple directory name is accepted from the state file.
                if version and Path(version).name == version and '/' not in version and '\\' not in version:
                    for source in sorted((updater / 'releases' / version).rglob('*.py')):
                        if source.is_file() and not source.is_symlink():
                            add('pi/native-release/' + source.relative_to(updater / 'releases' / version).as_posix(), source.read_bytes())
                    manifest['native_version'] = version
            try:
                raw, count = cloud_snapshot(root)
                add('cloud/application-database.json', raw)
                manifest.update(cloud_status='ok', cloud_tables=count)
            except Exception as error:
                # Still preserve the Pi if the internet/cloud is unavailable.
                # No credentials or response bodies are written to diagnostics.
                manifest['cloud_error_type'] = type(error).__name__
            archive.writestr('manifest.json', json.dumps(manifest, indent=2))
    return output.getvalue(), manifest


if __name__ == '__main__':
    bundle, manifest = create_bundle(Path.home())
    sys.stdout.buffer.write(bundle)
    sys.stdout.buffer.flush()
    if manifest['cloud_status'] != 'ok':
        print('Pi backup captured; hosted database backup unavailable (' + manifest['cloud_error_type'] + ').', file=sys.stderr)
        sys.exit(2)
