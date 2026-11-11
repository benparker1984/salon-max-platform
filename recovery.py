"""One-time, additive restoration of the saved platform records."""
import base64
import hashlib
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet


def restore(provider):
    db = provider.get_platform_db()
    db.execute('create table if not exists platform_recovery_runs (snapshot_sha256 text primary key, restored_at text not null, counts_json text not null)')
    db.commit()
    package_path = Path(__file__).with_name('recovery.sealed.json')
    if not package_path.exists():
        return
    package = json.loads(package_path.read_text())
    if db.execute('select * from platform_recovery_runs where snapshot_sha256=?', (package['sha256'],)).fetchone():
        return
    password = os.environ.get('SALONMAX_RECOVERY_KEY') or os.environ.get('SALONMAX_PLATFORM_ADMIN_PASSWORD', '')
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(package['salt']), 600000)
    raw = Fernet(base64.urlsafe_b64encode(key)).decrypt(package['data'].encode())
    if hashlib.sha256(raw).hexdigest() != package['sha256']:
        raise RuntimeError('Recovery snapshot checksum mismatch')
    data = json.loads(raw)
    allowed = {'cloud_business_accounts', 'cloud_business_sites', 'cloud_terminal_registry', 'cloud_device_licences',
               'cloud_sync_events', 'cloud_customers', 'cloud_customer_balance_ledger', 'cloud_customer_minute_ledger',
               'cloud_customer_balance_summary', 'cloud_customer_minute_summary', 'cloud_support_notes'}
    dormant = {'gym_staff_auth', 'gym_payment_settings'}
    try:
        for table, rows in data.items():
            if table in dormant:
                continue
            if table not in allowed:
                raise RuntimeError('Unexpected recovery table')
            for row in rows:
                columns = list(row)
                # IDs remain stable so ledger cursors and references survive recovery.
                fields = ','.join('"' + c + '"' for c in columns)
                placeholders = ','.join('?' for _ in columns)
                db.execute(f'insert into "{table}" ({fields}) values ({placeholders}) on conflict do nothing', tuple(row.values()))
        if hasattr(db, 'raw'):
            for table in data:
                if table in dormant or not data[table] or 'id' not in data[table][0]:
                    continue
                sequence = db.raw.execute('select pg_get_serial_sequence(%s, %s) as name', (table, 'id')).fetchone()
                if sequence and sequence['name']:
                    db.raw.execute(f"select setval(%s, greatest(coalesce((select max(id) from {table}), 0), 1), coalesce((select max(id) from {table}), 0)>0)", (sequence['name'],))
        db.execute('insert into platform_recovery_runs values (?, ?, ?)',
                   (package['sha256'], provider.now_utc_text(), json.dumps({t:len(r) for t,r in data.items() if t not in dormant})))
        db.commit()
    except Exception:
        db.rollback()
        raise
